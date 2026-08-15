"""Volatility, drawdown, and portfolio beta — deterministic, from price
history only. Nothing here is a model output; every figure is arithmetic on
values `get_price_history` / `get_market_regime` already returned.

One assumption threads through the portfolio-level functions and is
surfaced in every result that depends on it, never hidden: portfolio beta
and portfolio-level drawdown are computed using CURRENT position weights
held constant across the whole historical window. Actual historical weights
(what you held a year ago) are not known — only today's holdings.csv is.
This is the standard simplification for a quick beta estimate from a
point-in-time portfolio; treat it as "the beta of a portfolio shaped like
today's, over the last N months", not a true historical return series.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from mybroker.config import MIN_CORRELATION_OVERLAP_DAYS


def annualized_volatility_pct(returns: dict[str, float]) -> float | None:
    """Annualised stdev of daily returns, as a percentage.

    252 trading days/year is the standard NSE-equivalent convention. Returns
    None (not 0.0) when there's too little data to trust — a volatility of
    literally zero would read as "this is unusually stable," the opposite
    of "there isn't enough data to say."
    """
    values = list(returns.values())
    if len(values) < 2:
        return None
    return float(np.std(values, ddof=1) * math.sqrt(252) * 100)


@dataclass
class Drawdown:
    peak_date: str
    peak_close: float
    trough_date: str
    trough_close: float
    drawdown_pct: float   # negative or zero
    recovered: bool        # has the series since exceeded the prior peak?
    n_observations: int


def max_drawdown(series: list[dict]) -> Drawdown | None:
    """True peak-to-trough maximum drawdown over the WHOLE series — distinct
    from "distance below the period high", which only sees the single
    highest point, not the worst subsequent decline from any earlier peak.
    """
    if not series:
        return None
    rows = sorted(series, key=lambda r: r["date"])

    peak = rows[0]
    worst_dd = 0.0
    worst_peak, worst_trough = rows[0], rows[0]

    for row in rows:
        if row["close"] > peak["close"]:
            peak = row
        dd = (row["close"] - peak["close"]) / peak["close"] * 100
        if dd < worst_dd:
            worst_dd = dd
            worst_peak, worst_trough = peak, row

    last_close = rows[-1]["close"]
    return Drawdown(
        peak_date=worst_peak["date"], peak_close=worst_peak["close"],
        trough_date=worst_trough["date"], trough_close=worst_trough["close"],
        drawdown_pct=round(worst_dd, 2),
        recovered=last_close >= worst_peak["close"],
        n_observations=len(rows),
    )


def portfolio_return_series(
    position_returns: dict[str, dict[str, float]],
    weights_pct: dict[str, float],
) -> dict[str, float]:
    """Weighted-average daily portfolio return per date.

    Renormalises over whichever symbols actually have a return on a given
    date, rather than requiring every symbol present every day — a single
    short-history symbol (TMCV/TMPV) would otherwise truncate the whole
    portfolio series to its own start date.
    """
    all_dates: set[str] = set()
    for rets in position_returns.values():
        all_dates |= set(rets)

    out: dict[str, float] = {}
    for dt in sorted(all_dates):
        numerator = 0.0
        weight_present = 0.0
        for symbol, rets in position_returns.items():
            if dt in rets:
                w = weights_pct.get(symbol, 0.0)
                numerator += w * rets[dt]
                weight_present += w
        if weight_present > 0:
            out[dt] = numerator / weight_present
    return out


def portfolio_value_series(portfolio_returns: dict[str, float]) -> list[dict]:
    """Synthetic NAV series (base 100) from a daily return series — lets
    `max_drawdown` run on the portfolio as a whole, the same way it runs on
    one symbol."""
    dates = sorted(portfolio_returns)
    value = 100.0
    out: list[dict] = []
    for i, dt in enumerate(dates):
        if i > 0:
            value *= 1 + portfolio_returns[dt]
        out.append({"date": dt, "close": value})
    return out


def portfolio_beta(
    portfolio_returns: dict[str, float],
    benchmark_returns: dict[str, float],
    *,
    min_overlap: int = MIN_CORRELATION_OVERLAP_DAYS,
) -> tuple[float | None, int]:
    """beta = cov(portfolio, benchmark) / var(benchmark), over their shared
    dates. Returns (None, n_overlap) rather than a number when there isn't
    enough shared history to trust it, or when the benchmark itself shows no
    variance (a divide-by-zero that would otherwise silently become inf/nan).
    """
    common = sorted(set(portfolio_returns) & set(benchmark_returns))
    if len(common) < min_overlap:
        return None, len(common)

    p = np.array([portfolio_returns[d] for d in common])
    b = np.array([benchmark_returns[d] for d in common])
    var_b = float(np.var(b, ddof=1))
    if var_b == 0:
        return None, len(common)

    cov = float(np.cov(p, b, ddof=1)[0, 1])
    return cov / var_b, len(common)
