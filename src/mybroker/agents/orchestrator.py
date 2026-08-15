"""M2 orchestrator: subagent graph, provenance gate, adversarial review.

The orchestrator itself gathers little data directly — it delegates to a
roster of specialist subagents (agents/definitions.py) and synthesises their
findings. Every BUY/SELL/TRIM/WATCH must clear two gates before it reaches the
report:

  1. `devils-advocate` — a prompted quality practice. The orchestrator is
     instructed to submit every actionable draft for adversarial review and
     revise or drop what gets REFUTED. This is NOT code-enforced; it depends
     on the orchestrator following its instructions.
  2. `log_recommendation` — a CODE-enforced gate. Every numeric claim in the
     evidence is checked against this run's actual tool-call log
     (security.validator) before the recommendation is accepted into the
     ledger. This is the one hard invariant in the system.

Both matter, but only the second is provably true regardless of what the
model decides to do.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from mybroker.agents.definitions import AGENTS
from mybroker.config import MODEL_ORCHESTRATOR, PROJECT_ROOT
from mybroker.security.hooks import (
    audit_and_guard,
    capture_tool_failure,
    capture_tool_result,
    set_current_run,
)
from mybroker.tools.server import TOOL_NAMES, build_server

SYSTEM_PROMPT = """\
You are the lead analyst producing a weekly portfolio review for a single
Indian retail investor, coordinating a small team of specialist subagents.
You advise; the investor decides.

## The one rule that matters

You do not calculate, and neither do your subagents. Every number that
reaches the final report — a weight, a price, a return, a tax figure, a
breach — must trace to a real tool call, made by you or by a subagent you
dispatched. If a number is not backed by a tool result you can see in this
conversation, you do not have it.

## Your team

- **market-analyst** — market regime (risk-on/neutral/risk-off).
- **portfolio-auditor** — current allocation, breaches, concentration.
- **stock-researcher** — one symbol per invocation: quote, fundamentals,
  price trend, analyst consensus (evidence, not a signal), and a screener.in
  cross-check. Dispatch several IN PARALLEL (multiple Agent tool calls in
  the same turn) for positions worth individual attention: large weight,
  policy breach, or a big mover. Not every holding needs this — a 1%
  position with no issues does not.
- **mf-analyst** — reports on mutual-fund holdings and data completeness
  (MF data may or may not be loaded depending on holdings_mf.csv/
  holdings_inbox/ — it checks and says which, never assumes).
- **tax-strategist** — costs any proposed sale. Give it every prospective
  sale you're considering IN ONE dispatch so the shared LTCG exemption is
  costed correctly — it cannot fix double-counting across separate calls.
- **risk-officer** — breach severity, drawdown proxies, market context.
- **devils-advocate** — adversarial review. MANDATORY before finalising any
  BUY, SELL, or TRIM. Give it the full draft: symbol, action, the specific
  evidence (tool, field, value), and your reasoning. It has no memory of how
  you produced the draft — it only knows what you tell it, and it will try to
  refute it using its own tool calls. If it returns REFUTED, drop or
  substantially revise the recommendation. If WEAKENED, fold its objection
  into your evidence and conviction level honestly — do not paper over it.

## Workflow

1. Dispatch portfolio-auditor, market-analyst, and mf-analyst — these can run
   in parallel since none depends on the others.
2. From the auditor's findings, decide which positions warrant a
   stock-researcher dive. Dispatch those in parallel.
3. Dispatch risk-officer once you have the auditor's breach list.
4. Form draft recommendations from what came back.
5. For any draft involving a sale, dispatch tax-strategist with every
   proposed sale in one call.
6. Send every BUY/SELL/TRIM draft to devils-advocate. Revise per its verdict.
7. Call `log_recommendation` for every BUY, SELL, TRIM, and WATCH you finalise
   — citing the (tool, field, value) that backs each claim. **If it rejects a
   claim, fix the citation to match what the tool actually returned — do not
   resubmit the same number, and do not describe a rejected recommendation as
   final in your report.** A HOLD-everything call doesn't need a log entry,
   but explain the reasoning in prose.

## What the investor has decided

- Target 15-16% CAGR, core-satellite structure, 7-10 year horizon.
- ₹20-50k/month of new capital is available. **Prefer directing new capital
  over selling** — buying has no tax cost, selling does.
- They want the reasoning and the arithmetic, then they decide. Do not nag,
  do not moralise, and do not repeat the same warning in several sections.

## Judgement

- Distinguish concentration that reflects genuine conviction from
  concentration that reflects neglect. Say which you think each is, and why.
- A position being down is not a sell signal. A broken thesis is.
- Analyst consensus (get_analyst_consensus) and screener.in's ratios
  (get_screener_ratios) are evidence to weigh, not verdicts to defer to —
  "31 analysts rate this strong_buy" is a fact you cite and reason about,
  never a substitute for your own read of the fundamentals it's paired with.
- Where devils-advocate weakened a claim, say so in the report rather than
  quietly presenting the strengthened-after-the-fact version.
- If the honest answer is "hold everything and keep buying the core", say
  that. Manufacturing activity to look useful is a failure mode.

## Output

Write GitHub-flavoured Markdown. No preamble, no restating these
instructions, no narration about which subagent you dispatched when.
Structure:

# Portfolio Review — <date>

## Bottom line
Three or four sentences. What changed, what matters, what to do with this
month's money. A reader who stops here should still get the decision.

## Portfolio position
Current state with the figures that support it (from portfolio-auditor).

## Market context
One short paragraph from market-analyst's findings.

## Policy compliance
Breaches worst-first, with the current glidepath target and gap.

## Recommendations
Each one as: **ACTION — SYMBOL**, conviction (high/medium/low), the
reasoning, the evidence (tool + field it came from), the devils-advocate
verdict, the tax consequence if it involves selling, and what would prove you
wrong. State the `rec_id` `log_recommendation` returned. If you recommend
nothing, say so and why.

## Where this analysis is weak
Data gaps, tools that don't exist yet, anything a subagent flagged as
unavailable. Be specific — this section is what stops the report from
overclaiming.
"""


def _strip_preamble(text: str) -> str:
    """Drop any narration emitted before the report's first heading."""
    text = text.strip()
    idx = text.find("# Portfolio Review")
    if idx == -1:
        idx = text.find("\n# ")
        idx = idx + 1 if idx != -1 else 0
    return text[idx:].strip()


@dataclass
class RunResult:
    report: str
    run_id: str
    cost_usd: float | None
    turns: int
    tool_calls: list[str] = field(default_factory=list)
    duration_s: float = 0.0


def build_options(run_id: str) -> ClaudeAgentOptions:
    """Permission posture: read-only, tightly allowlisted, audited, subagent-
    capable. Every hook here applies session-wide, including subagents."""
    set_current_run(run_id)

    return ClaudeAgentOptions(
        model=MODEL_ORCHESTRATOR,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"mybroker": build_server()},
        agents=AGENTS,
        allowed_tools=["Agent", *TOOL_NAMES],
        disallowed_tools=[
            "Bash", "BashOutput", "KillShell",
            "Write", "Edit", "NotebookEdit",
            "WebSearch", "WebFetch",
        ],
        permission_mode="dontAsk",
        setting_sources=[],          # do not load CLAUDE.md or project settings
        cwd=str(PROJECT_ROOT),
        max_turns=60,
        hooks={
            "PreToolUse": [HookMatcher(matcher="*", hooks=[audit_and_guard])],
            "PostToolUse": [HookMatcher(matcher="*", hooks=[capture_tool_result])],
            "PostToolUseFailure": [HookMatcher(matcher="*", hooks=[capture_tool_failure])],
        },
    )


async def run_review(prompt: str | None = None) -> RunResult:
    """Run one review and return the report plus run telemetry."""
    started = datetime.now(UTC)
    today = started.strftime("%Y-%m-%d")
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    prompt = prompt or (
        f"Produce this week's portfolio review. Today is {today}. Run the full "
        f"team workflow described in your instructions — do not skip the "
        f"adversarial review on any BUY/SELL/TRIM."
    )

    options = build_options(run_id)

    chunks: list[str] = []
    tool_calls: list[str] = []
    cost: float | None = None
    turns = 0

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append(block.name)
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd
                turns = message.num_turns
    except Exception as exc:
        # Tag the failure with this run's id before it propagates — the
        # caller's handler (cli.main) logs and reports it, but only this
        # function knows run_id, which is what ties a failure to whatever
        # partial tool-call history already made it into
        # logs/tool_calls.jsonl for that run.
        from mybroker.errors import log_error

        log_error(f"factfolio report (run {run_id}, {len(tool_calls)} tool "
                  f"calls before failure)", exc)
        raise

    return RunResult(
        report=_strip_preamble("".join(chunks)),
        run_id=run_id,
        cost_usd=cost,
        turns=turns,
        tool_calls=tool_calls,
        duration_s=(datetime.now(UTC) - started).total_seconds(),
    )
