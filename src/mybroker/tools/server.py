"""Deterministic tools exposed to the agent as an in-process MCP server.

Every number the agent is permitted to state originates from one of these
functions. They are ordinary Python — no model involved — so their output is
reproducible and auditable.

Each tool returns JSON carrying a `provenance` block. The recommendation
validator later checks numeric claims against the tool-call log, so a value
without provenance is a value the agent may not cite.

Tools are namespaced `mcp__mybroker__<name>` when allowlisted.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from mybroker.config import LTCG_EXEMPTION, MIN_CORRELATION_OVERLAP_DAYS, load_tickers
from mybroker.data.screener_provider import ScreenerProvider
from mybroker.data.yfinance_provider import YFinanceProvider
from mybroker.graphs.clusters import communities, diversification_score, eigenvector_centrality
from mybroker.graphs.correlation import (
    correlation_graph,
    daily_returns,
    mantegna_mst,
    pairwise_correlations,
)
from mybroker.ledger import append_recommendation, load_ledger
from mybroker.portfolio.loader import load_portfolio
from mybroker.portfolio.metrics import snapshot
from mybroker.portfolio.policy import Policy
from mybroker.portfolio.purchase_estimator import load_estimates
from mybroker.portfolio.risk import (
    annualized_volatility_pct,
    max_drawdown,
    portfolio_beta,
    portfolio_return_series,
    portfolio_value_series,
)
from mybroker.scoring import grade_due_recommendations
from mybroker.security.hooks import get_current_run
from mybroker.security.validator import verify_recommendation
from mybroker.tax import DISCLAIMER, TaxYearPlanner, find_harvest_candidates

_provider: YFinanceProvider | None = None
_screener_provider: ScreenerProvider | None = None


def provider() -> YFinanceProvider:
    global _provider
    if _provider is None:
        _provider = YFinanceProvider()
    return _provider


def screener_provider() -> ScreenerProvider:
    global _screener_provider
    if _screener_provider is None:
        _screener_provider = ScreenerProvider()
    return _screener_provider


def _ok(data: Any, source: str, **extra: Any) -> dict:
    """Wrap a payload with provenance in the SDK's content shape."""
    body = {
        "data": data,
        "provenance": {
            "source": source,
            "as_of": datetime.now(UTC).isoformat(),
            "tool_version": "1.0",
            **extra,
        },
    }
    return {"content": [{"type": "text", "text": json.dumps(body, indent=2, default=str)}]}


def _err(message: str) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps({"error": message})}],
        "is_error": True,
    }


# ── Portfolio ────────────────────────────────────────────────────────────────
@tool(
    "get_portfolio_snapshot",
    "Current portfolio: every position with weight, value and P&L, plus sector "
    "and core/satellite breakdowns and concentration statistics. This is the "
    "authoritative source for any statement about what the portfolio contains.",
    {},
)
async def get_portfolio_snapshot(args: dict) -> dict:
    p = load_portfolio()
    s = snapshot(p)
    pc, sc = s.position_concentration, s.sector_concentration

    # Lot data, keyed by symbol — the inputs compute_tax_impact needs.
    lots = {
        pos.symbol: {
            "quantity": pos.quantity,
            "avg_cost": pos.avg_cost,
            "ltp": pos.ltp,
        }
        for pos in p.equity
    }

    return _ok(
        {
            "totals": {
                "invested": round(s.total_invested, 2),
                "current_value": round(s.total_value, 2),
                "pnl": round(s.total_pnl, 2),
                "pnl_pct": round(s.total_pnl_pct, 2),
                "equity_value": round(s.equity_value, 2),
                "mf_value": round(s.mf_value, 2),
                "has_mutual_funds": p.has_mutual_funds,
            },
            # quantity and avg_cost are included so `compute_tax_impact` can
            # actually be called. Without them the tax tool is unusable and the
            # agent can only decline to cost sales.
            "positions": [
                {
                    "symbol": w.key, "name": w.label,
                    "quantity": lots.get(w.key, {}).get("quantity"),
                    "avg_cost": lots.get(w.key, {}).get("avg_cost"),
                    "ltp": lots.get(w.key, {}).get("ltp"),
                    "value": round(w.value, 2), "weight_pct": round(w.weight_pct, 2),
                    "pnl": round(w.pnl, 2), "pnl_pct": round(w.pnl_pct, 2),
                    "sector": w.sector, "tier": w.tier, "bucket": w.bucket,
                }
                for w in s.positions
            ],
            "sectors": [
                {"sector": w.key, "weight_pct": round(w.weight_pct, 2),
                 "value": round(w.value, 2)}
                for w in s.sectors
            ],
            "core_satellite": {
                "core_pct": round(s.core_pct, 2),
                "satellite_pct": round(s.satellite_pct, 2),
            },
            "concentration": {
                "position_hhi": round(pc.hhi, 1),
                "position_verdict": pc.verdict,
                "sector_hhi": round(sc.hhi, 1),
                "sector_verdict": sc.verdict,
                "effective_positions": round(pc.effective_n, 1),
                "n_positions": pc.n_positions,
                "top3_pct": round(pc.top3_pct, 2),
                "interpretation": (
                    "Position-level HHI is correlation-blind: several holdings in "
                    "one sector look like diversification to it. Where sector HHI "
                    "is higher than position HHI, the sector figure is the more "
                    "honest reading of concentration risk."
                ),
            },
            "warnings": s.warnings,
        },
        source="holdings.csv + tickers.yaml",
    )


@tool(
    "check_policy_compliance",
    "Check the portfolio against memory/investment_policy.md. Returns every "
    "breach with severity, the actual vs permitted figure, and the current "
    "glidepath target. Use this rather than judging allocation limits yourself.",
    {},
)
async def check_policy_compliance(args: dict) -> dict:
    s = snapshot(load_portfolio())
    pol = Policy.load()
    summary = pol.compliance_summary(s)
    target, label = pol.current_core_target()

    summary["glidepath"] = {
        "current_target_pct": target,
        "step": label,
        "final_floor_pct": pol.core_min_pct,
    }
    summary["remediation_new_capital_only"] = pol.months_to_close_core_gap(s)
    return _ok(summary, source="investment_policy.md")


# ── Market data ──────────────────────────────────────────────────────────────
@tool(
    "get_quote",
    "Latest market price for one portfolio symbol (use the Zerodha symbol, "
    "e.g. TMCV). Returns price, previous close and day change.",
    {"symbol": str},
)
async def get_quote(args: dict) -> dict:
    symbol = str(args.get("symbol", "")).strip().upper()
    if not symbol:
        return _err("symbol is required")
    try:
        r = provider().get_quote(symbol)
    except KeyError as exc:
        return _err(str(exc))
    if not r.ok:
        return _err(f"No quote for {symbol}: {'; '.join(r.warnings)}")

    q = r.data
    return _ok(
        {
            "symbol": q.symbol, "price": q.price,
            "previous_close": q.previous_close,
            "day_change_pct": round(q.day_change_pct, 2) if q.day_change_pct else None,
            "warnings": r.warnings,
        },
        source=r.provenance.source, ticker=r.provenance.ticker,
        cached=r.provenance.cached, note=r.provenance.note,
    )


@tool(
    "get_fundamentals",
    "Valuation and quality metrics for one symbol: P/E, P/B, ROE, debt/equity, "
    "market cap, 52-week range. Fields the provider could not supply are "
    "reported as missing — absence is never a zero.",
    {"symbol": str},
)
async def get_fundamentals(args: dict) -> dict:
    symbol = str(args.get("symbol", "")).strip().upper()
    if not symbol:
        return _err("symbol is required")
    try:
        r = provider().get_fundamentals(symbol)
    except KeyError as exc:
        return _err(str(exc))
    if not r.ok:
        return _err(f"No fundamentals for {symbol}")

    f = r.data
    return _ok(
        {
            "symbol": f.symbol, "market_cap": f.market_cap,
            "pe_ratio": f.pe_ratio, "pb_ratio": f.pb_ratio, "roe_pct": f.roe,
            "debt_to_equity": f.debt_to_equity, "eps": f.eps,
            "dividend_yield_pct": f.dividend_yield,
            "fifty_two_week_high": f.fifty_two_week_high,
            "fifty_two_week_low": f.fifty_two_week_low,
            "sector_per_provider": f.sector, "industry": f.industry,
            "missing_fields": f.missing_fields(),
            "warnings": r.warnings,
        },
        source=r.provenance.source, ticker=r.provenance.ticker,
        cached=r.provenance.cached,
    )


@tool(
    "get_analyst_consensus",
    "Publicly-sourced EVIDENCE about market sentiment, not a prediction: "
    "analyst mean/high/low price targets, consensus rating, number of "
    "analysts covering the stock, and where price sits versus its own "
    "50-day/200-day moving averages (trend_position — descriptive, not "
    "forecasting). Small/micro caps frequently have zero analyst coverage; "
    "that is reported as an absence, not treated as bearish. Cite this "
    "alongside other evidence — it does not replace judgement about the "
    "underlying business.",
    {"symbol": str},
)
async def get_analyst_consensus(args: dict) -> dict:
    symbol = str(args.get("symbol", "")).strip().upper()
    if not symbol:
        return _err("symbol is required")
    try:
        r = provider().get_analyst_consensus(symbol)
    except KeyError as exc:
        return _err(str(exc))
    if not r.ok:
        return _err(f"No analyst consensus data for {symbol}")

    a = r.data
    return _ok(
        {
            "symbol": a.symbol, "current_price": a.current_price,
            "target_mean_price": a.target_mean_price,
            "target_high_price": a.target_high_price,
            "target_low_price": a.target_low_price,
            "target_median_price": a.target_median_price,
            "target_upside_pct": (
                round(u, 2) if (u := a.target_upside_pct) is not None else None
            ),
            "number_of_analysts": a.number_of_analysts,
            "recommendation_key": a.recommendation_key,
            "recommendation_mean": a.recommendation_mean,
            "fifty_day_average": a.fifty_day_average,
            "two_hundred_day_average": a.two_hundred_day_average,
            "trend_position": a.trend_position,
            "missing_fields": a.missing_fields(),
            "warnings": r.warnings,
        },
        source=r.provenance.source, ticker=r.provenance.ticker,
        cached=r.provenance.cached,
    )


@tool(
    "get_screener_ratios",
    "Supplementary Indian-equity metrics scraped from screener.in: bank "
    "Gross/Net NPA %, 'Financing Margin %' (screener's own P&L-margin "
    "label for lending businesses — NOT the same computation as bank-"
    "reported NIM, kept under screener's own name rather than renamed), "
    "shareholding pattern (promoter/FII/DII/government %), plus a second, "
    "independently-sourced read on P/E, ROE, ROCE and book value for "
    "cross-checking against get_fundamentals. No official API exists for "
    "screener.in — this is a best-effort scrape, cached, and every result "
    "says so in its warnings. Treat disagreement with get_fundamentals as "
    "worth noting, not as proof either source is wrong.",
    {"symbol": str},
)
async def get_screener_ratios(args: dict) -> dict:
    symbol = str(args.get("symbol", "")).strip().upper()
    if not symbol:
        return _err("symbol is required")

    r = screener_provider().get_ratios(symbol)
    if not r.ok:
        return _err(f"No screener.in data for {symbol}: {'; '.join(r.warnings)}")

    d = r.data
    return _ok(
        {
            "symbol": d.symbol, "market_cap_cr": d.market_cap_cr, "pe": d.pe,
            "book_value": d.book_value, "dividend_yield_pct": d.dividend_yield_pct,
            "roce_pct": d.roce_pct, "roe_pct": d.roe_pct,
            "fifty_two_week_high": d.fifty_two_week_high,
            "fifty_two_week_low": d.fifty_two_week_low,
            "quarterly_extras": d.quarterly_extras,
            "annual_extras": d.annual_extras,
            "shareholding_pct": d.shareholding_pct,
            "latest_period": d.period_labels,
            "warnings": r.warnings,
        },
        source=r.provenance.source, ticker=r.provenance.ticker,
        cached=r.provenance.cached,
    )


@tool(
    "get_price_history",
    "Daily closing prices for one symbol. Returns first/last dates, the number "
    "of observations, and period return. Short series are flagged — do not "
    "compute long-window statistics from them.",
    {"symbol": str, "days": int},
)
async def get_price_history(args: dict) -> dict:
    symbol = str(args.get("symbol", "")).strip().upper()
    days = int(args.get("days") or 250)
    if not symbol:
        return _err("symbol is required")
    try:
        r = provider().get_history(symbol, days=days)
    except KeyError as exc:
        return _err(str(exc))
    if not r.ok or not r.data:
        return _err(f"No history for {symbol}")

    series = r.data
    first, last = series[0], series[-1]
    ret = (last["close"] - first["close"]) / first["close"] * 100

    closes = [d["close"] for d in series]
    return _ok(
        {
            "symbol": symbol,
            "observations": len(series),
            "requested_days": days,
            "first_date": first["date"], "last_date": last["date"],
            "first_close": first["close"], "last_close": last["close"],
            "period_return_pct": round(ret, 2),
            "period_high": max(closes), "period_low": min(closes),
            "sufficient_for_correlation": provider().has_sufficient_history(symbol),
            "warnings": r.warnings,
        },
        source=r.provenance.source, ticker=r.provenance.ticker,
        cached=r.provenance.cached,
    )


@tool(
    "get_market_regime",
    "Benchmark index levels and returns (Nifty 50, Nifty 500, Midcap, Bank, "
    "India VIX) to judge whether conditions favour adding risk.",
    {},
)
async def get_market_regime(args: dict) -> dict:
    out, warnings = {}, []
    for name in ("NIFTY50", "NIFTY500", "NIFTYMIDCAP", "NIFTYBANK", "INDIAVIX"):
        try:
            r = provider().get_history(name, days=250)
        except KeyError:
            continue
        if not r.ok or not r.data:
            warnings.append(f"{name}: unavailable")
            continue
        s = r.data
        closes = [d["close"] for d in s]
        last = closes[-1]
        dma200 = sum(closes[-200:]) / len(closes[-200:]) if len(closes) >= 200 else None
        out[name] = {
            "last": round(last, 2),
            "return_1y_pct": round((last - closes[0]) / closes[0] * 100, 2),
            "return_3m_pct": (
                round((last - closes[-63]) / closes[-63] * 100, 2)
                if len(closes) >= 63 else None
            ),
            "dma200": round(dma200, 2) if dma200 else None,
            "above_200dma": (last > dma200) if dma200 else None,
            "pct_from_52w_high": round((last - max(closes)) / max(closes) * 100, 2),
        }
        warnings.extend(r.warnings)

    return _ok({"indices": out, "warnings": warnings}, source="yfinance")


# ── Graph theory & risk (M3) ───────────────────────────────────────────────────
def _correlation_window_days() -> int:
    return load_tickers().get("settings", {}).get("correlation_window_days", 250)


def _fetch_return_series(symbols: list[str], days: int) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Daily returns per symbol, plus warnings for anything skipped.

    Skips (rather than fabricates a result for) any symbol the provider
    itself flags INSUFFICIENT_HISTORY, or that simply fails to fetch —
    both are named in the warnings so an empty slot in the graph is visible,
    not silent.
    """
    returns: dict[str, dict[str, float]] = {}
    warnings: list[str] = []
    for symbol in symbols:
        if not provider().has_sufficient_history(symbol):
            warnings.append(f"{symbol}: excluded — provider flags INSUFFICIENT_HISTORY.")
            continue
        try:
            r = provider().get_history(symbol, days=days)
        except KeyError as exc:
            warnings.append(f"{symbol}: excluded — {exc}")
            continue
        if not r.ok or not r.data:
            warnings.append(f"{symbol}: excluded — no price history returned.")
            continue
        rets = daily_returns(r.data)
        if len(rets) < 2:
            warnings.append(f"{symbol}: excluded — fewer than 2 daily returns available.")
            continue
        returns[symbol] = rets
        warnings.extend(r.warnings)
    return returns, warnings


@tool(
    "compute_correlation_graph",
    "Mantegna minimum spanning tree over the portfolio's daily-return "
    "correlations, plus Louvain communities, eigenvector centrality, and a "
    "0-100 diversification score. This is the true correlation-based risk "
    "structure — it can and does disagree with the sector labels in "
    "get_portfolio_snapshot; where it does, the graph is the more honest "
    "reading of what actually moves together.",
    {},
)
async def compute_correlation_graph(args: dict) -> dict:
    s = snapshot(load_portfolio())
    weights_pct = {w.key: w.weight_pct for w in s.positions if w.sector != "Mutual Funds"}
    symbols = sorted(weights_pct)
    if len(symbols) < 2:
        return _err("Need at least 2 equity positions to compute a correlation graph.")

    days = _correlation_window_days()
    returns, fetch_warnings = _fetch_return_series(symbols, days)
    included = sorted(returns)
    if len(included) < 2:
        return _err(
            "Fewer than 2 symbols had usable return series — cannot build a "
            f"correlation graph. Warnings: {'; '.join(fetch_warnings)}"
        )

    # A tool handler must never crash the agent's loop (same contract as
    # log_recommendation below): an uncaught exception here aborts the call
    # with no actionable feedback and no partial result, even though MST/
    # communities/centrality are independent computations that don't all
    # need to fail together. A missing optional dependency (e.g. networkx's
    # eigenvector_centrality_numpy importing scipy internally) is exactly
    # the kind of environment issue this should degrade gracefully from.
    try:
        pairs, excluded_pairs = pairwise_correlations(returns)
        corr_graph = correlation_graph(pairs, included)
        mst = mantegna_mst(pairs, included)
        comms = communities(corr_graph)
        centrality = eigenvector_centrality(corr_graph)
        div_score = diversification_score(
            comms=comms, weights_pct=weights_pct, mst=mst,
            hhi=s.position_concentration.hhi if s.position_concentration else 0.0,
        )
    except Exception as exc:  # pragma: no cover - defensive backstop
        return _err(
            f"Correlation graph computation failed ({type(exc).__name__}: {exc}). "
            "This is an environment/dependency problem, not a data problem — "
            "report it rather than treating diversification as unmeasured."
        )

    return _ok(
        {
            "symbols_included": included,
            "symbols_excluded": sorted(set(symbols) - set(included)),
            "mst_edges": [
                {"a": a, "b": b, "distance": round(d["weight"], 4),
                 "correlation": round(d["correlation"], 4)}
                for a, b, d in mst.edges(data=True)
            ],
            "communities": [
                {"members": c, "weight_pct": round(sum(weights_pct.get(sym, 0) for sym in c), 2)}
                for c in comms
            ],
            "eigenvector_centrality": centrality,
            "diversification_score": (
                {
                    "overall": div_score.overall,
                    "community_score": div_score.community_score,
                    "concentration_score": div_score.concentration_score,
                    "hhi_score": div_score.hhi_score,
                    "mst_distance_score": div_score.mst_distance_score,
                    "n_communities": div_score.n_communities,
                    "largest_community": div_score.largest_community,
                    "largest_community_weight_pct": div_score.largest_community_weight_pct,
                }
                if div_score else None
            ),
            "warnings": fetch_warnings + excluded_pairs,
        },
        source="yfinance + networkx (Mantegna MST / Louvain)",
        window_days=days,
    )


@tool(
    "compute_risk_metrics",
    "Per-position annualised volatility, per-position and portfolio-level "
    "true maximum drawdown (peak-to-trough over the whole available window, "
    "not just distance below the current high), and portfolio beta versus "
    "the Nifty 50. Portfolio-level figures use TODAY's position weights held "
    "constant across history — an approximation, stated in the response, not "
    "hidden.",
    {},
)
async def compute_risk_metrics(args: dict) -> dict:
    s = snapshot(load_portfolio())
    weights_pct = {w.key: w.weight_pct for w in s.positions if w.sector != "Mutual Funds"}
    symbols = sorted(weights_pct)
    if not symbols:
        return _err("No equity positions to assess.")

    days = _correlation_window_days()
    warnings: list[str] = []

    position_volatility: dict[str, float | None] = {}
    position_drawdown: dict[str, dict | None] = {}
    return_series: dict[str, dict[str, float]] = {}

    for symbol in symbols:
        try:
            r = provider().get_history(symbol, days=days)
        except KeyError as exc:
            warnings.append(f"{symbol}: {exc}")
            continue
        if not r.ok or not r.data:
            warnings.append(f"{symbol}: no price history returned.")
            continue
        warnings.extend(r.warnings)
        rets = daily_returns(r.data)
        return_series[symbol] = rets
        position_volatility[symbol] = (
            round(v, 2) if (v := annualized_volatility_pct(rets)) is not None else None
        )
        dd = max_drawdown(r.data)
        position_drawdown[symbol] = (
            {
                "peak_date": dd.peak_date, "trough_date": dd.trough_date,
                "drawdown_pct": dd.drawdown_pct, "recovered": dd.recovered,
            }
            if dd else None
        )

    # Same defensive contract as compute_correlation_graph above: the network
    # calls above are already guarded per-symbol, but the aggregation math
    # itself (numpy under portfolio_return_series/beta/drawdown) isn't, and a
    # tool handler must never crash the agent's loop over it.
    try:
        port_returns = portfolio_return_series(return_series, weights_pct)
        port_value_series = portfolio_value_series(port_returns)
        port_dd = max_drawdown(port_value_series)

        try:
            bench = provider().get_history("NIFTY50", days=days)
            bench_returns = daily_returns(bench.data) if bench.ok and bench.data else {}
            warnings.extend(bench.warnings)
        except KeyError as exc:
            bench_returns = {}
            warnings.append(f"NIFTY50 benchmark unavailable: {exc}")

        beta_value, n_overlap = (
            portfolio_beta(port_returns, bench_returns) if bench_returns else (None, 0)
        )
    except Exception as exc:  # pragma: no cover - defensive backstop
        return _err(
            f"Risk-metric aggregation failed ({type(exc).__name__}: {exc}). "
            "This is an environment/computation problem, not a data problem — "
            "report it rather than treating risk as unmeasured."
        )
    if beta_value is None:
        warnings.append(
            f"Portfolio beta not computable: only {n_overlap} overlapping days "
            f"with NIFTY50 (need {MIN_CORRELATION_OVERLAP_DAYS})."
            if n_overlap else "Portfolio beta not computable: no benchmark data."
        )

    return _ok(
        {
            "position_volatility_annualized_pct": position_volatility,
            "position_max_drawdown": position_drawdown,
            "portfolio_max_drawdown": (
                {
                    "peak_date": port_dd.peak_date, "trough_date": port_dd.trough_date,
                    "drawdown_pct": port_dd.drawdown_pct, "recovered": port_dd.recovered,
                }
                if port_dd else None
            ),
            "portfolio_beta_vs_nifty50": round(beta_value, 3) if beta_value is not None else None,
            "beta_overlap_days": n_overlap,
            "assumption": (
                "Portfolio-level figures (drawdown, beta) use TODAY's position "
                "weights held constant across the whole historical window — "
                "actual historical weights are unknown."
            ),
            "warnings": warnings,
        },
        source="yfinance (price history) + portfolio/risk.py",
        window_days=days,
    )


@tool(
    "compute_overlap",
    "True look-through exposure combining direct equity with mutual-fund "
    "scheme holdings. Currently BLOCKED on two missing data sources — call "
    "this to get the exact, current reason, not a fabricated result.",
    {},
)
async def compute_overlap(args: dict) -> dict:
    p = load_portfolio()
    if not p.has_mutual_funds:
        return _ok(
            {
                "computable": False,
                "reason": (
                    "No mutual-fund holdings loaded (holdings_mf.csv is absent "
                    "or empty). Even once supplied, true look-through overlap "
                    "additionally needs each scheme's underlying holdings "
                    "disclosure, which no provider in this system fetches yet — "
                    "AMFI's NAV feed gives price, not portfolio composition."
                ),
            },
            source="portfolio/loader.py",
        )
    return _ok(
        {
            "computable": False,
            "reason": (
                "Mutual-fund holdings are loaded, but no scheme-holdings data "
                "provider exists yet to supply what each scheme actually owns. "
                "Overlap cannot be computed from NAV data alone."
            ),
        },
        source="portfolio/loader.py",
    )


# ── Tax ──────────────────────────────────────────────────────────────────────
@tool(
    "compute_tax_impact",
    "Tax cost of selling. Pass a list of sales, each with symbol, quantity, "
    "sale_price, avg_cost and optionally purchase_date (YYYY-MM-DD). If "
    "purchase_date is omitted, a TENTATIVE date from `mybroker estimate-dates` "
    "is used when one exists for that symbol (memory/estimated_purchase_"
    "dates.json) — flagged as estimated in the response, never silently "
    "treated as verified — otherwise the purchase date is unknown and the "
    "sale is costed as SHORT term (the conservative, higher-tax assumption). "
    "All sales are costed against ONE shared ₹1.25L LTCG exemption, because "
    "that exemption is annual — costing sales independently understates the tax.",
    {"sales": list},
)
async def compute_tax_impact(args: dict) -> dict:
    sales = args.get("sales") or []
    if not isinstance(sales, list) or not sales:
        return _err("sales must be a non-empty list")

    estimates = load_estimates()
    planner = TaxYearPlanner()
    rows = []
    for s in sales:
        try:
            symbol = str(s["symbol"]).upper()
            pd_raw = s.get("purchase_date")
            source = "explicit"
            purchase = date.fromisoformat(pd_raw) if pd_raw else None

            if purchase is None:
                est = estimates.get(symbol)
                if est and est.confident and est.estimated_date:
                    purchase = date.fromisoformat(est.estimated_date)
                    source = "estimated"
                else:
                    source = "unknown"

            r = planner.add(
                symbol=symbol,
                quantity=float(s["quantity"]),
                sale_price=float(s["sale_price"]),
                avg_cost=float(s["avg_cost"]),
                purchase_date=purchase,
            )
        except (KeyError, ValueError, TypeError) as exc:
            return _err(f"Bad sale entry {s!r}: {exc}")

        if source == "estimated":
            r.assumptions.append(
                f"purchase_date {purchase.isoformat()} is an ESTIMATE from price "
                f"history vs avg_cost (mybroker estimate-dates), NOT a verified "
                f"contract-note date — see memory/estimated_purchase_dates.md "
                f"for how it was derived. Confirm before filing."
            )

        rows.append(
            {
                "symbol": r.symbol, "quantity": r.quantity,
                "sale_value": round(r.sale_value, 2),
                "gain": round(r.gain, 2), "gain_type": r.gain_type,
                "holding_days": r.holding_days, "days_to_ltcg": r.days_to_ltcg,
                "purchase_date_source": source,  # explicit | estimated | unknown
                "exemption_used": round(r.exemption_used, 2),
                "tax": round(r.tax, 2), "stt": round(r.stt, 2),
                "net_proceeds": round(r.net_proceeds, 2),
                "effective_rate_pct": round(r.effective_rate_pct, 2),
                "worth_waiting_for_ltcg": r.worth_waiting,
                "assumptions": r.assumptions,
            }
        )

    return _ok(
        {"sales": rows, "summary": planner.summary(),
         "ltcg_exemption_per_year": LTCG_EXEMPTION, "disclaimer": DISCLAIMER},
        source="tax.py (FY2025-26 rules)",
    )


@tool(
    "find_tax_loss_harvest_candidates",
    "Positions currently at an unrealised loss that could offset realised "
    "gains. India has no wash-sale rule, so repurchase is permitted.",
    {},
)
async def find_tax_loss_harvest_candidates(args: dict) -> dict:
    p = load_portfolio()
    return _ok(
        {"candidates": find_harvest_candidates(p.equity), "disclaimer": DISCLAIMER},
        source="holdings.csv",
    )


# ── Recommendation ledger (the anti-hallucination gate) ──────────────────────
@tool(
    "log_recommendation",
    "Record ONE recommendation. REQUIRED before it counts as official — a "
    "recommendation only in your prose is not tracked or scored. "
    "\n\n"
    "`evidence` MUST be a list of OBJECTS, never strings — one number per "
    "item, exactly three keys each: {\"tool\": \"get_quote\", \"field\": "
    "\"price\", \"value\": 457.05}. A prose citation like "
    "\"get_quote: price 457.05, day_change 1.2%\" is REJECTED even though "
    "it names a real tool and real numbers — split it into two items, "
    "{tool:get_quote, field:price, value:457.05} and "
    "{tool:get_quote, field:day_change_pct, value:1.2}. "
    "\n\n"
    "Every value is checked against this run's actual tool outputs; a value "
    "that cannot be matched is REJECTED and you must fix or drop it. "
    "SELL/TRIM require `tax_impact` from a prior compute_tax_impact call. "
    "Call this once per recommendation, after you have the real evidence in "
    "hand — not as a first step.",
    {
        "symbol": str,
        "action": str,        # BUY | SELL | TRIM | HOLD | WATCH
        "conviction": str,    # high | medium | low
        "rationale": str,
        "evidence": list,     # [{"tool": str, "field": str, "value": number}, ...]
        "tax_impact": dict,
        "risk_if_wrong": str,
        "invalidation_trigger": str,
    },
)
async def log_recommendation(args: dict) -> dict:
    run_id = get_current_run()
    if not run_id:
        return _err("No active run — log_recommendation must be called during a review run.")

    # A tool handler must never crash the agent's loop — an uncaught exception
    # here would abort the call with no actionable feedback, exactly the
    # opposite of the "reject with guidance, let the agent fix it" contract
    # this tool exists to provide. verify_recommendation() is already
    # exception-safe for malformed evidence; this is the backstop for
    # anything else unanticipated.
    try:
        result = verify_recommendation(args, run_id)
    except Exception as exc:  # pragma: no cover - defensive backstop
        rejection = {
            "accepted": False,
            "problems": [f"internal error validating this recommendation: {exc}"],
            "guidance": "Check the shape of every field against the tool description and retry.",
        }
        return {
            "content": [{"type": "text", "text": json.dumps(rejection, indent=2)}],
            "is_error": True,
        }

    if not result.ok:
        rejection = {
            "accepted": False,
            "problems": result.problems,
            "guidance": (
                "Fix the failing evidence — cite the tool that actually "
                "returned this value, correct the figure to match what the "
                "tool returned, or call the tool again and use its real "
                "output. Do not resubmit the same unverifiable number. If "
                "'malformed' appears above, your evidence items are not "
                "shaped as {tool, field, value} objects — see the tool "
                "description for the exact required shape."
            ),
        }
        return {
            "content": [{"type": "text", "text": json.dumps(rejection, indent=2)}],
            "is_error": True,
        }

    entry = append_recommendation(args, run_id=run_id)
    return _ok(
        {
            "accepted": True,
            "rec_id": entry.rec_id,
            "review_after": entry.review_after,
            "evidence_checked": len(result.evidence_checks),
        },
        source="ledger.py",
    )


@tool(
    "review_recommendation_outcomes",
    "M5 — how past recommendations actually did. Grades every recommendation "
    "whose review_after date has passed and has no outcome yet (fetches a "
    "live quote, compares to price_at_recommendation, no LLM judgement "
    "involved), persists the outcomes, then returns a full performance "
    "summary: newly graded, still-ungradeable-and-why, already-graded "
    "history, and a verdict tally. Safe to call anytime — grading is "
    "idempotent, an entry with an outcome is never re-graded.",
    {},
)
async def review_recommendation_outcomes(args: dict) -> dict:
    newly_graded = grade_due_recommendations()

    all_entries = load_ledger()
    graded_entries = [e for e in all_entries if e.outcome is not None]
    tally: dict[str, int] = {}
    for e in graded_entries:
        v = (e.outcome or {}).get("verdict")
        if v:
            tally[v] = tally.get(v, 0) + 1

    return _ok(
        {
            "newly_graded": [
                {"rec_id": r.rec_id, "symbol": r.symbol, "action": r.action,
                 **(r.outcome or {})}
                for r in newly_graded if r.graded
            ],
            "ungradeable": [
                {"rec_id": r.rec_id, "symbol": r.symbol, "action": r.action,
                 "reason": r.reason}
                for r in newly_graded if not r.graded
            ],
            "graded_history": [
                {"rec_id": e.rec_id, "symbol": e.symbol, "action": e.action,
                 "conviction": e.conviction, **(e.outcome or {})}
                for e in graded_entries
            ],
            "verdict_tally": tally,
            "total_recommendations_ever": len(all_entries),
            # all_entries is read AFTER grade_due_recommendations() persists
            # its outcomes above, so this already reflects the post-grading
            # state — no separate subtraction needed.
            "still_pending_review": len([e for e in all_entries if e.outcome is None]),
        },
        source="ledger.py + scoring.py",
    )


# ── Server ───────────────────────────────────────────────────────────────────
ALL_TOOLS = [
    get_portfolio_snapshot,
    check_policy_compliance,
    get_quote,
    get_fundamentals,
    get_analyst_consensus,
    get_screener_ratios,
    get_price_history,
    get_market_regime,
    # M3 — these three were defined but never wired in until this fix; the
    # orchestrator (and now chat) could never actually call them.
    compute_correlation_graph,
    compute_risk_metrics,
    compute_overlap,
    compute_tax_impact,
    find_tax_loss_harvest_candidates,
    log_recommendation,
    # M5 — grades past recommendations against real outcomes.
    review_recommendation_outcomes,
]

TOOL_NAMES = [f"mcp__mybroker__{t.name}" for t in ALL_TOOLS]


def build_server():
    """Create the in-process MCP server carrying every deterministic tool."""
    return create_sdk_mcp_server(name="mybroker", version="1.0.0", tools=ALL_TOOLS)
