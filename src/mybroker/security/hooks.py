"""Security hooks: audit every tool call, confine every write, capture outputs.

Three jobs, all riding the same hook pair:

  1. `disallowed_tools` removes dangerous tools from the model's context
     entirely — it cannot attempt what it cannot see. (Configured in
     agents/orchestrator.py, not here.)
  2. `audit_and_guard` (PreToolUse) is the backstop: denies anything unsafe
     that slips through, and records the call.
  3. `capture_tool_result` (PostToolUse) records what the tool actually
     returned.

(2) and (3) together are what make the provenance validator possible. A
recommendation's evidence claims "tool X returned value Y" — the validator can
only check that claim against what X *actually* returned, which is (3), not
what the agent *said* it called, which is (2). Both PreToolUse and PostToolUse
fire for every agent in the run, including subagents (each record carries
`agent_id`/`agent_type`), so this is a whole-session ledger, not just the
orchestrator's own calls.

Run scoping: this application runs one review at a time in one process, so a
plain module-level variable is sufficient — no need for the concurrency
machinery a multi-tenant server would require.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mybroker.config import PROJECT_ROOT, TOOL_LOG, WRITABLE_DIRS, ensure_dirs

# Tools that must never run in this application, whatever the prompt says.
FORBIDDEN_TOOLS = {"Bash", "BashOutput", "KillShell", "Write", "Edit",
                   "NotebookEdit", "WebSearch", "WebFetch"}

# Paths that are off-limits even for reads.
SENSITIVE_PATH_MARKERS = (
    ".ssh", ".aws", ".gnupg", ".config/gcloud", "keychain", "id_rsa",
    ".env", "credentials", ".netrc", ".kube", ".docker/config.json",
)

_current_run_id: str | None = None


def set_current_run(run_id: str) -> None:
    """Called once by the orchestrator before starting a query()."""
    global _current_run_id
    _current_run_id = run_id


def get_current_run() -> str | None:
    return _current_run_id


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _audit(record: dict) -> None:
    """Append one line to the audit log. Never raises — auditing must not
    break the run, but a failure to audit is itself recorded to stderr."""
    record.setdefault("run_id", _current_run_id)
    try:
        ensure_dirs()
        with TOOL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:  # pragma: no cover
        import sys
        print(f"[audit] failed to write tool log: {exc}", file=sys.stderr)


def _extract_paths(tool_input: dict[str, Any]) -> list[str]:
    """Pull anything path-shaped out of a tool's arguments."""
    keys = ("file_path", "path", "notebook_path", "filePath", "directory")
    return [str(tool_input[k]) for k in keys if tool_input.get(k)]


def _is_sensitive(path_str: str) -> bool:
    low = path_str.lower()
    return any(marker in low for marker in SENSITIVE_PATH_MARKERS)


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved.is_relative_to(r.resolve()) for r in roots)


async def audit_and_guard(
    input_data: dict, tool_use_id: str | None, context: Any
) -> dict:
    """PreToolUse hook. Logs every call, denies anything unsafe.

    Returns {} to allow — the SDK treats an empty dict as "no objection".
    """
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {}) or {}

    record = {
        "kind": "call",
        "ts": datetime.now(UTC).isoformat(),
        "tool": tool_name,
        "tool_use_id": tool_use_id or input_data.get("tool_use_id"),
        "agent_id": input_data.get("agent_id"),
        "agent_type": input_data.get("agent_type"),
        "input": tool_input,
        "decision": "allow",
    }

    def refuse(reason: str) -> dict:
        record["decision"] = "deny"
        record["reason"] = reason
        _audit(record)
        return _deny(reason)

    # 1. Hard-forbidden tools.
    if tool_name in FORBIDDEN_TOOLS:
        return refuse(
            f"{tool_name} is not permitted in this application. It handles "
            f"personal financial data and runs advisory analysis only — shell "
            f"access, file writes and arbitrary web access are all out of scope."
        )

    # 2. Path safety for anything that touches the filesystem.
    for raw in _extract_paths(tool_input):
        if _is_sensitive(raw):
            return refuse(f"Access to {raw} denied: path looks credential-bearing.")

        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path

        if not _inside(path, (PROJECT_ROOT,)):
            return refuse(
                f"Access to {raw} denied: outside the project root "
                f"({PROJECT_ROOT}). This agent works only on its own project."
            )

        # Writes are confined more tightly than reads.
        if tool_name in {"Write", "Edit", "NotebookEdit"} and not _inside(
            path, WRITABLE_DIRS
        ):
            allowed = ", ".join(d.name for d in WRITABLE_DIRS)
            return refuse(
                f"Write to {raw} denied: writes are confined to {allowed}."
            )

    _audit(record)
    return {}


async def capture_tool_result(
    input_data: dict, tool_use_id: str | None, context: Any
) -> dict:
    """PostToolUse hook. Records what the tool actually returned.

    This is the record the provenance validator checks recommendations
    against — not the call (which only proves the agent *asked*), the result
    (which proves what it *got back*).
    """
    record = {
        "kind": "result",
        "ts": datetime.now(UTC).isoformat(),
        "tool": input_data.get("tool_name", ""),
        "tool_use_id": tool_use_id or input_data.get("tool_use_id"),
        "agent_id": input_data.get("agent_id"),
        "agent_type": input_data.get("agent_type"),
        "output": input_data.get("tool_response"),
    }
    _audit(record)
    return {}


async def capture_tool_failure(
    input_data: dict, tool_use_id: str | None, context: Any
) -> dict:
    """PostToolUseFailure hook. Records that a call errored, and why.

    A tool that fails (e.g. an exception the handler raised) does NOT fire
    PostToolUse — without this hook that failure leaves no trace at all,
    which made an earlier bug (log_recommendation crashing on malformed
    evidence) invisible in the audit log even though it had 13 recorded
    calls and zero results. This hook closes that gap.
    """
    record = {
        "kind": "error",
        "ts": datetime.now(UTC).isoformat(),
        "tool": input_data.get("tool_name", ""),
        "tool_use_id": tool_use_id or input_data.get("tool_use_id"),
        "agent_id": input_data.get("agent_id"),
        "agent_type": input_data.get("agent_type"),
        "error": input_data.get("error"),
    }
    _audit(record)
    return {}


def read_audit_log(run_id: str | None = None) -> list[dict]:
    """Read back the audit log, optionally filtered to one run."""
    if not TOOL_LOG.exists():
        return []
    out = []
    for line in TOOL_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if run_id is None or rec.get("run_id") == run_id:
            out.append(rec)
    return out


def tool_results_for_run(run_id: str) -> dict[str, list[Any]]:
    """Every tool's captured outputs this run, keyed by MCP-namespaced tool
    name minus the `mcp__mybroker__` prefix (bare tool name).

    Returns {"get_portfolio_snapshot": [<parsed output>, ...], ...} — a tool
    called more than once in a run contributes every call's output, since a
    later call may have refreshed data an earlier one didn't have.
    """
    out: dict[str, list[Any]] = {}
    for rec in read_audit_log(run_id):
        if rec.get("kind") != "result":
            continue
        name = rec.get("tool", "")
        short = name.rsplit("__", 1)[-1] if "__" in name else name
        payload = rec.get("output")
        parsed = _parse_tool_response(payload)
        if parsed is not None:
            out.setdefault(short, []).append(parsed)
    return out


def _parse_tool_response(payload: Any) -> Any:
    """Tool responses arrive as content blocks. Two shapes have been observed
    live from the SDK's PostToolUse `tool_response` field: a dict wrapper
    `{"content": [{"type": "text", "text": "<json>"}]}`, and — for MCP tool
    calls made by subagents, and in fact for the orchestrator's own direct
    calls too — a BARE list `[{"type": "text", "text": "<json>"}]` with no
    "content" key at all. Handling only the dict shape silently parses every
    real run to nothing (every field reads as "no numeric output at all"),
    which is a distinct bug from — and more damaging than — the earlier
    evidence-shape crash: it looks like a validator working correctly while
    rejecting every well-formed recommendation. Unwrap either shape to the
    JSON payload our tools actually returned (with its `data`/`provenance`
    keys).
    """
    if payload is None:
        return None
    if isinstance(payload, dict) and "content" in payload:
        payload = payload.get("content") or []
    if isinstance(payload, list):
        texts = [b.get("text") for b in payload if isinstance(b, dict) and b.get("type") == "text"]
        for text in texts:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
        return None
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return payload
