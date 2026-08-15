"""Parsing of PostToolUse `tool_response` payloads.

Regression coverage for the second live-run bug (found in run
20260814-003901-538918, immediately after the evidence-shape bug was fixed):
the model submitted well-formed `{tool, field, value}` evidence, yet every
single `log_recommendation` call was still rejected with "no numeric output
at all" for every cited tool. Root cause was in `_parse_tool_response`, not
the validator — the live SDK delivers `tool_response` as a BARE list
`[{"type": "text", "text": "<json>"}]`, not the `{"content": [...]}` wrapper
every existing test fixture assumed. That shape was never exercised, so the
bug was invisible to 111 passing tests. This file locks in the real shape
observed live, alongside the wrapped shape for backward compatibility.
"""

import json

import anyio
import pytest

from mybroker.security.hooks import (
    _parse_tool_response,
    audit_and_guard,
    capture_tool_result,
    set_current_run,
    tool_results_for_run,
)


class TestParseToolResponse:
    def test_bare_list_shape_is_the_real_live_shape(self):
        """Observed live for every agent type, including the orchestrator's
        own direct calls — not just subagent-dispatched ones."""
        payload = [{"type": "text", "text": json.dumps({"data": {"price": 410.5}})}]
        assert _parse_tool_response(payload) == {"data": {"price": 410.5}}

    def test_dict_wrapped_shape_still_works(self):
        """The shape every earlier test fixture assumed — keep supporting it
        in case the SDK ever delivers this form too."""
        payload = {"content": [{"type": "text", "text": json.dumps({"data": {"price": 410.5}})}]}
        assert _parse_tool_response(payload) == {"data": {"price": 410.5}}

    def test_none_payload(self):
        assert _parse_tool_response(None) is None

    def test_empty_list(self):
        assert _parse_tool_response([]) is None

    def test_list_with_unparseable_text(self):
        payload = [{"type": "text", "text": "not json"}]
        assert _parse_tool_response(payload) is None

    def test_bare_json_string(self):
        assert _parse_tool_response(json.dumps({"data": {"x": 1}})) == {"data": {"x": 1}}


class TestToolResultsForRunWithRealShape:
    """End-to-end: a hook-captured record using the exact live shape must be
    readable by the validator's evidence lookup."""

    @pytest.fixture(autouse=True)
    def clean_log(self, tmp_path, monkeypatch):
        log = tmp_path / "tool_calls.jsonl"
        monkeypatch.setattr("mybroker.config.TOOL_LOG", log)
        monkeypatch.setattr("mybroker.security.hooks.TOOL_LOG", log)
        yield

    def test_subagent_call_with_bare_list_output_is_captured(self):
        run_id = "real-shape-run"
        set_current_run(run_id)
        pre = {"tool_name": "mcp__mybroker__get_quote", "tool_input": {"symbol": "BEL"},
               "agent_id": "a5a2b855ca71aaa87", "agent_type": "stock-researcher"}
        anyio.run(audit_and_guard, pre, "tu", None)
        post = {**pre, "tool_response": [
            {"type": "text", "text": json.dumps({
                "data": {"symbol": "BEL", "price": 410.5, "day_change_pct": 1.32},
                "provenance": {"source": "yfinance"},
            })}
        ]}
        anyio.run(capture_tool_result, post, "tu", None)

        results = tool_results_for_run(run_id)
        assert "get_quote" in results
        assert results["get_quote"][0]["data"]["price"] == 410.5

    def test_orchestrator_direct_call_with_bare_list_output_is_captured(self):
        """agent_type=None (the orchestrator itself) uses the same bare-list
        shape live — must not be treated differently from a subagent call."""
        run_id = "real-shape-run-2"
        set_current_run(run_id)
        pre = {"tool_name": "mcp__mybroker__get_portfolio_snapshot", "tool_input": {},
               "agent_id": None, "agent_type": None}
        anyio.run(audit_and_guard, pre, "tu", None)
        post = {**pre, "tool_response": [
            {"type": "text", "text": json.dumps({"data": {"totals": {"pnl_pct": 3.55}}})}
        ]}
        anyio.run(capture_tool_result, post, "tu", None)

        results = tool_results_for_run(run_id)
        assert results["get_portfolio_snapshot"][0]["data"]["totals"]["pnl_pct"] == 3.55
