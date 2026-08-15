"""Market data provider interface — the paid-upgrade seam.

Everything downstream (tools, agents, reports) depends only on this interface.
Swapping the free yfinance/AMFI stack for Kite Connect later means writing one
new class here; no agent, tool, or report code changes.

Every response carries `Provenance`. That block is what the recommendation
validator later checks numeric claims against, so a value with no provenance
is a value the agent is not permitted to cite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Provenance:
    """Where a number came from and when. Non-optional by design."""

    source: str                       # e.g. "yfinance", "amfi"
    as_of: str                        # ISO-8601 UTC
    ticker: str | None = None
    cached: bool = False
    note: str | None = None

    @staticmethod
    def now(source: str, **kw: Any) -> Provenance:
        return Provenance(source=source, as_of=datetime.now(UTC).isoformat(), **kw)


@dataclass
class DataResult:
    """A data payload plus its provenance and any caveats.

    `warnings` is deliberately part of the payload rather than logged away —
    a caveat the agent cannot see is a caveat that will not reach the report.
    """

    data: Any
    provenance: Provenance
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.data is not None

    def to_dict(self) -> dict:
        return {
            "data": self.data,
            "provenance": asdict(self.provenance),
            "warnings": self.warnings,
        }


@dataclass
class Quote:
    symbol: str
    price: float
    previous_close: float | None = None
    day_change_pct: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: int | None = None
    currency: str = "INR"


@dataclass
class Fundamentals:
    symbol: str
    market_cap: float | None = None
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    roe: float | None = None
    debt_to_equity: float | None = None
    dividend_yield: float | None = None
    eps: float | None = None
    book_value: float | None = None
    sector: str | None = None
    industry: str | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None

    def missing_fields(self) -> list[str]:
        """Fields the provider could not supply — surfaced, not hidden."""
        return [k for k, v in asdict(self).items() if v is None and k != "symbol"]


@dataclass
class AnalystConsensus:
    """Publicly-sourced evidence about market sentiment — analyst price
    targets/ratings, and where price sits versus its own 50/200-day trend.

    This is EVIDENCE, not a signal: no field here is a buy/sell verdict, and
    `trend_position` describes where the price sits, not where it is
    forecast to go. The agent reasons from these facts; nothing here reasons
    for it. Small/micro caps frequently have zero analyst coverage — that is
    reported as None, never estimated or interpolated.
    """

    symbol: str
    current_price: float | None = None
    target_mean_price: float | None = None
    target_high_price: float | None = None
    target_low_price: float | None = None
    target_median_price: float | None = None
    number_of_analysts: int | None = None
    recommendation_key: str | None = None      # e.g. "strong_buy", "hold", "sell", "none"
    recommendation_mean: float | None = None   # 1.0 = strong buy .. 5.0 = strong sell
    fifty_day_average: float | None = None
    two_hundred_day_average: float | None = None

    @property
    def target_upside_pct(self) -> float | None:
        """% gap between current price and analyst mean target. Not a
        forecast of what will happen — a statement of what analysts, on
        average, currently say the stock is worth."""
        if not self.target_mean_price or not self.current_price:
            return None
        return (self.target_mean_price - self.current_price) / self.current_price * 100

    @property
    def trend_position(self) -> str | None:
        """Where price sits versus its own 50/200-DMA — descriptive, not
        predictive. A stock above both DMAs is in an established uptrend by
        this one definition; that says nothing about tomorrow."""
        if None in (self.current_price, self.fifty_day_average, self.two_hundred_day_average):
            return None
        above_50 = self.current_price > self.fifty_day_average
        above_200 = self.current_price > self.two_hundred_day_average
        if above_50 and above_200:
            return "above both 50-DMA and 200-DMA"
        if not above_50 and not above_200:
            return "below both 50-DMA and 200-DMA"
        return "mixed — above one DMA, below the other"

    def missing_fields(self) -> list[str]:
        return [k for k, v in asdict(self).items() if v is None and k != "symbol"]


class MarketDataProvider(ABC):
    """The contract every data source must satisfy."""

    name: str = "abstract"

    @abstractmethod
    def get_quote(self, symbol: str) -> DataResult:
        """Latest price for one portfolio symbol."""

    @abstractmethod
    def get_history(self, symbol: str, *, days: int = 250) -> DataResult:
        """Daily closes. `data` is a list of {date, close} dicts."""

    @abstractmethod
    def get_fundamentals(self, symbol: str) -> DataResult:
        """Valuation and quality metrics."""

    def close(self) -> None:  # pragma: no cover - optional hook
        """Release any held resources."""
        return None
