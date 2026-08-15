"""log_recommendation, exercised through the actual MCP tool handler.

This is the regression suite for the bug found in the first live M2 run: the
model submitted `evidence` as a list of prose strings, and the handler
crashed with an unhandled AttributeError instead of returning a structured
rejection. A tool handler that crashes breaks the agent's ability to retry —
this file locks in that it now degrades gracefully for every malformed shape
that produced the original failure, plus a defensive sweep beyond it.
"""

import json

import anyio
import pytest

from mybroker.security.hooks import audit_and_guard, capture_tool_result, set_current_run
from mybroker.tools.server import log_recommendation


@pytest.fixture(autouse=True)
def clean_log(tmp_path, monkeypatch):
    log = tmp_path / "tool_calls.jsonl"
    monkeypatch.setattr("mybroker.config.TOOL_LOG", log)
    monkeypatch.setattr("mybroker.security.hooks.TOOL_LOG", log)
    yield


def seed_real_quote(run_id: str, symbol: str, price: float) -> None:
    """Seed a run with a real-shaped get_quote result, via the actual hooks."""
    set_current_run(run_id)
    pre = {"tool_name": "mcp__mybroker__get_quote", "tool_input": {"symbol": symbol},
           "agent_id": "a", "agent_type": "orchestrator"}
    anyio.run(audit_and_guard, pre, "tu", None)
    post = {**pre, "tool_response": {
        "content": [{"type": "text", "text": json.dumps({
            "data": {"symbol": symbol, "price": price}, "provenance": {"source": "yfinance"}
        })}]
    }}
    anyio.run(capture_tool_result, post, "tu", None)


# action=BUY deliberately — it needs no tax_impact, keeping these tests
# focused purely on evidence-shape handling rather than the separate
# sell-requires-tax rule (covered in test_validator.py).
BASE_REC = {
    "symbol": "TMCV", "action": "BUY", "conviction": "high",
    "rationale": "test", "risk_if_wrong": "x", "invalidation_trigger": "y",
}


class TestNeverCrashes:
    """The literal reproduction of the live failure, plus a sweep of nearby
    malformed shapes — every one must return a structured rejection."""

    @pytest.mark.parametrize("evidence", [
        # The exact shape the model actually submitted, live, 13 times:
        ["check_policy_compliance: position_size TMCV actual_pct 14.27 vs "
         "limit_pct 5.0, excess_pct 9.27, severity high"],
        ["a prose string", "another prose string"],
        [],
        None,
        "not even a list",
        [{"tool": "get_quote"}],                                   # missing keys
        [{"tool": "get_quote", "field": "price", "value": "high"}],  # non-numeric
        [123, {"tool": "get_quote", "field": "price", "value": 1.0}],  # mixed
    ])
    def test_malformed_evidence_returns_error_not_exception(self, evidence):
        run_id = "test-run"
        set_current_run(run_id)
        rec = {**BASE_REC, "evidence": evidence}

        result = anyio.run(log_recommendation.handler, rec)  # must not raise

        assert result.get("is_error") is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["accepted"] is False
        assert payload["problems"]
        assert "guidance" in payload


class TestHappyPathStillWorks:
    """The fix must not have broken the case that already worked."""

    def test_correctly_shaped_evidence_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mybroker.config.MEMORY_DIR", tmp_path)
        monkeypatch.setattr("mybroker.config.LEDGER_FILE", tmp_path / "decision_journal.md")
        monkeypatch.setattr("mybroker.ledger.LEDGER_JSONL", tmp_path / "ledger.jsonl")
        monkeypatch.setattr("mybroker.ledger.LEDGER_FILE", tmp_path / "decision_journal.md")

        run_id = "happy-run"
        seed_real_quote(run_id, "TMCV", 457.05)

        rec = {
            **BASE_REC,
            "evidence": [{"tool": "get_quote", "field": "price", "value": 457.05}],
        }
        result = anyio.run(log_recommendation.handler, rec)

        assert not result.get("is_error")
        payload = json.loads(result["content"][0]["text"])
        assert payload["data"]["accepted"] is True
        assert payload["data"]["rec_id"]

    def test_fabricated_value_in_otherwise_valid_shape_still_rejected(self):
        run_id = "test-run-2"
        seed_real_quote(run_id, "TMCV", 457.05)

        rec = {
            **BASE_REC,
            "evidence": [{"tool": "get_quote", "field": "price", "value": 999999.0}],
        }
        result = anyio.run(log_recommendation.handler, rec)

        assert result.get("is_error") is True
        payload = json.loads(result["content"][0]["text"])
        assert not payload["accepted"]
        assert any("NOT FOUND" in p for p in payload["problems"])
