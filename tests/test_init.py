"""`factfolio init` — first-run setup.

Covers:
1. The folder-nesting behaviour: `init` creates a dedicated `factfolio/`
   project folder under wherever it's run from (like `git clone` or
   `create-react-app`), rather than scattering runtime dirs into the
   current directory — see cli._resolve_init_target's docstring for the
   full decision tree.
2. tickers.yaml being seeded into that project folder from the bundled
   defaults (never read in place — see config.py's PyInstaller-onefile
   note on why).
3. The 4-question policy interview (interactive terminals, brand-new
   policy file only) and its fallback to the generic template otherwise.
   The interview's f-string template embeds literal `{months: ...}` YAML
   mappings — a regression test locks in that they're escaped correctly
   rather than misparsed as format placeholders (the old `.replace()`
   implementation had exactly this bug once already).
"""

from __future__ import annotations

import pytest
import yaml

from mybroker.portfolio.policy import Policy
from mybroker.ui.cli import (
    _looks_initialized,
    _render_policy_template,
    _resolve_init_target,
    _run_policy_interview,
    cmd_init,
)


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    """Run every test from inside an empty tmp_path — never the real
    project's own directory. Also blocks real network/agent calls from
    cmd_init's ticker-resolution steps (see _suggest_ticker_matches) — no
    test here should depend on, or be slowed or flaked by, live
    network/Yahoo/claude behavior (this machine may genuinely have a
    working `claude` login, which would otherwise make the M7 agent path
    actually run for real inside a unit test). Tests that care about
    either the agent or plain-search suggestion path override these
    explicitly."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mybroker.config.suggest_ticker_for_name", lambda name: None)

    async def _no_agent(_holdings):
        raise RuntimeError("agent path disabled in tests by default")

    monkeypatch.setattr("mybroker.agents.ticker_resolver.resolve_names", _no_agent)
    yield tmp_path


def _policy_file(project_dir):
    return project_dir / "memory" / "investment_policy.md"


def test_nests_a_factfolio_folder_under_cwd(isolated_project):
    assert cmd_init(None) == 0

    project_dir = isolated_project / "factfolio"
    assert project_dir.is_dir()
    assert _policy_file(project_dir).exists()
    # Nothing written directly into the cwd itself.
    assert not (isolated_project / "memory").exists()


def test_writes_a_parseable_policy_file(isolated_project):
    assert cmd_init(None) == 0
    policy_file = _policy_file(isolated_project / "factfolio")

    policy = Policy.load(policy_file)
    assert policy.target_cagr_pct == 12.0
    assert policy.core_min_pct == 60.0
    assert policy.max_position_pct == 8.0
    assert policy.speculative_symbols == []


def test_does_not_overwrite_an_existing_policy(isolated_project):
    project_dir = isolated_project / "factfolio"
    policy_file = _policy_file(project_dir)
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text("MY CUSTOM POLICY — do not touch")

    cmd_init(None)

    assert policy_file.read_text() == "MY CUSTOM POLICY — do not touch"


def test_stamps_todays_date_in_the_changelog(isolated_project):
    from datetime import date

    cmd_init(None)
    text = _policy_file(isolated_project / "factfolio").read_text()
    assert date.today().isoformat() in text
    assert "__TODAY__" not in text  # no stray template placeholder left behind


def test_idempotent_across_repeated_runs(isolated_project):
    assert cmd_init(None) == 0
    assert cmd_init(None) == 0
    assert cmd_init(None) == 0

    project_dir = isolated_project / "factfolio"
    Policy.load(_policy_file(project_dir))  # still parses fine after N re-runs
    # And still only one project folder, not factfolio-2/-3/...
    assert sorted(p.name for p in isolated_project.iterdir()) == ["factfolio"]


def test_rerun_from_inside_the_project_stays_there(isolated_project, monkeypatch):
    """Once you've `cd`ed into the project factfolio/ printed, re-running
    init from inside it must refresh in place — not nest a second copy."""
    cmd_init(None)
    project_dir = isolated_project / "factfolio"
    monkeypatch.chdir(project_dir)

    cmd_init(None)

    assert not (project_dir / "factfolio").exists()


def test_looks_initialized_requires_a_real_policy_file(tmp_path):
    assert not _looks_initialized(tmp_path)
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "investment_policy.md").write_text("x")
    assert _looks_initialized(tmp_path)


def test_resolve_target_reuses_an_existing_factfolio_project(tmp_path):
    project_dir = tmp_path / "factfolio"
    (project_dir / "memory").mkdir(parents=True)
    (project_dir / "memory" / "investment_policy.md").write_text("x")

    assert _resolve_init_target(tmp_path) == project_dir


def test_resolve_target_copies_instead_of_crashing_on_a_stray_file(tmp_path):
    """Regression: a plain FILE named 'factfolio' (e.g. left over from an
    old download) previously crashed with NotADirectoryError when 'init'
    tried to mkdir memory/ inside it. There's no in-place option that
    doesn't mean deleting the user's file, so always make way instead."""
    stray_file = tmp_path / "factfolio"
    stray_file.write_text("not a project, not even a folder")

    target = _resolve_init_target(tmp_path)

    assert target == tmp_path / "factfolio-2"
    assert stray_file.is_file()
    assert stray_file.read_text() == "not a project, not even a folder"  # untouched


def test_init_end_to_end_with_a_stray_file_in_the_way(isolated_project):
    """cmd_init itself must not crash in this scenario either."""
    (isolated_project / "factfolio").write_text("stray file, not a folder")

    assert cmd_init(None) == 0

    project_dir = isolated_project / "factfolio-2"
    assert _policy_file(project_dir).exists()


def test_resolve_target_copies_instead_of_clobbering_an_unrelated_folder(
    tmp_path, monkeypatch
):
    """A pre-existing 'factfolio' folder that ISN'T one of our projects
    (no memory/investment_policy.md) must never be silently written into."""
    unrelated = tmp_path / "factfolio"
    unrelated.mkdir()
    (unrelated / "some_unrelated_file.txt").write_text("not ours")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)  # non-interactive

    target = _resolve_init_target(tmp_path)

    assert target == tmp_path / "factfolio-2"
    assert (unrelated / "some_unrelated_file.txt").read_text() == "not ours"  # untouched


# ── "forgot to cd" guard ─────────────────────────────────────────────────────
# Regression coverage for a real bug: running any command other than
# init/bare `factfolio` from beside an already-initialized nested
# `factfolio/` project used to silently scaffold a second, empty logs/
# reports/memory/holdings_inbox layout right there — every file in the
# *real* project's holdings_inbox/, every tickers.yaml entry, invisible
# from that vantage point. See _standing_beside_own_project's docstring.

def test_standing_beside_own_project_true_when_forgot_to_cd(isolated_project):
    from mybroker.ui.cli import _standing_beside_own_project

    project_dir = isolated_project / "factfolio"
    (project_dir / "memory").mkdir(parents=True)
    (project_dir / "memory" / "investment_policy.md").write_text("x")

    assert _standing_beside_own_project() is True


def test_standing_beside_own_project_false_when_already_inside(isolated_project):
    from mybroker.ui.cli import _standing_beside_own_project

    (isolated_project / "memory").mkdir(parents=True)
    (isolated_project / "memory" / "investment_policy.md").write_text("x")

    assert _standing_beside_own_project() is False


def test_standing_beside_own_project_false_when_no_nested_project(isolated_project):
    from mybroker.ui.cli import _standing_beside_own_project

    assert _standing_beside_own_project() is False


def test_main_refuses_other_commands_when_standing_beside_own_project(
    isolated_project, capsys
):
    from mybroker import config
    from mybroker.ui.cli import main

    project_dir = isolated_project / "factfolio"
    (project_dir / "memory").mkdir(parents=True)
    (project_dir / "memory" / "investment_policy.md").write_text("x")
    config.set_project_root(isolated_project)  # as a fresh process would see it

    rc = main(["status"])

    assert rc == 1
    err = capsys.readouterr().err
    assert f"cd {project_dir.name}" in err
    # No stray scaffold created beside the real project.
    assert not (isolated_project / "logs").exists()
    assert not (isolated_project / "reports").exists()
    assert not (isolated_project / "memory").exists()  # only inside factfolio/


def test_main_init_is_never_blocked_by_the_guard(isolated_project):
    """init/welcome redirect themselves into the nested project on purpose
    (see _resolve_init_target) — must stay exempt from the guard, and must
    not scaffold anything beside the real project either (the bug this
    closes: get_logger()'s own ensure_dirs() firing before that redirect)."""
    from mybroker import config
    from mybroker.ui.cli import main

    project_dir = isolated_project / "factfolio"
    (project_dir / "memory").mkdir(parents=True)
    (project_dir / "memory" / "investment_policy.md").write_text("MY POLICY")
    (project_dir / "tickers.yaml").write_text("symbols: {}\nindices: {}\n")
    config.set_project_root(isolated_project)

    rc = main(["init"])

    assert rc == 0
    assert not (isolated_project / "logs").exists()
    assert not (isolated_project / "reports").exists()
    # Real project untouched, and correctly the one logging landed in.
    assert (project_dir / "memory" / "investment_policy.md").read_text() == "MY POLICY"
    assert "init finished" in (project_dir / "logs" / "factfolio.log").read_text()


# ── tickers.yaml seeding ─────────────────────────────────────────────────────

def test_seeds_tickers_yaml_from_bundled_defaults(isolated_project):
    from mybroker.config import DEFAULT_TICKERS_FILE

    assert cmd_init(None) == 0

    project_tickers = isolated_project / "factfolio" / "tickers.yaml"
    assert project_tickers.exists()
    assert project_tickers.read_text() == DEFAULT_TICKERS_FILE.read_text()


def test_does_not_overwrite_an_existing_tickers_yaml(isolated_project):
    project_dir = isolated_project / "factfolio"
    (project_dir / "memory").mkdir(parents=True)
    (project_dir / "memory" / "investment_policy.md").write_text("MY POLICY")
    (project_dir / "tickers.yaml").write_text("symbols: {CUSTOM: {}}")

    cmd_init(None)

    assert (project_dir / "tickers.yaml").read_text() == "symbols: {CUSTOM: {}}"


# ── Draft tickers.yaml entries seeded from holdings ──────────────────────────
# Real-world request: rather than seeding tickers.yaml purely from the
# maintainer's own bundled portfolio, scan the holdings you've actually
# dropped in and draft entries for whatever's found — genuine short-symbol
# sources only (see importers.discover_equity_symbols_for_drafting's own
# docstring for why a full-company-name column can't be drafted safely).

_BASE_TICKERS_YAML = (
    "symbols:\n"
    "  EXISTING:\n"
    "    name: Existing Corp\n"
    "    candidates: [EXISTING.NS]\n"
    "    sector: Tech\n"
    "    tier: large\n"
    "    bucket: core\n"
    "\n"
    "indices:\n"
    '  NIFTY50: { name: Nifty 50, candidates: ["^NSEI"] }\n'
)

_HOLDINGS_CSV_HEADER = (
    '"Instrument","Qty.","Avg. cost","LTP","Invested","Cur. val","P&L",'
    '"Net chg.","Day chg.",""\n'
)


def _init_project(isolated_project, tickers_yaml_text):
    """A pre-initialized project folder with specific tickers.yaml content,
    so cmd_init() reuses it in place rather than creating a fresh nested
    copy — see _resolve_init_target."""
    project_dir = isolated_project / "factfolio"
    (project_dir / "memory").mkdir(parents=True)
    (project_dir / "memory" / "investment_policy.md").write_text("MY POLICY")
    (project_dir / "tickers.yaml").write_text(tickers_yaml_text)
    return project_dir


def test_drafts_a_new_symbol_from_root_holdings_csv(isolated_project):
    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings.csv").write_text(
        _HOLDINGS_CSV_HEADER + '"NEWSTOCK",10,100.0,110.0,1000.0,1100.0,100.0,10.0,0,""\n'
    )

    cmd_init(None)

    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert data["symbols"]["NEWSTOCK"]["sector"] == "Unknown"
    assert data["symbols"]["NEWSTOCK"]["candidates"] == ["NEWSTOCK.NS", "NEWSTOCK.BO"]
    assert "EXISTING" in data["symbols"]  # untouched
    assert data["indices"]["NIFTY50"]["name"] == "Nifty 50"  # untouched


def test_drafts_a_new_symbol_from_holdings_inbox(isolated_project):
    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "broker.csv").write_text(
        "Symbol,Qty,Avg Price,Invested,Current Value\nNEWSTOCK,10,100.0,1000.0,1100.0\n"
    )

    cmd_init(None)

    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert "NEWSTOCK" in data["symbols"]


def test_does_not_redraft_an_already_mapped_symbol(isolated_project):
    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings.csv").write_text(
        _HOLDINGS_CSV_HEADER + '"EXISTING",10,100.0,110.0,1000.0,1100.0,100.0,10.0,0,""\n'
    )
    before = (project_dir / "tickers.yaml").read_text()

    cmd_init(None)

    assert (project_dir / "tickers.yaml").read_text() == before  # unchanged


def test_does_not_draft_a_full_company_name_column(isolated_project):
    """Sharekhan-style 'Scrip Name' is a full company name, not a genuine
    trading symbol — guessing HDFCBANK from "HDFC BANK LTD." is exactly
    the kind of guess this project refuses to make."""
    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "HDFC BANK LTD.,4,851.71,3406.85,731.55,2926.20,-480.64,-0.56\n"
    )

    cmd_init(None)

    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert set(data["symbols"]) == {"EXISTING"}  # nothing drafted


def test_plain_fallback_suggests_but_never_writes(
    isolated_project, monkeypatch, capsys
):
    """A full-name-only holding never gets auto-drafted. With the agent
    path unavailable (isolated_project's default), init should still
    surface a plain yfinance-search suggestion — never written to
    tickers.yaml itself."""
    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "AXIS BANK LIMITED,39,1331.66,51934.82,1161.30,45290.70,-6644.04,-4.99\n"
    )
    monkeypatch.setattr(
        "mybroker.config.suggest_ticker_for_name",
        lambda name: "AXISBANK.NS" if "AXIS" in name else None,
    )

    assert cmd_init(None) == 0

    out = capsys.readouterr().out
    assert "AXIS BANK LIMITED" in out
    assert "AXISBANK.NS" in out
    # Still never auto-written.
    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert set(data["symbols"]) == {"EXISTING"}


def test_agent_path_auto_writes_a_high_confidence_resolution(
    isolated_project, monkeypatch, capsys
):
    """A validated, high-confidence, non-duplicate agent resolution gets
    written straight to tickers.yaml — a materially better entry than the
    plain fallback's bare guess, since it carries a real sector from the
    same evidence the agent was validated against."""
    from mybroker.agents.ticker_resolver import ResolvedName

    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "AXIS BANK LIMITED,39,1331.66,51934.82,1161.30,45290.70,-6644.04,-4.99\n"
    )

    async def _fake_resolve(holdings):
        return [ResolvedName(
            name="AXIS BANK LIMITED", symbol="AXISBANK.NS", confidence="high",
            reasoning="Exact NSE match.", sector="Financial Services",
            company_name="Axis Bank Limited",
        )]

    monkeypatch.setattr("mybroker.agents.ticker_resolver.resolve_names", _fake_resolve)

    cmd_init(None)

    out = capsys.readouterr().out
    assert "AXISBANK.NS" in out
    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert "AXISBANK" in data["symbols"]
    assert data["symbols"]["AXISBANK"]["sector"] == "Financial Services"
    assert data["symbols"]["AXISBANK"]["candidates"] == ["AXISBANK.NS"]
    assert data["symbols"]["AXISBANK"]["tier"] == "unknown"  # still your own call


def test_agent_path_backfills_an_untouched_draft_entry(
    isolated_project, monkeypatch, capsys
):
    """Real-world case this closes: holdings.csv auto-drafts HDFCBANK with
    a placeholder `name: HDFCBANK`; a demat PDF's 'HDFC BANK LTD' row
    can't be name-matched to it until that placeholder is a real name. The
    agent resolving to the SAME symbol an existing DRAFT already claims,
    with high confidence, is safe to backfill in place — the placeholder
    name proves no human has touched that entry yet."""
    from mybroker.agents.ticker_resolver import ResolvedName

    tickers_yaml = (
        "symbols:\n"
        "  HDFCBANK:\n"
        "    name: HDFCBANK  # TODO: replace with the real company name\n"
        "    candidates: [HDFCBANK.NS, HDFCBANK.BO]  # DRAFT — unverified guess\n"
        "    sector: Unknown  # TODO\n"
        "    tier: unknown  # TODO: large | mid | small\n"
        "    bucket: satellite  # TODO: core | satellite\n"
        "\n"
        "indices:\n"
        '  NIFTY50: { name: Nifty 50, candidates: ["^NSEI"] }\n'
    )
    project_dir = _init_project(isolated_project, tickers_yaml)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "demat.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "HDFC BANK LTD,74,925.41,68480.19,731.55,54134.70,-14345.64,-15.50\n"
    )

    async def _fake_resolve(holdings):
        return [ResolvedName(
            name="HDFC BANK LTD", symbol="HDFCBANK.NS", confidence="high",
            reasoning="Exact NSE match; already a DRAFT entry for HDFCBANK.",
            sector="Financial Services", company_name="HDFC Bank Limited",
        )]

    monkeypatch.setattr("mybroker.agents.ticker_resolver.resolve_names", _fake_resolve)

    cmd_init(None)

    out = capsys.readouterr().out
    assert "backfilled" in out
    text = (project_dir / "tickers.yaml").read_text()
    assert text.count("HDFCBANK:") == 1  # not duplicated
    data = yaml.safe_load(text)
    assert data["symbols"]["HDFCBANK"]["name"] == "HDFC Bank Limited"
    assert data["symbols"]["HDFCBANK"]["sector"] == "Financial Services"
    # Untouched — still the deterministic guess, still your own call.
    assert data["symbols"]["HDFCBANK"]["candidates"] == ["HDFCBANK.NS", "HDFCBANK.BO"]
    assert data["symbols"]["HDFCBANK"]["bucket"] == "satellite"


def test_agent_path_does_not_backfill_an_already_named_entry(
    isolated_project, monkeypatch, capsys
):
    """The TCS/TATAPOWER real-world case: an existing entry that already
    carries a real name (from an earlier resolver run, say) is NOT a
    pristine draft — must stay hands-off, same as before, even at high
    confidence."""
    from mybroker.agents.ticker_resolver import ResolvedName

    tickers_yaml = (
        "symbols:\n"
        "  TCS:\n"
        "    name: Tata Consultancy Services Limited\n"
        "    candidates: [TCS.NS]  # resolved by the init agent — verified, not guessed\n"
        "    sector: Technology\n"
        "    tier: unknown\n"
        "    bucket: satellite\n"
        "\n"
        "indices:\n"
        '  NIFTY50: { name: Nifty 50, candidates: ["^NSEI"] }\n'
    )
    project_dir = _init_project(isolated_project, tickers_yaml)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "demat.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "TATA CONSULTANCY SERV LT,5,3189.86,15949.29,2358.90,11794.50,-4154.80,-1.30\n"
    )

    async def _fake_resolve(holdings):
        return [ResolvedName(
            name="TATA CONSULTANCY SERV LT", symbol="TCS.NS", confidence="high",
            reasoning="Abbreviated name, exact match.", sector="Technology",
            company_name="Tata Consultancy Services Limited",
        )]

    monkeypatch.setattr("mybroker.agents.ticker_resolver.resolve_names", _fake_resolve)

    cmd_init(None)

    out = capsys.readouterr().out
    assert "backfilled" not in out
    assert "is already in tickers.yaml" in out
    before = tickers_yaml
    after = (project_dir / "tickers.yaml").read_text()
    assert yaml.safe_load(before) == yaml.safe_load(after)  # byte-for-byte unchanged


def test_agent_path_does_not_write_medium_or_low_confidence(
    isolated_project, monkeypatch, capsys
):
    from mybroker.agents.ticker_resolver import ResolvedName

    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "SOME COMPANY LTD,10,100,1000,110,1100,100,10\n"
    )

    async def _fake_resolve(holdings):
        return [ResolvedName(
            name="SOME COMPANY LTD", symbol="SOMECO.BO", confidence="medium",
            reasoning="Only a BSE listing found, some ambiguity.",
        )]

    monkeypatch.setattr("mybroker.agents.ticker_resolver.resolve_names", _fake_resolve)

    cmd_init(None)

    out = capsys.readouterr().out
    assert "medium confidence" in out
    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert set(data["symbols"]) == {"EXISTING"}


# ── Interactive follow-up on a medium/low-confidence match ──────────────────
# Only offered in an interactive terminal (see test above for the
# non-interactive default: prints the '?' line and stops there). Lets a
# human accept the agent's own guess, type a different symbol, or skip —
# right there, instead of a printed line that's easy to miss.

def _uncertain_resolve(name="SOME COMPANY LTD", symbol="SOMECO.BO", confidence="medium"):
    from mybroker.agents.ticker_resolver import ResolvedName

    async def _fake_resolve(holdings):
        return [ResolvedName(
            name=name, symbol=symbol, confidence=confidence,
            reasoning="Only a BSE listing found, some ambiguity.",
            sector="Technology", company_name="Some Company Limited",
        )]
    return _fake_resolve


def test_interactive_accept_writes_the_suggested_symbol(
    isolated_project, monkeypatch, capsys
):
    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "SOME COMPANY LTD,10,100,1000,110,1100,100,10\n"
    )
    monkeypatch.setattr(
        "mybroker.agents.ticker_resolver.resolve_names", _uncertain_resolve()
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "a")

    cmd_init(None)

    out = capsys.readouterr().out
    assert "added" in out and "SOMECO.BO" in out and "tickers.yaml" in out
    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert data["symbols"]["SOMECO"]["name"] == "Some Company Limited"
    assert data["symbols"]["SOMECO"]["candidates"] == ["SOMECO.BO"]


def test_interactive_edit_writes_the_typed_symbol_instead(
    isolated_project, monkeypatch, capsys
):
    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "SOME COMPANY LTD,10,100,1000,110,1100,100,10\n"
    )
    monkeypatch.setattr(
        "mybroker.agents.ticker_resolver.resolve_names", _uncertain_resolve()
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter(["e", "SOMECO.NS"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: next(answers))

    cmd_init(None)

    out = capsys.readouterr().out
    assert "added" in out and "SOMECO.NS" in out and "tickers.yaml" in out
    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    # Your own typed symbol, not the agent's original guess or its
    # unverified company_name/sector for a DIFFERENT ticker.
    assert data["symbols"]["SOMECO"]["candidates"] == ["SOMECO.NS"]
    assert data["symbols"]["SOMECO"]["name"] == "SOME COMPANY LTD"
    assert data["symbols"]["SOMECO"]["sector"] == "Unknown"


def test_interactive_skip_writes_nothing(isolated_project, monkeypatch, capsys):
    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "SOME COMPANY LTD,10,100,1000,110,1100,100,10\n"
    )
    monkeypatch.setattr(
        "mybroker.agents.ticker_resolver.resolve_names", _uncertain_resolve()
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "s")

    cmd_init(None)

    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert set(data["symbols"]) == {"EXISTING"}


def test_interactive_blank_input_defaults_to_skip(isolated_project, monkeypatch):
    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "SOME COMPANY LTD,10,100,1000,110,1100,100,10\n"
    )
    monkeypatch.setattr(
        "mybroker.agents.ticker_resolver.resolve_names", _uncertain_resolve()
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")

    cmd_init(None)

    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert set(data["symbols"]) == {"EXISTING"}


def test_interactive_edit_never_overwrites_an_existing_symbol(
    isolated_project, monkeypatch, capsys
):
    """Typing a symbol at the edit prompt that's already mapped must never
    silently clobber it — same "never touch what's already there" rule as
    everywhere else. (A symbol the agent suggests directly, before any
    prompt, is a different, earlier-handled collision — see
    test_agent_path_does_not_write_medium_or_low_confidence and
    test_agent_path_does_not_write_a_flagged_duplicate; this test is
    specifically about what you type in response to the prompt.)"""
    tickers_yaml = _BASE_TICKERS_YAML.replace(
        "indices:",
        "  ALREADYHERE:\n    name: Already Here Ltd\n    candidates: [ALREADYHERE.NS]\n\nindices:",
    )
    project_dir = _init_project(isolated_project, tickers_yaml)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "SOME COMPANY LTD,10,100,1000,110,1100,100,10\n"
    )
    monkeypatch.setattr(
        "mybroker.agents.ticker_resolver.resolve_names", _uncertain_resolve()
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter(["e", "ALREADYHERE.NS"])  # types an already-mapped symbol
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: next(answers))

    cmd_init(None)

    out = capsys.readouterr().out
    assert "not overwriting it" in out
    before = yaml.safe_load(tickers_yaml)["symbols"]["ALREADYHERE"]
    after = yaml.safe_load(
        (project_dir / "tickers.yaml").read_text()
    )["symbols"]["ALREADYHERE"]
    assert before == after


def test_agent_path_does_not_write_a_flagged_duplicate(
    isolated_project, monkeypatch, capsys
):
    """The NTPC LIMITED / NTPC LTD real-world case: the agent flags the
    second as the same holding as the first — must not get its own
    tickers.yaml entry."""
    from mybroker.agents.ticker_resolver import ResolvedName

    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "NTPC LIMITED,50,356.94,17847.11,370.65,18532.50,685.50,1.92\n"
        "NTPC LTD,50,376.04,18802.21,370.65,18532.50,-269.50,-0.72\n"
    )

    async def _fake_resolve(holdings):
        return [
            ResolvedName(name="NTPC LIMITED", symbol="NTPC.NS", confidence="high",
                         reasoning="Clear match."),
            ResolvedName(name="NTPC LTD", symbol="NTPC.NS", confidence="high",
                         reasoning="Same holding as NTPC LIMITED — identical quantity.",
                         duplicate_of="NTPC LIMITED"),
        ]

    monkeypatch.setattr("mybroker.agents.ticker_resolver.resolve_names", _fake_resolve)

    cmd_init(None)

    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert list(data["symbols"]).count("NTPC") == 1
    out = capsys.readouterr().out
    assert "same holding as" in out


def test_agent_path_never_double_writes_the_same_symbol_even_if_unflagged(
    isolated_project, monkeypatch, capsys
):
    """Code-level backstop: even if the agent fails to set duplicate_of
    itself, two names resolving to the same symbol must never both get
    written — the second becomes a review item instead of silently
    clobbering the first entry."""
    from mybroker.agents.ticker_resolver import ResolvedName

    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings_inbox").mkdir()
    (project_dir / "holdings_inbox" / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "NTPC LIMITED,50,356.94,17847.11,370.65,18532.50,685.50,1.92\n"
        "NTPC LTD,50,376.04,18802.21,370.65,18532.50,-269.50,-0.72\n"
    )

    async def _fake_resolve(holdings):
        return [
            ResolvedName(name="NTPC LIMITED", symbol="NTPC.NS", confidence="high",
                         reasoning="Clear match."),
            ResolvedName(name="NTPC LTD", symbol="NTPC.NS", confidence="high",
                         reasoning="Also looks like a clear match."),  # not flagged
        ]

    monkeypatch.setattr("mybroker.agents.ticker_resolver.resolve_names", _fake_resolve)

    cmd_init(None)

    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert list(data["symbols"]).count("NTPC") == 1


def test_rerun_drafts_newly_added_holdings_only(isolated_project):
    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings.csv").write_text(
        _HOLDINGS_CSV_HEADER + '"FIRSTSTOCK",10,100.0,110.0,1000.0,1100.0,100.0,10.0,0,""\n'
    )
    cmd_init(None)  # drafts FIRSTSTOCK

    (project_dir / "holdings.csv").write_text(
        _HOLDINGS_CSV_HEADER
        + '"FIRSTSTOCK",10,100.0,110.0,1000.0,1100.0,100.0,10.0,0,""\n'
        + '"SECONDSTOCK",5,200.0,210.0,1000.0,1050.0,50.0,5.0,0,""\n'
    )
    cmd_init(None)  # should draft SECONDSTOCK only, not re-add FIRSTSTOCK

    text = (project_dir / "tickers.yaml").read_text()
    data = yaml.safe_load(text)
    assert {"EXISTING", "FIRSTSTOCK", "SECONDSTOCK"} <= set(data["symbols"])
    assert text.count("FIRSTSTOCK:") == 1


def test_yaml_stays_valid_after_multiple_drafts_in_one_run(isolated_project):
    project_dir = _init_project(isolated_project, _BASE_TICKERS_YAML)
    (project_dir / "holdings.csv").write_text(
        _HOLDINGS_CSV_HEADER
        + '"AAA",1,1,1,1,1,0,0,0,""\n'
        + '"BBB",1,1,1,1,1,0,0,0,""\n'
        + '"CCC",1,1,1,1,1,0,0,0,""\n'
    )

    cmd_init(None)

    data = yaml.safe_load((project_dir / "tickers.yaml").read_text())
    assert {"AAA", "BBB", "CCC"} <= set(data["symbols"])
    assert "NIFTY50" in data["indices"]  # still parses, still intact


# ── Policy interview ─────────────────────────────────────────────────────────

def test_render_policy_template_generic_matches_old_defaults(tmp_path):
    """No answers → the same numbers the plain template always shipped."""
    path = tmp_path / "policy.md"
    path.write_text(_render_policy_template(None))

    policy = Policy.load(path)
    assert policy.target_cagr_pct == 12.0
    assert policy.core_min_pct == 60.0
    assert policy.max_position_pct == 8.0
    assert policy.speculative_symbols == []
    assert "__TODAY__" not in path.read_text()


def test_render_policy_template_from_interview_answers(tmp_path):
    answers = {
        "target_cagr_pct": 14.0, "risk": "aggressive", "horizon_years": 20.0,
        "monthly_capital": 15000.0, "core_min_pct": 40.0, "core_max_pct": 55.0,
        "max_position_pct": 10.0, "max_satellite_position_pct": 7.0,
        "max_sector_pct": 30.0, "speculative_cap_pct": 15.0,
        "min_positions": 6, "max_positions": 30, "max_annual_turnover_pct": 35.0,
    }
    path = tmp_path / "policy.md"
    text = _render_policy_template(answers)
    path.write_text(text)

    # Regression: the interview template is an f-string with literal
    # {months: ...} YAML mappings embedded — must render as literal braces,
    # not crash or get swallowed as a format placeholder.
    assert "{months: 6,  core_pct: 30.0}" in text

    policy = Policy.load(path)
    assert policy.target_cagr_pct == 14.0
    assert policy.core_min_pct == 40.0
    assert policy.core_max_pct == 55.0
    assert policy.max_position_pct == 10.0
    assert policy.monthly_capital == 15000.0
    assert policy.min_positions == 6
    assert "aggressive" in text


def test_interview_returns_answers_with_risk_preset(monkeypatch):
    answers_iter = iter(["13", "aggressive", "20", "15000"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: next(answers_iter))

    result = _run_policy_interview()

    assert result["target_cagr_pct"] == 13.0
    assert result["risk"] == "aggressive"
    assert result["horizon_years"] == 20.0
    assert result["monthly_capital"] == 15000.0
    assert result["core_min_pct"] == 40.0  # aggressive preset


def test_interview_blank_input_takes_every_default(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")

    result = _run_policy_interview()

    assert result["target_cagr_pct"] == 12.0
    assert result["risk"] == "moderate"
    assert result["horizon_years"] == 10.0
    assert result["monthly_capital"] == 0.0


def test_interview_falls_back_to_default_on_garbage_input(monkeypatch):
    answers_iter = iter(["not-a-number", "yolo-risk", "", ""])
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: next(answers_iter))

    result = _run_policy_interview()

    assert result["target_cagr_pct"] == 12.0  # invalid float -> default
    assert result["risk"] == "moderate"  # invalid choice -> default


def test_interview_returns_none_on_eof_instead_of_crashing(monkeypatch):
    def _raise_eof(*_a, **_kw):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)

    assert _run_policy_interview() is None


def test_init_runs_the_interview_when_interactive(isolated_project, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers_iter = iter(["9", "conservative", "5", "10000"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: next(answers_iter))

    assert cmd_init(None) == 0

    policy = Policy.load(_policy_file(isolated_project / "factfolio"))
    assert policy.target_cagr_pct == 9.0
    assert policy.core_min_pct == 70.0  # conservative preset
    assert policy.monthly_capital == 10000.0


def test_init_skips_the_interview_when_not_interactive(isolated_project, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert cmd_init(None) == 0

    policy = Policy.load(_policy_file(isolated_project / "factfolio"))
    assert policy.target_cagr_pct == 12.0  # generic default, no interview ran
