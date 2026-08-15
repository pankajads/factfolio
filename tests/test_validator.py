"""The provenance validator: the code gate against hallucinated numbers.

Every test seeds a fake run's tool-call log via the hooks module (bypassing
any live agent), then checks the validator's verdict. This is the mechanism
that turns "no fabricated numbers" from a prompt request into an enforced
invariant, so it gets the most scrutiny of anything in this codebase.
"""

import json

import anyio
import pytest

from mybroker.security.hooks import audit_and_guard, capture_tool_result, set_current_run
from mybroker.security.validator import (
    flatten_numeric,
    verify_evidence_item,
    verify_recommendation,
)


def seed_call(run_id: str, tool: str, output: dict, *, agent_type: str = "orchestrator") -> None:
    """Simulate one real tool call+result for a run, via the actual hooks —
    exercising the same code path a live agent run uses."""
    set_current_run(run_id)
    pre = {"tool_name": f"mcp__mybroker__{tool}", "tool_input": {},
           "agent_id": "a", "agent_type": agent_type}
    anyio.run(audit_and_guard, pre, "tu", None)
    post = {**pre, "tool_response": {
        "content": [{"type": "text", "text": json.dumps(output)}]
    }}
    anyio.run(capture_tool_result, post, "tu", None)


@pytest.fixture(autouse=True)
def clean_log(tmp_path, monkeypatch):
    """Every test gets an isolated audit log so runs never bleed together."""
    log = tmp_path / "tool_calls.jsonl"
    monkeypatch.setattr("mybroker.config.TOOL_LOG", log)
    monkeypatch.setattr("mybroker.security.hooks.TOOL_LOG", log)
    yield


class TestFlattenNumeric:
    def test_nested_dict(self):
        flat = flatten_numeric({"a": {"b": 1.5, "c": {"d": 2}}})
        assert flat["a.b"] == 1.5
        assert flat["a.c.d"] == 2.0

    def test_lists(self):
        flat = flatten_numeric({"positions": [{"weight_pct": 15.9}, {"weight_pct": 8.3}]})
        assert flat["positions[0].weight_pct"] == 15.9
        assert flat["positions[1].weight_pct"] == 8.3

    def test_booleans_excluded(self):
        """bool is a subclass of int in Python — must not be treated as numeric,
        or a claimed value of 1 would match a `true` anywhere in the output."""
        flat = flatten_numeric({"cached": True, "sufficient": False, "price": 100})
        assert "cached" not in flat
        assert "sufficient" not in flat
        assert flat["price"] == 100.0

    def test_strings_and_none_excluded(self):
        flat = flatten_numeric({"symbol": "TMCV", "note": None, "price": 457.05})
        assert set(flat) == {"price"}


class TestEvidenceMatching:
    def test_exact_match(self):
        seed_call("r1", "get_portfolio_snapshot",
                  {"data": {"totals": {"current_value": 640677.95}}})
        c = verify_evidence_item(
            {"tool": "get_portfolio_snapshot", "field": "current_value", "value": 640677.95},
            "r1",
        )
        assert c.strength == "exact"
        assert c.ok

    def test_rounded_value_still_matches(self):
        """The agent states 640678, tool actually returned 640677.95."""
        seed_call("r1", "get_portfolio_snapshot",
                  {"data": {"totals": {"current_value": 640677.95}}})
        c = verify_evidence_item(
            {"tool": "get_portfolio_snapshot", "field": "current_value", "value": 640678},
            "r1",
        )
        assert c.ok

    def test_value_present_under_related_field_name_is_exact(self):
        """'price' as a substring of 'last_traded_price' is a legitimate
        soft match — related labels should count as exact, not weak."""
        seed_call("r1", "get_quote", {"data": {"price": 457.05}})
        c = verify_evidence_item(
            {"tool": "get_quote", "field": "last_traded_price", "value": 457.05}, "r1"
        )
        assert c.strength == "exact"
        assert c.ok

    def test_value_present_under_unrelated_field_name_is_a_weak_pass(self):
        """No lexical overlap at all between claimed and actual field name —
        the number is real, but the label doesn't obviously correspond."""
        seed_call("r1", "get_quote", {"data": {"price": 457.05}})
        c = verify_evidence_item(
            {"tool": "get_quote", "field": "day_high", "value": 457.05}, "r1"
        )
        assert c.strength == "value"
        assert c.ok

    def test_fabricated_value_is_rejected(self):
        """The core invariant: a number with no basis in any tool call fails."""
        seed_call("r1", "get_quote", {"data": {"price": 457.05}})
        c = verify_evidence_item(
            {"tool": "get_quote", "field": "price", "value": 999.99}, "r1"
        )
        assert c.strength == "none"
        assert not c.ok
        assert "NOT FOUND" in c.explain()

    def test_wrong_tool_is_rejected_even_if_value_exists_elsewhere(self):
        """A value that's real, but for a DIFFERENT tool, must not validate —
        citing the wrong source is itself a form of fabrication."""
        seed_call("r1", "get_quote", {"data": {"price": 457.05}})
        seed_call("r1", "get_fundamentals", {"data": {"pe_ratio": 82.5}})
        c = verify_evidence_item(
            {"tool": "get_fundamentals", "field": "price", "value": 457.05}, "r1"
        )
        assert c.strength == "none"

    def test_no_calls_to_that_tool_this_run(self):
        c = verify_evidence_item(
            {"tool": "get_quote", "field": "price", "value": 100}, "r1"
        )
        assert c.strength == "none"
        assert "no numeric output at all" in c.explain()

    def test_second_call_to_same_tool_is_also_checked(self):
        """A refreshed quote later in the run must still validate."""
        seed_call("r1", "get_quote", {"data": {"price": 450.0}})
        seed_call("r1", "get_quote", {"data": {"price": 457.05}})
        c = verify_evidence_item(
            {"tool": "get_quote", "field": "price", "value": 457.05}, "r1"
        )
        assert c.ok

    def test_runs_are_isolated(self):
        seed_call("run-a", "get_quote", {"data": {"price": 100}})
        seed_call("run-b", "get_quote", {"data": {"price": 200}})
        c = verify_evidence_item({"tool": "get_quote", "field": "price", "value": 100}, "run-b")
        assert not c.ok

    def test_far_off_value_does_not_match_by_luck(self):
        seed_call("r1", "get_quote", {"data": {"price": 100.0}})
        c = verify_evidence_item({"tool": "get_quote", "field": "price", "value": 150.0}, "r1")
        assert not c.ok


class TestMalformedEvidenceNeverCrashes:
    """The regression class from the first live M2 run: log_recommendation
    must reject malformed evidence, never raise."""

    @pytest.mark.parametrize("bad_item", [
        "get_quote: price 457.05, day_change 1.2%",   # the actual failure seen live
        "",
        None,
        42,
        3.14,
        ["tool", "field", "value"],
        {"tool": "get_quote"},                          # missing field, value
        {"tool": "get_quote", "field": "price"},         # missing value
        {"field": "price", "value": 100},                # missing tool
        {"tool": "get_quote", "field": "price", "value": "not a number"},
        {"tool": "get_quote", "field": "price", "value": None},
        {"tool": "get_quote", "field": "price", "value": [1, 2, 3]},
    ])
    def test_never_raises(self, bad_item):
        c = verify_evidence_item(bad_item, "r1")  # must not raise for any shape
        assert c.strength == "malformed"
        assert not c.ok
        assert "tool" in c.explain() and "field" in c.explain()  # shows the example


class TestRecommendationValidation:
    def _valid_rec(self, **overrides) -> dict:
        base = {
            "symbol": "TMCV",
            "action": "BUY",
            "conviction": "high",
            "rationale": "Strong fundamentals post-demerger.",
            "evidence": [
                {"tool": "get_quote", "field": "price", "value": 457.05},
            ],
            "risk_if_wrong": "Demerger transition costs exceed expectations.",
            "invalidation_trigger": "Two consecutive quarters of margin decline.",
        }
        base.update(overrides)
        return base

    def test_valid_recommendation_passes(self):
        seed_call("r1", "get_quote", {"data": {"price": 457.05}})
        result = verify_recommendation(self._valid_rec(), "r1")
        assert result.ok, result.problems

    def test_fabricated_evidence_fails_the_whole_recommendation(self):
        seed_call("r1", "get_quote", {"data": {"price": 457.05}})
        rec = self._valid_rec(
            evidence=[{"tool": "get_quote", "field": "price", "value": 9999.0}]
        )
        result = verify_recommendation(rec, "r1")
        assert not result.ok
        assert any("NOT FOUND" in p for p in result.problems)

    def test_missing_required_field(self):
        rec = self._valid_rec()
        del rec["rationale"]
        result = verify_recommendation(rec, "r1")
        assert not result.ok
        assert any("rationale" in p for p in result.problems)

    def test_invalid_action_rejected(self):
        result = verify_recommendation(self._valid_rec(action="MAYBE"), "r1")
        assert not result.ok
        assert any("action" in p for p in result.problems)

    def test_invalid_conviction_rejected(self):
        result = verify_recommendation(self._valid_rec(conviction="very high"), "r1")
        assert not result.ok

    def test_empty_evidence_rejected(self):
        result = verify_recommendation(self._valid_rec(evidence=[]), "r1")
        assert not result.ok
        assert any("evidence" in p for p in result.problems)

    def test_sell_without_tax_impact_rejected(self):
        seed_call("r1", "get_quote", {"data": {"price": 457.05}})
        rec = self._valid_rec(action="SELL")
        result = verify_recommendation(rec, "r1")
        assert not result.ok
        assert any("tax_impact" in p for p in result.problems)

    def test_sell_with_tax_impact_passes(self):
        seed_call("r1", "get_quote", {"data": {"price": 457.05}})
        seed_call("r1", "compute_tax_impact", {
            "data": {"summary": {"total_tax": 9375.0}}
        })
        rec = self._valid_rec(
            action="SELL",
            tax_impact={"total_tax": 9375.0},
            evidence=[
                {"tool": "get_quote", "field": "price", "value": 457.05},
                {"tool": "compute_tax_impact", "field": "total_tax", "value": 9375.0},
            ],
        )
        result = verify_recommendation(rec, "r1")
        assert result.ok, result.problems

    def test_string_evidence_item_rejected_not_crashed(self):
        """Regression: the live agent submitted evidence as a list of prose
        strings ("check_policy_compliance: position_size TMCV actual_pct
        14.27...") instead of {tool, field, value} objects. This must reject
        gracefully, not raise AttributeError from item.get() on a str."""
        rec = self._valid_rec(
            evidence=["check_policy_compliance: position_size TMCV actual_pct 14.27"]
        )
        result = verify_recommendation(rec, "r1")  # must not raise
        assert not result.ok
        assert any("malformed" in p for p in result.problems)

    def test_evidence_item_missing_keys_rejected_not_crashed(self):
        rec = self._valid_rec(evidence=[{"tool": "get_quote", "value": 457.05}])
        result = verify_recommendation(rec, "r1")
        assert not result.ok
        assert any("field" in p for p in result.problems)

    def test_evidence_item_non_numeric_value_rejected_not_crashed(self):
        rec = self._valid_rec(
            evidence=[{"tool": "get_quote", "field": "price", "value": "about 457"}]
        )
        result = verify_recommendation(rec, "r1")
        assert not result.ok

    def test_evidence_item_none_and_int_types_rejected_not_crashed(self):
        for bad_item in (None, 42, ["nested", "list"], 3.14):
            rec = self._valid_rec(evidence=[bad_item])
            result = verify_recommendation(rec, "r1")  # must not raise for any of these
            assert not result.ok

    def test_multiple_evidence_items_all_checked(self):
        seed_call("r1", "get_quote", {"data": {"price": 457.05}})
        seed_call("r1", "get_fundamentals", {"data": {"roe_pct": 26.0}})
        rec = self._valid_rec(
            evidence=[
                {"tool": "get_quote", "field": "price", "value": 457.05},
                {"tool": "get_fundamentals", "field": "roe_pct", "value": 99.9},  # fabricated
            ]
        )
        result = verify_recommendation(rec, "r1")
        assert not result.ok
        assert len(result.evidence_checks) == 2
        assert result.evidence_checks[0].ok
        assert not result.evidence_checks[1].ok
