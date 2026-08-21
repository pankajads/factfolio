"""Chat: a lightweight, multi-turn REPL over the same deterministic tools.

Deliberately NOT the M2 orchestrator: one agent, direct tool access, no
subagent roster, no mandatory devils-advocate pass, and a cheaper model
(MODEL_WORKER, not MODEL_ORCHESTRATOR) — a single Q&A turn should be fast and
cheap, not a multi-minute multi-agent review. `log_recommendation` is
deliberately NOT in chat's tool list: formal, ledger-tracked recommendations
still go through `factfolio report`, where the adversarial review is a
mandatory step in the system prompt. Chat can discuss what a good call might
look like, but it does not write one to the ledger — say so if asked.

Same security posture as the orchestrator: the same PreToolUse/PostToolUse
hooks apply (audit log, path/tool guardrails), just scoped to a chat-prefixed
run_id so its calls are distinguishable in logs/tool_calls.jsonl from a
`report` run.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from rich.console import Console

from mybroker.config import MODEL_WORKER, PROJECT_ROOT
from mybroker.security.hooks import (
    audit_and_guard,
    capture_tool_failure,
    capture_tool_result,
    set_current_run,
)
from mybroker.tools.server import ALL_TOOLS, build_server

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, RED = "\033[36m", "\033[31m"

err_console = Console(stderr=True)

# Every read/compute tool except log_recommendation — chat answers questions,
# it does not create ledger-tracked recommendations. review_recommendation_
# outcomes IS included: "how did my past calls do" is a natural chat question
# and grading is pure arithmetic, not a new recommendation.
_EXCLUDED = {"log_recommendation"}
CHAT_TOOL_NAMES = [
    f"mcp__mybroker__{t.name}" for t in ALL_TOOLS if t.name not in _EXCLUDED
]

SYSTEM_PROMPT = """\
You are a portfolio Q&A assistant for a single Indian retail investor,
answering questions conversationally in a terminal chat session.

## The one rule that matters

You do not calculate. Every number you state — a weight, a price, a return, a
tax figure, a breach — must trace to a real tool call you just made. If you
don't have a tool result for it in this conversation, say you don't have it
and offer to fetch it, rather than estimating.

## What you're for

Quick, grounded answers about the portfolio: current positions and weights,
policy breaches, a stock's fundamentals or price history, the tax cost of a
hypothetical sale, market regime, correlation/risk structure, and how past
recommendations have performed (review_recommendation_outcomes).

## What you're NOT for

You do not log formal recommendations — `log_recommendation` is not
available to you. If the user wants an official, ledger-tracked, adversarially-
reviewed BUY/SELL/TRIM recommendation, tell them to run `factfolio report`.
You can still discuss what you think, informally, clearly framed as
unreviewed chat opinion, not a logged call.

## Style

Terse. This is a chat, not a report — a few sentences per answer, tool
citations inline (e.g. "TMCV is 14.27% of the portfolio, per
get_portfolio_snapshot"), no headers, no restating the question. Ask a
clarifying question if the request is ambiguous rather than guessing what
they meant.
"""


def build_options(run_id: str) -> ClaudeAgentOptions:
    set_current_run(run_id)
    return ClaudeAgentOptions(
        model=MODEL_WORKER,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"mybroker": build_server()},
        allowed_tools=CHAT_TOOL_NAMES,
        disallowed_tools=[
            "Bash", "BashOutput", "KillShell", "Agent",
            "Write", "Edit", "NotebookEdit",
            "WebSearch", "WebFetch",
        ],
        permission_mode="dontAsk",
        setting_sources=[],
        cwd=str(PROJECT_ROOT),
        max_turns=20,
        hooks={
            "PreToolUse": [HookMatcher(matcher="*", hooks=[audit_and_guard])],
            "PostToolUse": [HookMatcher(matcher="*", hooks=[capture_tool_result])],
            "PostToolUseFailure": [HookMatcher(matcher="*", hooks=[capture_tool_failure])],
        },
    )


def _auth_status_line() -> str:
    """Which credential the SDK subprocess will use. Neither build_options()
    nor ClaudeAgentOptions sets an API key — the subprocess just inherits
    this process's environment, so ANTHROPIC_API_KEY (if set) wins;
    otherwise it falls back to a local `claude login` session. Local login
    is the zero-config default; the env var is the override."""
    import os

    if os.environ.get("ANTHROPIC_API_KEY"):
        return f"{DIM}auth: ANTHROPIC_API_KEY (env var override){RESET}"
    return f"{DIM}auth: local `claude login` session (no ANTHROPIC_API_KEY set){RESET}"


async def run_chat_repl() -> int:
    """Terminal REPL: read a line, stream the reply, repeat. `exit`/`quit`/
    Ctrl+D/Ctrl+C ends the session."""
    started = datetime.now(UTC)
    run_id = f"chat-{started.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    options = build_options(run_id)

    print(f"{BOLD}factfolio chat{RESET}  {DIM}(model={MODEL_WORKER}, run {run_id}, "
          f"'exit' to quit){RESET}", file=sys.stderr)
    print(_auth_status_line(), file=sys.stderr)
    print(file=sys.stderr)

    async with ClaudeSDKClient(options=options) as client:
        while True:
            try:
                line = input(f"{CYAN}>{RESET} ")
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                break

            text = line.strip()
            if not text:
                continue
            if text.lower() in {"exit", "quit"}:
                break

            await client.query(text)

            # Tool calls (a quote lookup, a tax-impact calc, ...) happen
            # before any text — without this, the terminal just sits blank
            # for however long that takes. Cleared the moment real text
            # starts streaming in, same idea as Claude Code's own spinner.
            cost = None
            status = err_console.status("[dim]thinking…[/dim]", spinner="dots")
            status.start()
            try:
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                status.stop()
                                print(block.text, end="", flush=True)
                            elif isinstance(block, ToolUseBlock):
                                status.update(f"[dim]tool: {block.name}[/dim]")
                    elif isinstance(message, ResultMessage):
                        cost = message.total_cost_usd
            except Exception as exc:
                # A single turn failing (rate limit, network blip, ...)
                # shouldn't kill the whole session — log it, tell the user,
                # and drop back to the prompt so the conversation continues.
                from mybroker.errors import friendly_message, log_error

                log_file = log_error(f"factfolio chat (run {run_id})", exc)
                print(f"\n{RED}✗{RESET} {friendly_message(exc)}", file=sys.stderr)
                if log_file:
                    print(f"  {DIM}Full traceback: {log_file}{RESET}", file=sys.stderr)
                continue
            finally:
                status.stop()
            print()
            if cost:
                print(f"{DIM}(${cost:.4f}){RESET}", file=sys.stderr)

    print(f"\n{DIM}session ended.{RESET}", file=sys.stderr)
    return 0
