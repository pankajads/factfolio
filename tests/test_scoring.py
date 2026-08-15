"""M5 — the outcome scoring loop.

grade_entry never talks to the network here: a fake provider stands in for
YFinanceProvider so these tests are fast, deterministic, and exercise the
grading arithmetic and verdict rules directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from mybroker.data.base import DataResult, Provenance, Quote
from mybroker.ledger import LedgerEntry, due_for_review, load_ledger, record_outcome
from mybroker.scoring import grade_due_recommendations, grade_entry


@dataclass
class FakeProvider:
    """Returns a canned quote per symbol, or a failure DataResult if absent."""

    prices: dict[str, float]

    def get_quote(self, symbol: str) -> DataResult:
        if symbol not in self.prices:
            return DataResult(
                data=None,
                provenance=Provenance.now("fake"),
                warnings=[f"{symbol}: no data available."],
            )
        return DataResult(
            data=Quote(symbol=symbol, price=self.prices[symbol]),
            provenance=Provenance.now("fake", ticker=symbol),
        )


def _entry(**overrides) -> LedgerEntry:
    base = dict(
        rec_id="20260101-000000-TEST-BUY",
        run_id="run-1",
        logged_at=(datetime.now(UTC) - timedelta(days=40)).isoformat(),
        symbol="TMCV",
        action="BUY",
        conviction="high",
        rationale="test",
        price_at_recommendation=100.0,
        review_after=(datetime.now(UTC).date() - timedelta(days=1)).isoformat(),
    )
    base.update(overrides)
    return LedgerEntry(**base)


class TestGradeEntry:
    def test_buy_that_gained_is_graded_correctly(self):
        entry = _entry(action="BUY", price_at_recommendation=100.0)
        result = grade_entry(entry, FakeProvider({"TMCV": 110.0}))
        assert result.graded
        assert result.outcome["return_pct"] == pytest.approx(10.0)
        assert result.outcome["verdict"] == "gained"

    def test_buy_that_lost_is_graded_correctly(self):
        entry = _entry(action="BUY", price_at_recommendation=100.0)
        result = grade_entry(entry, FakeProvider({"TMCV": 90.0}))
        assert result.outcome["verdict"] == "lost"
        assert result.outcome["return_pct"] == pytest.approx(-10.0)

    def test_trim_that_avoided_a_decline_is_graded_correctly(self):
        entry = _entry(action="TRIM", price_at_recommendation=100.0)
        result = grade_entry(entry, FakeProvider({"TMCV": 80.0}))
        assert result.outcome["verdict"] == "avoided_decline"

    def test_trim_that_missed_a_gain_is_graded_correctly(self):
        entry = _entry(action="TRIM", price_at_recommendation=100.0)
        result = grade_entry(entry, FakeProvider({"TMCV": 120.0}))
        assert result.outcome["verdict"] == "missed_gain"

    def test_sell_uses_same_rule_as_trim(self):
        entry = _entry(action="SELL", price_at_recommendation=100.0)
        result = grade_entry(entry, FakeProvider({"TMCV": 80.0}))
        assert result.outcome["verdict"] == "avoided_decline"

    def test_watch_gets_no_verdict(self):
        entry = _entry(action="WATCH", price_at_recommendation=100.0)
        result = grade_entry(entry, FakeProvider({"TMCV": 90.0}))
        assert result.graded
        assert result.outcome["verdict"] is None
        assert result.outcome["return_pct"] == pytest.approx(-10.0)

    def test_missing_price_at_recommendation_is_ungradeable_not_crashed(self):
        entry = _entry(price_at_recommendation=None)
        result = grade_entry(entry, FakeProvider({"TMCV": 100.0}))
        assert not result.graded
        assert result.outcome is None
        assert "price_at_recommendation" in result.reason

    def test_zero_price_at_recommendation_is_ungradeable(self):
        entry = _entry(price_at_recommendation=0.0)
        result = grade_entry(entry, FakeProvider({"TMCV": 100.0}))
        assert not result.graded
        assert "0" in result.reason

    def test_unavailable_quote_is_ungradeable_not_crashed(self):
        entry = _entry(symbol="NOSUCHSYMBOL")
        result = grade_entry(entry, FakeProvider({}))
        assert not result.graded
        assert result.reason
        assert result.outcome is None

    def test_unresolvable_symbol_keyerror_is_ungradeable_not_raised(self):
        class RaisingProvider:
            def get_quote(self, symbol):
                raise KeyError(f"{symbol} not in tickers.yaml")

        entry = _entry(symbol="GHOST")
        result = grade_entry(entry, RaisingProvider())
        assert not result.graded
        assert "unresolvable" in result.reason.lower()


class TestLedgerRoundTrip:
    """record_outcome persists to the same ledger.jsonl append_recommendation
    writes, and due_for_review() stops returning a graded entry."""

    @pytest.fixture(autouse=True)
    def isolated_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mybroker.ledger.LEDGER_JSONL", tmp_path / "ledger.jsonl")
        monkeypatch.setattr("mybroker.ledger.LEDGER_FILE", tmp_path / "decision_journal.md")
        monkeypatch.setattr("mybroker.ledger.MEMORY_DIR", tmp_path)
        yield

    def _seed(self, entry: LedgerEntry) -> None:
        import json
        from dataclasses import asdict

        from mybroker.ledger import LEDGER_JSONL

        LEDGER_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER_JSONL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry), default=str) + "\n")

    def test_due_for_review_finds_and_then_excludes_a_graded_entry(self):
        entry = _entry(rec_id="R1")
        self._seed(entry)

        assert [e.rec_id for e in due_for_review()] == ["R1"]

        record_outcome("R1", {"graded_at": "x", "verdict": "gained",
                               "return_pct": 5.0, "price_at_recommendation": 100.0,
                               "price_at_grading": 105.0, "days_since_recommendation": 40})

        assert due_for_review() == []
        assert load_ledger()[0].outcome["verdict"] == "gained"

    def test_record_outcome_unknown_rec_id_raises(self):
        with pytest.raises(KeyError):
            record_outcome("NOPE", {})

    def test_not_yet_due_entries_are_excluded(self):
        from datetime import date
        from datetime import timedelta as td

        entry = _entry(rec_id="FUTURE", review_after=(date.today() + td(days=30)).isoformat())
        self._seed(entry)
        assert due_for_review() == []

    def test_grade_due_recommendations_end_to_end_with_fake_provider(self):
        self._seed(_entry(rec_id="E2E", symbol="TMCV", action="BUY",
                           price_at_recommendation=100.0))

        results = grade_due_recommendations(provider=FakeProvider({"TMCV": 111.0}))

        assert len(results) == 1
        assert results[0].graded
        assert results[0].outcome["verdict"] == "gained"
        # Persisted, not just returned:
        assert load_ledger()[0].outcome["verdict"] == "gained"
        assert due_for_review() == []
