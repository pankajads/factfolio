"""Tax rules, with emphasis on the boundaries that are easy to get wrong."""

from datetime import date

import pytest

from mybroker.config import LTCG_EXEMPTION
from mybroker.tax import (
    TaxYearPlanner,
    compute_sale,
    days_to_long_term,
    financial_year,
    find_harvest_candidates,
    is_long_term,
    long_term_date,
)


# ── Holding-period boundary ──────────────────────────────────────────────────
class TestHoldingPeriod:
    def test_364_days_is_short_term(self):
        assert not is_long_term(date(2024, 1, 1), date(2024, 12, 30))

    def test_exactly_365_days_is_long_term(self):
        """The boundary itself. >= 365 qualifies."""
        assert is_long_term(date(2024, 1, 1), date(2024, 12, 31))

    def test_366_days_is_long_term(self):
        assert is_long_term(date(2024, 1, 1), date(2025, 1, 1))

    def test_days_to_long_term_counts_down(self):
        assert days_to_long_term(date(2024, 1, 1), date(2024, 12, 30)) == 1
        assert days_to_long_term(date(2024, 1, 1), date(2024, 12, 31)) == 0

    def test_already_long_term_returns_zero_not_negative(self):
        assert days_to_long_term(date(2020, 1, 1), date(2025, 1, 1)) == 0

    def test_long_term_date(self):
        assert long_term_date(date(2024, 1, 1)) == date(2024, 12, 31)


# ── Financial year ───────────────────────────────────────────────────────────
class TestFinancialYear:
    @pytest.mark.parametrize(
        "d,expected",
        [
            (date(2025, 4, 1), "FY2025-26"),   # first day of FY
            (date(2025, 3, 31), "FY2024-25"),  # last day of previous FY
            (date(2025, 12, 31), "FY2025-26"),
            (date(2026, 1, 1), "FY2025-26"),
        ],
    )
    def test_fy_boundaries(self, d, expected):
        assert financial_year(d) == expected


# ── Single sale ──────────────────────────────────────────────────────────────
class TestSingleSale:
    def test_short_term_gain_taxed_at_20pct(self):
        r = compute_sale(
            symbol="X", quantity=100, sale_price=150, avg_cost=100,
            purchase_date=date(2025, 1, 1), sale_date=date(2025, 6, 1),
        )
        assert r.gain_type == "STCG"
        assert r.gain == pytest.approx(5000)
        assert r.tax == pytest.approx(1000)          # 20% of 5000
        assert r.exemption_used == 0                 # no exemption for STCG

    def test_long_term_gain_under_exemption_is_untaxed(self):
        r = compute_sale(
            symbol="X", quantity=100, sale_price=150, avg_cost=100,
            purchase_date=date(2024, 1, 1), sale_date=date(2025, 6, 1),
        )
        assert r.gain_type == "LTCG"
        assert r.tax == 0
        assert r.exemption_used == pytest.approx(5000)

    def test_long_term_gain_above_exemption(self):
        """₹2L gain: first ₹1.25L exempt, remaining ₹75k at 12.5%."""
        r = compute_sale(
            symbol="X", quantity=1000, sale_price=300, avg_cost=100,
            purchase_date=date(2024, 1, 1), sale_date=date(2025, 6, 1),
        )
        assert r.gain == pytest.approx(200_000)
        assert r.exemption_used == pytest.approx(LTCG_EXEMPTION)
        assert r.taxable_gain == pytest.approx(75_000)
        assert r.tax == pytest.approx(9_375)         # 12.5% of 75k

    def test_loss_is_not_taxed(self):
        r = compute_sale(
            symbol="X", quantity=100, sale_price=50, avg_cost=100,
            purchase_date=date(2024, 1, 1), sale_date=date(2025, 6, 1),
        )
        assert r.gain_type == "NONE"
        assert r.tax == 0
        assert r.gain == pytest.approx(-5000)

    def test_unknown_purchase_date_assumes_short_term_and_says_so(self):
        """The conservative assumption must be explicit, never silent."""
        r = compute_sale(symbol="X", quantity=100, sale_price=150, avg_cost=100)
        assert r.gain_type == "STCG"
        assert r.holding_days is None
        assert any("Purchase date unknown" in a for a in r.assumptions)

    def test_stt_charged_on_sale_value(self):
        r = compute_sale(
            symbol="X", quantity=100, sale_price=150, avg_cost=100,
            purchase_date=date(2024, 1, 1), sale_date=date(2025, 6, 1),
        )
        assert r.stt == pytest.approx(15.0)          # 0.1% of 15,000

    def test_net_proceeds_deduct_tax_and_stt(self):
        r = compute_sale(
            symbol="X", quantity=100, sale_price=150, avg_cost=100,
            purchase_date=date(2025, 1, 1), sale_date=date(2025, 6, 1),
        )
        assert r.net_proceeds == pytest.approx(15_000 - 1_000 - 15)

    def test_worth_waiting_flags_near_ltcg_gains(self):
        r = compute_sale(
            symbol="X", quantity=100, sale_price=150, avg_cost=100,
            purchase_date=date(2024, 7, 1), sale_date=date(2025, 6, 1),
        )
        assert r.gain_type == "STCG"
        assert r.worth_waiting is True               # 30 days from LTCG

    def test_not_worth_waiting_when_far_from_ltcg(self):
        r = compute_sale(
            symbol="X", quantity=100, sale_price=150, avg_cost=100,
            purchase_date=date(2025, 5, 1), sale_date=date(2025, 6, 1),
        )
        assert r.worth_waiting is False


# ── The shared-exemption trap ────────────────────────────────────────────────
class TestExemptionIsSharedAcrossTheYear:
    def test_two_sales_share_one_exemption(self):
        """Each sale computed alone would be untaxed; together they are not.

        This is the single most common way to under-state Indian LTCG tax.
        """
        planner = TaxYearPlanner()
        common = dict(
            quantity=1000, sale_price=200, avg_cost=100,
            purchase_date=date(2024, 1, 1), sale_date=date(2025, 6, 1),
        )
        a = planner.add(symbol="A", **common)   # ₹100k gain
        b = planner.add(symbol="B", **common)   # ₹100k gain

        assert a.tax == 0                                    # fully exempt
        assert b.exemption_used == pytest.approx(25_000)     # only 25k left
        assert b.tax == pytest.approx(75_000 * 0.125)        # 9,375
        assert planner.exemption_remaining == 0

    def test_computing_sales_independently_understates_tax(self):
        """Demonstrates the bug this planner exists to prevent."""
        common = dict(
            quantity=1000, sale_price=200, avg_cost=100,
            purchase_date=date(2024, 1, 1), sale_date=date(2025, 6, 1),
        )
        naive = sum(compute_sale(symbol=s, **common).tax for s in ("A", "B"))

        planner = TaxYearPlanner()
        for s in ("A", "B"):
            planner.add(symbol=s, **common)

        assert naive == 0
        assert planner.total_tax > 0        # the correct answer

    def test_summary_reports_remaining_budget(self):
        planner = TaxYearPlanner()
        planner.add(
            symbol="A", quantity=100, sale_price=600, avg_cost=100,
            purchase_date=date(2024, 1, 1), sale_date=date(2025, 6, 1),
        )
        s = planner.summary()
        assert s["ltcg_exemption_used"] == pytest.approx(50_000)
        assert s["ltcg_exemption_remaining"] == pytest.approx(75_000)
        assert "surcharge" in s["disclaimer"].lower()


# ── Harvesting ───────────────────────────────────────────────────────────────
class TestHarvesting:
    def test_finds_losers_sorted_worst_first(self):
        class P:
            def __init__(self, symbol, pnl, pnl_pct, current_value):
                self.symbol, self.pnl = symbol, pnl
                self.pnl_pct, self.current_value = pnl_pct, current_value

        out = find_harvest_candidates([
            P("WINNER", 5000, 10.0, 55000),
            P("SMALL_LOSS", -500, -1.0, 49500),   # below threshold
            P("BIG_LOSS", -16499, -31.7, 35512),
            P("MID_LOSS", -10956, -17.1, 53217),
        ])
        assert [c["symbol"] for c in out] == ["BIG_LOSS", "MID_LOSS"]
        assert "wash-sale" in out[0]["note"]
