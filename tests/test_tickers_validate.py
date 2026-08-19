"""`factfolio validate` — the ticker-resolution gate.

Covers the "wrong sample data" failure mode: running `validate` before a
project has any tickers.yaml used to silently fall back to the bundled
scaffold and probe yfinance for illustrative placeholder tickers that were
never real, producing a wall of confusing 404s (see
src/mybroker/data/tickers.yaml). `validate` now refuses to do that — it
either points you at your holdings, or builds tickers.yaml from them itself.

Doesn't cover resolve_group/probe's own yfinance-facing behaviour — that's
exercised live (see test_data.py's `live`-marked tests).
"""

from __future__ import annotations

import pytest
import yaml

from mybroker import config, tickers_validate


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    """Point every PROJECT_ROOT-relative path tickers_validate.py already
    bound at its own import time (TICKERS_FILE, HOLDINGS_EQUITY,
    HOLDINGS_INBOX_DIR, RESOLVED_TICKERS — see config.py's docstring on
    why those are snapshots, not lazy lookups) at a fresh tmp_path, so no
    test here can see or touch the real project's own tickers.yaml/holdings.
    """
    monkeypatch.chdir(tmp_path)
    config.set_project_root(tmp_path)
    for name in ("TICKERS_FILE", "HOLDINGS_EQUITY", "HOLDINGS_INBOX_DIR", "RESOLVED_TICKERS"):
        monkeypatch.setattr(tickers_validate, name, getattr(config, name))
    config.load_tickers.cache_clear()
    yield tmp_path
    config.load_tickers.cache_clear()


def _fake_probe(_ticker, period="1y"):  # noqa: ARG001
    return True, 100, 123.45


def test_no_holdings_no_tickers_yaml_prompts_for_holdings(capsys):
    """Nothing on disk at all — the exact first-run state from the bug
    report. Must not touch yfinance, and must say what to do."""
    assert tickers_validate.main() == 1

    out = capsys.readouterr().out
    assert "No holdings found" in out
    assert str(tickers_validate.HOLDINGS_INBOX_DIR) in out
    assert not tickers_validate.TICKERS_FILE.exists()


def test_builds_tickers_yaml_from_holdings_inbox_and_resolves(monkeypatch, capsys):
    """Holdings exist, tickers.yaml doesn't — validate builds one from a
    genuine short-symbol source (a generic 'Symbol' column) and resolves
    it, no `factfolio init` step required."""
    tickers_validate.HOLDINGS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    (tickers_validate.HOLDINGS_INBOX_DIR / "broker.csv").write_text(
        "Symbol,Qty,Avg Price,Invested,Current Value\nNEWSTOCK,10,100.0,1000.0,1100.0\n"
    )
    monkeypatch.setattr(tickers_validate, "probe", _fake_probe)

    assert tickers_validate.main() == 0

    out = capsys.readouterr().out
    assert "Drafted 1 entry" in out
    assert "NEWSTOCK" in out

    data = yaml.safe_load(tickers_validate.TICKERS_FILE.read_text())
    assert "NEWSTOCK" in data["symbols"]
    # The bundled scaffold's real indices came along too — not fake ones.
    assert "NIFTY50" in data["indices"]


def test_rerun_drafts_newly_added_holdings_only(monkeypatch, capsys):
    """Self-healing runs every time, not just the first — a holding added
    after the first `validate` gets picked up on the next one too."""
    tickers_validate.HOLDINGS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    inbox_file = tickers_validate.HOLDINGS_INBOX_DIR / "broker.csv"
    inbox_file.write_text(
        "Symbol,Qty,Avg Price,Invested,Current Value\nFIRSTSTOCK,10,100.0,1000.0,1100.0\n"
    )
    monkeypatch.setattr(tickers_validate, "probe", _fake_probe)

    assert tickers_validate.main() == 0
    capsys.readouterr()

    inbox_file.write_text(
        "Symbol,Qty,Avg Price,Invested,Current Value\n"
        "FIRSTSTOCK,10,100.0,1000.0,1100.0\n"
        "SECONDSTOCK,5,200.0,1000.0,1050.0\n"
    )

    assert tickers_validate.main() == 0
    out = capsys.readouterr().out
    assert "SECONDSTOCK" in out

    text = tickers_validate.TICKERS_FILE.read_text()
    assert text.count("FIRSTSTOCK:") == 1  # not re-drafted


def test_full_company_name_only_holdings_cannot_be_auto_drafted(capsys):
    """Sharekhan-style 'Scrip Name' is a full company name, not a genuine
    trading symbol — this deterministic path refuses to guess one, and
    must say so rather than reporting a misleadingly clean 0/0 pass."""
    tickers_validate.HOLDINGS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    (tickers_validate.HOLDINGS_INBOX_DIR / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
        "HDFC BANK LTD.,4,851.71,3406.85,731.55,2926.20,-480.64,-0.56\n"
    )

    assert tickers_validate.main() == 1

    out = capsys.readouterr().out
    assert "no symbols mapped" in out.lower()
    assert "factfolio init" in out


def test_existing_tickers_yaml_with_no_symbols_prompts_instead_of_passing(capsys):
    """A tickers.yaml that parses but maps nothing must not report a
    misleadingly clean 0/0 resolution."""
    tickers_validate.TICKERS_FILE.write_text("symbols: {}\nindices: {}\n")

    assert tickers_validate.main() == 1

    out = capsys.readouterr().out
    assert "no symbols mapped" in out.lower()


def test_never_resolves_the_bundled_placeholder_symbols(monkeypatch, capsys):
    """Regression for the reported bug: a fresh run must never probe
    SAMPLECORE/SAMPLERENAME/etc — those were illustrative-only and were
    never real yfinance tickers."""
    probed: list[str] = []

    def _tracking_probe(ticker, period="1y"):  # noqa: ARG001
        probed.append(ticker)
        return True, 100, 1.0

    monkeypatch.setattr(tickers_validate, "probe", _tracking_probe)

    tickers_validate.main()  # no holdings → returns 1, guidance only

    assert not probed
    assert not any("SAMPLE" in t for t in probed)
