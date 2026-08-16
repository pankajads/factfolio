"""Standalone MCP server exposing factfolio to external clients — VS Code's
Claude extension, Claude Desktop, or any other MCP-aware tool or agent.

This is distinct from `tools/server.py`, which is an in-process MCP server
used only by this project's own agents *during* a review run (never seen
outside this codebase). This one runs as its own stdio process — the
standard local-tool transport MCP clients expect — and is what
`factfolio mcp` starts. Point a client at
`factfolio mcp` (or `uvx factfolio mcp`) the same way you'd point it at any
other local MCP server.

Every tool here wraps the identical engine the CLI uses — same numbers,
same gate, same ledger — just returning structured data instead of
formatted terminal output.
"""

from __future__ import annotations

import contextlib
import io
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("factfolio")


@mcp.tool()
def portfolio_status() -> dict[str, Any]:
    """Deterministic portfolio snapshot: invested/current value, P&L,
    core/satellite allocation, top positions, sector breakdown, policy
    breaches, and concentration (HHI). No LLM call, no news/analysis data —
    only holdings math and whatever price data is already available."""
    from mybroker.portfolio.loader import load_portfolio
    from mybroker.portfolio.metrics import snapshot
    from mybroker.portfolio.policy import Policy

    portfolio = load_portfolio()
    snap = snapshot(portfolio)
    pol = Policy.load()
    breaches = pol.check(snap)
    target, step = pol.current_core_target()

    return {
        "invested": snap.total_invested,
        "current_value": snap.total_value,
        "pnl": snap.total_pnl,
        "pnl_pct": snap.total_pnl_pct,
        "core_pct": snap.core_pct,
        "satellite_pct": snap.satellite_pct,
        "core_target_now_pct": target,
        "core_target_step": step,
        "positions": [
            {
                "symbol": w.key,
                "weight_pct": w.weight_pct,
                "value": w.value,
                "pnl_pct": w.pnl_pct,
                "sector": w.sector,
            }
            for w in snap.positions
        ],
        "sectors": [
            {"sector": w.key, "weight_pct": w.weight_pct} for w in snap.sectors
        ],
        "policy_breaches": [
            {
                "severity": b.severity,
                "subject": b.subject,
                "actual_pct": b.actual,
                "limit_pct": b.limit,
            }
            for b in breaches
        ],
        "position_concentration_hhi": snap.position_concentration.hhi,
        "sector_concentration_hhi": snap.sector_concentration.hhi,
        "warnings": snap.warnings,
    }


@mcp.tool()
def validate_tickers() -> dict[str, Any]:
    """Re-resolve every portfolio symbol to a working ticker. This is the
    gate that must pass — call it and check `ok` — before
    run_portfolio_review, which will otherwise be working from stale or
    missing price data for anything unresolved."""
    from mybroker import tickers_validate

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = tickers_validate.main()
    return {"ok": exit_code == 0, "output": buf.getvalue()}


@mcp.tool()
async def run_portfolio_review() -> dict[str, Any]:
    """Full multi-agent portfolio review: market/fundamentals/tax/risk
    analysis, adversarially reviewed, gated so every number traces to a
    real tool call. Calls the LLM — takes a few minutes and costs money.
    Run validate_tickers first. Returns the full markdown report plus every
    BUY/SELL/TRIM/HOLD/WATCH recommendation with its rationale, evidence,
    and conviction — the same recommendations `factfolio report` writes to
    reports/ and the ledger."""
    from mybroker.agents.orchestrator import run_review
    from mybroker.ledger import recommendations_for_run

    result = await run_review()
    recs = recommendations_for_run(result.run_id)

    return {
        "run_id": result.run_id,
        "report": result.report,
        "recommendations": [
            {
                "symbol": r.symbol,
                "action": r.action,
                "conviction": r.conviction,
                "rationale": r.rationale,
                "evidence": r.evidence,
                "risk_if_wrong": r.risk_if_wrong,
                "invalidation_trigger": r.invalidation_trigger,
                "tax_impact": r.tax_impact,
            }
            for r in recs
        ],
        "tool_calls": len(result.tool_calls),
        "duration_s": result.duration_s,
        "cost_usd": result.cost_usd,
    }


def run() -> int:
    """Entry point for `factfolio mcp`. stdio transport — the standard way
    a local MCP server talks to its client (VS Code, Claude Desktop,
    a custom agent harness); nothing to configure, no port to pick."""
    mcp.run()
    return 0
