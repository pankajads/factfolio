"""logs/factfolio.log — the operational trail, not just crash tracebacks
(see errors.py's errors.log for those).

Covers: the log file actually gets created and written to, it follows
PROJECT_ROOT when that changes mid-process, and the specific real-world
gap this closed — holdings_inbox ingestion outcomes (including a file
that fails to parse) and a silently-swallowed AI-resolver failure both
now leave a trail.
"""

from __future__ import annotations

import logging

import pytest

from mybroker import config, logging_setup


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.set_project_root(tmp_path)
    # get_logger()'s handler-reconfiguration is keyed off LOGS_DIR changing
    # — force it to reconfigure against *this* test's tmp_path even if an
    # earlier test already pointed it somewhere else.
    logging_setup._configured_dir = None
    yield tmp_path
    for h in list(logging.getLogger("mybroker").handlers):
        logging.getLogger("mybroker").removeHandler(h)
        h.close()
    logging_setup._configured_dir = None


def test_creates_and_writes_to_logs_factfolio_log(tmp_path):
    logger = logging_setup.get_logger("mybroker.test")
    logger.info("hello from a test")

    log_file = tmp_path / "logs" / "factfolio.log"
    assert log_file.exists()
    assert "hello from a test" in log_file.read_text()


def test_follows_project_root_when_it_changes(tmp_path):
    logging_setup.get_logger().info("first project")

    other = tmp_path / "other_project"
    other.mkdir()
    config.set_project_root(other)
    logging_setup.get_logger().info("second project")

    assert "first project" in (tmp_path / "logs" / "factfolio.log").read_text()
    assert "second project" in (other / "logs" / "factfolio.log").read_text()


def test_repeated_calls_do_not_duplicate_handlers():
    for _ in range(5):
        logging_setup.get_logger()
    assert len(logging.getLogger("mybroker").handlers) == 1


def test_holdings_inbox_ingestion_is_logged(tmp_path):
    """A genuine symbol-column source parses cleanly and logs it; a file
    that can't be classified at all logs an ERROR naming it — the exact
    trail that was missing when a PDF "didn't get picked up" with nothing
    anywhere explaining why."""
    from mybroker.portfolio.loader import load_portfolio

    inbox = tmp_path / "holdings_inbox"
    inbox.mkdir()
    (inbox / "broker.csv").write_text(
        "Symbol,Qty,Avg Price,Invested,Current Value\nNEWSTOCK,10,100.0,1000.0,1100.0\n"
    )
    (inbox / "junk.csv").write_text("not,a,holdings,file\n1,2,3,4\n")

    # Passing every path explicitly, matching test_portfolio.py's own
    # convention here — load_portfolio()'s bare defaults fall back to
    # names loader.py/importers.py bound at their own top-level import
    # time, which won't have followed this fixture's set_project_root().
    load_portfolio(
        equity_path=tmp_path / "holdings.csv",
        mf_path=tmp_path / "holdings_mf.csv",
        inbox_dir=inbox,
    )

    log_text = (tmp_path / "logs" / "factfolio.log").read_text()
    assert "broker.csv: parsed as equity, 1 position" in log_text
    assert "junk.csv: could not parse" in log_text


def test_ticker_drafting_logs_a_reason_per_file(tmp_path):
    """Regression: discover_equity_symbols_for_drafting found nothing for a
    mutual-fund file or a full-company-name file — both entirely correct —
    but said nothing about why, which read as "the file wasn't read" from
    the outside. Every file now gets a reasoned log line, not just the
    files that actually contributed a symbol."""
    from mybroker.portfolio.ticker_seeding import seed_draft_ticker_entries

    inbox = tmp_path / "holdings_inbox"
    inbox.mkdir()
    (inbox / "equity.csv").write_text(
        '"Instrument","Qty."\n"NEWSTOCK",10\n'
    )
    (inbox / "mf.csv").write_text(
        "Folio,Scheme Name,Units,Avg NAV,Current NAV\n123,Some Large Cap Fund,10,100,110\n"
    )
    (inbox / "sharekhan.csv").write_text(
        "Scrip Name,Total Qty,Avg Rate,Holding Value\nHDFC BANK LTD.,4,851.71,3406.85\n"
    )

    tickers_file = tmp_path / "tickers.yaml"
    tickers_file.write_text("symbols: {}\nindices: {}\n")

    added = seed_draft_ticker_entries(tickers_file)
    assert added == {"NEWSTOCK"}

    log_text = (tmp_path / "logs" / "factfolio.log").read_text()
    assert "equity.csv: 1 candidate symbol(s) found: NEWSTOCK" in log_text
    assert "mf.csv: classified as mutual-fund, not equity — nothing to draft" in log_text
    assert "sharekhan.csv: equity header found, but the symbol column's " \
        "data doesn't look like genuine trading symbols" in log_text


def test_agent_resolver_failure_is_logged_not_swallowed(monkeypatch):
    """Regression: _suggest_ticker_matches used to catch any exception from
    the AI-assisted resolver and silently fall back to plain search, with
    the actual failure reason (no `claude login`, network, etc.) lost
    forever. It must now be logged."""
    from mybroker.ui import cli

    monkeypatch.setattr(cli, "_collect_unmapped_holdings", lambda: [{"name": "SOME CO LTD"}])

    def _boom(_todo, _skipped):
        raise RuntimeError("no `claude` login found")

    monkeypatch.setattr(cli, "_resolve_via_agent", _boom)
    monkeypatch.setattr(cli, "_suggest_via_plain_search", lambda *_a, **_kw: None)

    cli._suggest_ticker_matches()

    log_text = (config.LOGS_DIR / "factfolio.log").read_text()
    assert "AI-assisted ticker resolution failed" in log_text
    assert "no `claude` login found" in log_text
