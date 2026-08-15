"""Tentative purchase-date estimation (portfolio/purchase_estimator.py) and
its wiring into compute_tax_impact as a fallback for a missing purchase_date.
"""

from __future__ import annotations

from datetime import date

import anyio
import pytest

from mybroker.portfolio.purchase_estimator import (
    DateEstimate,
    estimate_purchase_date,
    load_estimates,
    save_estimates,
)


def _series(*pairs: tuple[str, float]) -> list[dict]:
    return [{"date": d, "close": c} for d, c in pairs]


class TestEstimatePurchaseDate:
    def test_finds_most_recent_match_within_tightest_tolerance(self):
        history = _series(
            ("2024-01-10", 100.0),   # old match, exact
            ("2026-08-01", 101.0),   # recent match, within 1.5%
            ("2026-08-10", 150.0),   # unrelated, most recent
        )
        est = estimate_purchase_date(
            "TEST", 100.5, history, today=date(2026, 8, 14)
        )
        assert est.confident
        assert est.estimated_date == "2026-08-01"  # most recent, not oldest
        assert est.tolerance_pct == 1.5
        assert est.holding_days_from_today == 13

    def test_widens_tolerance_only_when_tight_band_has_no_match(self):
        # avg_cost 100, closest available is 106 (6% away) — needs the 8% band.
        history = _series(("2026-01-01", 106.0))
        est = estimate_purchase_date("TEST", 100.0, history, today=date(2026, 8, 14))
        assert est.confident
        assert est.tolerance_pct == 8.0

    def test_no_match_within_any_band_is_unconfident_not_forced(self):
        history = _series(("2026-01-01", 200.0))  # 100% away from avg_cost 100
        est = estimate_purchase_date("TEST", 100.0, history, today=date(2026, 8, 14))
        assert not est.confident
        assert est.estimated_date is None
        assert "2026-01-01" in est.note  # names the oldest-available bound

    def test_empty_history_is_unconfident_not_crashed(self):
        est = estimate_purchase_date("TEST", 100.0, [], today=date(2026, 8, 14))
        assert not est.confident
        assert est.estimated_date is None

    def test_zero_or_negative_avg_cost_is_unconfident_not_crashed(self):
        history = _series(("2026-01-01", 0.0))
        est = estimate_purchase_date("TEST", 0.0, history, today=date(2026, 8, 14))
        assert not est.confident

    def test_history_order_does_not_matter(self):
        """Ascending or descending input — the function sorts internally."""
        ascending = _series(("2024-01-01", 90.0), ("2026-08-01", 101.0))
        descending = list(reversed(ascending))
        a = estimate_purchase_date("TEST", 100.5, ascending, today=date(2026, 8, 14))
        b = estimate_purchase_date("TEST", 100.5, descending, today=date(2026, 8, 14))
        assert a.estimated_date == b.estimated_date == "2026-08-01"

    def test_close_values_of_none_are_skipped_not_crashed(self):
        history = [
            {"date": "2026-08-01", "close": None},
            {"date": "2026-07-01", "close": 100.2},
        ]
        est = estimate_purchase_date("TEST", 100.0, history, today=date(2026, 8, 14))
        assert est.confident
        assert est.estimated_date == "2026-07-01"


class TestSaveLoadRoundTrip:
    @pytest.fixture(autouse=True)
    def isolated_memory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "mybroker.portfolio.purchase_estimator.ESTIMATES_JSON",
            tmp_path / "estimated_purchase_dates.json",
        )
        monkeypatch.setattr(
            "mybroker.portfolio.purchase_estimator.ESTIMATES_MD",
            tmp_path / "estimated_purchase_dates.md",
        )
        monkeypatch.setattr("mybroker.portfolio.purchase_estimator.MEMORY_DIR", tmp_path)
        yield

    def test_round_trip_preserves_fields(self):
        est = estimate_purchase_date(
            "BEL", 401.38, _series(("2026-08-12", 405.15)), today=date(2026, 8, 14)
        )
        save_estimates([est])

        loaded = load_estimates()
        assert "BEL" in loaded
        assert loaded["BEL"].estimated_date == "2026-08-12"
        assert loaded["BEL"].confident is True

    def test_writes_markdown_too(self):
        from mybroker.portfolio.purchase_estimator import ESTIMATES_MD

        est = estimate_purchase_date(
            "BEL", 401.38, _series(("2026-08-12", 405.15)), today=date(2026, 8, 14)
        )
        save_estimates([est])
        assert ESTIMATES_MD.exists()
        assert "BEL" in ESTIMATES_MD.read_text()
        assert "tentative" in ESTIMATES_MD.read_text().lower()

    def test_load_with_no_file_returns_empty_dict(self):
        assert load_estimates() == {}

    def test_load_with_corrupt_file_returns_empty_dict_not_raises(self):
        from mybroker.portfolio.purchase_estimator import ESTIMATES_JSON

        ESTIMATES_JSON.parent.mkdir(parents=True, exist_ok=True)
        ESTIMATES_JSON.write_text("not valid json{{{")
        assert load_estimates() == {}

    def test_unconfident_estimate_round_trips_as_none(self):
        est = DateEstimate(
            symbol="TMCV", avg_cost=457.0, estimated_date=None, matched_price=None,
            tolerance_pct=None, holding_days_from_today=None,
            method="x", confident=False, note="no match",
        )
        save_estimates([est])
        loaded = load_estimates()
        assert loaded["TMCV"].confident is False
        assert loaded["TMCV"].estimated_date is None


class TestComputeTaxImpactWiring:
    """The MCP tool handler, exercised directly — same style as
    test_log_recommendation_tool.py. Patching `purchase_estimator.
    ESTIMATES_JSON` is sufficient to isolate this: `load_estimates()`
    resolves that name from its own module's globals at call time, the same
    mechanism `ledger.py`'s tests rely on for LEDGER_JSONL."""

    @pytest.fixture(autouse=True)
    def isolated_estimates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "mybroker.portfolio.purchase_estimator.ESTIMATES_JSON",
            tmp_path / "estimated_purchase_dates.json",
        )
        monkeypatch.setattr(
            "mybroker.portfolio.purchase_estimator.ESTIMATES_MD",
            tmp_path / "estimated_purchase_dates.md",
        )
        monkeypatch.setattr("mybroker.portfolio.purchase_estimator.MEMORY_DIR", tmp_path)
        yield

    def _sale(self, **overrides):
        base = {"symbol": "BEL", "quantity": 10, "sale_price": 500.0, "avg_cost": 400.0}
        base.update(overrides)
        return base

    def test_explicit_purchase_date_is_not_overridden_by_an_estimate(self):
        est = estimate_purchase_date(
            "BEL", 400.0, _series(("2020-01-01", 400.0)), today=date(2026, 8, 14)
        )
        save_estimates([est])

        from mybroker.tools.server import compute_tax_impact

        result = anyio.run(
            compute_tax_impact.handler,
            {"sales": [self._sale(purchase_date="2026-06-01")]},
        )
        import json
        payload = json.loads(result["content"][0]["text"])
        row = payload["data"]["sales"][0]
        assert row["purchase_date_source"] == "explicit"

    def test_confident_estimate_is_used_and_flagged(self):
        est = estimate_purchase_date(
            "BEL", 400.0, _series(("2024-01-01", 400.0)), today=date(2026, 8, 14)
        )
        save_estimates([est])

        from mybroker.tools.server import compute_tax_impact

        result = anyio.run(compute_tax_impact.handler, {"sales": [self._sale()]})
        import json
        payload = json.loads(result["content"][0]["text"])
        row = payload["data"]["sales"][0]

        assert row["purchase_date_source"] == "estimated"
        assert row["gain_type"] in ("LTCG", "NONE")  # 2024 purchase -> long-term by 2026
        assert any("ESTIMATE" in a for a in row["assumptions"])

    def test_no_estimate_falls_back_to_unknown_short_term(self):
        # isolated_estimates points ESTIMATES_JSON at an empty tmp_path — no
        # file has been written, so load_estimates() returns {}.
        from mybroker.tools.server import compute_tax_impact

        result = anyio.run(compute_tax_impact.handler, {"sales": [self._sale()]})
        import json
        payload = json.loads(result["content"][0]["text"])
        row = payload["data"]["sales"][0]

        assert row["purchase_date_source"] == "unknown"
        assert row["gain_type"] == "STCG"
