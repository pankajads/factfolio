"""Shared error handling for every entry point (CLI commands, the chat REPL,
the MCP server).

Before this module existed, an uncaught exception anywhere in a command
dumped a raw Python traceback straight to the user's terminal — technically
correct, useless in practice: nothing said what to do next, and nothing was
kept anywhere for later debugging once the terminal scrolled away. Two
things fix that:

  `log_error()`   — best-effort, appends the full traceback to
                     logs/errors.log so it's never lost, without ever
                     itself becoming the reason a command fails.
  `friendly_message()` — a short, actionable translation of the failure
                     modes actually seen in this project. Falls back to
                     `type(exc).__name__: exc` (still far better than a raw
                     traceback) for anything it doesn't recognise.

Every call site should do both: show the friendly message, log the rest.
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from pathlib import Path

from mybroker.config import LOGS_DIR, ensure_dirs

ERROR_LOG = LOGS_DIR / "errors.log"


def log_error(context: str, exc: BaseException) -> Path | None:
    """Append a timestamped full traceback to logs/errors.log.

    Best-effort and silent on failure (e.g. a read-only filesystem) — a
    command that already failed must not fail a second, more confusing way
    because it couldn't write its own error log. Returns the log path on
    success so the caller can point the user at it, or None.
    """
    try:
        ensure_dirs()
        with ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 70}\n")
            f.write(f"{datetime.now(UTC).isoformat()}  {context}\n")
            f.write(f"{'=' * 70}\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
        return ERROR_LOG
    except Exception:
        return None


def friendly_message(exc: BaseException) -> str:
    """A short, actionable translation of common failure modes.

    Pattern-matched on the exception classes/text this project actually
    produces (mainly from `claude_agent_sdk`, which raises a mix of typed
    errors and, for some CLI-subprocess failures, a bare `Exception` with a
    terse or misleading message). Unrecognised exceptions still get a
    cleaner one-liner than a traceback: `TypeName: message`.
    """
    from claude_agent_sdk import CLIConnectionError, CLINotFoundError, ProcessError

    from mybroker.llm_config import UnsupportedProviderError

    if isinstance(exc, UnsupportedProviderError):
        # Already a short, actionable message — see llm_config.py — no
        # rewriting needed, unlike the SDK exceptions below.
        return str(exc)
    if isinstance(exc, CLINotFoundError):
        return (
            "The `claude` CLI wasn't found on PATH. Install it "
            "(https://docs.claude.com/claude-code), confirm `claude "
            "--version` works, then retry."
        )
    if isinstance(exc, ProcessError):
        tail = f" ({exc.stderr.strip()})" if exc.stderr else ""
        return f"The `claude` CLI process failed{tail}. Retry; if it persists, run `claude doctor`."
    if isinstance(exc, CLIConnectionError):
        return f"Couldn't connect to the `claude` CLI: {exc}. Retry, or run `claude login` again."

    text = str(exc)
    if "Claude Code returned an error result" in text:
        # The SDK's transport layer raises this for a mid-run CLI failure —
        # `message.get("error", ...)` here is frequently just the last known
        # *subtype* (often literally "success"), not a real description, so
        # the text itself is close to useless. What it reliably means in
        # practice: a transient failure partway through the run (rate limit,
        # an overloaded model, a network blip) — not a bug in your portfolio
        # data, and nothing was corrupted.
        return (
            "Claude Code hit a transient error partway through this run "
            "(rate limit, an overloaded model, or a network blip — the SDK "
            "doesn't expose more detail than that). Nothing was corrupted; "
            "just re-run the command. If it keeps happening, check "
            "https://status.anthropic.com and that `claude login` (or "
            "ANTHROPIC_API_KEY) is still valid."
        )

    return f"{type(exc).__name__}: {exc}"
