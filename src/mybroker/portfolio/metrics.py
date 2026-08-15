"""Deterministic portfolio arithmetic.

Every number the agent is ever allowed to state about the portfolio originates
here. Nothing in this module calls an LLM, and nothing in this module is
approximate — if a value cannot be computed exactly it is returned as None and
flagged, never estimated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mybroker.portfolio.loader import Portfolio


@dataclass
class Weight:
    """A single position's share of the portfolio."""

    key: str
    label: str
    value: float
    weight_pct: float
    sector: str = ""
    tier: str = ""
    bucket: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class Concentration:
    """Concentration diagnostics for one grouping (position, sector, bucket)."""

    hhi: float                    # Herfindahl-Hirschman Index, 0-10000
    top1_pct: float
    top3_pct: float
    top5_pct: float
    effective_n: float            # 1/sum(w^2): how many *equally sized* holdings
                                  # this portfolio behaves like
    n_positions: int

    @property
    def verdict(self) -> str:
        """Plain-language reading of the HHI, using the standard thresholds."""
        if self.hhi >= 2500:
            return "highly concentrated"
        if self.hhi >= 1500:
            return "moderately concentrated"
        return "diversified"


@dataclass
class PortfolioSnapshot:
    """Everything computable about the portfolio without market data."""

    total_invested: float
    total_value: float
    total_pnl: float
    total_pnl_pct: float

    equity_value: float
    mf_value: float

    positions: list[Weight] = field(default_factory=list)
    sectors: list[Weight] = field(default_factory=list)
    buckets: list[Weight] = field(default_factory=list)
    tiers: list[Weight] = field(default_factory=list)

    position_concentration: Concentration | None = None
    sector_concentration: Concentration | None = None

    core_pct: float = 0.0
    satellite_pct: float = 0.0

    warnings: list[str] = field(default_factory=list)


def _concentration(values: list[float], total: float) -> Concentration:
    """Compute HHI and top-N shares from a list of position values."""
    if total <= 0 or not values:
        return Concentration(0.0, 0.0, 0.0, 0.0, 0.0, 0)

    shares = sorted((v / total for v in values), reverse=True)
    hhi = sum((s * 100) ** 2 for s in shares)
    sum_sq = sum(s * s for s in shares)

    def top(n: int) -> float:
        return sum(shares[:n]) * 100

    return Concentration(
        hhi=hhi,
        top1_pct=top(1),
        top3_pct=top(3),
        top5_pct=top(5),
        effective_n=(1 / sum_sq) if sum_sq else 0.0,
        n_positions=len(values),
    )


def _group(
    pairs: list[tuple[str, float]], total: float
) -> list[Weight]:
    """Aggregate (label, value) pairs into sorted weights."""
    agg: dict[str, float] = {}
    for label, value in pairs:
        agg[label] = agg.get(label, 0.0) + value

    out = [
        Weight(
            key=label,
            label=label,
            value=value,
            weight_pct=(value / total * 100) if total else 0.0,
        )
        for label, value in agg.items()
    ]
    return sorted(out, key=lambda w: w.weight_pct, reverse=True)


def snapshot(portfolio: Portfolio) -> PortfolioSnapshot:
    """Compute the full deterministic snapshot.

    Mutual funds are treated as a single 'Mutual Funds' sector and always
    classified `core` — they are diversified vehicles by construction, so
    attributing them to a single sector would misstate concentration.
    """
    total = portfolio.total_value
    warnings = list(portfolio.warnings)

    positions: list[Weight] = []
    sector_pairs: list[tuple[str, float]] = []
    bucket_pairs: list[tuple[str, float]] = []
    tier_pairs: list[tuple[str, float]] = []

    for p in portfolio.equity:
        positions.append(
            Weight(
                key=p.symbol,
                label=p.name or p.symbol,
                value=p.current_value,
                weight_pct=(p.current_value / total * 100) if total else 0.0,
                sector=p.sector,
                tier=p.tier,
                bucket=p.bucket,
                pnl=p.pnl,
                pnl_pct=p.pnl_pct,
            )
        )
        sector_pairs.append((p.sector, p.current_value))
        bucket_pairs.append((p.bucket, p.current_value))
        tier_pairs.append((p.tier, p.current_value))

    for m in portfolio.mutual_funds:
        positions.append(
            Weight(
                key=m.amfi_code or m.scheme_name,
                label=m.scheme_name,
                value=m.current_value,
                weight_pct=(m.current_value / total * 100) if total else 0.0,
                sector="Mutual Funds",
                tier="diversified",
                bucket="core",
                pnl=m.pnl,
                pnl_pct=m.pnl_pct,
            )
        )
        sector_pairs.append(("Mutual Funds", m.current_value))
        bucket_pairs.append(("core", m.current_value))
        tier_pairs.append(("diversified", m.current_value))

    positions.sort(key=lambda w: w.weight_pct, reverse=True)

    buckets = _group(bucket_pairs, total)
    core_pct = next((b.weight_pct for b in buckets if b.key == "core"), 0.0)
    satellite_pct = next((b.weight_pct for b in buckets if b.key == "satellite"), 0.0)

    values = [w.value for w in positions]
    sector_values = [w.value for w in _group(sector_pairs, total)]

    return PortfolioSnapshot(
        total_invested=portfolio.total_invested,
        total_value=total,
        total_pnl=portfolio.total_pnl,
        total_pnl_pct=portfolio.total_pnl_pct,
        equity_value=portfolio.equity_value,
        mf_value=portfolio.mf_value,
        positions=positions,
        sectors=_group(sector_pairs, total),
        buckets=buckets,
        tiers=_group(tier_pairs, total),
        position_concentration=_concentration(values, total),
        sector_concentration=_concentration(sector_values, total),
        core_pct=core_pct,
        satellite_pct=satellite_pct,
        warnings=warnings,
    )
