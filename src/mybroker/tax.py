"""Indian capital-gains tax for listed equity and equity mutual funds.

Rules encoded (FY 2025-26, post-23-July-2024 regime):
  • Short-term  (held < 365 days): 20% on the gain, no exemption.
  • Long-term   (held >= 365 days): 12.5% on gains above a ₹1,25,000
    exemption that applies ONCE PER FINANCIAL YEAR across all such gains —
    not per transaction.
  • STT of 0.1% on the delivery sell value.

The per-financial-year nature of the LTCG exemption is the detail most often
got wrong: computing each sale in isolation silently under-states tax whenever
more than one long-term sale happens in the same year. `TaxYearPlanner` below
exists specifically to model that correctly.

Surcharge and cess are deliberately NOT modelled — they depend on total income
which this system does not know. Figures are therefore a floor, and every
result says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from mybroker.config import (
    LTCG_EXEMPTION,
    LTCG_HOLDING_DAYS,
    LTCG_RATE,
    STCG_RATE,
    STT_RATE,
)

DISCLAIMER = (
    "Excludes surcharge and cess (both depend on total income, which is not "
    "known here). Treat as a lower bound, not a filing figure."
)


def financial_year(d: date) -> str:
    """Indian financial year label for a date. FY runs 1 April → 31 March."""
    return f"FY{d.year}-{d.year + 1 - 2000:02d}" if d.month >= 4 else (
        f"FY{d.year - 1}-{d.year - 2000:02d}"
    )


def holding_days(purchase: date, sale: date) -> int:
    return (sale - purchase).days


def is_long_term(purchase: date, sale: date) -> bool:
    return holding_days(purchase, sale) >= LTCG_HOLDING_DAYS


def days_to_long_term(purchase: date, today: date | None = None) -> int:
    """Days remaining until the holding qualifies as long term. 0 if already."""
    today = today or date.today()
    return max(0, LTCG_HOLDING_DAYS - holding_days(purchase, today))


def long_term_date(purchase: date) -> date:
    """The first date on which this holding becomes long term."""
    return purchase + timedelta(days=LTCG_HOLDING_DAYS)


@dataclass
class SaleResult:
    """Tax consequence of one hypothetical sale."""

    symbol: str
    quantity: float
    sale_value: float
    cost_basis: float
    gain: float
    gain_type: str            # "STCG" | "LTCG" | "NONE" (a loss)
    holding_days: int | None  # None when the purchase date is unknown
    days_to_ltcg: int | None

    taxable_gain: float       # after any exemption applied
    tax: float
    stt: float
    net_proceeds: float       # sale_value - tax - stt

    exemption_used: float = 0.0
    assumptions: list[str] = field(default_factory=list)

    @property
    def effective_rate_pct(self) -> float:
        return (self.tax / self.gain * 100) if self.gain > 0 else 0.0

    @property
    def worth_waiting(self) -> bool:
        """True when holding a little longer converts STCG into cheaper LTCG."""
        return (
            self.gain_type == "STCG"
            and self.gain > 0
            and self.days_to_ltcg is not None
            and 0 < self.days_to_ltcg <= 90
        )


def compute_sale(
    *,
    symbol: str,
    quantity: float,
    sale_price: float,
    avg_cost: float,
    purchase_date: date | None = None,
    sale_date: date | None = None,
    exemption_remaining: float = LTCG_EXEMPTION,
) -> SaleResult:
    """Tax on selling `quantity` at `sale_price`.

    When `purchase_date` is unknown the sale is treated as SHORT term — the
    conservative assumption, since it produces the higher tax figure. The
    assumption is recorded in `assumptions` so it is never invisible.
    """
    sale_date = sale_date or date.today()
    sale_value = quantity * sale_price
    cost_basis = quantity * avg_cost
    gain = sale_value - cost_basis
    stt = sale_value * STT_RATE

    assumptions: list[str] = []
    hold_days: int | None = None
    to_ltcg: int | None = None

    if purchase_date is None:
        long_term = False
        assumptions.append(
            "Purchase date unknown — assumed SHORT term (higher tax). Supply the "
            "purchase date for an accurate figure."
        )
    else:
        hold_days = holding_days(purchase_date, sale_date)
        long_term = hold_days >= LTCG_HOLDING_DAYS
        to_ltcg = max(0, LTCG_HOLDING_DAYS - hold_days)

    # A loss is not taxed. (Carry-forward/set-off is not modelled here.)
    if gain <= 0:
        return SaleResult(
            symbol=symbol, quantity=quantity, sale_value=sale_value,
            cost_basis=cost_basis, gain=gain, gain_type="NONE",
            holding_days=hold_days, days_to_ltcg=to_ltcg,
            taxable_gain=0.0, tax=0.0, stt=stt,
            net_proceeds=sale_value - stt,
            assumptions=assumptions + [
                "Loss — no tax. May be set off against other capital gains "
                "(set-off/carry-forward rules are not modelled)."
            ],
        )

    if long_term:
        exempt = min(gain, max(0.0, exemption_remaining))
        taxable = gain - exempt
        tax = taxable * LTCG_RATE
        gain_type = "LTCG"
    else:
        exempt = 0.0
        taxable = gain
        tax = taxable * STCG_RATE
        gain_type = "STCG"

    return SaleResult(
        symbol=symbol, quantity=quantity, sale_value=sale_value,
        cost_basis=cost_basis, gain=gain, gain_type=gain_type,
        holding_days=hold_days, days_to_ltcg=to_ltcg,
        taxable_gain=taxable, tax=tax, stt=stt,
        net_proceeds=sale_value - tax - stt,
        exemption_used=exempt,
        assumptions=assumptions,
    )


class TaxYearPlanner:
    """Sequences several sales within one financial year.

    The ₹1.25L LTCG exemption is a per-year budget shared across every
    long-term sale. Computing each sale independently double-counts it, so any
    multi-sell recommendation must be costed through a single planner instance.
    """

    def __init__(self, exemption: float = LTCG_EXEMPTION) -> None:
        self.exemption_total = exemption
        self.exemption_remaining = exemption
        self.sales: list[SaleResult] = []

    def add(self, **kwargs) -> SaleResult:
        """Cost one sale against the remaining exemption budget."""
        kwargs.setdefault("exemption_remaining", self.exemption_remaining)
        result = compute_sale(**kwargs)
        self.exemption_remaining = max(0.0, self.exemption_remaining - result.exemption_used)
        self.sales.append(result)
        return result

    @property
    def total_tax(self) -> float:
        return sum(s.tax for s in self.sales)

    @property
    def total_stt(self) -> float:
        return sum(s.stt for s in self.sales)

    @property
    def total_gain(self) -> float:
        return sum(s.gain for s in self.sales)

    @property
    def total_net_proceeds(self) -> float:
        return sum(s.net_proceeds for s in self.sales)

    @property
    def exemption_used(self) -> float:
        return self.exemption_total - self.exemption_remaining

    def summary(self) -> dict:
        return {
            "n_sales": len(self.sales),
            "total_gain": round(self.total_gain, 2),
            "total_tax": round(self.total_tax, 2),
            "total_stt": round(self.total_stt, 2),
            "total_cost": round(self.total_tax + self.total_stt, 2),
            "net_proceeds": round(self.total_net_proceeds, 2),
            "ltcg_exemption_used": round(self.exemption_used, 2),
            "ltcg_exemption_remaining": round(self.exemption_remaining, 2),
            "disclaimer": DISCLAIMER,
        }


def find_harvest_candidates(
    positions: list, *, min_loss: float = 1000.0
) -> list[dict]:
    """Positions sitting at a loss that could offset realised gains.

    Returns them largest-loss-first. Note the 30-day wash-sale style
    restriction does NOT exist in Indian tax law, so repurchasing
    immediately is permitted — but doing so purely for tax reasons can be
    challenged as a sham transaction, which is flagged in the note.
    """
    out = []
    for p in positions:
        loss = getattr(p, "pnl", 0.0)
        if loss < -abs(min_loss):
            out.append(
                {
                    "symbol": getattr(p, "symbol", "?"),
                    "unrealised_loss": round(loss, 2),
                    "loss_pct": round(getattr(p, "pnl_pct", 0.0), 2),
                    "current_value": round(getattr(p, "current_value", 0.0), 2),
                    "note": (
                        "India has no wash-sale rule, so repurchase is legal. "
                        "Harvest only where the investment case genuinely "
                        "warrants an exit."
                    ),
                }
            )
    return sorted(out, key=lambda d: d["unrealised_loss"])
