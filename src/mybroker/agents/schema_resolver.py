"""M9: agent-assisted structure resolution for a holdings file the
deterministic keyword matching in portfolio/importers.py can't parse at
all — whether it's a known table shape with an unfamiliar column (a wrapped
header, a swapped column — those are handled by importers.py's own
heuristics, unchanged, and stay the fast, free, always-tried-first path) or
a genuinely novel export whose header uses different terminology
throughout, which the keyword list never even recognises as a table.

Every new export format hit in practice so far led to another hand-tuned
heuristic (a keyword-set reorder, a whitespace-collapsing fallback, a
candidate-symbol scan) — correct for the file that prompted it, but a code
change and a release for every future format nobody's seen yet, which
doesn't scale to formats this project's author can't predict. This is the
general fix: hand the agent the raw start of the file — the same account
metadata, disclaimer paragraphs, and mystery formatting a human would have
to scroll past — and let it find the real table and read it the way a
person would, using judgement instead of another fixed rule. It isn't told
where the header is any more than that person would be; finding it is
part of the job, not a precondition for calling this at all.

Same discipline as agents/ticker_resolver.py, scaled to a different claim:
no tools, no external grounding to check a search result against — the
grounding here is INTERNAL, against the file's own real data (see
portfolio.importers.validate_schema), which is checked in code afterward,
never just requested in the prompt. Only a mapping that passes that check
gets written to the durable column-map cache (portfolio.importers's
load/save_column_map); everything else surfaces with the agent's own
reasoning for a human to decide.

Best-effort by design: no `claude` login, a network hiccup, a malformed
response — any failure here must leave the file exactly as unparseable as
it was, never partially/incorrectly resolved. Callers are expected to wrap
this in a broad try/except for exactly that reason.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    TextBlock,
    query,
)

from mybroker.config import MODEL_WORKER
from mybroker.portfolio.importers import (
    EQUITY_ALL_FIELDS,
    EQUITY_REQUIRED,
    MF_ALL_FIELDS,
    MF_REQUIRED,
)
from mybroker.security.hooks import (
    audit_and_guard,
    capture_tool_failure,
    capture_tool_result,
    set_current_run,
)

SYSTEM_PROMPT = f"""\
You find and map the holdings table inside the raw start of an Indian
broker or mutual-fund statement — reading the actual data the way a person
would, not matching header wording against a fixed list. Real statements
are messy: account details, disclaimer paragraphs, or blank rows before
the real table starts; PDF table extraction that wraps a single word
across two columns; whatever jargon or abbreviation that specific
broker/DP prefers; column order that varies. You are not told which row
is the header — finding it is your job, the same as it would be for a
person opening this file for the first time.

## What you're given

The first several rows of a holdings file, exactly as extracted — a JSON
array of rows, each row a JSON array of cell strings, 0-indexed. This may
include rows that aren't part of any table at all (account name, PAN,
dates, a totals summary) before the real header row appears, or even no
recognisable holdings table at all.

## What to decide

1. Which row index (0-based, into the array you were given) is the real
   header row — or null if none of these rows form a recognisable equity
   or mutual-fund holdings table at all. Don't assume row 0.
2. If you found one: is it an EQUITY holdings table or a MUTUAL FUND
   holdings table?
   Equity fields: {", ".join(EQUITY_ALL_FIELDS)}.
   Mutual fund fields: {", ".join(MF_ALL_FIELDS)}.
3. For each field that applies, which column index (0-based, within that
   header row) holds it — or null if the file genuinely doesn't have that
   column. Not every field is required; see below for which ones are.

## Rules

- Equity's {", ".join(EQUITY_REQUIRED)} and mutual fund's
  {", ".join(MF_REQUIRED)} are load-bearing — if you can't find a
  confident column for one of those, say so honestly (confidence "low")
  rather than guess. Everything else is a bonus.
- A column's DATA (the rows below the header you picked) must actually
  support your claim — a numeric field (quantity, invested, units, ...)
  should look numeric across those rows; symbol/scheme_name should look
  like text, not a bare number. Do not map a field to a column just
  because its header sounds close if the data doesn't fit — this gets
  checked against the file's real data afterward, and a claim that
  doesn't hold up is rejected outright.
- If the same underlying value could reasonably be either of two columns,
  say so in `reasoning` rather than silently picking one.
- If nothing in what you were given looks like a holdings table, say so
  plainly (header_row null, kind null, confidence "low", and explain why
  in `reasoning`) rather than forcing a guess onto the least-bad row.

## Output

Return ONLY a JSON object, no other text before or after it:
{{"header_row": <row index> | null, "kind": "equity" | "mf" | null,
"columns": {{"<field>": <column index> | null, ...}}, "confidence": "high"
| "medium" | "low", "reasoning": "..."}}
"""


@dataclass
class ResolvedSchema:
    """One file's structure — always produced, even on failure
    (confidence="low" explaining why), so a caller never has to handle a
    missing result."""

    header_row: int | None
    kind: str | None
    columns: dict[str, int | None] = field(default_factory=dict)
    confidence: str = "low"
    reasoning: str = ""


_JSON_OBJECT = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _parse_response(text: str) -> dict:
    """Extract the JSON object from the agent's response, wherever it
    actually is — same robust extraction as ticker_resolver.py's
    _parse_response (a fenced block if present, otherwise the first
    balanced {...} span), for the same reason: a model asked for ONLY a
    JSON object still reliably wants to narrate first in practice."""
    text = text.strip()

    fence_match = _JSON_OBJECT.search(text)
    if fence_match:
        candidate = fence_match.group(1)
    else:
        start = text.find("{")
        if start == -1:
            raise ValueError("no JSON object found in the agent's response")
        depth = 0
        end = None
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            raise ValueError("no closing brace found for the JSON object")
        candidate = text[start:end]

    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    return data


def build_options(run_id: str) -> ClaudeAgentOptions:
    from mybroker.llm_config import ensure_supported_provider

    ensure_supported_provider()
    set_current_run(run_id)
    return ClaudeAgentOptions(
        model=MODEL_WORKER,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=[],
        disallowed_tools=[
            "Bash", "BashOutput", "KillShell", "Agent",
            "Write", "Edit", "NotebookEdit", "WebSearch", "WebFetch",
        ],
        permission_mode="dontAsk",
        setting_sources=[],
        max_turns=4,
        hooks={
            "PreToolUse": [HookMatcher(matcher="*", hooks=[audit_and_guard])],
            "PostToolUse": [HookMatcher(matcher="*", hooks=[capture_tool_result])],
            "PostToolUseFailure": [HookMatcher(matcher="*", hooks=[capture_tool_failure])],
        },
    )


async def resolve_schema(grid_excerpt: list[list[str]]) -> ResolvedSchema:
    """Ask the agent to find the holdings table in `grid_excerpt` — the
    raw first rows of a file, exactly as extracted — and map its columns
    to semantic fields. Unlike a design that hands the agent a pre-picked
    header row, this doesn't presuppose the deterministic path already
    found one; it's exactly as capable of resolving a file whose header
    uses completely unfamiliar terminology as one with merely a wrapped
    or swapped column.

    Raises on any failure (auth, network, malformed response) rather than
    swallowing it — callers (cli.py) are expected to catch broadly and
    fall back to leaving the file unparseable, same contract as
    agents/ticker_resolver.py's resolve_names.
    """
    run_id = f"schemaresolve-{uuid.uuid4().hex[:8]}"
    options = build_options(run_id)

    prompt = (
        "Find and map this holdings file's table:\n\n"
        f"{json.dumps(grid_excerpt, indent=2)}"
    )

    chunks: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)

    if not chunks:
        raise RuntimeError(
            "Agent produced no text response at all (likely ran out of turns "
            "before finishing) — leaving this file unparseable rather than "
            "guessing."
        )

    data = _parse_response("".join(chunks))
    header_row = data.get("header_row")
    if not isinstance(header_row, int):
        header_row = None
    kind = data.get("kind")
    if kind not in ("equity", "mf"):
        kind = None
    confidence = str(data.get("confidence") or "low").lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    columns_raw = data.get("columns") or {}
    columns = {
        k: v for k, v in columns_raw.items()
        if isinstance(v, int) or v is None
    }

    return ResolvedSchema(
        header_row=header_row, kind=kind, columns=columns,
        confidence=confidence, reasoning=str(data.get("reasoning") or ""),
    )
