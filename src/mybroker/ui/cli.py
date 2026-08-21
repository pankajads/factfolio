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
import yaml
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from mybroker.config import REPORTS_DIR, ensure_dirs

# Drafting logic itself (pure Python, no network/LLM) lives in
# portfolio/ticker_seeding.py — shared with `factfolio validate`, which
# self-heals a missing/incomplete tickers.yaml the same way rather than
# falling back to probing the bundled illustrative defaults. Aliased back
# to their old private names here since this module's the one place that
# also drives the AI-assisted resolver around them (_suggest_ticker_matches
# below) — which also uses is_pristine_draft/backfill_draft_entry directly
# (via the `ticker_seeding` module import), so an existing entry the agent
# is confident about but that's still an untouched draft gets its name/
# sector backfilled instead of just printed as a "?" for a human to redo.
from mybroker.portfolio import ticker_seeding
from mybroker.portfolio.loader import load_portfolio
from mybroker.portfolio.metrics import snapshot
from mybroker.portfolio.policy import Policy
from mybroker.portfolio.ticker_seeding import insert_ticker_yaml_block as _insert_ticker_yaml_block
from mybroker.portfolio.ticker_seeding import (
    seed_draft_ticker_entries as _seed_draft_ticker_entries,
)

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


def _extract_section(report: str, heading: str) -> str | None:
    """Pull the prose under a `## <heading>` line, up to the next `## `
    heading or end of report. Used to surface the report's own "Bottom
    line" — the 3-4 sentence answer to "what do I do with this" that the
    orchestrator's system prompt already requires every report to open
    with — in the terminal by default, instead of leaving it buried in the
    saved .md file where only `--show` (or opening the file by hand) would
    reveal it. Returns None if the heading isn't found, so callers can fall
    back gracefully instead of assuming every report is shaped exactly as
    asked."""
    import re

    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.IGNORECASE)
    lines = report.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if pattern.match(line.strip())), None)
    if start is None:
        return None
    end = next((j for j in range(start, len(lines)) if lines[j].startswith("## ")), len(lines))
    section = "\n".join(lines[start:end]).strip()
    return section or None


def _render_recommendations(recs: list) -> None:
    """The real BUY/SELL/HOLD decisions logged this run — a compact table
    for the at-a-glance list, then each one's full reasoning printed as
    normal wrapped prose underneath, not a wall of markdown to scroll
    through to find it.

    Rationale and evidence used to live as extra table columns instead —
    squeezed to ~50 and ~34 characters wide, which read fine when the
    terminal was wide enough to give a table five columns' worth of room,
    but on an ordinary terminal width, Rich had to shrink those columns
    further to make the row fit at all: the rationale wrapped one or two
    words per line for paragraphs long enough to need "Independently
    re-checked" and "Tax consequence" sub-sections, and evidence got
    ellipsis-truncated mid-token ("get_portfolio_snapshot.weight_pct…") —
    a real user's own transcript showed both happening at once. A table
    cell's width is fixed by the table; a paragraph printed on its own
    just wraps at the full console width, the same as `status`'s own
    output, which is what actually makes a multi-paragraph rationale
    readable. The full report (with everything else the agents found) is
    still written to reports/ either way.
    """
    if not recs:
        console.print("[dim]No recommendations were logged this run.[/dim]")
        return

    table = Table(title="Recommendations", header_style="bold", show_lines=True)
    table.add_column("Symbol", style="bold", no_wrap=True)
    table.add_column("Action", no_wrap=True)
    table.add_column("Conviction", no_wrap=True)

    for r in recs:
        style = _ACTION_STYLE.get(r.action, "")
        action = f"[{style}]{r.action}[/{style}]" if style else r.action
        table.add_row(r.symbol, action, r.conviction)

    console.print(table)

    for r in recs:
        style = _ACTION_STYLE.get(r.action, "")
        action = f"[{style}]{r.action}[/{style}]" if style else r.action
        console.print()
        console.print(f"[bold]{r.symbol}[/bold] — {action} [dim]({r.conviction} conviction)[/dim]")
        console.print(r.rationale)
        if r.evidence:
            evidence = "  ·  ".join(
                f"{e.get('tool', '?')}.{e.get('field', '?')}={e.get('value', '?')}"
                for e in r.evidence[:3]
            )
            console.print(f"[dim]Evidence: {evidence}[/dim]")


def cmd_report(args) -> int:
    """Full agent-generated review, written to reports/YYYY-MM-DD.md."""
    from mybroker.agents.orchestrator import run_review
    from mybroker.ledger import recommendations_for_run

    ensure_dirs()
    err_console.print(_auth_status_line())
    # Set upfront, not discovered after the fact: this dispatches a roster of
    # subagents across the whole portfolio, so it's minutes and real API
    # spend, not a `status`-style instant snapshot. A first run with no
    # warning either reads as hung or lands as a surprise bill — either way,
    # a bad first impression that one line fixes.
    err_console.print(
        "[dim]Full review: dispatches a team of subagents across your whole "
        "portfolio — typically 10-15 min and a few dollars of API usage "
        "(varies with portfolio size). Ctrl-C to cancel.[/dim]"
    )

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

    # The report's own "Bottom line" section (mandatory per the orchestrator's
    # system prompt) is written to BE the answer to "so what do I do" — a
    # reader who stops there should still get the decision. Printing only
    # the raw recommendations table by default buried that answer inside the
    # saved .md file, behind --show, which is exactly why that table alone
    # reads as "not sure what the recommendation is": the table is the
    # evidence, this is the verdict, and the verdict was never shown.
    bottom_line = _extract_section(result.report, "Bottom line")
    if bottom_line:
        console.print()
        console.print("[bold]Bottom line[/bold]")
        console.print(Markdown(bottom_line))

    recs = recommendations_for_run(result.run_id)
    console.print()
    _render_recommendations(recs)

    if recs:
        console.print(
            "\n[dim]Every BUY/SELL/TRIM above was independently challenged by "
            "a second, adversarial pass before being logged — the full "
            f"reasoning trail (including anything that got revised as a "
            f"result) is in {out}.[/dim]"
        )

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


def _standing_beside_own_project() -> bool:
    """True for exactly the mistake config.cd_hint_if_project_nearby()
    already describes: `factfolio init` was run from directory X, creating
    `X/factfolio/`, and a later command runs from X itself — forgetting
    the `cd` — rather than the folder init actually printed.

    Every command except `init`/bare `factfolio` (which redirect
    themselves into that nested folder on purpose, see
    _resolve_init_target) must catch this BEFORE doing any work: the old
    behaviour was to silently scaffold a second, empty logs/reports/
    memory/holdings_inbox layout right beside the real project — every
    file dropped in a *previous* run's holdings_inbox/, every entry in its
    tickers.yaml, invisible from here.
    """
    return not _looks_initialized(Path.cwd()) and _looks_initialized(Path.cwd() / "factfolio")


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
        # Loud on purpose — this decides WHERE your project lives, and
        # printing it dim as the very first line (before the policy
        # interview even starts) meant it was trivial to scroll past and
        # only notice a `factfolio-2` folder afterwards with no idea why.
        print(f"\n{YELLOW}! '{candidate}' already exists and isn't a "
              f"folder{RESET} {DIM}(a file with that exact name — maybe "
              f"the factfolio executable itself, if you copied it "
              f"here){RESET}")
        print(f"  {DIM}Not touching it — setting up the project one folder "
              f"over instead.{RESET}")
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

    added = _seed_draft_ticker_entries(config.TICKERS_FILE)
    if added:
        plural = "y" if len(added) == 1 else "ies"
        print(f"{GREEN}✓{RESET} Added {len(added)} draft entr{plural} to "
              f"{config.TICKERS_FILE} from your holdings — DRAFT, review before "
              f"relying on them: {', '.join(sorted(added))}")
        # load_tickers() is cached — anything it already returned this
        # process is now stale after the write above, and the resolver
        # steps below need to see these new entries, not miss them.
        config.load_tickers.cache_clear()

    _suggest_ticker_matches()

    _print_getting_started(target, config.TICKERS_FILE)
    return 0


_MAX_SUGGESTIONS = 15


def _suggest_ticker_matches() -> None:
    """For every full-company-name holding that can't be auto-drafted (see
    importers.discover_unmapped_full_names — a Sharekhan-style "Scrip
    Name" column, say), try the M7 agent-assisted resolver first
    (agents/ticker_resolver.py) — it reasons about cross-row duplicates
    and grounds every claim in a real search result, not just a fuzzy
    top-hit. High-confidence, validated, non-duplicate resolutions get
    written straight to tickers.yaml; everything else prints the agent's
    own reasoning for a human to decide.

    Falls back to a single plain yfinance-search suggestion per name (no
    reasoning, never auto-written) if the agent path fails for any reason
    — no `claude` login, network, a malformed response — since this is
    convenience end to end, never a gate that could block `init`.
    """
    holdings = ticker_seeding.collect_unmapped_holdings()
    if not holdings:
        return

    todo = holdings[:_MAX_SUGGESTIONS]
    skipped = len(holdings) - len(todo)

    try:
        _resolve_via_agent(todo, skipped)
    except Exception as exc:
        # Deliberately never a gate that could block `init` — but silently
        # swallowing the *reason* meant a real cause (no `claude login`, a
        # network blip, a malformed response) was unrecoverable after the
        # fact: the holdings just never got mapped, with nothing anywhere
        # saying why. Logged, not printed — the plain-search fallback below
        # already tells the user what to do next; this is for someone later
        # asking "why didn't AXISBANK/TCS/etc. end up in tickers.yaml?".
        from mybroker.logging_setup import get_logger

        get_logger(__name__).warning(
            "AI-assisted ticker resolution failed for %d holding(s) (%s: %s) "
            "— falling back to plain yfinance-search suggestions.",
            len(todo), type(exc).__name__, exc,
        )
        _suggest_via_plain_search(todo, skipped)


_REASON_LIMIT = 140


def _short_reason(text: str, symbol: str = "") -> str:
    """The resolver's own prompt now asks for one short sentence (see
    ticker_resolver.py), but a model can still ignore that — this is the
    code-level backstop, same "prompt asks, code enforces" split as the
    duplicate-tracking above. A wall of reasoning text mid-`init`, before a
    new user has seen the product do anything useful yet, reads as "this is
    going to be a lot of work" — exactly the wrong first impression. Nothing
    is lost: the full text still goes to the log for whoever later wants to
    know why a symbol didn't resolve."""
    text = (text or "").strip()
    if len(text) <= _REASON_LIMIT:
        return text or "no confident match found"

    from mybroker.logging_setup import get_logger

    get_logger(__name__).info(
        "Full ticker-resolution reasoning for %s: %s", symbol or "?", text
    )
    return text[:_REASON_LIMIT].rstrip() + "… (see logs/ for the full reasoning)"


def _resolve_via_agent(todo: list[dict], skipped: int) -> None:
    from mybroker import config
    from mybroker.agents.ticker_resolver import resolve_names

    print(f"\n{DIM}Asking an agent to resolve {len(todo)} unmapped holding "
          f"name(s)…{RESET}")
    resolved = anyio.run(resolve_names, todo)

    # Don't rely on the agent always flagging a same-symbol duplicate
    # correctly (the prompt asks for it, but this is the code-level
    # backstop) — track every key that already existed (from before this
    # run, e.g. a pre-existing entry — Tata Motors' 2024 demerger means
    # the bundled defaults already carry TMCV/TMPV, say) separately from
    # ones this pass writes itself, so a second name resolving to the
    # same symbol can never clobber either, and the printed reason for
    # why stays accurate either way.
    pre_existing_keys: set[str] = set(yaml.safe_load(config.TICKERS_FILE.read_text())["symbols"])
    written_this_run: set[str] = set()
    for r in resolved:
        key = r.symbol.split(".")[0] if r.symbol else None
        already_present = key in pre_existing_keys or key in written_this_run
        if r.symbol and r.confidence == "high" and not r.duplicate_of and not already_present:
            block = _agent_resolved_entry(r)
            if _insert_ticker_yaml_block(config.TICKERS_FILE, block):
                written_this_run.add(key)
                print(f"  {GREEN}✓{RESET} {r.name!r} → {BOLD}{r.symbol}{RESET} "
                      f"{DIM}— added to tickers.yaml; still review tier/bucket{RESET}")
                continue
        if key in written_this_run and r.symbol and not r.duplicate_of:
            print(f"  {YELLOW}?{RESET} {r.name!r} — {r.symbol} was already added this "
                  f"run (likely the same holding as another row): "
                  f"{_short_reason(r.reasoning, r.name)}")
        elif (
            key in pre_existing_keys and r.symbol and not r.duplicate_of
            and r.confidence == "high"
            and ticker_seeding.is_pristine_draft(config.TICKERS_FILE, key)
        ):
            # The existing entry's name is still literally the ticker
            # itself — proof it's the deterministic draft this same
            # holding's short-symbol source seeded, never touched by a
            # human since. Safe to backfill the name (and sector, if the
            # agent has one) rather than just asking a human to go do it —
            # unlike a fresh entry, nothing about candidates/tier/bucket
            # changes, so there's no new guess being made here.
            ticker_seeding.backfill_draft_entry(
                config.TICKERS_FILE, key, name=r.company_name or r.name,
                sector=r.sector, alias=r.name,
            )
            print(f"  {GREEN}✓{RESET} {r.name!r} → {BOLD}{key}{RESET} "
                  f"{DIM}— existing DRAFT entry, backfilled its name/sector{RESET}")
        elif key in pre_existing_keys and r.symbol and not r.duplicate_of:
            print(f"  {YELLOW}?{RESET} {r.name!r} — {r.symbol} is already in tickers.yaml "
                  f"(check it covers this holding too): {_short_reason(r.reasoning, r.name)}")
            # Same accept/edit/skip offer as the plain-uncertain-match case
            # below — a demerger (Tata Motors → TMCV/TMPV, say) means the
            # agent's own suggested symbol legitimately already existing in
            # tickers.yaml does NOT mean this holding is covered by it; a
            # human needs to say whether it's the same holding or a genuinely
            # separate one that needs its own entry.
            if sys.stdin.isatty():
                result = _confirm_uncertain_match(r)
                if result:
                    chosen, confirmed = result
                    _write_reviewed_symbol(
                        r, chosen, confirmed=confirmed,
                        pre_existing_keys=pre_existing_keys,
                        written_this_run=written_this_run,
                    )
        elif r.duplicate_of:
            print(f"  {YELLOW}?{RESET} {r.name!r} — looks like the same holding as "
                  f"{r.duplicate_of!r}: {_short_reason(r.reasoning, r.name)}")
        elif r.symbol:
            print(f"  {YELLOW}?{RESET} {r.name!r} — possible match {BOLD}{r.symbol}{RESET} "
                  f"({r.confidence} confidence): {_short_reason(r.reasoning, r.name)}")
            # Only ever offered interactively — a scripted/non-interactive
            # run (a frozen build piped in CI, say) keeps the old behaviour
            # of just printing the line above and moving on.
            if sys.stdin.isatty():
                result = _confirm_uncertain_match(r)
                if result:
                    chosen, confirmed = result
                    _write_reviewed_symbol(
                        r, chosen, confirmed=confirmed,
                        pre_existing_keys=pre_existing_keys,
                        written_this_run=written_this_run,
                    )
        else:
            hint = (
                f" {DIM}(candidate from elsewhere in the same row: "
                f"{RESET}{BOLD}{r.candidate_symbol}{RESET}{DIM} — unverified, "
                f"your call){RESET}"
                if r.candidate_symbol else ""
            )
            print(f"  {YELLOW}?{RESET} {r.name!r} — {_short_reason(r.reasoning, r.name)}{hint}")
            # The agent found nothing trustworthy at all (zero search
            # candidates, or a claimed symbol that failed grounding) — the
            # accept/edit prompt above doesn't apply by default. But two
            # real options remain: r.candidate_symbol (a short code found
            # elsewhere in the SAME source row — see importers.py's
            # discover_unmapped_full_names; the agent's own SYSTEM_PROMPT
            # tells it to search this first, but that's a prompt
            # instruction, not code-enforced, so it's shown here as
            # unverified either way, not as "already tried and failed")
            # can still be accepted on a human's own judgment; and the
            # SOURCE NAME is often the actual problem — a demat PDF's text
            # extraction routinely mangles names ("HINDUSTA N COPPER LTD"
            # for "Hindustan Copper Ltd" — a stray mid-word space, not a
            # real company) — so fixing it and retrying is a real third
            # option, not just typing a symbol blind.
            if sys.stdin.isatty():
                result = _confirm_unmapped(r)
                if result:
                    chosen, confirmed = result
                    _write_reviewed_symbol(
                        r, chosen, confirmed=confirmed,
                        pre_existing_keys=pre_existing_keys,
                        written_this_run=written_this_run,
                    )

    # Same reason as cmd_init's own cache_clear after seed_draft_ticker_
    # entries: load_tickers() is cached, and every write above (a new entry
    # via written_this_run.add(), OR a backfilled name/sector on an existing
    # draft — which never touches written_this_run) makes that cache stale.
    # Unconditional on purpose, not gated on written_this_run being
    # non-empty: a backfill-only run (nothing new, only existing DRAFT
    # entries getting their name filled in) writes to disk too. Missing
    # this was the actual bug behind "validate resolved 5 holdings, then
    # said all 11 were still unmapped, in the SAME run" — cmd_validate's
    # re-validate call right after this function returns read the
    # pre-resolution snapshot straight out of the cache, never touching what
    # was just written to disk. The old two-process workflow (init, then a
    # separately invoked validate) never hit this: a fresh process starts
    # with an empty cache regardless.
    config.load_tickers.cache_clear()

    if skipped:
        print(f"  {DIM}…and {skipped} more — re-run init after adding some of "
              f"these to see the rest.{RESET}")


def _alias_yaml(name: str) -> str:
    """A single-item flow-style YAML list for an `aliases:` line, quoted
    only when the raw text actually needs it (colons, leading punctuation,
    ...) — safe_dump already knows the rule, no need to hand-roll it."""
    return yaml.safe_dump([name], default_flow_style=True).strip()


def _agent_resolved_entry(r) -> str:
    """A higher-quality entry than ticker_seeding.draft_ticker_entry: the candidate and
    sector come from a real search result the resolver's own validation
    gate already checked (agents/ticker_resolver.py's _validate), not a
    bare guess — so, unlike a plain DRAFT, only the single verified
    candidate is listed, never padded out with an unverified .BO/.NS
    guess alongside it. tier/bucket still can't come from any market-data
    source, so those stay your own call either way.

    aliases: records r.name (the RAW source text, before any cleanup)
    verbatim — see config.py's resolve_symbol_by_name for why: a source
    file's own PDF-wrapping or abbreviations routinely make the raw name
    normalize to something the clean `name:` field never will, so without
    this, the exact same holding gets re-flagged as "still unmapped" the
    very next time this file is re-scanned, even though it was already
    correctly resolved moments ago.
    """
    return (
        f"  {r.symbol.split('.')[0]}:\n"
        f"    name: {r.company_name or r.name}\n"
        f"    candidates: [{r.symbol}]  # resolved by the init agent — verified, not guessed\n"
        f"    sector: {r.sector or 'Unknown'}\n"
        f"    tier: unknown  # TODO: large | mid | small\n"
        f"    bucket: satellite  # TODO: core | satellite\n"
        f"    aliases: {_alias_yaml(r.name)}  # raw source text this was resolved from\n"
        f"    notes: >\n"
        f"      Resolved by the factfolio init agent from {r.name!r}, high confidence.\n"
        f"      tier/bucket still need your own judgment either way — nothing\n"
        f"      external can tell you which bucket a stock belongs in for your\n"
        f"      own strategy.\n"
    )


def _prompt_symbol(hint: str = "e.g. TCS.NS") -> str | None:
    """Raw 'type a symbol, blank to skip' prompt shared by every interactive
    resolution path below. Returns None on EOF/Ctrl-C/blank input — always
    treated as skip, never as an error."""
    try:
        typed = input(f"    Symbol ({hint}), blank to skip: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return typed or None


def _confirm_uncertain_match(r) -> tuple[str, bool] | None:
    """Interactive-only follow-up to a printed medium/low-confidence '?'
    line: let a human accept the agent's own suggested symbol, type a
    different one, or skip — right here, instead of leaving it as a line
    in scrollback that's easy to miss and forces a separate trip to a text
    editor to fix. Returns (symbol, confirmed) to write, or None to leave
    this holding unmapped, same as before. `confirmed` is True only for
    "accept" — the agent's own suggestion, grounded in a real search for
    THIS holding — never for a hand-typed symbol, which might just be a
    typo that happens to collide with something unrelated already in
    tickers.yaml (see _write_reviewed_symbol for what that distinction
    controls). Never called outside an interactive terminal — see its
    call site.
    """
    try:
        answer = input(
            f"    [a]ccept {r.symbol}, [e]dit the symbol, [s]kip? [a/e/S] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if answer.startswith("a"):
        return r.symbol, True
    if answer.startswith("e"):
        typed = _prompt_symbol()
        return (typed, False) if typed else None
    return None


def _print_row_data(r) -> None:
    """The full source row (see importers.py's discover_unmapped_full_names)
    behind an unresolved holding — every column, raw, under its own real
    header label. Printed on request from _confirm_unmapped rather than
    always, so the common case (accept/type/skip) isn't buried under a
    dozen lines most of the time — but available in full for exactly the
    case a fixed heuristic and an agent's own read both missed: a person
    looking at the actual statement, the same material either of them had,
    often spots it in a second."""
    if not r.row_data:
        print(f"    {DIM}No row data available for this holding.{RESET}")
        return
    width = max(len(k) for k in r.row_data)
    for key, value in r.row_data.items():
        print(f"    {DIM}{key:<{width}}{RESET}  {value}")


def _confirm_unmapped(r) -> tuple[str, bool] | None:
    """Interactive-only follow-up when the agent found nothing trustworthy
    at all — zero search candidates, or a claimed symbol that failed
    grounding (agents/ticker_resolver.py's _validate). Unlike
    _confirm_uncertain_match, there's no AGENT-verified suggestion to
    accept — but r.candidate_symbol (a short code found elsewhere in the
    same source row) is still worth offering as a one-keystroke accept: a
    human recognising "oh, that's obviously the right one" from the same
    statement row is a real, fast option, even though nothing here
    independently verified it. r.row_data — the WHOLE row, not just that
    one heuristic guess — is offered too, on request: the actual fix for
    "don't just trust a fixed set of Python assumptions" is giving a human
    the same raw material an intelligent reader (the agent, or a person)
    needs to actually understand the file, not a narrower pre-filtered
    guess. Beyond that, the SOURCE NAME is often itself the problem — a
    demat PDF's text extraction routinely mangles names ("HINDUSTA N
    COPPER LTD" for "Hindustan Copper Ltd", a stray mid-word space), which
    is exactly the kind of thing that makes a real company's search come
    back empty. Correcting the name and retrying a plain search (no new
    agent call — same deterministic yfinance lookup the no-agent fallback
    path already uses) turns that from "give up" into "actually resolves."

    Returns (symbol, confirmed) or None — `confirmed` is True for the two
    paths grounded in real evidence about THIS holding (the same-row
    candidate_symbol, or a deterministic search on a human-corrected
    name), False for a hand-typed symbol with nothing behind it but a
    guess. See _write_reviewed_symbol for what that distinction controls.
    """
    while True:
        parts = []
        if r.candidate_symbol:
            parts.append(f"[a]ccept {r.candidate_symbol} (unverified, from the same row)")
        parts.append("[t]ype a different symbol" if r.candidate_symbol else "[t]ype the symbol yourself")
        parts.append("[c]orrect the company name and retry search")
        if r.row_data:
            parts.append("[r]ow data")
        parts.append("[s]kip")
        letters = "/".join(p[1] for p in parts[:-1]) + "/S"
        try:
            answer = input(f"    {', '.join(parts)}? [{letters}] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if r.candidate_symbol and answer.startswith("a"):
            # Unlike r.symbol on the _confirm_uncertain_match path (always
            # a real yfinance result, already exchange-suffixed),
            # candidate_symbol comes straight from the source file's own
            # text and never carries a .NS/.BO suffix — writing it bare
            # would silently produce an entry `factfolio validate` can
            # never resolve. NSE-first is the same default preference used
            # everywhere else here (the resolver's own instructions,
            # draft_ticker_entry's [.NS, .BO] guess order); validate's own
            # probe is still what actually confirms it, same as every
            # other guessed candidate.
            symbol = (
                r.candidate_symbol if "." in r.candidate_symbol
                else f"{r.candidate_symbol}.NS"
            )
            return symbol, True

        if r.row_data and answer.startswith("r"):
            _print_row_data(r)
            continue

        break

    if answer.startswith("t"):
        typed = _prompt_symbol()
        return (typed, False) if typed else None

    if answer.startswith("c"):
        try:
            corrected = input(
                f"    Correct company name (was {r.name!r}), blank to skip: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not corrected:
            return None

        from mybroker.config import suggest_ticker_for_name

        suggestion = suggest_ticker_for_name(corrected)
        if not suggestion:
            print(f"    {DIM}No match found for {corrected!r} either.{RESET}")
            return None

        print(f"    {DIM}Found{RESET} {BOLD}{suggestion}{RESET} {DIM}for "
              f"{corrected!r}.{RESET}")
        try:
            confirm = input(
                f"    [a]ccept {suggestion}, [e]dit, [s]kip? [a/e/S] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if confirm.startswith("a"):
            return suggestion, True
        if confirm.startswith("e"):
            typed = _prompt_symbol()
            return (typed, False) if typed else None
        return None

    return None


def _write_reviewed_symbol(
    r, chosen: str, *, confirmed: bool, pre_existing_keys: set[str],
    written_this_run: set[str],
) -> None:
    """Write a human-confirmed-or-typed symbol to tickers.yaml.

    If `chosen` already has an entry from BEFORE this run AND `confirmed`
    is True — the symbol came from real evidence about THIS holding
    (accepting the agent's own suggestion, or its same-row
    candidate_symbol, or a deterministic search on a human-corrected
    name), not a blind guess — there's no new entry to insert, but the
    confirmation itself still needs to go somewhere durable. Without
    this, accepting such a match used to just print "already in
    tickers.yaml — not overwriting it" and throw the confirmation away,
    which meant the exact same holding got flagged as unmapped again on
    the very next `factfolio validate`, forever, with no way to actually
    close it out (the real bug behind a Tata Steel demat-PDF entry that
    could never be accepted). Now: a pristine (never-touched) draft gets
    its name/sector backfilled the same way the automatic
    high-confidence path does; anything else gets r.name recorded as an
    alias (ticker_seeding.add_alias_to_entry) so
    config.resolve_symbol_by_name matches it outright next time.

    `confirmed=False` (a hand-typed symbol at the [e]dit prompt, with
    nothing behind it but a guess) keeps the old hands-off behaviour even
    on a collision — it might just be a typo that happens to match an
    unrelated existing entry, and silently aliasing onto it would
    conflate two different holdings. Same for a symbol written earlier in
    THIS same run (written_this_run) — two different holdings resolving
    to the same brand-new symbol in one pass, genuinely ambiguous either
    way.

    Shared by every interactive prompt above so this stays one rule, not
    several that could drift.
    """
    from mybroker import config

    chosen_key = chosen.split(".")[0]
    if chosen_key in written_this_run:
        print(f"    {YELLOW}!{RESET} {chosen_key} was already added this run — "
              f"not overwriting it; edit that entry yourself if it needs to change.")
        return
    if chosen_key in pre_existing_keys and not confirmed:
        print(f"    {YELLOW}!{RESET} {chosen_key} is already in tickers.yaml — "
              f"not overwriting it; edit that entry yourself if it needs to change.")
        return
    if chosen_key in pre_existing_keys:
        if ticker_seeding.is_pristine_draft(config.TICKERS_FILE, chosen_key):
            ticker_seeding.backfill_draft_entry(
                config.TICKERS_FILE, chosen_key, name=r.company_name or r.name,
                sector=r.sector, alias=r.name,
            )
            print(f"    {GREEN}✓{RESET} {r.name!r} → existing {BOLD}{chosen_key}{RESET} "
                  f"entry — backfilled its name/sector and recorded as an alias")
        elif ticker_seeding.add_alias_to_entry(config.TICKERS_FILE, chosen_key, r.name):
            print(f"    {GREEN}✓{RESET} {r.name!r} recorded as another name for the "
                  f"existing {BOLD}{chosen_key}{RESET} entry")
        else:
            print(f"    {YELLOW}!{RESET} {chosen_key} is already in tickers.yaml — "
                  f"couldn't attach an alias to it; edit that entry yourself.")
        return
    block = _reviewed_entry(r, chosen, edited=chosen != r.symbol)
    if _insert_ticker_yaml_block(config.TICKERS_FILE, block):
        written_this_run.add(chosen_key)
        print(f"    {GREEN}✓{RESET} added {BOLD}{chosen}{RESET} to tickers.yaml "
              f"— still review tier/bucket")


def _reviewed_entry(r, symbol: str, *, edited: bool) -> str:
    """A tickers.yaml entry for a medium/low-confidence match a human just
    confirmed or edited at `factfolio init` — distinct from
    _agent_resolved_entry (only ever written at high confidence, never
    edited) so the notes honestly say a human vouched for this, not just
    the agent's own uncertain guess."""
    if edited:
        # r.sector (if any) was the agent's own guess for its OWN
        # suggested symbol — carrying it over here would attach it to a
        # ticker the agent never actually looked at.
        name, sector = r.name, "Unknown"
        candidates_comment = "your own edit — verify it resolves"
        note = (
            f"You typed {symbol!r} yourself at `factfolio init`, in place of the "
            f"agent's own {r.confidence}-confidence guess for {r.name!r}. Verify it "
            f"resolves (`factfolio validate`) and fill in sector/tier/bucket."
        )
    else:
        name, sector = r.company_name or r.name, r.sector or "Unknown"
        candidates_comment = f"{r.confidence} confidence — you reviewed and accepted this"
        note = (
            f"{r.confidence.capitalize()} confidence match from {r.name!r}, reviewed "
            f"and accepted by you at `factfolio init`: {r.reasoning}"
        )
    return (
        f"  {symbol.split('.')[0]}:\n"
        f"    name: {name}\n"
        f"    candidates: [{symbol}]  # {candidates_comment}\n"
        f"    sector: {sector}\n"
        f"    tier: unknown  # TODO: large | mid | small\n"
        f"    bucket: satellite  # TODO: core | satellite\n"
        f"    aliases: {_alias_yaml(r.name)}  # raw source text this was resolved from\n"
        f"    notes: >\n"
        f"      {note}\n"
    )


def _suggest_via_plain_search(todo: list[dict], skipped: int) -> None:
    """The fallback when the agent path isn't available or fails: one
    plain yfinance-search suggestion per name, no reasoning, never
    auto-written — same behaviour this had before M7."""
    from mybroker.config import suggest_ticker_for_name

    print(f"\n{DIM}Looking up possible matches for {len(todo) + skipped} unmapped "
          f"holding name(s) via yfinance…{RESET}")
    for h in todo:
        suggestion = suggest_ticker_for_name(h["name"])
        if suggestion:
            print(f"  {YELLOW}?{RESET} {h['name']!r} — possible match {BOLD}{suggestion}{RESET} "
                  f"{DIM}— verify, then add it to tickers.yaml yourself{RESET}")
        else:
            print(f"  {YELLOW}?{RESET} {h['name']!r} — no confident match, search manually")
    if skipped:
        print(f"  {DIM}…and {skipped} more — re-run init after adding some of "
              f"these to see the rest.{RESET}")


def _print_getting_started(target: Path, tickers_file: Path) -> None:
    """Printed once init is done — where things live, what's required
    before the first real command, and what's optional. Not a "next steps"
    checklist someone has to re-derive from four different docs."""
    # Backstop for _resolve_init_target's own (now-loud) warning about why
    # it picked a non-default location: that warning prints before the
    # policy interview even starts, so by the time someone's reading this
    # final summary it may already be several screens back. Repeating the
    # "why" right next to the "Project:" line below means it survives even
    # if that earlier line got scrolled past.
    default_here = target.parent / "factfolio"
    if target.name != "factfolio" and default_here.exists():
        reason = (
            "a file with that exact name — maybe the factfolio executable "
            "itself, if you copied it here"
            if default_here.is_file()
            else "a folder there that isn't a factfolio project"
        )
        print(f"\n{YELLOW}! Landed in {target.name}/, not factfolio/{RESET}"
              f" — {DIM}{default_here} is already {reason}.{RESET}")

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

    print(f"\n{BOLD}3. Run{RESET} factfolio validate")
    print("     it drafts and resolves what it can on its own, and offers "
          "AI-assisted lookup")
    print("     for anything left (a full company name with no ticker, say) — "
          "an unmapped symbol")
    print("     is a hard error on purpose, never a silent .NS guess, but you "
          "shouldn't need to")
    print(f"     hand-edit {tickers_file} yourself unless you want to")

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

    print(f"\n{BOLD}The LLM, in short:{RESET} `report`, `chat`, `init`, and mcp's "
          f"run_portfolio_review call one —")
    print("  your local `claude login` session by default, or export "
          "ANTHROPIC_API_KEY to override.")
    print("  `validate` is pure deterministic Python by default, no LLM — it "
          "only ever asks to call")
    print("  one, interactively, if it finds a holding it can't map, or a "
          "holdings file it can't read,")
    print("  any other way. status/cron/estimate-dates never do.")
    print("  Either way, your holdings and their values never leave this machine.")


def _print_mf_summary() -> None:
    """One line of confirmation that mutual-fund holdings were found and
    valued, printed at the end of `factfolio validate` regardless of
    outcome. `validate` genuinely never needed to touch them — there's no
    yfinance ticker to resolve for a mutual fund the way there is for a
    stock — but saying NOTHING about them left a real user unable to tell
    "correctly parsed, just not this command's job" apart from "silently
    dropped", with no signal at all pointing them toward `status`, which
    is the only place they were ever going to show up.

    Never lets a failure here block/crash validate — best-effort, exactly
    like the AI-assisted offers above it.
    """
    try:
        from mybroker.portfolio.loader import load_portfolio

        portfolio = load_portfolio()
    except Exception:
        return

    if not portfolio.mutual_funds:
        return

    total = sum(m.current_value for m in portfolio.mutual_funds)
    count = len(portfolio.mutual_funds)
    plural = "" if count == 1 else "s"
    print(f"\n{BOLD}Mutual funds{RESET}")
    print(f"  {GREEN}✓{RESET} {count} holding{plural} found "
          f"(₹{total:,.0f} current value) — `factfolio validate` doesn't "
          f"resolve these (no ticker to look up); run {BOLD}factfolio "
          f"status{RESET} to see them.")


def _offer_schema_resolution(unresolvable: list[dict]) -> None:
    """Interactive-only: a holdings_inbox file the deterministic path
    couldn't parse at all — no recognisable header row by keyword, or a
    header it found but whose required columns it couldn't resolve. Both
    are handed to the AI resolver the same way (see portfolio/importers
    .py's module docstring for why this exists instead of another
    hand-tuned heuristic): it isn't told where the header is any more
    than a human opening the file would be — finding it is part of what
    it's asked to do, so this isn't limited to only the narrower of the
    two failure modes. Its proposal is verified against the file's OWN
    data (validate_schema) before it's trusted, and written to the
    durable column-map cache only once it passes, so the SAME format is
    recognised instantly in any future file, no code change and no
    re-asking required. Never called outside an interactive terminal —
    see cmd_validate's own docstring on why `validate` must stay
    deterministic-by-default regardless.
    """
    from mybroker.portfolio.importers import save_column_map, validate_schema

    for item in unresolvable:
        path = item["path"]
        print(f"\n{YELLOW}!{RESET} {path.name}: {item['error']}")
        try:
            answer = input(
                "    Ask an AI agent to read this file and figure out its "
                "structure? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer.startswith("n"):
            continue

        print(f"    {_auth_status_line()}")
        try:
            from mybroker.agents.schema_resolver import resolve_schema

            schema = anyio.run(resolve_schema, item["grid_excerpt"])
        except Exception as exc:
            from mybroker.logging_setup import get_logger

            get_logger(__name__).warning(
                "Schema resolution failed for %s (%s: %s) — leaving it "
                "unparseable.", path.name, type(exc).__name__, exc,
            )
            print(f"    {RED}✗{RESET} Couldn't resolve it: {exc}")
            continue

        if schema.header_row is None or schema.kind is None:
            print(f"    {RED}✗{RESET} The agent couldn't find a holdings "
                  f"table in this file at all.")
            if schema.reasoning:
                print(f"    {DIM}Its reasoning: {schema.reasoning}{RESET}")
            continue

        ok, problems = validate_schema(
            schema.kind, schema.columns, item["grid"], schema.header_row
        )
        if not ok:
            print(f"    {RED}✗{RESET} The agent's proposed mapping didn't "
                  f"check out against the file's real data:")
            for p in problems:
                print(f"      - {p}")
            if schema.reasoning:
                print(f"    {DIM}Its reasoning: {schema.reasoning}{RESET}")
            continue

        header = item["grid"][schema.header_row]
        print(f"    {GREEN}✓{RESET} Found the table at row "
              f"{schema.header_row} — mapped as {BOLD}{schema.kind}{RESET} "
              f"({schema.confidence} confidence)")
        for field_name, col in schema.columns.items():
            if col is not None:
                print(f"      {field_name:<15} → column {col} ({header[col]!r})")
        if schema.reasoning:
            print(f"    {DIM}{schema.reasoning}{RESET}")

        save_column_map(
            header, kind=schema.kind, columns=schema.columns,
            source=path.name, confidence=schema.confidence,
            reasoning=schema.reasoning,
        )
        print(f"    {DIM}Learned for future files with this same header — "
              f"see .cache/column_maps.json.{RESET}")


def cmd_validate(_args) -> int:
    """Re-resolve tickers. The gate that must pass before any agent run.

    Previously, a holding that only carries a full company name (a demat
    statement's "Scrip Name" column, say) needed a completely separate trip
    back to `factfolio init` to get AI-resolved — `validate` on its own
    could only fail and point at that command by name, never do anything
    about it itself. That's the exact "duplicate work" a real user hit:
    init once with no holdings yet, add holdings, validate (fails), init
    again (only now does anything), validate again. This collapses that
    down to one command: on a failure caused specifically by unmapped
    full-name holdings, offer the same AI-assisted resolver right here —
    interactively only, since `validate` is documented elsewhere (the
    `init` summary, the README) as pure deterministic Python with no LLM
    call, and that promise must stay true for anyone piping/scripting this
    (a non-interactive run just gets today's exact behaviour: fail, name
    what's missing, point at `factfolio init`).

    Also offers AI-assisted schema (column) resolution for any
    holdings_inbox file whose required columns couldn't be found at all —
    a genuinely novel export format, not just an unmapped name — same
    interactive-only, deterministic-by-default discipline. Run BEFORE the
    ticker gate below: a file whose columns can't be read has nothing
    valid to offer tickers.yaml in the first place.

    Prints a mutual-fund summary before returning, on every exit path,
    pass or fail: `validate` is specifically the EQUITY ticker-resolution
    gate — a mutual fund doesn't need yfinance ticker resolution the way
    a stock does, so it was never part of what this command checks — but
    printing NOTHING about mutual funds here made it look, to a real
    user, like they'd been silently dropped or ignored, when they were
    actually read and valued correctly the entire time. This is a pointer
    to where to actually see them (`factfolio status`), not a second pass
    over their data.
    """
    from mybroker.portfolio.importers import find_unresolvable_files
    from mybroker.tickers_validate import main as validate_main

    def _finish(rc: int) -> int:
        _print_mf_summary()
        return rc

    unresolvable = find_unresolvable_files()
    if unresolvable and sys.stdin.isatty():
        _offer_schema_resolution(unresolvable)

    rc = validate_main()
    if rc == 0:
        return _finish(rc)

    unmapped = ticker_seeding.collect_unmapped_holdings()
    if not unmapped or not sys.stdin.isatty():
        return _finish(rc)

    print(f"\n{_auth_status_line()}")
    try:
        answer = input(
            f"Resolve {len(unmapped)} of these now via AI-assisted lookup? [Y/n] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return _finish(rc)
    if answer.startswith("n"):
        return _finish(rc)

    _suggest_ticker_matches()

    print(f"\n{DIM}Re-running validate…{RESET}")
    return _finish(validate_main())


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
        prog="factfolio",
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

    # Every command except init/bare `factfolio` (welcome) must refuse
    # outright rather than scaffold a second, empty project layout beside
    # the real one — see _standing_beside_own_project's own docstring.
    # Checked BEFORE anything else touches disk (ensure_dirs(), the logger
    # below, all of it), since that's the only way to guarantee nothing
    # gets created at the wrong root.
    if args.command not in (None, "init") and _standing_beside_own_project():
        target = Path.cwd() / "factfolio"
        print(f"{YELLOW}! Your factfolio project is in {target}, not here "
              f"— run `cd {target.name}` first.{RESET}", file=sys.stderr)
        print(f"  {DIM}Running `factfolio {args.command}` from {Path.cwd()} "
              f"would otherwise scaffold a second, empty copy of logs/"
              f"reports/memory/holdings_inbox right beside it.{RESET}",
              file=sys.stderr)
        return 1

    from mybroker.logging_setup import get_logger

    # init/welcome can redirect PROJECT_ROOT mid-flight (into a nested
    # `factfolio/` folder they reuse or create — see _resolve_init_target).
    # Logging here, before that redirect, would scaffold logs/ (and, via
    # ensure_dirs(), memory/reports/holdings_inbox/ too) at the OLD root —
    # exactly the stray-duplicate-folder bug this whole check exists to
    # prevent, just self-inflicted via the logger instead of a subcommand.
    # Every other command's PROJECT_ROOT is stable for the whole process,
    # so logging "started" up front is safe for those.
    if args.command not in (None, "init"):
        get_logger("mybroker.cli").info("factfolio %s started", args.command)

    try:
        rc = args.func(args)
        get_logger("mybroker.cli").info(
            "factfolio %s finished (exit %d)", args.command, rc
        )
        return rc
    except FileNotFoundError as exc:
        # Expected, user-actionable states (no holdings yet, no policy
        # file) — not a crash, so a WARNING here, not an ERROR/traceback.
        get_logger("mybroker.cli").warning("factfolio %s: %s", args.command, exc)
        print(f"{RED}✗{RESET} {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        get_logger("mybroker.cli").info("factfolio %s interrupted", args.command)
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
        get_logger("mybroker.cli").error(
            "factfolio %s failed: %s: %s%s", args.command,
            type(exc).__name__, exc,
            f" (full traceback: {log_file})" if log_file else "",
        )
        print(f"\n{RED}✗{RESET} `factfolio {args.command}` failed: "
              f"{friendly_message(exc)}", file=sys.stderr)
        if log_file:
            print(f"  {DIM}Full traceback: {log_file}{RESET}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
