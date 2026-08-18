"""Command-line interface.

Every interface (CLI, chat, cron, MCP server) calls the same engine, so there
is one implementation of the analysis and several ways to reach it — all of
them plain terminal I/O or structured data, no GUI.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

import anyio
from rich.console import Console
from rich.table import Table

from mybroker.config import REPORTS_DIR, ensure_dirs
from mybroker.portfolio.loader import load_portfolio
from mybroker.portfolio.metrics import snapshot
from mybroker.portfolio.policy import Policy

# stdout: the actual thing someone ran a command to see (tables, reports).
# stderr: chrome around that -- auth status, progress, "next steps" menus.
# Piping `factfolio status > snapshot.txt` should capture the snapshot, not
# also the "Running review..." line -- same split the old print(..., file=
# sys.stderr) calls already made, just through Rich now for the parts that
# render as tables or need a live-updating status line.
console = Console()
err_console = Console(stderr=True)

_ACTION_STYLE = {"BUY": "green", "SELL": "red", "TRIM": "yellow", "HOLD": "cyan", "WATCH": "dim"}

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
RED, YELLOW, GREEN = "\033[31m", "\033[33m", "\033[32m"

# Risk-tier presets for the `factfolio init` policy interview — everything
# in Policy that isn't a direct answer to one of its 4 questions (target
# CAGR, horizon, monthly capital come straight from the answers; risk
# appetite picks one of these bundles for the rest). Same defaults the
# generic template already shipped are "moderate" here, so skipping the
# interview and picking "moderate" land on the same numbers.
_RISK_PRESETS: dict[str, dict[str, float | int]] = {
    "conservative": dict(
        core_min_pct=70.0, core_max_pct=80.0,
        max_position_pct=6.0, max_satellite_position_pct=4.0, max_sector_pct=20.0,
        speculative_cap_pct=5.0, min_positions=10, max_positions=20,
        max_annual_turnover_pct=15.0,
    ),
    "moderate": dict(
        core_min_pct=60.0, core_max_pct=70.0,
        max_position_pct=8.0, max_satellite_position_pct=5.0, max_sector_pct=25.0,
        speculative_cap_pct=10.0, min_positions=8, max_positions=25,
        max_annual_turnover_pct=25.0,
    ),
    "aggressive": dict(
        core_min_pct=40.0, core_max_pct=55.0,
        max_position_pct=10.0, max_satellite_position_pct=7.0, max_sector_pct=30.0,
        speculative_cap_pct=15.0, min_positions=6, max_positions=30,
        max_annual_turnover_pct=35.0,
    ),
}


def _ask(prompt: str, default: str) -> str:
    """One interview question — Enter accepts the bracketed default."""
    return input(f"{prompt} [{default}]: ").strip() or default


def _ask_float(prompt: str, default: float) -> float:
    raw = _ask(prompt, str(default))
    try:
        return float(raw)
    except ValueError:
        print(f"  {DIM}Not a number — using {default}.{RESET}")
        return default


def _ask_choice(prompt: str, choices: tuple[str, ...], default: str) -> str:
    raw = _ask(f"{prompt} ({'/'.join(choices)})", default).strip().lower()
    if raw not in choices:
        print(f"  {DIM}Not one of {', '.join(choices)} — using {default}.{RESET}")
        return default
    return raw


def _run_policy_interview() -> dict[str, Any] | None:
    """Four questions to turn the starter policy into one that actually
    reflects what you told it, instead of pure placeholders. Only ever
    called for a brand-new policy file in an interactive terminal — see
    cmd_init. Returns None on Ctrl-C/EOF, falling back to the generic
    template rather than crashing init.
    """
    print(f"\n{BOLD}Let's set your investment policy{RESET} — 4 quick questions.")
    print(f"{DIM}Press Enter to accept the default shown in [brackets]. You can "
          f"always hand-edit memory/investment_policy.md later too.{RESET}\n")
    try:
        target_cagr = _ask_float(
            "1. Target annual return (CAGR %)? Nifty's long-run average is ~12-13%.",
            12.0,
        )
        risk = _ask_choice(
            "2. Risk appetite —", ("conservative", "moderate", "aggressive"), "moderate"
        )
        horizon = _ask_float("3. Investment horizon, in years?", 10.0)
        monthly_capital = _ask_float(
            "4. New capital you can invest monthly, in ₹ (0 if none)?", 0.0
        )
    except (EOFError, KeyboardInterrupt):
        print(f"\n{DIM}Skipped — using the generic starter instead.{RESET}")
        return None

    print()
    return {
        "target_cagr_pct": target_cagr,
        "risk": risk,
        "horizon_years": horizon,
        "monthly_capital": monthly_capital,
        **_RISK_PRESETS[risk],
    }


def _render_policy_template(answers: dict[str, Any] | None) -> str:
    """The starter investment_policy.md — generic placeholders by default,
    or filled in from a completed _run_policy_interview()."""
    if answers is None:
        objective = (
            'State your target CAGR and horizon here, and why: e.g. "13% CAGR '
            "over 10 years — roughly the Nifty's long-run average, prioritising "
            'low drawdown over outperformance."'
        )
        structure = (
            "Describe what belongs in each bucket for you (index funds/large-caps "
            "vs. midcap/smallcap/thematic bets), and your current split."
        )
        limits_note = (
            "Describe your position/sector/speculative caps and why — the numbers "
            "below are generic starting points, not a recommendation."
        )
        yaml_comment = "# Generic starting values — replace every one of these with your own."
        vals: dict[str, Any] = dict(
            target_cagr_pct=12.0, core_min_pct=60.0, core_max_pct=70.0,
            max_position_pct=8.0, max_satellite_position_pct=5.0, max_sector_pct=25.0,
            speculative_cap_pct=10.0, min_positions=8, max_positions=25,
            monthly_capital=0.0, max_annual_turnover_pct=25.0,
        )
        changelog = "Generated by `factfolio init`. Not yet customised or approved."
    else:
        risk = answers["risk"]
        objective = (
            f"{answers['target_cagr_pct']:.1f}% CAGR over {answers['horizon_years']:.0f} "
            f"years, {risk} risk appetite — from the `factfolio init` interview. Edit "
            "this prose to say *why*, not just what."
        )
        structure = (
            f"{risk.capitalize()} preset: {answers['core_min_pct']:.0f}–"
            f"{answers['core_max_pct']:.0f}% core (index funds/large-caps), the rest "
            "satellite (midcap/smallcap/thematic bets). Describe your actual split "
            "and what belongs in each bucket for you."
        )
        limits_note = (
            f"{risk.capitalize()}-preset starting points from the interview — still "
            "generic to your risk tier, not tailored to your specific holdings. "
            "Describe why these are (or aren't) right for you."
        )
        yaml_comment = (
            f"# From the `factfolio init` interview — {risk} risk preset. Still\n"
            "# generic to the risk tier, not your specific holdings — review."
        )
        vals = {k: answers[k] for k in (
            "target_cagr_pct", "core_min_pct", "core_max_pct", "max_position_pct",
            "max_satellite_position_pct", "max_sector_pct", "speculative_cap_pct",
            "min_positions", "max_positions", "max_annual_turnover_pct",
        )}
        vals["monthly_capital"] = answers["monthly_capital"]
        changelog = (
            f"Generated by `factfolio init` — {risk} risk, "
            f"{answers['target_cagr_pct']:.1f}% CAGR target. Not yet approved."
        )

    return f"""\
# Investment Policy

**Status:** DRAFT — edit every number below before relying on this.

This is the standard every recommendation is measured against. The agent
does not decide whether a position is too large; this file decides, and
the agent reasons about what to do next. Change the numbers here and the
agent's behaviour changes with them.

The fenced `yaml` block is parsed by the code. The prose around it should
explain *why* each number is what it is, so the two never drift apart —
edit both together.

## Objective

{objective}

## Structure: core–satellite

{structure}

## Position limits

{limits_note}

## Rebalancing approach

New capital first, or trim-to-cap, or something else — and your rule for
when a sale is actually justified given its tax cost.

---

```yaml
# ── Machine-enforceable limits. Parsed by portfolio/policy.py ──
{yaml_comment}
target_cagr_pct: {vals['target_cagr_pct']:.1f}

core_min_pct: {vals['core_min_pct']:.1f}
core_max_pct: {vals['core_max_pct']:.1f}

# Optional dated glidepath — delete this block entirely to use core_min_pct
# as a flat target from day one instead of a phased-in one.
# glidepath_start: YYYY-MM-DD
# glidepath:
#   - {{months: 6,  core_pct: 30.0}}
#   - {{months: 12, core_pct: 45.0}}

max_position_pct: {vals['max_position_pct']:.1f}                 # any single core holding
max_satellite_position_pct: {vals['max_satellite_position_pct']:.1f}       # any single satellite holding
max_sector_pct: {vals['max_sector_pct']:.1f}                  # any one sector

speculative_cap_pct: {vals['speculative_cap_pct']:.1f}
speculative_symbols: []               # e.g. [IDEA, YESBANK] — binary-outcome names

min_positions: {vals['min_positions']}
max_positions: {vals['max_positions']}

monthly_capital: {vals['monthly_capital']:.0f}                    # new capital available per month, in ₹
max_annual_turnover_pct: {vals['max_annual_turnover_pct']:.1f}
```

---

## Change log

| Date | Change |
|---|---|
| {date.today().isoformat()} | {changelog} |
"""

SEVERITY_STYLE = {"critical": "red", "high": "red", "medium": "yellow", "low": "dim"}


def _auth_status_line() -> str:
    """Which credential the Claude Agent SDK will actually use.

    Neither `agents/orchestrator.py` nor `agents/chat.py` sets an API key —
    the subprocess just inherits this process's environment, so the
    resolution is entirely the `claude` CLI's own: ANTHROPIC_API_KEY, if
    set, wins; otherwise it falls back to a local `claude login` session.
    Local login is therefore the default with zero configuration, and
    exporting the env var is the override — never the other way around.
    """
    import os

    if os.environ.get("ANTHROPIC_API_KEY"):
        return f"{DIM}auth: ANTHROPIC_API_KEY (env var override){RESET}"
    return f"{DIM}auth: local `claude login` session (no ANTHROPIC_API_KEY set){RESET}"


def cmd_status(_args) -> int:
    """Deterministic portfolio status. No LLM, no network, instant."""
    snap = snapshot(load_portfolio())
    pol = Policy.load()
    target, step = pol.current_core_target()

    pnl_style = "green" if snap.total_pnl >= 0 else "red"
    console.print()
    console.print("[bold]Portfolio[/bold] [dim](from holdings.csv)[/dim]")
    console.print(f"  Invested       ₹{snap.total_invested:>14,.2f}")
    console.print(f"  Current        ₹{snap.total_value:>14,.2f}")
    console.print(
        f"  P&L            [{pnl_style}]₹{snap.total_pnl:>14,.2f}  "
        f"({snap.total_pnl_pct:+.2f}%)[/{pnl_style}]"
    )

    console.print()
    console.print("[bold]Allocation[/bold]")
    console.print(
        f"  Core           {snap.core_pct:>6.1f}%   "
        f"[dim]target now {target:.0f}% ({step}); final {pol.core_min_pct:.0f}%[/dim]"
    )
    console.print(f"  Satellite      {snap.satellite_pct:>6.1f}%")

    console.print()
    positions = Table(title="Top positions", header_style="bold", show_edge=False)
    positions.add_column("Symbol")
    positions.add_column("Weight", justify="right")
    positions.add_column("Value", justify="right")
    positions.add_column("P&L", justify="right")
    positions.add_column("Sector", style="dim")
    for w in snap.positions[:8]:
        style = "green" if w.pnl >= 0 else "red"
        positions.add_row(
            w.key, f"{w.weight_pct:.1f}%", f"₹{w.value:,.0f}",
            f"[{style}]{w.pnl_pct:+.1f}%[/{style}]", w.sector,
        )
    console.print(positions)

    console.print()
    sectors = Table(title="Sectors", header_style="bold", show_edge=False)
    sectors.add_column("Sector")
    sectors.add_column("Weight", justify="right")
    sectors.add_column("")
    for w in snap.sectors[:6]:
        over = w.weight_pct > pol.max_sector_pct
        flag = f"[red]← over {pol.max_sector_pct:.0f}% cap[/red]" if over else ""
        sectors.add_row(w.key, f"{w.weight_pct:.1f}%", flag)
    console.print(sectors)

    breaches = pol.check(snap)
    console.print()
    console.print(f"[bold]Policy[/bold] [dim]({len(breaches)} breaches)[/dim]")
    if breaches:
        policy_table = Table(header_style="bold", show_edge=False)
        policy_table.add_column("Severity")
        policy_table.add_column("Subject")
        policy_table.add_column("Actual", justify="right")
        policy_table.add_column("Limit", justify="right")
        for b in breaches[:10]:
            style = SEVERITY_STYLE.get(b.severity, "")
            policy_table.add_row(
                f"[{style}]{b.severity}[/{style}]" if style else b.severity,
                b.subject, f"{b.actual:.1f}%", f"{b.limit:.0f}%",
            )
        console.print(policy_table)
        if len(breaches) > 10:
            console.print(f"  [dim]… and {len(breaches) - 10} more[/dim]")

    pc, sc = snap.position_concentration, snap.sector_concentration
    console.print()
    console.print("[bold]Concentration[/bold]")
    console.print(f"  Position HHI   {pc.hhi:>6.0f}  ({pc.verdict})")
    console.print(f"  Sector HHI     {sc.hhi:>6.0f}  ({sc.verdict})")
    if sc.hhi > pc.hhi:
        console.print(
            "  [dim]Sector HHI exceeds position HHI — position-level HHI is[/dim]"
        )
        console.print(
            "  [dim]correlation-blind, so the sector reading is the honest one.[/dim]"
        )

    for w in snap.warnings:
        console.print(f"\n  [yellow]![/yellow] {w}")
    console.print()
    return 0


def _render_recommendations(recs: list) -> None:
    """One row per recommendation actually logged this run — the real
    BUY/SELL/HOLD decision and why, not a wall of markdown to scroll
    through to find it. The full report (with everything else the agents
    found) is still written to reports/ either way."""
    if not recs:
        console.print("[dim]No recommendations were logged this run.[/dim]")
        return

    table = Table(title="Recommendations", header_style="bold", show_lines=True)
    table.add_column("Symbol", style="bold", no_wrap=True)
    table.add_column("Action", no_wrap=True)
    table.add_column("Conviction", no_wrap=True)
    table.add_column("Rationale", max_width=56)
    table.add_column("Key evidence", max_width=34, style="dim")

    for r in recs:
        style = _ACTION_STYLE.get(r.action, "")
        action = f"[{style}]{r.action}[/{style}]" if style else r.action
        evidence = "\n".join(
            f"{e.get('tool', '?')}.{e.get('field', '?')}={e.get('value', '?')}"
            for e in (r.evidence or [])[:3]
        )
        table.add_row(r.symbol, action, r.conviction, r.rationale, evidence)

    console.print(table)


def cmd_report(args) -> int:
    """Full agent-generated review, written to reports/YYYY-MM-DD.md."""
    from mybroker.agents.orchestrator import run_review
    from mybroker.ledger import recommendations_for_run

    ensure_dirs()
    err_console.print(_auth_status_line())

    # A multi-minute silent wait reads as "did this hang?" — the live status
    # line (driven by run_review's on_event callback, one update per tool
    # call / agent dispatch / text chunk) is the difference between that and
    # visible, ongoing progress, the same idea as Claude Code's own spinner.
    with err_console.status("[dim]starting review…[/dim]", spinner="dots") as status:
        def on_event(text: str) -> None:
            status.update(f"[dim]{text}[/dim]")

        result = anyio.run(run_review, None, on_event)

    if not result.report:
        err_console.print("[red]No report produced.[/red]")
        return 1

    out = REPORTS_DIR / f"{date.today().isoformat()}.md"
    footer = (
        f"\n\n---\n*Generated {date.today().isoformat()} · run `{result.run_id}` · "
        f"{result.turns} turns · {len(result.tool_calls)} tool calls · "
        f"{result.duration_s:.0f}s"
        + (f" · ${result.cost_usd:.4f}" if result.cost_usd else "")
        + "*\n"
    )
    out.write_text(result.report + footer, encoding="utf-8")

    err_console.print(f"\n[green]✓[/green] {out}")
    err_console.print(
        f"  [dim]run {result.run_id} · {result.turns} turns · "
        f"{len(result.tool_calls)} tool calls · {result.duration_s:.0f}s"
        + (f" · ${result.cost_usd:.4f}" if result.cost_usd else "") + "[/dim]"
    )

    console.print()
    _render_recommendations(recommendations_for_run(result.run_id))

    if args.show:
        console.print()
        console.print(result.report)
    return 0


def cmd_welcome(args) -> int:
    """No subcommand given — what someone who downloaded the standalone
    executable and double-clicked it actually experiences, as opposed to
    someone who already knows to type `factfolio status`. Runs first-time
    setup (see cmd_init) and prints exactly where it landed and what to do
    next — plain terminal output, identical on every platform, never a GUI.
    Since a double-clicked console window on Windows closes itself the
    instant the process exits, this also waits for a keypress afterward
    rather than vanishing before anyone can read it.
    """
    print(f"{BOLD}FactFolio{RESET}\n", flush=True)

    cmd_init(args)  # idempotent — safe whether this is a first run or not

    # Only pause for a frozen build in an actual interactive console — never
    # when piped/scripted (would hang forever waiting for input that never
    # comes), and pip-installed/uv-run users are already at a shell prompt
    # that won't disappear on them regardless.
    if getattr(sys, "frozen", False) and sys.stdin.isatty():
        input(f"\n{DIM}Press Enter to exit…{RESET}")
    return 0


def _looks_initialized(path: Path) -> bool:
    """True if `path` is itself an existing factfolio project root."""
    return (path / "memory" / "investment_policy.md").exists()


def _next_available(cwd: Path) -> Path:
    """The first unused `factfolio-2`, `factfolio-3`, … under `cwd`."""
    n = 2
    while (cwd / f"factfolio-{n}").exists():
        n += 1
    return cwd / f"factfolio-{n}"


def _resolve_init_target(cwd: Path) -> Path:
    """Decide which directory this run of `factfolio init` should set up.

    - Already standing inside an initialized project (this dir has its own
      memory/investment_policy.md) → use it as-is. Re-running init from
      inside your own project must stay a safe no-op refresh, not another
      layer of nesting.
    - Otherwise, create a fresh `factfolio/` folder under here — like `git
      clone` or `create-react-app`, init hands you a folder to `cd` into
      rather than scattering runtime dirs into whatever directory you
      happened to be standing in when you ran it.
    - If `factfolio/` already exists here and is itself an initialized
      project → reuse it (same safe-rerun guarantee).
    - If it exists but isn't one — some unrelated folder that happens to be
      named `factfolio` — ask before touching it instead of assuming.
    - If it exists and isn't even a directory (a stray file from an old
      download, say) there's no in-place option that doesn't mean deleting
      someone's file, so always make way instead of asking.
    """
    if _looks_initialized(cwd):
        return cwd

    candidate = cwd / "factfolio"
    if not candidate.exists():
        return candidate
    if candidate.is_dir() and _looks_initialized(candidate):
        return candidate

    if not candidate.is_dir():
        print(f"{DIM}'{candidate}' already exists and isn't a folder — "
              f"creating a new copy instead of touching it.{RESET}")
        return _next_available(cwd)

    if sys.stdin.isatty():
        print(f"{YELLOW}A folder named 'factfolio' already exists here, but it "
              f"isn't a factfolio project.{RESET}")
        answer = input(
            "  [o]verwrite — set up factfolio inside it as-is, or "
            "create a new [c]opy? [o/C] "
        ).strip().lower()
    else:
        answer = "c"
        print(f"{DIM}'{candidate}' already exists and isn't a factfolio project — "
              f"non-interactive run, so creating a new copy rather than touching it.{RESET}")

    if answer.startswith("o"):
        return candidate

    return _next_available(cwd)


def cmd_init(_args) -> int:
    """First-run setup: create a dedicated project folder (runtime dirs + a
    starter investment_policy.md) and print exactly how to use it.

    Idempotent: re-running from inside an already-initialized folder, or
    from the folder that contains one, never overwrites an existing
    memory/investment_policy.md and never nests a second copy — safe to
    re-run any time (e.g. after adding a new symbol to tickers.yaml).
    """
    from mybroker import config

    target = _resolve_init_target(Path.cwd())
    config.set_project_root(target)
    config.ensure_dirs()

    if config.POLICY_FILE.exists():
        print(f"{DIM}✓ {config.POLICY_FILE} already exists — left untouched.{RESET}")
    else:
        answers = _run_policy_interview() if sys.stdin.isatty() else None
        config.POLICY_FILE.write_text(_render_policy_template(answers), encoding="utf-8")
        print(f"{GREEN}✓{RESET} Set up {target}")
        print(f"  {DIM}wrote memory/investment_policy.md"
              f"{' from your answers above' if answers else ''}{RESET}")

    if config.TICKERS_FILE.exists():
        print(f"{DIM}✓ {config.TICKERS_FILE} already exists — left untouched.{RESET}")
    else:
        # Seeded, not read in place from SRC_DIR — a PyInstaller onefile
        # build's SRC_DIR is a temp folder wiped on exit, so that path is
        # never somewhere anyone could durably edit. This copy is yours.
        config.TICKERS_FILE.write_text(
            config.DEFAULT_TICKERS_FILE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        print(f"{GREEN}✓{RESET} Seeded {config.TICKERS_FILE} from the bundled defaults")

    _print_getting_started(target, config.TICKERS_FILE)
    return 0


def _print_getting_started(target: Path, tickers_file: Path) -> None:
    """Printed once init is done — where things live, what's required
    before the first real command, and what's optional. Not a "next steps"
    checklist someone has to re-derive from four different docs."""
    print(f"\n{BOLD}Project:{RESET} {target}")

    print(f"\n{BOLD}1. Go there{RESET}")
    print(f"     cd {target}")

    print(f"\n{BOLD}2. Add your holdings{RESET} — either:")
    print(f"     • {target / 'holdings.csv'} (+ optional holdings_mf.csv) "
          f"— standard Zerodha export")
    print(f"     • or drop any broker export into {target / 'holdings_inbox'}/ "
          f"— csv, xls, xlsx, pdf, or txt,")
    print("       equity or mutual fund, any filename — each file is sniffed "
          "and classified automatically")

    print(f"\n{BOLD}3. Map every symbol you hold{RESET} in {tickers_file}")
    print("     an unmapped symbol is a hard error on purpose — no silent .NS guessing")

    print(f"\n{BOLD}4. Set your real numbers{RESET} in "
          f"{target / 'memory' / 'investment_policy.md'}")
    print("     target CAGR, core/satellite split, position/sector caps — every "
          "recommendation")
    print("     is checked against these, never against whatever the LLM feels like saying")
    print("     it's plain text — as your goals, risk appetite, or target return "
          "change, just edit")
    print("     it again; nothing about it is fixed at init time")

    print(f"\n{BOLD}Then, from inside {target.name}/:{RESET}")
    print(f"  factfolio validate    {DIM}resolve every ticker — run this before report/chat{RESET}")
    print(f"  factfolio status      {DIM}instant snapshot — deterministic, no LLM{RESET}")
    print(f"  factfolio report      {DIM}full multi-agent review → reports/{RESET}")
    print(f"  factfolio chat        {DIM}terminal Q&A, one agent{RESET}")
    print(f"  factfolio mcp         {DIM}run as an MCP server for other tools{RESET}")
    print(f"  factfolio --help      {DIM}everything{RESET}")

    print(f"\n{BOLD}The LLM, in short:{RESET} only `report`, `chat`, and mcp's "
          f"run_portfolio_review call one —")
    print("  your local `claude login` session by default, or export "
          "ANTHROPIC_API_KEY to override.")
    print("  status/validate/cron/estimate-dates are pure deterministic Python, "
          "no LLM involved.")
    print("  Either way, your holdings and their values never leave this machine.")


def cmd_validate(_args) -> int:
    """Re-resolve tickers. The gate that must pass before any agent run."""
    from mybroker.tickers_validate import main as validate_main

    return validate_main()


def cmd_mcp(_args) -> int:
    """Run as a standalone MCP server (stdio) for external clients — VS
    Code's Claude extension, Claude Desktop, or any other MCP-aware tool or
    agent. Point a client at `factfolio mcp`; nothing to configure, no port
    to pick. See mcp_server.py for the exposed tools."""
    from mybroker.mcp_server import run as run_mcp_server

    return run_mcp_server()


def cmd_estimate_dates(_args) -> int:
    """Tentative purchase-date estimation from price history + avg_cost.

    Deterministic, no LLM — one price-history fetch per equity position, then
    portfolio.purchase_estimator's tightest-tolerance-first, most-recent-
    match-first search. Writes memory/estimated_purchase_dates.{json,md}.
    compute_tax_impact reads the json back as a fallback when a sale doesn't
    supply an explicit purchase_date.
    """
    from mybroker.data.yfinance_provider import YFinanceProvider
    from mybroker.portfolio.purchase_estimator import estimate_purchase_date, save_estimates

    portfolio = load_portfolio()
    provider = YFinanceProvider()

    print(f"{DIM}Estimating purchase dates for {len(portfolio.equity)} positions "
          f"(fetching price history)…{RESET}", file=sys.stderr)

    estimates = []
    for pos in portfolio.equity:
        try:
            result = provider.get_history(pos.symbol, days=1100)
        except KeyError as exc:
            print(f"  {YELLOW}{pos.symbol}: skipped — {exc}{RESET}", file=sys.stderr)
            continue

        if not result.data:
            print(f"  {YELLOW}{pos.symbol}: skipped — "
                  f"{'; '.join(result.warnings) or 'no price history'}{RESET}", file=sys.stderr)
            continue

        est = estimate_purchase_date(pos.symbol, pos.avg_cost, result.data)
        estimates.append(est)

        if est.confident:
            print(f"  {GREEN}{pos.symbol:<12}{RESET} → {est.estimated_date}  "
                  f"({est.holding_days_from_today}d ago, ±{est.tolerance_pct:.1f}%)",
                  file=sys.stderr)
        else:
            print(f"  {DIM}{pos.symbol:<12} → no confident match{RESET}", file=sys.stderr)

    save_estimates(estimates)
    n_confident = sum(1 for e in estimates if e.confident)
    print(f"\n{BOLD}{n_confident}/{len(estimates)} estimated{RESET} "
          f"{DIM}→ memory/estimated_purchase_dates.{{json,md}}. "
          f"TENTATIVE — verify against contract notes before filing taxes.{RESET}",
          file=sys.stderr)
    return 0


def cmd_chat(_args) -> int:
    """Interactive REPL. Lighter than `report` — one agent, direct tool
    access, no subagent roster or adversarial review. Q&A, not a new
    recommendation path."""
    from mybroker.agents.chat import run_chat_repl

    return anyio.run(run_chat_repl)


def cmd_cron(args) -> int:
    """M5 unattended job: grade due recommendations against real outcomes.

    Pure Python + one price fetch per due recommendation — no LLM, no agent
    session, safe on a schedule (see docs/MILESTONES.md for a crontab/launchd
    example). Idempotent: re-running finds nothing new to grade. Exit code
    reflects whether the run itself errored, not whether anything was due.
    """
    import json
    from datetime import UTC, datetime

    from mybroker.config import CRON_LOG, ensure_dirs
    from mybroker.scoring import grade_due_recommendations

    ensure_dirs()
    started = datetime.now(UTC)

    try:
        results = grade_due_recommendations()
    except Exception as exc:
        print(f"{RED}✗ cron run failed: {exc}{RESET}", file=sys.stderr)
        with CRON_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": started.isoformat(), "ok": False, "error": str(exc)}) + "\n")
        return 1

    graded = [r for r in results if r.graded]
    ungradeable = [r for r in results if not r.graded]

    with CRON_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": started.isoformat(),
            "ok": True,
            "due": len(results),
            "graded": len(graded),
            "ungradeable": len(ungradeable),
            "results": [
                {"rec_id": r.rec_id, "symbol": r.symbol, "action": r.action,
                 "graded": r.graded,
                 **({"verdict": r.outcome.get("verdict"),
                     "return_pct": r.outcome.get("return_pct")} if r.outcome else {}),
                 **({"reason": r.reason} if r.reason else {})}
                for r in results
            ],
        }, default=str) + "\n")

    prefix = f"[{started.strftime('%Y-%m-%d %H:%M')}]"
    if not results:
        print(f"{DIM}{prefix} nothing due for review.{RESET}", file=sys.stderr)
        return 0

    print(f"{BOLD}{prefix} due_for_review: {len(results)}{RESET}", file=sys.stderr)
    for r in graded:
        o = r.outcome or {}
        col = GREEN if (o.get("return_pct") or 0) >= 0 else RED
        verdict = f" — {o.get('verdict')}" if o.get("verdict") else ""
        print(f"  {r.symbol:<12} {r.action:<6} {col}{o.get('return_pct'):+.2f}%{RESET}"
              f"{verdict}  `{r.rec_id}`", file=sys.stderr)
    for r in ungradeable:
        print(f"  {YELLOW}{r.symbol:<12} {r.action:<6} ungradeable — {r.reason}{RESET}",
              file=sys.stderr)
    print(f"{DIM}{len(graded)} graded, {len(ungradeable)} ungradeable "
          f"→ memory/ledger.jsonl updated{RESET}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows only gives stdout/stderr a UTF-8 codepage for an *interactive*
    # console — the moment either is redirected/piped/captured (a CI step,
    # `factfolio status > out.txt`, anything), Python falls back to the
    # legacy codepage (cp1252), which can't encode the ✓/✗/₹ this CLI prints
    # everywhere, and crashes with UnicodeEncodeError before printing
    # anything useful. reconfigure() (Python 3.7+) forces UTF-8 with a safe
    # fallback instead of a crash; harmless no-op on platforms already UTF-8.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

    from mybroker import __version__

    parser = argparse.ArgumentParser(
        prog="mybroker",
        description="Indian equity & mutual fund portfolio advisory agent.",
    )
    parser.add_argument("--version", action="version", version=f"factfolio {__version__}")
    # Not required: someone who downloaded the standalone executable and
    # double-clicked it (no terminal, no arguments) should land on
    # cmd_welcome below, not a bare "command required" usage error in a
    # console window that closes itself the instant the process exits.
    parser.set_defaults(func=cmd_welcome)
    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser("init", help="first-run setup: dirs + a starter investment_policy.md"
                   ).set_defaults(func=cmd_init)

    sub.add_parser("status", help="deterministic snapshot (no LLM, instant)"
                   ).set_defaults(func=cmd_status)

    p_report = sub.add_parser("report", help="full agent review → reports/")
    p_report.add_argument("--show", action="store_true", help="also print to stdout")
    p_report.set_defaults(func=cmd_report)

    sub.add_parser("validate", help="re-resolve tickers (gate)"
                   ).set_defaults(func=cmd_validate)

    sub.add_parser("mcp", help="run as an MCP server (stdio) for other tools/agents"
                   ).set_defaults(func=cmd_mcp)

    sub.add_parser("estimate-dates",
                    help="tentative purchase-date estimation from price history (no LLM)"
                    ).set_defaults(func=cmd_estimate_dates)

    sub.add_parser("chat", help="interactive Q&A REPL, one agent, no report gate"
                   ).set_defaults(func=cmd_chat)

    sub.add_parser("cron", help="M5: grade due recommendations (no LLM, unattended-safe)"
                   ).set_defaults(func=cmd_cron)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"{RED}✗{RESET} {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        # Backstop for everything a subcommand didn't handle itself — without
        # this, any uncaught exception (an SDK/network hiccup, a bad broker
        # export, anything) dumps a raw traceback and exits with no hint of
        # what to do next. Show a short, actionable message; keep the full
        # traceback on disk instead of scrolling off the terminal.
        from mybroker.errors import friendly_message, log_error

        log_file = log_error(f"factfolio {args.command}", exc)
        print(f"\n{RED}✗{RESET} `factfolio {args.command}` failed: "
              f"{friendly_message(exc)}", file=sys.stderr)
        if log_file:
            print(f"  {DIM}Full traceback: {log_file}{RESET}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
