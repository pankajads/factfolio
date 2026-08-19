"""M7: agent-assisted resolution for holdings whose only identifier is a
full company name (a demat holdings PDF's "Scrip Name" column, say) — where
no genuine short trading symbol exists to safely auto-draft (see
portfolio.importers.discover_equity_symbols_for_drafting's own docstring
for why plain heuristics stop there).

Same discipline as the M2 report orchestrator, scaled down to one narrow
task: the agent gets exactly one tool — a live yfinance company-name
search — and every symbol it claims must be grounded in that specific
name's own real tool-call result. That's checked in code afterward
(_validate below), not just requested in the prompt: a claim that doesn't
trace to an actual result is downgraded to unresolved, never trusted. Only
"high confidence, no duplicate flag, passed validation" resolutions are
auto-written to tickers.yaml by the caller (cli.py); everything else
surfaces with the agent's own reasoning for a human to decide — the same
"a number without provenance is a number that can't be cited" rule
tools/server.py's docstring states for the report pipeline, applied here.

Best-effort by design: no `claude` login, a network hiccup, a malformed
response, an import error — any failure here must fall back to the plain
yfinance-search suggestion (config.suggest_ticker_for_name) rather than
blocking `factfolio init`. Callers are expected to wrap this in a broad
try/except for exactly that reason.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from mybroker.config import MODEL_WORKER
from mybroker.security.hooks import (
    audit_and_guard,
    capture_tool_failure,
    capture_tool_result,
    set_current_run,
)

SYSTEM_PROMPT = """\
You resolve full company names from a holdings statement to their real NSE/BSE
trading symbol, using ONLY the search_ticker_by_name tool's actual results —
never a symbol you already "know" or believe is likely without calling it.

## The one rule that matters

Every `symbol` you return for a name MUST be one of the candidates THAT
name's own search_ticker_by_name call returned. Anything else is rejected in
code afterward regardless of how confident you sound — so there is no reward
for guessing when the search comes back thin or empty. Say so honestly
instead (confidence "low", symbol null).

## What to do

1. Call search_ticker_by_name once per name you're given (not once for the
   whole batch).
2. Pick the best real candidate from that call's own results — prefer a
   `.NS` listing over `.BO` for the same company. The tool already filters
   to NSE/BSE equity listings, but still reject a candidate that's obviously
   the wrong company despite a textual match.
3. Compare the full list of names/quantities you were given for likely
   duplicates of the SAME real holding — two rows with the same or very
   close quantity and a closely related name (a legal-suffix difference, or
   a company that renamed/demerged) often mean one statement recorded the
   same holding twice. Flag the second with `duplicate_of` set to the first
   row's exact name, rather than resolving both as if independent. If you
   are not sure whether it's a genuine duplicate or two real separate
   holdings (e.g. a demerger where both entities could legitimately still be
   held), do NOT guess — say so in `reasoning` and use confidence "low".
4. Rate your own confidence honestly:
   - "high": exactly one unambiguous NSE/BSE match, clearly the right
     company, no plausible duplicate/demerger ambiguity with another row.
   - "medium": a plausible match, but some real ambiguity (only a BSE
     listing, multiple similarly-scored candidates, minor name mismatch).
   - "low": no confident match, or a genuine duplicate/demerger ambiguity
     that needs a human's own judgment — explain why in `reasoning`.

## Output

Return ONLY a JSON array, no other text before or after it, one object per
name you were given, even the ones you can't resolve:
[{"name": "...", "symbol": "TICKER.NS" | null,
  "confidence": "high" | "medium" | "low", "reasoning": "...",
  "duplicate_of": "<other name from the input>" | null}, ...]
"""


@dataclass
class ResolvedName:
    """One holding's resolution — always produced, even on failure
    (confidence="low" explaining why), so a caller iterating results never
    has to handle a missing entry for a name it asked about."""

    name: str
    symbol: str | None
    confidence: str
    reasoning: str
    duplicate_of: str | None = None
    sector: str | None = None
    company_name: str | None = None


def _build_search_tool(evidence: dict[str, list[dict]]):
    """search_ticker_by_name, scoped to one run. Every call's real results
    get recorded into `evidence` (keyed by the exact name queried) so
    _validate can check the agent's claims against them afterward — the
    actual gate; the system prompt's instruction alone is not trusted."""

    @tool(
        "search_ticker_by_name",
        "Search yfinance for NSE/BSE equity listings matching a company "
        "name. Returns every real Indian-exchange match found — the only "
        "candidates a resolution may be drawn from.",
        {"name": str},
    )
    async def search_ticker_by_name(args: dict) -> dict:
        import yfinance as yf

        name = str(args.get("name", "")).strip()
        try:
            quotes = yf.Search(name, max_results=8).quotes
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                "is_error": True,
            }

        # NSI is yfinance's own exchange code for the NSE — not a typo.
        candidates = [
            {
                "symbol": q["symbol"],
                "company_name": q.get("longname") or q.get("shortname"),
                "sector": q.get("sector"),
            }
            for q in quotes
            if q.get("quoteType") == "EQUITY" and q.get("exchange") in ("NSI", "BSE")
        ]
        evidence.setdefault(name, []).extend(candidates)
        return {"content": [{"type": "text", "text": json.dumps({"candidates": candidates})}]}

    return search_ticker_by_name


def _validate(claims: list[dict], evidence: dict[str, list[dict]]) -> list[ResolvedName]:
    """The actual gate: a claimed symbol must appear in THIS name's own
    recorded search results, or it's rejected — the same "a claim must
    trace to a real tool call" rule tools/server.py's provenance validator
    already enforces for report recommendations, applied here directly
    since this task's evidence shape doesn't fit that validator's schema.
    """
    resolved = []
    for claim in claims:
        name = str(claim.get("name", "")).strip()
        symbol = claim.get("symbol")
        by_symbol = {c["symbol"]: c for c in evidence.get(name, [])}

        if symbol and symbol not in by_symbol:
            resolved.append(ResolvedName(
                name=name, symbol=None, confidence="low",
                reasoning=(f"Agent claimed {symbol!r}, but that wasn't in this "
                           f"name's own search results — rejected, not trusted."),
            ))
            continue

        meta = by_symbol.get(symbol, {}) if symbol else {}
        confidence = str(claim.get("confidence") or "low").lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"

        resolved.append(ResolvedName(
            name=name,
            symbol=symbol,
            confidence=confidence,
            reasoning=str(claim.get("reasoning") or ""),
            duplicate_of=(claim.get("duplicate_of") or None),
            sector=meta.get("sector"),
            company_name=meta.get("company_name"),
        ))
    return resolved


_JSON_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _parse_response(text: str) -> list[dict]:
    """Extract the JSON array from the agent's response, wherever it
    actually is. The system prompt asks for ONLY a JSON array and nothing
    else, but real runs showed a model working through several
    duplicate-detection calls reliably wants to narrate its reasoning too
    — prose before a ```json fence, and a markdown summary table after
    it, not just the bare array. Rather than keep tightening the prompt
    and hoping it sticks, extract robustly instead: a fenced ```json
    block anywhere in the text if there is one, falling back to the first
    balanced [...] span found by bracket-matching if there's no fence at
    all.
    """
    text = text.strip()

    fence_match = _JSON_FENCE.search(text)
    if fence_match:
        candidate = fence_match.group(1)
    else:
        start = text.find("[")
        if start == -1:
            raise ValueError("no JSON array found in the agent's response")
        depth = 0
        end = None
        for i, ch in enumerate(text[start:], start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            raise ValueError("no closing bracket found for the JSON array")
        candidate = text[start:end]

    data = json.loads(candidate)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array, got {type(data).__name__}")
    return data


async def resolve_names(holdings: list[dict]) -> list[ResolvedName]:
    """Resolve every full-name holding in `holdings`
    (`[{"name": ..., "quantity": ..., "avg_cost": ...}, ...]`) via one
    agent turn with shared context, so it can reason about cross-row
    duplicates rather than resolving each name in isolation.

    Raises on any failure (auth, network, malformed response) rather than
    swallowing it — callers (cli.py) are expected to catch broadly and
    fall back to the plain suggestion path; this function's job is only to
    do the resolution when it can, not to decide what happens when it
    can't.
    """
    evidence: dict[str, list[dict]] = {}
    run_id = f"tickerresolve-{uuid.uuid4().hex[:8]}"
    set_current_run(run_id)

    options = ClaudeAgentOptions(
        model=MODEL_WORKER,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"resolver": create_sdk_mcp_server(
            name="resolver", version="1.0.0", tools=[_build_search_tool(evidence)],
        )},
        allowed_tools=["mcp__resolver__search_ticker_by_name"],
        disallowed_tools=[
            "Bash", "BashOutput", "KillShell", "Agent",
            "Write", "Edit", "NotebookEdit", "WebSearch", "WebFetch",
        ],
        permission_mode="dontAsk",
        setting_sources=[],
        # One tool-call turn per name plus real headroom for the model's
        # own reasoning turns in between — too tight a budget means the
        # agent runs out mid-batch and never reaches its final summary
        # turn at all, losing the whole result (safely caught by cli.py's
        # fallback, but silently, for exactly the larger batches where
        # this feature matters most). Confirmed in practice: 2 turns/name
        # was not enough for a 6-name batch.
        max_turns=len(holdings) * 5 + 10,
        hooks={
            "PreToolUse": [HookMatcher(matcher="*", hooks=[audit_and_guard])],
            "PostToolUse": [HookMatcher(matcher="*", hooks=[capture_tool_result])],
            "PostToolUseFailure": [HookMatcher(matcher="*", hooks=[capture_tool_failure])],
        },
    )

    prompt = (
        "Resolve every one of these holdings to a real NSE/BSE symbol:\n\n"
        + json.dumps(holdings, indent=2)
    )

    chunks: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)

    if not chunks:
        # Most likely ran out of max_turns mid-batch (all tool-call turns,
        # never reaching a final summary) rather than a parsing problem —
        # say so plainly instead of a bare JSONDecodeError on "".
        raise RuntimeError(
            "Agent produced no text response at all (likely ran out of turns "
            "before finishing) — falling back to the plain suggestion path."
        )

    claims = _parse_response("".join(chunks))
    resolved = _validate(claims, evidence)

    # The agent might drop a name from its response entirely (a bad turn,
    # hitting max_turns) — make sure every input still gets a result
    # rather than silently vanishing from what the caller iterates.
    seen = {r.name for r in resolved}
    for h in holdings:
        if h["name"] not in seen:
            resolved.append(ResolvedName(
                name=h["name"], symbol=None, confidence="low",
                reasoning="Agent did not return a result for this name.",
            ))

    return resolved
