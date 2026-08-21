"""Portfolio loading and metrics.

The golden-value tests pin the real holdings.csv. If a refactor changes what
the system believes the portfolio is worth, these fail loudly — which is the
whole point, because every downstream recommendation rests on these figures.

These fixtures load with `include_inbox=False` deliberately: holdings_inbox/
now holds a real mutual-fund statement (see TestInboxImport below), and the
golden values here were pinned before that existed. Equity-only loading keeps
this file's numbers meaning what they've always meant; the inbox-merge
behaviour gets its own reconciliation tests instead of being folded in here.

The equity file itself has since moved from the project root into
holdings_inbox/ (the user's own doing, not a fixture choice) — `_real_equity_
path()` resolves whichever location actually has it, root first, so these
tests don't care where it lives as long as it exists somewhere real.
"""

import csv

import pytest

from mybroker.portfolio.loader import load_equity, load_portfolio
from mybroker.portfolio.metrics import snapshot

# Golden values, computed independently from holdings.csv.
EXPECTED_INVESTED = 618_734.05
EXPECTED_VALUE = 640_677.95
EXPECTED_PNL = 21_943.90
EXPECTED_POSITIONS = 14


def _real_equity_path():
    from mybroker.config import HOLDINGS_EQUITY, HOLDINGS_INBOX_DIR

    if HOLDINGS_EQUITY.exists():
        return HOLDINGS_EQUITY
    inbox_csv = HOLDINGS_INBOX_DIR / "holdings.csv"
    if inbox_csv.exists():
        return inbox_csv
    pytest.skip("No real holdings.csv found at project root or in holdings_inbox/")


@pytest.fixture(scope="module")
def portfolio():
    return load_portfolio(equity_path=_real_equity_path(), include_inbox=False)


@pytest.fixture(scope="module")
def snap(portfolio):
    return snapshot(portfolio)


# ── The numbers must be exactly right ────────────────────────────────────────
class TestGoldenValues:
    def test_position_count(self, portfolio):
        assert len(portfolio.equity) == EXPECTED_POSITIONS

    def test_total_invested(self, snap):
        assert snap.total_invested == pytest.approx(EXPECTED_INVESTED, abs=0.01)

    def test_total_value(self, snap):
        assert snap.total_value == pytest.approx(EXPECTED_VALUE, abs=0.01)

    def test_total_pnl(self, snap):
        assert snap.total_pnl == pytest.approx(EXPECTED_PNL, abs=0.01)

    def test_pnl_is_value_minus_invested(self, snap):
        """Internal consistency — P&L must not drift from its components."""
        assert snap.total_pnl == pytest.approx(
            snap.total_value - snap.total_invested, abs=0.01
        )

    def test_totals_equal_sum_of_positions(self, snap):
        assert sum(w.value for w in snap.positions) == pytest.approx(
            snap.total_value, abs=0.01
        )

    def test_weights_sum_to_100(self, snap):
        assert sum(w.weight_pct for w in snap.positions) == pytest.approx(100.0, abs=0.01)


# ── Reconcile against the raw file, not our own parser ───────────────────────
class TestReconcilesWithRawCsv:
    def test_totals_match_independent_csv_sum(self):
        """Re-sum the CSV with stdlib only. Catches parser-wide bugs that a
        self-consistent but wrong loader would hide."""
        equity_path = _real_equity_path()

        with equity_path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))

        raw_invested = sum(float(r["Invested"]) for r in rows if r.get("Invested"))
        raw_value = sum(float(r["Cur. val"]) for r in rows if r.get("Cur. val"))

        snap_ = snapshot(load_portfolio(equity_path=equity_path, include_inbox=False))
        assert snap_.total_invested == pytest.approx(raw_invested, abs=0.01)
        assert snap_.total_value == pytest.approx(raw_value, abs=0.01)

    def test_every_position_value_equals_qty_times_ltp(self, portfolio):
        """Sanity-check the broker's own arithmetic."""
        for p in portfolio.equity:
            assert p.current_value == pytest.approx(p.quantity * p.ltp, rel=0.001), (
                f"{p.symbol}: qty×LTP does not match reported value"
            )


# ── Enrichment from tickers.yaml ─────────────────────────────────────────────
class TestEnrichment:
    def test_all_symbols_are_mapped(self, portfolio):
        """An unmapped symbol gets no market data — must never pass silently."""
        unmapped = [p.symbol for p in portfolio.equity if not p.name]
        assert unmapped == [], f"Symbols missing from tickers.yaml: {unmapped}"

    def test_no_unknown_sectors(self, portfolio):
        unknown = [p.symbol for p in portfolio.equity if p.sector == "Unknown"]
        assert unknown == [], f"Symbols without a sector: {unknown}"

    def test_tata_motors_siblings_share_a_sector(self, portfolio):
        """TMCV and TMPV are one demerged business — they must aggregate."""
        by_symbol = {p.symbol: p for p in portfolio.equity}
        assert by_symbol["TMCV"].sector == by_symbol["TMPV"].sector == "Automobile"


# ── Concentration findings ───────────────────────────────────────────────────
class TestConcentration:
    def test_auto_sector_is_the_largest_exposure(self, snap):
        top = snap.sectors[0]
        assert top.key == "Automobile"
        assert top.weight_pct == pytest.approx(32.0, abs=0.5)

    def test_tata_motors_pair_is_a_quarter_of_the_portfolio(self, snap):
        pair = sum(w.weight_pct for w in snap.positions if w.key in ("TMCV", "TMPV"))
        assert pair == pytest.approx(25.0, abs=0.5)

    def test_portfolio_is_overwhelmingly_satellite(self, snap):
        """The headline finding: policy wants 70-75% core, reality is ~8%."""
        assert snap.core_pct < 10
        assert snap.satellite_pct > 90

    def test_core_and_satellite_sum_to_100(self, snap):
        assert snap.core_pct + snap.satellite_pct == pytest.approx(100.0, abs=0.01)

    def test_position_hhi_understates_risk_versus_sector_hhi(self, snap):
        """Position-level HHI is correlation-blind: three auto names look like
        diversification to it. Sector HHI must read as more concentrated —
        this asserts the two measures genuinely disagree, which is why the
        report shows both."""
        assert snap.position_concentration.hhi < snap.sector_concentration.hhi

    def test_effective_n_is_below_position_count(self, snap):
        c = snap.position_concentration
        assert c.effective_n < c.n_positions


# ── Failure modes ────────────────────────────────────────────────────────────
class TestFailureModes:
    def test_missing_file_raises_clearly(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No equity holdings"):
            load_equity(tmp_path / "nope.csv")

    def test_unparseable_number_raises_rather_than_defaulting_to_zero(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text(
            '"Instrument","Qty.","Avg. cost","LTP","Invested","Cur. val"\n'
            '"BEL",251,401.38,405.7,"NOT_A_NUMBER",101830.7\n'
        )
        with pytest.raises(ValueError, match="cannot parse"):
            load_equity(bad)

    def test_missing_required_column_raises(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text('"Instrument","LTP"\n"BEL",405.7\n')
        with pytest.raises(ValueError, match="missing required column"):
            load_equity(bad)

    def test_header_only_file_raises(self, tmp_path):
        bad = tmp_path / "empty.csv"
        bad.write_text('"Instrument","Qty.","Avg. cost","Invested","Cur. val"\n')
        with pytest.raises(ValueError, match="no position rows"):
            load_equity(bad)

    def test_blank_trailing_rows_are_skipped(self, tmp_path):
        f = tmp_path / "ok.csv"
        f.write_text(
            '"Instrument","Qty.","Avg. cost","LTP","Invested","Cur. val"\n'
            '"BEL",251,401.38,405.7,100745.75,101830.7\n'
            "\n\n"
        )
        positions, _ = load_equity(f)
        assert len(positions) == 1

    def test_absent_mf_file_is_not_an_error(self, tmp_path):
        """Isolated from real repo state (holdings_inbox/ now has real MF
        data) — this exercises the specific case of no MF source at all."""
        p = load_portfolio(
            equity_path=_real_equity_path(),
            mf_path=tmp_path / "nope.csv",
            inbox_dir=tmp_path,
        )
        assert p.mutual_funds == []
        assert any("No mutual-fund holdings" in w for w in p.warnings)

    def test_no_equity_anywhere_raises_clearly(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No equity holdings"):
            load_portfolio(
                equity_path=tmp_path / "nope.csv",
                mf_path=tmp_path / "nope_mf.csv",
                inbox_dir=tmp_path,
            )


# ── Inbox import (portfolio/importers.py) ────────────────────────────────────
class TestInboxImport:
    """holdings_inbox/holding_mf.xls is a real Sharekhan MF statement — messy
    header rows, PII, Indian-format number grouping, a trailing Total row.
    These tests exercise the general importer against it directly, then
    confirm load_portfolio() picks it up automatically."""

    def test_importer_classifies_and_parses_the_real_file(self):
        from mybroker.config import HOLDINGS_INBOX_DIR
        from mybroker.portfolio.importers import extract_positions

        path = HOLDINGS_INBOX_DIR / "holding_mf.xls"
        if not path.exists():
            pytest.skip("holdings_inbox/holding_mf.xls not present")

        equity, mfs, warnings = extract_positions(path)
        assert equity == []
        assert len(mfs) == 7

    def test_importer_totals_reconcile_independently(self):
        """Re-read the raw xls with pandas directly — not via the importer —
        and re-sum, the same discipline TestReconcilesWithRawCsv applies to
        holdings.csv."""
        pd = pytest.importorskip("pandas")
        from mybroker.config import HOLDINGS_INBOX_DIR
        from mybroker.portfolio.importers import extract_positions

        path = HOLDINGS_INBOX_DIR / "holding_mf.xls"
        if not path.exists():
            pytest.skip("holdings_inbox/holding_mf.xls not present")

        raw = pd.read_excel(path, header=None, dtype=str).fillna("")
        # Row 22 (0-indexed) is the statement's own "Total" row; column 2 is
        # Invested Amount, column 6 is Current Value.
        raw_invested = float(str(raw.iloc[22, 2]).replace(",", ""))
        raw_current = float(str(raw.iloc[22, 6]).replace(",", ""))

        _, mfs, _ = extract_positions(path)
        assert sum(p.invested for p in mfs) == pytest.approx(raw_invested, abs=0.01)
        assert sum(p.current_value for p in mfs) == pytest.approx(raw_current, abs=1.0)

    def test_load_portfolio_merges_inbox_by_default(self):
        from mybroker.config import HOLDINGS_INBOX_DIR

        if not (HOLDINGS_INBOX_DIR / "holding_mf.xls").exists():
            pytest.skip("holdings_inbox/holding_mf.xls not present")

        merged = load_portfolio()  # include_inbox=True by default
        assert merged.has_mutual_funds
        assert len(merged.mutual_funds) >= 7

    def test_equity_derives_invested_and_current_value_from_qty(self, tmp_path):
        """Real-world regression: a broker PDF export whose header uses
        'Avg Rate' (not 'avg cost'/'avg price') and 'Holding Value' —
        ambiguous, could mean cost basis or current value depending on the
        broker, so rather than guess, invested/current_value are derived
        from qty*avg_cost / qty*ltp instead of trusting that column at all.
        The exact header reported: ['Scrip Name', '', 'Total Qty',
        'Avg Rate', 'Holding Value', '', 'LTP', 'Market Value',
        'PL (Rs.)', 'PL%']."""
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,,Total Qty,Avg Rate,Holding Value,,LTP,Market Value,"
            "PL (Rs.),PL%\n"
            "INFY,,10,1500.00,999999,,1600.00,16000.00,1000.00,6.67\n"
        )

        equity, mfs, _warnings = extract_positions(path)
        assert mfs == []
        pos = equity[0]
        assert pos.symbol == "INFY"
        # Derived from qty*avg_cost (10*1500), NOT the ambiguous "Holding
        # Value" column (999999) — proves it's genuinely ignored, not
        # coincidentally correct.
        assert pos.invested == pytest.approx(15_000.0)
        assert pos.current_value == pytest.approx(16_000.0)  # qty*ltp
        assert pos.pnl == pytest.approx(1_000.0)  # current_value - invested

    def test_equity_still_requires_avg_cost_or_invested_column(self, tmp_path):
        """Can't derive invested without avg_cost, and there's no explicit
        invested column either — must still raise, naming the file, not
        silently default to 0."""
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "holdings.csv"
        path.write_text("Scrip Name,Total Qty,LTP,Market Value\nINFY,10,1600,16000\n")

        with pytest.raises(ValueError, match="avg_cost"):
            extract_positions(path)

    def test_xls_that_is_actually_html_still_parses(self, tmp_path):
        """Plenty of Indian broker/bank 'Excel' exports are actually an HTML
        table saved with an .xls extension, not a real binary workbook.
        pandas.read_excel can't identify a format from that content and
        raises "Excel file format cannot be determined, you must specify an
        engine manually" rather than guessing — this is the exact error a
        user hit in the wild. A second, unrelated <table> (e.g. a letterhead
        or disclaimer) must not confuse the header-scan either."""
        pytest.importorskip("lxml")
        path = tmp_path / "holdings.xls"
        path.write_text("""
            <html><body>
            <table><tr><td>Client Statement — Confidential</td></tr></table>
            <table>
              <tr><th>Instrument</th><th>Qty.</th><th>Avg. cost</th>
                  <th>Invested</th><th>Cur. val</th></tr>
              <tr><td>INFY</td><td>10</td><td>1,500.00</td>
                  <td>15,000.00</td><td>16,000.00</td></tr>
              <tr><td>TCS</td><td>5</td><td>3,200.00</td>
                  <td>16,000.00</td><td>17,000.00</td></tr>
              <tr><td>Total</td><td></td><td></td><td>31,000.00</td><td>33,000.00</td></tr>
            </table>
            </body></html>
        """)

        from mybroker.portfolio.importers import extract_positions

        equity, mfs, _warnings = extract_positions(path)
        assert mfs == []
        assert {p.symbol for p in equity} == {"INFY", "TCS"}
        assert sum(p.invested for p in equity) == pytest.approx(31_000.0)

    def test_xls_that_is_actually_html_without_th_tags_still_parses(self, tmp_path):
        """Header row via plain <td>, not <th> — pandas.read_html only
        auto-promotes a real <th> row to column labels; a <td>-only header
        must survive as an ordinary row for the header-scan to find."""
        pytest.importorskip("lxml")
        path = tmp_path / "holdings.xls"
        path.write_text("""
            <html><body><table>
              <tr><td>Instrument</td><td>Qty.</td><td>Avg. cost</td>
                  <td>Invested</td><td>Cur. val</td></tr>
              <tr><td>INFY</td><td>10</td><td>1,500.00</td>
                  <td>15,000.00</td><td>16,000.00</td></tr>
            </table></body></html>
        """)

        from mybroker.portfolio.importers import extract_positions

        equity, mfs, _warnings = extract_positions(path)
        assert mfs == []
        assert [p.symbol for p in equity] == ["INFY"]

    def test_genuinely_corrupt_xls_still_raises(self, tmp_path):
        """Not every "cannot be determined" file is secretly HTML — a
        truly corrupt/empty file must still fail loudly, naming the file,
        not disappear into a silent empty result."""
        path = tmp_path / "holdings.xls"
        path.write_bytes(b"this is neither Excel nor HTML")

        from mybroker.portfolio.importers import extract_positions

        with pytest.raises(ValueError, match="holdings.xls"):
            extract_positions(path)

    def test_unrecognisable_file_raises_naming_the_file(self, tmp_path):
        from mybroker.portfolio.importers import extract_positions

        bad = tmp_path / "mystery.csv"
        bad.write_text("Some random column,Another column\nfoo,bar\n")
        with pytest.raises(ValueError, match="mystery.csv"):
            extract_positions(bad)

    @pytest.mark.parametrize("delimiter", [",", "\t", ";"])
    def test_txt_sniffs_its_own_delimiter(self, tmp_path, delimiter):
        """.txt has no fixed delimiter across brokers — comma, tab, and
        semicolon all have to parse, not just whichever one a hand-written
        fixture happens to use."""
        from mybroker.portfolio.importers import extract_positions

        rows = [
            ["Instrument", "Qty.", "Avg. cost", "Invested", "Cur. val"],
            ["INFY", "10", "1500.00", "15000.00", "16000.00"],
            ["TCS", "5", "3200.00", "16000.00", "17000.00"],
        ]
        path = tmp_path / "holdings.txt"
        path.write_text("\n".join(delimiter.join(row) for row in rows) + "\n")

        equity, mfs, _warnings = extract_positions(path)
        assert mfs == []
        assert {p.symbol for p in equity} == {"INFY", "TCS"}

    def test_txt_falls_back_to_comma_when_sniffing_fails(self, tmp_path):
        """A single-column file gives the sniffer nothing to distinguish —
        it should default to comma rather than raising."""
        from mybroker.portfolio.importers import _read_txt_grid

        path = tmp_path / "single_column.txt"
        path.write_text("Instrument\nINFY\nTCS\n")

        grid = _read_txt_grid(path)
        assert grid == [["Instrument"], ["INFY"], ["TCS"]]


class TestExcelWarningSuppression:
    """A real user's terminal output had 'Workbook contains no default
    style, apply openpyxl's default' printed 6+ times for one `factfolio
    validate` run — the same .xlsx gets read independently by
    classification, drafting, and extraction, and nothing suppressed
    openpyxl's own warnings.warn() the way xlrd's print-based notice was
    already handled. Verified here by making the mocked pandas.read_excel
    itself emit the warning, rather than depending on a real .xlsx that
    happens to trigger openpyxl's internal condition."""

    def test_pandas_warnings_during_excel_read_never_escape(self, tmp_path, monkeypatch):
        import warnings

        import pandas as pd

        from mybroker.portfolio.importers import _read_excel_grid

        def _read_excel_and_warn(*_a, **_kw):
            warnings.warn("Workbook contains no default style, apply openpyxl's default",
                           UserWarning, stacklevel=2)
            return pd.DataFrame([["Instrument", "Qty"], ["INFY", "10"]])

        monkeypatch.setattr(pd, "read_excel", _read_excel_and_warn)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _read_excel_grid(tmp_path / "holdings.xlsx")

        assert not caught


# ── Bonds excluded, name-based resolution, cross-source lot merging ─────────
# Real-world regression from a Sharekhan demat holdings PDF: bonds listed
# alongside equity, and the same stock recorded under slightly different
# name spellings within one statement AND across a second broker's
# holdings.csv (the two accounts share some holdings, e.g. HDFC Bank).
@pytest.fixture
def fake_tickers(monkeypatch):
    """An isolated tickers.yaml — using the real one would make these tests
    fragile to unrelated edits of it, and they don't need real yfinance
    candidates anyway."""
    data = {
        "symbols": {
            "HDFCBANK": {
                "name": "HDFC Bank Ltd", "sector": "Banking",
                "tier": "large", "bucket": "core",
            },
            "TATASTEEL": {
                "name": "Tata Steel Ltd", "sector": "Metals",
                "tier": "large", "bucket": "satellite",
            },
        },
        "indices": {},
    }
    monkeypatch.setattr("mybroker.config.load_tickers", lambda: data)
    return data


class TestDraftSymbolDiscovery:
    """discover_equity_symbols_for_drafting — the scan behind `factfolio
    init`'s draft tickers.yaml entries. Deliberately independent of
    tickers.yaml/symbol_meta: it only asks "does this look like a genuine
    short trading symbol", never whether it's already mapped."""

    def test_finds_symbols_from_a_genuine_symbol_column(self, tmp_path):
        from mybroker.portfolio.importers import discover_equity_symbols_for_drafting

        path = tmp_path / "holdings.csv"
        path.write_text(
            '"Instrument","Qty.","Avg. cost","LTP","Invested","Cur. val"\n'
            '"INFY",10,1500,1600,15000,16000\n'
            '"TCS",5,3200,3400,16000,17000\n'
        )

        assert discover_equity_symbols_for_drafting(path) == {"INFY", "TCS"}

    def test_skips_a_full_company_name_column(self, tmp_path):
        """'Scrip Name' says "name" right in the header — no reliable way
        to derive a real trading symbol from that text, so nothing is
        discovered rather than guessing."""
        from mybroker.portfolio.importers import discover_equity_symbols_for_drafting

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Invested,Current Value\n"
            "HDFC BANK LTD.,4,851.71,3406.85,2926.20\n"
        )

        assert discover_equity_symbols_for_drafting(path) == set()

    def test_excludes_bond_like_rows(self, tmp_path):
        from mybroker.portfolio.importers import discover_equity_symbols_for_drafting

        path = tmp_path / "holdings.csv"
        path.write_text(
            '"Instrument","Qty.","Avg. cost","LTP","Invested","Cur. val"\n'
            '"2.50%GOLDBONDS2029SR-IX",1,5000,5100,5000,5100\n'
            '"INFY",10,1500,1600,15000,16000\n'
        )

        assert discover_equity_symbols_for_drafting(path) == {"INFY"}

    def test_returns_empty_on_unparseable_file_rather_than_raising(self, tmp_path):
        """Best-effort by design — a malformed file here must never block
        `init`; only `validate`/`status` actually enforce correctness."""
        from mybroker.portfolio.importers import discover_equity_symbols_for_drafting

        path = tmp_path / "mystery.csv"
        path.write_text("nothing that looks like a header at all\nfoo,bar\n")

        assert discover_equity_symbols_for_drafting(path) == set()

    def test_returns_empty_for_a_mutual_fund_file(self, tmp_path):
        from mybroker.portfolio.importers import discover_equity_symbols_for_drafting

        path = tmp_path / "mf.csv"
        path.write_text(
            "Folio,Scheme Name,Units,Avg NAV,Current NAV\n"
            "123,Some Large Cap Fund,10,100,110\n"
        )

        assert discover_equity_symbols_for_drafting(path) == set()

    def test_skips_a_column_whose_header_says_symbol_but_data_is_full_names(
        self, tmp_path
    ):
        """Real-world case: a broker PDF's table extraction mislabelled/
        misaligned its own columns — the header keyword-matches as
        "symbol" ('Scrip Code'), but the actual extracted data in that
        column is full company names, not codes. Trusting the header text
        alone here would draft garbage; must key off what the data looks
        like instead (see _looks_like_a_symbol_column)."""
        from mybroker.portfolio.importers import discover_equity_symbols_for_drafting

        path = tmp_path / "transactions.csv"
        path.write_text(
            "CustomerId,Scrip Code,Stock Name,Quantity,Avg Buy Price\n"
            "2448297,AXIS BANK LIMITED,AXISBANK,39,1331.66\n"
            "2448297,NTPC LIMITED,NTPC,100,366.49\n"
        )

        assert discover_equity_symbols_for_drafting(path) == set()


class TestUnmappedNameDiscovery:
    """discover_unmapped_full_names — the mirror image of
    TestDraftSymbolDiscovery, behind cmd_init's yfinance-search
    *suggestions* (never auto-written, see _suggest_ticker_matches)."""

    def test_finds_names_from_a_full_name_column(self, tmp_path):
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Invested,Current Value\n"
            "AXIS BANK LIMITED,39,1331.66,51934.82,45290.70\n"
        )

        holdings = discover_unmapped_full_names(path)
        assert [h["name"] for h in holdings] == ["AXIS BANK LIMITED"]
        assert holdings[0]["quantity"] == 39
        assert holdings[0]["avg_cost"] == pytest.approx(1331.66)

    def test_skips_a_genuine_symbol_column(self, tmp_path):
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "holdings.csv"
        path.write_text(
            '"Instrument","Qty.","Avg. cost","LTP","Invested","Cur. val"\n'
            '"INFY",10,1500,1600,15000,16000\n'
        )

        assert discover_unmapped_full_names(path) == []

    def test_excludes_a_name_already_resolvable_via_tickers_yaml(
        self, tmp_path, fake_tickers
    ):
        """HDFCBANK is in fake_tickers with name 'HDFC Bank Ltd' — a name
        variant of it needs no suggestion, since it already resolves."""
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Invested,Current Value\n"
            "HDFC BANK LTD.,4,851.71,3406.85,2926.20\n"
        )

        assert discover_unmapped_full_names(path) == []

    def test_excludes_bond_like_rows(self, tmp_path):
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Invested,Current Value\n"
            "2.50%GOLDBONDS2029SR-IX,1,5000,5000,5100\n"
        )

        assert discover_unmapped_full_names(path) == []

    def test_deduplicates_repeated_names_within_one_file(self, tmp_path):
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Invested,Current Value\n"
            "AXIS BANK LIMITED,39,1331.66,51934.82,45290.70\n"
            "AXIS BANK LIMITED,39,1331.66,51934.82,45290.70\n"
        )

        assert len(discover_unmapped_full_names(path)) == 1

    def test_surfaces_names_from_a_column_mislabelled_as_symbol(self, tmp_path):
        """The exact mirror of TestDraftSymbolDiscovery's swapped-column
        case: a header that keyword-matches as "symbol" ('Scrip Code')
        whose actual data is full company names must still reach the AI
        resolver — the old header-text-only check made both discovery
        functions give up here, so these holdings were invisible to
        `factfolio init` entirely, not just left undrafted."""
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "transactions.csv"
        path.write_text(
            "CustomerId,Scrip Code,Stock Name,Quantity,Avg Buy Price\n"
            "2448297,AXIS BANK LIMITED,AXISBANK,39,1331.66\n"
        )

        holdings = discover_unmapped_full_names(path)
        assert [h["name"] for h in holdings] == ["AXIS BANK LIMITED"]


class TestCandidateSymbolHint:
    """_find_candidate_symbol_column / discover_unmapped_full_names'
    candidate_symbol field — the actual fix for a reported real-world bug:
    a broker PDF's own table extraction had "Stock Name" (header) holding
    "AXISBANK" (a real trading symbol) right next to "SKScrip Code"
    (header) holding "AXIS BANK LIMITED" (the full name) — column labels
    and data swapped. The old code picked the keyword-matched column
    ("Scrip Code" contains "scrip"), decided its full-name data wasn't
    symbol-shaped, and gave up — discarding the real symbol sitting one
    column over instead of ever looking at it. This never auto-drafts
    anything (see discover_equity_symbols_for_drafting's own untouched
    tests above) — it only surfaces a hint for the AI resolver / a human
    to verify."""

    def test_surfaces_the_real_symbol_from_the_mislabelled_column(self, tmp_path):
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "transactions.csv"
        path.write_text(
            "CustomerId,SKScrip Code,Stock Name,Quantity,Avg Buy Price\n"
            "2448297,AXIS BANK LIMITED,AXISBANK,39,1331.66\n"
            "2448297,NTPC LTD,NTPC,100,366.49\n"
        )

        holdings = discover_unmapped_full_names(path)
        assert {h["name"]: h["candidate_symbol"] for h in holdings} == {
            "AXIS BANK LIMITED": "AXISBANK",
            "NTPC LTD": "NTPC",
        }

    def test_collapses_a_pdf_wrapped_symbol(self, tmp_path):
        """A narrow PDF column wraps "HDFCBANK" across two lines;
        _clean_pdf_cell already joins that back with a space ("HDFCBAN
        K") — correct for a name column, wrong for a code column. The
        hint must be the real unbroken symbol, not the wrapped text."""
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "transactions.csv"
        path.write_text(
            "CustomerId,SKScrip Code,Stock Name,Quantity,Avg Buy Price\n"
            "2448297,HDFC BANK LTD,HDFCBAN K,78,921.63\n"
            "2448297,NTPC LTD,NTPC,100,366.49\n"
        )

        holdings = discover_unmapped_full_names(path)
        assert holdings[0]["candidate_symbol"] == "HDFCBANK"

    def test_no_hint_when_no_other_column_looks_symbol_shaped(self, tmp_path):
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Invested,Current Value\n"
            "AXIS BANK LIMITED,39,1331.66,51934.82,45290.70\n"
        )

        holdings = discover_unmapped_full_names(path)
        assert "candidate_symbol" not in holdings[0]

    def test_no_hint_when_a_constant_column_would_otherwise_qualify(self, tmp_path):
        """A Y/N "is tradable"-style flag column is short, alphabetic, and
        passes the character-shape check just as easily as a real symbol
        — but it's the same value on every row, which a genuine per-
        holding symbol column never is."""
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "transactions.csv"
        path.write_text(
            "CustomerId,SKScrip Code,IsTradable,Quantity,Avg Buy Price\n"
            "2448297,AXIS BANK LIMITED,Y,39,1331.66\n"
            "2448297,NTPC LTD,Y,100,366.49\n"
            "2448297,TCS LTD,Y,20,3432.49\n"
        )

        holdings = discover_unmapped_full_names(path)
        assert all("candidate_symbol" not in h for h in holdings)

    def test_no_hint_when_a_numeric_id_column_would_otherwise_qualify(self, tmp_path):
        """A per-row numeric serial ID is unique per row (unlike the
        constant-flag case above) and passes the bare character-class
        check — but a real NSE/BSE trading symbol always contains a
        letter; a purely numeric column never legitimately is one."""
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "transactions.csv"
        path.write_text(
            "CustomerId,SKScrip Code,SerialNo,Quantity,Avg Buy Price\n"
            "2448297,AXIS BANK LIMITED,3062,39,1331.66\n"
            "2448297,NTPC LTD,4828,100,366.49\n"
        )

        holdings = discover_unmapped_full_names(path)
        assert all("candidate_symbol" not in h for h in holdings)

    def test_no_hint_when_two_columns_are_ambiguously_symbol_shaped(self, tmp_path):
        """Two genuinely plausible short-code columns is real ambiguity,
        not a hint worth surfacing — same "never guess" discipline as
        discover_equity_symbols_for_drafting itself."""
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "transactions.csv"
        path.write_text(
            "CustomerId,SKScrip Code,Stock Name,AltCode,Quantity,Avg Buy Price\n"
            "2448297,AXIS BANK LIMITED,AXISBANK,AXBK,39,1331.66\n"
            "2448297,NTPC LTD,NTPC,NTPCLT,100,366.49\n"
        )

        holdings = discover_unmapped_full_names(path)
        assert all("candidate_symbol" not in h for h in holdings)

    def test_excludes_bond_rows_by_scanning_the_whole_row(self, tmp_path):
        """A gold-bond row's give-away text ("2.50%GOLDBONDS...") sits in
        the NAME column, not necessarily the candidate column — the bond
        check must still catch it regardless of which column is used as
        the symbol/name source."""
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "transactions.csv"
        path.write_text(
            "CustomerId,SKScrip Code,Stock Name,Quantity,Avg Buy Price\n"
            "2448297,2.50%GOLDBONDS2029SR-IX,SGBJAN29IX,1,5000\n"
            "2448297,AXIS BANK LIMITED,AXISBANK,39,1331.66\n"
        )

        holdings = discover_unmapped_full_names(path)
        assert [h["name"] for h in holdings] == ["AXIS BANK LIMITED"]


class TestRowData:
    """discover_unmapped_full_names' row_data field — every column of the
    source row, raw, under its own real header label. Deliberately NOT
    filtered down to whatever this module's own shape heuristics happen to
    recognise (that's candidate_symbol's job, and it's necessarily narrow)
    — the whole point of row_data is letting whoever resolves the holding
    (the AI agent, or a human) read and judge the complete original
    context itself, the way a person looking at the real statement would,
    rather than being handed only what a fixed set of Python rules
    decided was worth keeping."""

    def test_carries_every_column_under_its_real_header(self, tmp_path):
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "transactions.csv"
        path.write_text(
            "CustomerId,SKScrip Code,Stock Name,Quantity,Avg Buy Price\n"
            "2448297,AXIS BANK LIMITED,AXISBANK,39,1331.66\n"
        )

        holdings = discover_unmapped_full_names(path)
        assert holdings[0]["row_data"] == {
            "CustomerId": "2448297",
            "SKScrip Code": "AXIS BANK LIMITED",
            "Stock Name": "AXISBANK",
            "Quantity": "39",
            "Avg Buy Price": "1331.66",
        }

    def test_present_even_when_no_candidate_symbol_was_found(self, tmp_path):
        """row_data must not depend on _find_candidate_symbol_column
        having succeeded — it's the fallback for exactly the cases that
        heuristic can't confidently resolve on its own (ambiguous or
        genuinely absent), not a bonus only shown when it already worked."""
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Invested,Current Value\n"
            "AXIS BANK LIMITED,39,1331.66,51934.82,45290.70\n"
        )

        holdings = discover_unmapped_full_names(path)
        assert "candidate_symbol" not in holdings[0]
        assert holdings[0]["row_data"]["Scrip Name"] == "AXIS BANK LIMITED"
        assert holdings[0]["row_data"]["Total Qty"] == "39"

    def test_repeated_header_labels_do_not_clobber_each_other(self, tmp_path):
        from mybroker.portfolio.importers import discover_unmapped_full_names

        path = tmp_path / "transactions.csv"
        path.write_text(
            "SKScrip Code,Code,Code,Quantity,Avg Buy Price\n"
            "AXIS BANK LIMITED,AXISBANK,532215,39,1331.66\n"
        )

        holdings = discover_unmapped_full_names(path)
        row = holdings[0]["row_data"]
        assert row["Code"] == "AXISBANK"
        assert row["Code (2)"] == "532215"


class TestSuggestTickerForName:
    """suggest_ticker_for_name — a SUGGESTION only, never written to
    tickers.yaml automatically. yfinance.Search is always mocked here:
    a live network call has no place in this test suite (slow, flaky,
    and not what these tests are actually verifying — the NSE/BSE
    filtering and NS-preferred-over-BO logic don't need real data)."""

    @staticmethod
    def _mock_search(quotes):
        class _FakeSearch:
            def __init__(self, *_a, **_kw):
                self.quotes = quotes

        return _FakeSearch

    def test_prefers_nse_over_bse_and_filters_foreign_listings(self, monkeypatch):
        from mybroker.config import suggest_ticker_for_name

        quotes = [
            {"symbol": "AXISBANK.BO", "quoteType": "EQUITY", "exchange": "BSE"},
            {"symbol": "AXB.IL", "quoteType": "EQUITY", "exchange": "IOB"},  # London GDR
            {"symbol": "AXISBANK.NS", "quoteType": "EQUITY", "exchange": "NSI"},
        ]
        monkeypatch.setattr("yfinance.Search", self._mock_search(quotes))

        assert suggest_ticker_for_name("AXIS BANK LIMITED") == "AXISBANK.NS"

    def test_falls_back_to_bse_when_no_nse_listing(self, monkeypatch):
        from mybroker.config import suggest_ticker_for_name

        quotes = [{"symbol": "SOMECO.BO", "quoteType": "EQUITY", "exchange": "BSE"}]
        monkeypatch.setattr("yfinance.Search", self._mock_search(quotes))

        assert suggest_ticker_for_name("Some Co") == "SOMECO.BO"

    def test_returns_none_when_nothing_matches(self, monkeypatch):
        """The real-world ZOMATO-after-rename-to-ETERNAL case: yfinance's
        own search can come back empty."""
        from mybroker.config import suggest_ticker_for_name

        monkeypatch.setattr("yfinance.Search", self._mock_search([]))

        assert suggest_ticker_for_name("ZOMATO") is None

    def test_ignores_non_equity_and_non_indian_results(self, monkeypatch):
        from mybroker.config import suggest_ticker_for_name

        quotes = [
            {"symbol": "SOMECO.SA", "quoteType": "EQUITY", "exchange": "SAO"},  # Brazil
            {"symbol": "SOMECOF", "quoteType": "FUTURE", "exchange": "NSI"},
        ]
        monkeypatch.setattr("yfinance.Search", self._mock_search(quotes))

        assert suggest_ticker_for_name("Some Co") is None

    def test_returns_none_on_any_error_rather_than_raising(self, monkeypatch):
        """A network hiccup or rate limit here must never propagate —
        this is pure convenience, never a gate."""
        from mybroker.config import suggest_ticker_for_name

        def _raise(*_a, **_kw):
            raise RuntimeError("network is down")

        monkeypatch.setattr("yfinance.Search", _raise)

        assert suggest_ticker_for_name("Anything") is None


class TestBondExclusionAndNameResolution:
    def test_bond_and_gold_bond_rows_are_excluded_from_equity(self, tmp_path, fake_tickers):
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "2.50% JAN29 SERIES X FY 2020-2,4,5104.00,20416.00,13604.17,54416.68,34000.68,6.66\n"
            "2.50%GOLDBONDS2029SR-IX,1,5000.00,5000.00,13574.78,13574.78,8574.78,1.71\n"
            "HDFC BANK LTD,74,925.41,68480.19,731.55,54134.70,-14345.64,-15.50\n"
        )

        equity, mfs, warnings = extract_positions(path)
        assert mfs == []
        assert [p.symbol for p in equity] == ["HDFCBANK"]
        assert len([w for w in warnings if "bond" in w.lower()]) == 2

    def test_name_variants_resolve_to_the_same_symbol(self, tmp_path, fake_tickers):
        """'HDFC BANK LTD' and 'HDFC BANK LTD.' are the same company
        recorded slightly differently — both must resolve to HDFCBANK via
        tickers.yaml's own `name:` field, not stay as two unmapped symbols."""
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "HDFC BANK LTD,74,925.41,68480.19,731.55,54134.70,-14345.64,-15.50\n"
            "HDFC BANK LTD.,4,851.71,3406.85,731.55,2926.20,-480.64,-0.56\n"
            "TATA STEEL LTD.,500,139.01,69502.74,191.86,95930.00,26425.00,190.09\n"
        )

        equity, _mfs, warnings = extract_positions(path)
        assert {p.symbol for p in equity} == {"HDFCBANK", "TATASTEEL"}
        assert all(p.sector != "Unknown" for p in equity)
        assert warnings == []  # every row resolved — no "not in tickers.yaml"

    def test_unmatched_name_stays_unresolved_not_guessed(self, tmp_path, fake_tickers):
        """A name matching no tickers.yaml entry must never be guessed at —
        same rule symbol_meta() already applies to exact-symbol lookups."""
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "AXIS BANK LIMITED,39,1331.66,51934.82,1161.30,45290.70,-6644.04,-4.99\n"
        )

        equity, _mfs, warnings = extract_positions(path)
        assert equity[0].symbol == "AXIS BANK LIMITED"  # left as the raw name
        assert equity[0].sector == "Unknown"
        assert any("not in tickers.yaml" in w for w in warnings)
        # Not just a bare fact — says what to actually do about it.
        assert any("factfolio init" in w for w in warnings)

    def test_ambiguous_name_match_stays_unresolved(self, monkeypatch, tmp_path):
        """Two tickers.yaml entries with the same normalized name is a
        pathological config, but must still refuse to guess rather than
        pick one arbitrarily."""
        data = {
            "symbols": {
                "FOOA": {"name": "Foo Industries Ltd"},
                "FOOB": {"name": "Foo Industries Ltd."},  # same after normalizing
            },
            "indices": {},
        }
        monkeypatch.setattr("mybroker.config.load_tickers", lambda: data)
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "FOO INDUSTRIES LIMITED,10,100.00,1000.00,110.00,1100.00,100.00,10.0\n"
        )

        equity, _mfs, warnings = extract_positions(path)
        assert equity[0].sector == "Unknown"
        assert any("not in tickers.yaml" in w for w in warnings)

    def test_alias_resolves_a_name_the_normalizer_cannot_repair(self, monkeypatch):
        """Regression for a reported bug: a PDF-wrapped raw name
        ("VODAFON E IDEA LIMITED" — a stray mid-word space breaking
        "VODAFONE") normalizes to something a correctly-spelled `name:`
        field ("Vodafone Idea Limited") never will, no matter how good
        _normalize_company_name is — the raw text is genuinely broken.
        Without an alias, this holding gets re-flagged as unmapped every
        time its source file is re-scanned, even seconds after being
        correctly resolved. `aliases:` is an exact-text escape hatch for
        exactly this: something already proven to mean this symbol,
        recorded verbatim, checked before ever falling back to fuzzy
        name matching."""
        from mybroker.config import resolve_symbol_by_name

        data = {
            "symbols": {
                "IDEA": {
                    "name": "Vodafone Idea Limited",
                    "aliases": ["VODAFON E IDEA LIMITED"],
                },
            },
            "indices": {},
        }
        monkeypatch.setattr("mybroker.config.load_tickers", lambda: data)

        assert resolve_symbol_by_name("VODAFON E IDEA LIMITED") == "IDEA"
        # The clean, correctly-spelled name-based path still works too —
        # aliases are additive, not a replacement for it.
        assert resolve_symbol_by_name("Vodafone Idea Ltd") == "IDEA"
        # A merely similar name that was never actually recorded as an
        # alias must still refuse to guess.
        assert resolve_symbol_by_name("VODAFONE IDEA LTD XYZ") is None

    def test_alias_match_is_exact_not_fuzzy(self, monkeypatch):
        """An alias is a claim that THIS EXACT raw text was already
        proven to mean this symbol — not another invitation to fuzzy-
        match. A near-miss must not silently resolve."""
        from mybroker.config import resolve_symbol_by_name

        data = {
            "symbols": {"IDEA": {"name": "Vodafone Idea Limited", "aliases": ["VODAFON E IDEA LIMITED"]}},
            "indices": {},
        }
        monkeypatch.setattr("mybroker.config.load_tickers", lambda: data)

        assert resolve_symbol_by_name("VODAFON E IDEA LIMITE") is None  # truncated
        assert resolve_symbol_by_name("VODAFONE IDEA LIMITED") == "IDEA"  # via name:, not alias

    def test_alias_match_is_whitespace_and_case_tolerant(self, monkeypatch):
        from mybroker.config import resolve_symbol_by_name

        data = {
            "symbols": {"IDEA": {"name": "Vodafone Idea Limited", "aliases": ["VODAFON E IDEA LIMITED"]}},
            "indices": {},
        }
        monkeypatch.setattr("mybroker.config.load_tickers", lambda: data)

        assert resolve_symbol_by_name("  vodafon e   idea limited ") == "IDEA"


class TestCrossSourceLotMerging:
    def test_merges_lots_of_the_same_symbol_across_sources(self, tmp_path, fake_tickers):
        """The full real scenario: the same stock held across two different
        brokers (a Zerodha-style root holdings.csv, and a Sharekhan-style
        statement in the inbox with two name-variant lots) must merge into
        one combined position for accurate concentration/policy checks."""
        from mybroker.portfolio.loader import load_portfolio

        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "sharekhan.csv").write_text(
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "HDFC BANK LTD,74,925.41,68480.19,731.55,54134.70,-14345.64,-15.50\n"
            "HDFC BANK LTD.,4,851.71,3406.85,731.55,2926.20,-480.64,-0.56\n"
        )
        root = tmp_path / "holdings.csv"
        root.write_text(
            '"Instrument","Qty.","Avg. cost","LTP","Invested","Cur. val","P&L","Net chg.","Day chg.",""\n'
            '"HDFCBANK",73,879.08,729,64173.2,53217,-10956.2,-17.07,0,""\n'
        )

        portfolio = load_portfolio(
            equity_path=root, mf_path=tmp_path / "no_mf.csv", inbox_dir=inbox,
        )

        assert len(portfolio.equity) == 1
        merged = portfolio.equity[0]
        assert merged.symbol == "HDFCBANK"
        assert merged.quantity == pytest.approx(73 + 74 + 4)
        assert merged.current_value == pytest.approx(53217 + 54134.70 + 2926.20)
        # Weighted average cost, recomputed from the merged totals — not a
        # plain average of the three lots' own avg_cost figures.
        assert merged.avg_cost == pytest.approx(merged.invested / merged.quantity)

    def test_unresolved_positions_only_merge_on_exact_raw_name(self, tmp_path, fake_tickers):
        """Two DIFFERENT unmapped spellings of the same company must NOT be
        silently merged — that would be guessing they're the same stock
        without tickers.yaml ever confirming it."""
        from mybroker.portfolio.importers import extract_positions
        from mybroker.portfolio.loader import _merge_same_symbol_lots

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "AXIS BANK LIMITED,39,1331.66,51934.82,1161.30,45290.70,-6644.04,-4.99\n"
            "AXIS BANK LTD,10,1300.00,13000.00,1200.00,12000.00,-1000.00,-7.69\n"
        )

        equity, _mfs, _ = extract_positions(path)
        merged = _merge_same_symbol_lots(equity)
        assert len(merged) == 2  # stayed separate — different raw strings


# ── Broker column-name variety ───────────────────────────────────────────────
class TestColumnNameVariants:
    """Confirmed real terminology across brokers/DPs — Groww/Upstox call
    average cost "Average Price" (not "avg cost"/"avg rate"), which the
    original keyword lists genuinely didn't catch: "average" doesn't
    contain the substring "avg", so ("avg", "price") never matched it."""

    def test_average_price_is_recognised_as_avg_cost(self, tmp_path, fake_tickers):
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Qty,Average Price,Invested,Current Value\n"
            "HDFC BANK LTD,10,1500.00,15000.00,16000.00\n"
        )

        equity, _mfs, _ = extract_positions(path)
        assert equity[0].avg_cost == pytest.approx(1500.0)

    @pytest.mark.parametrize("column", ["Buy Price", "Purchase Price"])
    def test_buy_purchase_price_recognised_as_avg_cost(self, tmp_path, fake_tickers, column):
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "holdings.csv"
        path.write_text(
            f"Scrip Name,Qty,{column},Invested,Current Value\n"
            "HDFC BANK LTD,10,1500.00,15000.00,16000.00\n"
        )

        equity, _mfs, _ = extract_positions(path)
        assert equity[0].avg_cost == pytest.approx(1500.0)

    def test_current_price_recognised_as_ltp(self, tmp_path, fake_tickers):
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Qty,Avg Cost,Current Price,Invested,Current Value\n"
            "HDFC BANK LTD,10,1500.00,1600.00,15000.00,16000.00\n"
        )

        equity, _mfs, _ = extract_positions(path)
        assert equity[0].ltp == pytest.approx(1600.0)

    def test_cost_of_acquisition_recognised_as_invested(self, tmp_path, fake_tickers):
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "holdings.csv"
        path.write_text(
            "Scrip Name,Qty,Avg Cost,Cost of Acquisition,Current Value\n"
            "HDFC BANK LTD,10,1500.00,15000.00,16000.00\n"
        )

        equity, _mfs, _ = extract_positions(path)
        assert equity[0].invested == pytest.approx(15000.0)


class TestPdfWrappedHeaders:
    """Regression for a reported bug: a real mutual-fund PDF's own table
    extraction wrapped nearly every header across two internal lines
    ("InvestmentValue" -> "Investme" + "\\n" + "ntValue", collapsed by
    _clean_pdf_cell into "Investme ntValue") — breaking the plain keyword
    substring check even though a human reading the header would have no
    trouble seeing "Investment Value". Every mutual-fund position built
    from that file silently got invested=0, current_value=0 (both columns
    unresolved, and nothing else could derive them either) — real
    holdings worth real money, reported as worthless, with no warning or
    error anywhere. _find_col's whitespace-collapsed fallback is the fix;
    the "invested"/"DivReinvested" tests below are the false-positive risk
    that fix introduces if not ordered carefully.
    """

    def test_wrapped_mf_header_resolves_correctly(self, tmp_path):
        """The actual real-world header, reproduced verbatim (down to
        which specific words got broken) — not a simplified stand-in."""
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "mf.csv"
        path.write_text(
            "SchemeN ame,SchemeC lass,Investme ntValue,CurrentV alue,NetUnits,"
            "NAVRate,NAVDAT E,AverageC ost,UnRealis ed,DivReinv ested,DivPaid,"
            "Absolute,SAR,AvgHoldi ngDays,SchemeC ode,BseToken\n"
            "Invesco India Large & Mid Cap Fund - Growth,Equity,39998.00,"
            "44940.39,405.7090,110.7700,18/08/2026,98.59,4942.38,0.00,0.00,"
            "12.36,42.06,94.00,14053190.002066,487\n"
        )

        equity, mfs, _warnings = extract_positions(path)
        assert equity == []
        assert mfs[0].invested == pytest.approx(39998.00)
        assert mfs[0].current_value == pytest.approx(44940.39)

    def test_wrapped_equity_header_resolves_correctly(self, tmp_path, fake_tickers):
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "equity.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Investe d,Current Value\n"
            "HDFC BANK LTD,10,1500.00,15000.00,16000.00\n"
        )

        equity, _mfs, _ = extract_positions(path)
        assert equity[0].invested == pytest.approx(15000.0)

    def test_a_dividend_reinvested_column_is_not_mistaken_for_invested(self, tmp_path):
        """The false-positive risk a naive whitespace-collapsing fallback
        introduces: "DivReinvested" (a real, unrelated MF field) contains
        "invested" as a plain substring — with no wrapping involved at
        all, "re" + "invested" already spells it. A column actually
        headed "Investment Value" must still win over one merely
        containing "invested" as an accidental substring of a longer,
        unrelated word."""
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "mf.csv"
        path.write_text(
            "Scheme Name,Investment Value,Current Value,Units,Div Reinvested\n"
            "Some Fund - Growth,39998.00,44940.39,405.71,999999.00\n"
        )

        _equity, mfs, _ = extract_positions(path)
        assert mfs[0].invested == pytest.approx(39998.00)  # not 999999.00

    def test_unresolvable_mf_columns_raise_instead_of_silently_zeroing(self, tmp_path):
        """The safety net behind the fix: if invested/current_value still
        can't be found or derived at all (a genuinely unrecognisable
        format, not just a wrapped one), this must fail loudly — matching
        loader.py's own stated rule ("a column that cannot be interpreted
        is an error, never a silent zero") — rather than building
        positions worth real money that silently report as worthless."""
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "mf.csv"
        path.write_text(
            "Scheme Name,Folio,Category\n"
            "Some Fund - Growth,12345,Equity\n"
        )

        with pytest.raises(ValueError, match="invested"):
            extract_positions(path)


class TestMixedEquityAndMfInOneFile:
    """A real user's exact scenario: a consolidated broker/DP statement
    can legitimately contain BOTH an equity holdings table and a mutual-
    fund holdings table as separate sections of the SAME file. The old
    code stopped scanning at the first classifiable header — the second
    table's holdings were silently dropped entirely, no warning, no
    error, nothing: real money, invisible. extract_positions must find
    and extract every table in the file, however many there are, of
    whichever kind, in whatever order they appear."""

    def test_mf_then_equity_in_one_file(self, tmp_path, fake_tickers):
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "consolidated.csv"
        path.write_text(
            "Scheme Name,Folio,Invested,Current Value,Units\n"
            "UTI Large Cap Fund - Growth,111,120000,134000,500\n"
            "\n"
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "HDFC BANK LTD,74,925.41,68480.19,731.55,54134.70,-14345.64,-15.50\n"
        )

        equity, mfs, _warnings = extract_positions(path)

        assert [p.symbol for p in equity] == ["HDFCBANK"]
        assert [m.scheme_name for m in mfs] == ["UTI Large Cap Fund - Growth"]

    def test_equity_then_mf_in_one_file(self, tmp_path, fake_tickers):
        """Order must not matter — the old "stop at the first table"
        behaviour would have caught this direction (equity first) by
        coincidence, dropping the MF section instead; neither ordering
        may lose data."""
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "consolidated.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "HDFC BANK LTD,74,925.41,68480.19,731.55,54134.70,-14345.64,-15.50\n"
            "\n"
            "Scheme Name,Folio,Invested,Current Value,Units\n"
            "UTI Large Cap Fund - Growth,111,120000,134000,500\n"
        )

        equity, mfs, _warnings = extract_positions(path)

        assert [p.symbol for p in equity] == ["HDFCBANK"]
        assert [m.scheme_name for m in mfs] == ["UTI Large Cap Fund - Growth"]

    def test_three_tables_in_one_file(self, tmp_path, fake_tickers):
        """Not just two — however many tables actually exist, all get
        found: equity, then mf, then a second equity lot further down
        (two separate demat accounts consolidated into one export, say)."""
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "consolidated.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "HDFC BANK LTD,74,925.41,68480.19,731.55,54134.70,-14345.64,-15.50\n"
            "\n"
            "Scheme Name,Folio,Invested,Current Value,Units\n"
            "UTI Large Cap Fund - Growth,111,120000,134000,500\n"
            "\n"
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "TATA STEEL LTD.,500,139.01,69502.74,191.86,95930.00,26425.00,190.09\n"
        )

        equity, mfs, _warnings = extract_positions(path)

        assert {p.symbol for p in equity} == {"HDFCBANK", "TATASTEEL"}
        assert [m.scheme_name for m in mfs] == ["UTI Large Cap Fund - Growth"]

    def test_one_good_table_and_one_bad_table_still_extracts_the_good_one(
        self, tmp_path, fake_tickers,
    ):
        """A partial failure in one section must not sacrifice a
        perfectly good table elsewhere in the same file — the bad
        section's error becomes a warning, not a reason to return
        nothing at all."""
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "consolidated.csv"
        path.write_text(
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "HDFC BANK LTD,74,925.41,68480.19,731.55,54134.70,-14345.64,-15.50\n"
            "\n"
            "Scheme Name,Folio,Category\n"  # no invested/current_value at all
            "Some Fund - Growth,12345,Equity\n"
        )

        equity, mfs, warnings = extract_positions(path)

        assert [p.symbol for p in equity] == ["HDFCBANK"]
        assert mfs == []
        assert any("invested" in w for w in warnings)

    def test_every_table_unresolvable_still_raises(self, tmp_path):
        """The other side of the same coin: if NOTHING in the whole file
        could be extracted (every table found failed), this must still
        fail loudly — not silently succeed with two empty lists."""
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "consolidated.csv"
        path.write_text(
            "Scrip Name,Total Qty,LTP,Market Value\n"  # missing avg_cost/invested
            "HDFC BANK LTD,74,731.55,54134.70\n"
            "\n"
            "Scheme Name,Folio,Category\n"  # missing invested/current_value
            "Some Fund - Growth,12345,Equity\n"
        )

        with pytest.raises(ValueError):
            extract_positions(path)

    def test_load_portfolio_picks_up_both_from_one_mixed_file(self, tmp_path, monkeypatch):
        """End to end, through the actual caller — not just the importer
        in isolation."""
        from mybroker import config
        from mybroker.portfolio.loader import load_portfolio

        monkeypatch.chdir(tmp_path)
        config.set_project_root(tmp_path)
        data = {"symbols": {}, "indices": {}}
        monkeypatch.setattr("mybroker.config.load_tickers", lambda: data)

        (tmp_path / "holdings_inbox").mkdir()
        (tmp_path / "holdings_inbox" / "consolidated.csv").write_text(
            "Scrip Name,Total Qty,Avg Rate,Holding Value,LTP,Market Value,PL (Rs.),PL%\n"
            "HDFC BANK LTD,74,925.41,68480.19,731.55,54134.70,-14345.64,-15.50\n"
            "\n"
            "Scheme Name,Folio,Invested,Current Value,Units\n"
            "UTI Large Cap Fund - Growth,111,120000,134000,500\n"
        )

        portfolio = load_portfolio(
            equity_path=tmp_path / "holdings.csv", mf_path=tmp_path / "holdings_mf.csv",
        )

        assert len(portfolio.equity) == 1
        assert len(portfolio.mutual_funds) == 1
        assert portfolio.mutual_funds[0].scheme_name == "UTI Large Cap Fund - Growth"


class TestMfNoAmfiCodeWarning:
    """A direct-plan investor's own statement never carries an AMFI code
    at all (there's no distributor to populate one), so the old per-row
    "no AMFI code" warning fired on every single scheme, every time —
    19 near-identical lines for a real user's actual statement, drowning
    out everything else. Must be one summary line, not one per scheme."""

    def test_batches_into_one_warning_not_one_per_scheme(self, tmp_path):
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "mf.csv"
        path.write_text(
            "Scheme Name,Folio No.,Invested Value,Current Value,Units\n"
            "Fund A - Growth,111,10000,11000,10\n"
            "Fund B - Growth,222,20000,21000,20\n"
            "Fund C - Growth,333,30000,31000,30\n"
        )

        _equity, mfs, warnings = extract_positions(path)

        assert len(mfs) == 3
        amfi_warnings = [w for w in warnings if "AMFI code" in w]
        assert len(amfi_warnings) == 1
        assert "3 schemes" in amfi_warnings[0]
        assert "direct-plan" in amfi_warnings[0]
        assert "folio" in amfi_warnings[0].lower()

    def test_singular_wording_for_exactly_one_scheme(self, tmp_path):
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "mf.csv"
        path.write_text(
            "Scheme Name,Folio No.,Invested Value,Current Value,Units\n"
            "Fund A - Growth,111,10000,11000,10\n"
        )

        _, _, warnings = extract_positions(path)

        amfi_warnings = [w for w in warnings if "AMFI code" in w]
        assert len(amfi_warnings) == 1
        assert "1 scheme " in amfi_warnings[0]  # not "1 schemes"

    def test_no_warning_at_all_when_every_scheme_has_a_code(self, tmp_path):
        from mybroker.portfolio.importers import extract_positions

        path = tmp_path / "mf.csv"
        path.write_text(
            "Scheme Name,AMFI Code,Invested Value,Current Value,Units\n"
            "Fund A - Growth,120503,10000,11000,10\n"
        )

        _, _, warnings = extract_positions(path)

        assert not [w for w in warnings if "AMFI code" in w]

    def test_strict_loader_path_batches_the_same_way(self, tmp_path):
        """loader.py's load_mutual_funds (the canonical holdings_mf.csv
        path) had the exact same per-row warning — same fix, same
        expectation, so a holdings_mf.csv user gets equally clean output
        as a holdings_inbox one."""
        from mybroker.portfolio.loader import load_mutual_funds

        path = tmp_path / "holdings_mf.csv"
        path.write_text(
            "Scheme Name,Folio,Invested,Current Value,Units\n"
            "Fund A - Growth,111,10000,11000,10\n"
            "Fund B - Growth,222,20000,21000,20\n"
        )

        _, warnings = load_mutual_funds(path)

        amfi_warnings = [w for w in warnings if "AMFI code" in w]
        assert len(amfi_warnings) == 1
        assert "2 schemes" in amfi_warnings[0]


class TestValidateSchema:
    """validate_schema — the actual gate an AI-proposed column mapping
    must clear before anything trusts it, checked against the file's own
    real data (agents/schema_resolver.py never gets to just assert a
    mapping is correct; this is what decides that, in code)."""

    _GRID = [
        ["Scheme Name", "Folio", "Invested", "Current Value", "Units"],
        ["Fund A - Growth", "111", "10000", "11000", "10.5"],
        ["Fund B - Growth", "222", "20000", "21500", "20.1"],
        ["Fund C - Growth", "333", "30000", "31000", "30.0"],
    ]

    def test_accepts_a_correct_mapping(self):
        from mybroker.portfolio.importers import validate_schema

        ok, problems = validate_schema(
            "mf", {"scheme_name": 0, "folio": 1, "invested": 2, "current_value": 3, "units": 4},
            self._GRID, header_row=0,
        )

        assert ok is True
        assert problems == []

    def test_rejects_a_numeric_field_pointed_at_text(self):
        """The core grounding check: a claim that "invested" is actually
        the scheme-name column must fail because the data doesn't
        support it, regardless of how confident the agent claims to be."""
        from mybroker.portfolio.importers import validate_schema

        ok, problems = validate_schema(
            "mf", {"scheme_name": 0, "invested": 0, "current_value": 3},
            self._GRID, header_row=0,
        )

        assert ok is False
        assert any("invested" in p and "doesn't look numeric" in p for p in problems)

    def test_rejects_a_text_field_pointed_at_numbers(self):
        from mybroker.portfolio.importers import validate_schema

        ok, problems = validate_schema(
            "mf", {"scheme_name": 1, "invested": 2, "current_value": 3},
            self._GRID, header_row=0,
        )

        assert ok is False
        assert any("scheme_name" in p and "purely numeric" in p for p in problems)

    def test_rejects_missing_required_fields(self):
        from mybroker.portfolio.importers import validate_schema

        ok, problems = validate_schema(
            "mf", {"scheme_name": 0}, self._GRID, header_row=0,
        )

        assert ok is False
        assert any("missing required field" in p for p in problems)

    def test_rejects_an_out_of_range_column_index(self):
        from mybroker.portfolio.importers import validate_schema

        ok, problems = validate_schema(
            "mf", {"scheme_name": 0, "invested": 2, "current_value": 99},
            self._GRID, header_row=0,
        )

        assert ok is False
        assert any("out of range" in p for p in problems)

    def test_rejects_an_unrecognised_kind(self):
        from mybroker.portfolio.importers import validate_schema

        ok, problems = validate_schema(None, {}, self._GRID, header_row=0)

        assert ok is False
        assert any("unrecognised kind" in p for p in problems)

    def test_null_columns_are_simply_skipped(self):
        """A field the agent honestly says it couldn't find (null) is not
        a problem — only a WRONG claim is."""
        from mybroker.portfolio.importers import validate_schema

        ok, problems = validate_schema(
            "mf",
            {"scheme_name": 0, "invested": 2, "current_value": 3, "amfi_code": None},
            self._GRID, header_row=0,
        )

        assert ok is True
        assert problems == []

    def test_tolerates_a_stray_blank_in_an_otherwise_numeric_column(self):
        grid = [
            ["Scheme Name", "Invested", "Current Value"],
            ["Fund A", "10000", "11000"],
            ["Fund B", "20000", "21000"],
            ["Fund C", "30000", "31000"],
            ["Fund D", "-", "40000"],  # one legitimate blank
        ]
        from mybroker.portfolio.importers import validate_schema

        ok, problems = validate_schema(
            "mf", {"scheme_name": 0, "invested": 1, "current_value": 2},
            grid, header_row=0,
        )

        assert ok is True

    def test_rejects_a_predominantly_non_numeric_claimed_numeric_column(self):
        grid = [
            ["Scheme Name", "Invested", "Current Value"],
            ["Fund A", "not a number", "11000"],
            ["Fund B", "also text", "21000"],
            ["Fund C", "30000", "31000"],
        ]
        from mybroker.portfolio.importers import validate_schema

        ok, problems = validate_schema(
            "mf", {"scheme_name": 0, "invested": 1, "current_value": 2},
            grid, header_row=0,
        )

        assert ok is False
        assert any("invested" in p for p in problems)


class TestColumnMapCache:
    """The durable record of a column mapping already learned via
    AI-assisted schema resolution and confirmed against real data — keyed
    by header signature, not filename, so the SAME broker/DP format is
    recognised in any FUTURE file too."""

    @pytest.fixture(autouse=True)
    def isolated_project(self, tmp_path, monkeypatch):
        from mybroker import config

        monkeypatch.chdir(tmp_path)
        config.set_project_root(tmp_path)
        yield tmp_path

    def test_round_trips_through_save_and_load(self):
        from mybroker.portfolio.importers import load_column_map_cache, save_column_map

        header = ["Scheme Name", "Folio", "Invested", "Current Value"]
        save_column_map(
            header, kind="mf",
            columns={"scheme_name": 0, "folio": 1, "invested": 2, "current_value": 3},
            source="statement.xlsx", confidence="high", reasoning="clear header",
        )

        cache = load_column_map_cache()
        assert len(cache) == 1
        entry = next(iter(cache.values()))
        assert entry["kind"] == "mf"
        assert entry["columns"]["invested"] == 2
        assert entry["learned_from"] == "statement.xlsx"

    def test_missing_cache_file_returns_empty_dict(self):
        from mybroker.portfolio.importers import load_column_map_cache

        assert load_column_map_cache() == {}

    def test_corrupted_cache_file_is_treated_as_empty_not_a_crash(self):
        from mybroker.config import COLUMN_MAP_CACHE
        from mybroker.portfolio.importers import load_column_map_cache

        COLUMN_MAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
        COLUMN_MAP_CACHE.write_text("not valid json {{{")

        assert load_column_map_cache() == {}

    def test_extract_positions_uses_a_cached_mapping_when_keyword_matching_would_fail(self, tmp_path):
        """The actual point of the whole mechanism: a file whose header
        the deterministic keyword matching can't resolve at all still
        parses correctly once a mapping for that exact header has been
        learned and cached."""
        from mybroker.portfolio.importers import extract_positions, save_column_map

        # A deliberately unrecognisable header — none of _MF_COL_KEYWORDS'
        # keywords appear anywhere in it, so without the cache this would
        # not even classify as a table at all.
        header = ["Xyzzy1", "Xyzzy2", "Xyzzy3"]
        save_column_map(
            header, kind="mf",
            columns={"scheme_name": 0, "invested": 1, "current_value": 2},
            source="weird.csv", confidence="high", reasoning="learned earlier",
        )

        path = tmp_path / "weird.csv"
        path.write_text(
            "Xyzzy1,Xyzzy2,Xyzzy3\n"
            "Fund A - Growth,10000,11000\n"
        )

        equity, mfs, _warnings = extract_positions(path)

        assert equity == []
        assert mfs[0].scheme_name == "Fund A - Growth"
        assert mfs[0].invested == pytest.approx(10000.0)
        assert mfs[0].current_value == pytest.approx(11000.0)


class TestFindUnresolvableFiles:
    """find_unresolvable_files — what cli.py's `factfolio validate` uses
    to decide which holdings_inbox files to offer AI-assisted schema
    resolution for."""

    @pytest.fixture(autouse=True)
    def isolated_project(self, tmp_path, monkeypatch):
        from mybroker import config

        monkeypatch.chdir(tmp_path)
        config.set_project_root(tmp_path)
        yield tmp_path

    def test_does_not_flag_a_file_that_already_parses_fine(self, tmp_path):
        from mybroker.config import HOLDINGS_INBOX_DIR
        from mybroker.portfolio.importers import find_unresolvable_files

        HOLDINGS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
        (HOLDINGS_INBOX_DIR / "clean.csv").write_text(
            "Scheme Name,Folio,Invested,Current Value,Units\n"
            "Fund A - Growth,111,10000,11000,10\n"
        )

        assert find_unresolvable_files() == []

    def test_flags_a_recognised_header_with_unresolvable_columns(self, tmp_path):
        """A header keyword matching DOES find (scheme/folio/units all
        hit) but whose required columns still can't be resolved."""
        from mybroker.config import HOLDINGS_INBOX_DIR
        from mybroker.portfolio.importers import find_unresolvable_files

        HOLDINGS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
        (HOLDINGS_INBOX_DIR / "weird_mf.csv").write_text(
            "Scheme Name,Folio,Weirdly Named Value Column,Units\n"
            "Fund A - Growth,111,10000,10\n"
        )

        found = find_unresolvable_files()

        assert len(found) == 1
        assert found[0]["path"].name == "weird_mf.csv"
        assert found[0]["grid_excerpt"][0] == [
            "Scheme Name", "Folio", "Weirdly Named Value Column", "Units",
        ]
        assert found[0]["grid_excerpt"][1] == ["Fund A - Growth", "111", "10000", "10"]
        assert "current_value" in found[0]["error"] or "invested" in found[0]["error"]

    def test_flags_a_header_no_keyword_recognises_at_all(self, tmp_path):
        """The actual gap a real user pointed out: a file whose header
        uses completely different terminology throughout — zero overlap
        with any equity/mf keyword hint — used to be entirely invisible
        to this function (it required a header row already found by
        keyword first), so `factfolio validate` never even offered AI
        help for it. Must be flagged exactly the same as the "found but
        unresolved" case above."""
        from mybroker.config import HOLDINGS_INBOX_DIR
        from mybroker.portfolio.importers import find_unresolvable_files

        HOLDINGS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
        (HOLDINGS_INBOX_DIR / "totally_alien.csv").write_text(
            "Asset,Qty Held,Buy Rate,Mkt Rate,Total Cost,Total Worth\n"
            "RELIANCE,10,2500,2600,25000,26000\n"
        )

        found = find_unresolvable_files()

        assert len(found) == 1
        assert found[0]["path"].name == "totally_alien.csv"
        assert "could not find a recognisable" in found[0]["error"]
        assert found[0]["grid_excerpt"][0] == [
            "Asset", "Qty Held", "Buy Rate", "Mkt Rate", "Total Cost", "Total Worth",
        ]

    def test_grid_excerpt_is_capped(self, tmp_path):
        from mybroker.config import HOLDINGS_INBOX_DIR
        from mybroker.portfolio.importers import (
            _SCHEMA_EXCERPT_ROWS,
            find_unresolvable_files,
        )

        HOLDINGS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
        rows = "\n".join(f"junk{i},{i}" for i in range(_SCHEMA_EXCERPT_ROWS + 20))
        (HOLDINGS_INBOX_DIR / "huge.csv").write_text(f"Asset,Qty Held\n{rows}\n")

        found = find_unresolvable_files()

        assert len(found[0]["grid_excerpt"]) == _SCHEMA_EXCERPT_ROWS

    def test_does_not_reflag_a_file_already_covered_by_the_cache(self, tmp_path):
        from mybroker.config import HOLDINGS_INBOX_DIR
        from mybroker.portfolio.importers import find_unresolvable_files, save_column_map

        HOLDINGS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
        header = ["Scheme Name", "Folio", "Weirdly Named Value Column", "Units"]
        (HOLDINGS_INBOX_DIR / "weird_mf.csv").write_text(
            "Scheme Name,Folio,Weirdly Named Value Column,Units\n"
            "Fund A - Growth,111,10000,10\n"
        )
        save_column_map(
            header, kind="mf",
            columns={"scheme_name": 0, "folio": 1, "invested": 2, "current_value": 2, "units": 3},
            source="weird_mf.csv", confidence="high", reasoning="learned",
        )

        assert find_unresolvable_files() == []


# ── PDF cell text cleanup ────────────────────────────────────────────────────
class TestCleanPdfCell:
    """_clean_pdf_cell — pdfplumber wraps long cell text across internal
    lines (a narrow 'Stock Name' column, say), which used to reach every
    downstream warning/log/name as literal embedded newlines: "AXIS
    BANK\\nLIMITED" printed as a garbled multi-line mess in the terminal."""

    def test_collapses_embedded_newlines_to_single_spaces(self):
        from mybroker.portfolio.importers import _clean_pdf_cell

        assert _clean_pdf_cell("AXIS BANK\nLIMITED") == "AXIS BANK LIMITED"
        assert _clean_pdf_cell("TATA\nSTEEL\nLIMITED") == "TATA STEEL LIMITED"

    def test_none_becomes_empty_string(self):
        from mybroker.portfolio.importers import _clean_pdf_cell

        assert _clean_pdf_cell(None) == ""

    def test_ordinary_cell_is_unchanged(self):
        from mybroker.portfolio.importers import _clean_pdf_cell

        assert _clean_pdf_cell("HDFCBANK") == "HDFCBANK"

    def test_leading_and_trailing_whitespace_still_stripped(self):
        from mybroker.portfolio.importers import _clean_pdf_cell

        assert _clean_pdf_cell("  NTPC LTD  \n") == "NTPC LTD"


# ── Password-protected PDFs (CAMS/KFintech/NSDL/CDSL CAS statements) ────────
class TestPasswordProtectedPdf:
    @pytest.fixture
    def encrypted_pdf(self, tmp_path):
        """A minimal real encrypted PDF — pypdf is test-only, never a
        runtime dependency (pdfplumber does the actual reading)."""
        pypdf = pytest.importorskip("pypdf")

        path = tmp_path / "statement.pdf"
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=400, height=200)
        writer.encrypt(user_password="MYPAN1234A", owner_password="MYPAN1234A")
        with path.open("wb") as fh:
            writer.write(fh)
        return path

    def test_no_password_available_raises_actionably(self, encrypted_pdf):
        from mybroker.portfolio.importers import read_grid

        with pytest.raises(ValueError, match="password"):
            read_grid(encrypted_pdf)

    def test_sidecar_password_file_opens_it(self, encrypted_pdf):
        from mybroker.portfolio.importers import read_grid

        (encrypted_pdf.parent / f"{encrypted_pdf.name}.password").write_text("MYPAN1234A")

        read_grid(encrypted_pdf)  # doesn't raise

    def test_env_var_password_opens_it(self, encrypted_pdf, monkeypatch):
        from mybroker.portfolio.importers import read_grid

        monkeypatch.setenv("FACTFOLIO_PDF_PASSWORD", "MYPAN1234A")

        read_grid(encrypted_pdf)  # doesn't raise

    def test_wrong_password_raises_actionably(self, encrypted_pdf, monkeypatch):
        from mybroker.portfolio.importers import read_grid

        monkeypatch.setenv("FACTFOLIO_PDF_PASSWORD", "WRONGPASS")

        with pytest.raises(ValueError, match="password"):
            read_grid(encrypted_pdf)

    def test_password_sidecar_is_never_treated_as_a_holdings_file(self, encrypted_pdf):
        """The sidecar itself must not be picked up by the inbox scan —
        .password isn't a supported holdings format."""
        from mybroker.portfolio.importers import discover_inbox_files

        (encrypted_pdf.parent / f"{encrypted_pdf.name}.password").write_text("MYPAN1234A")

        found = discover_inbox_files(encrypted_pdf.parent)
        assert found == [encrypted_pdf]


# ── "cd into the project first" hint ─────────────────────────────────────────
# Real-world regression: `factfolio init` ran from ~/Desktop, creating
# ~/Desktop/factfolio/ — then `factfolio status` ran from ~/Desktop itself
# (forgetting the `cd` init's own output told you to do), hitting "no
# holdings"/"no policy" even though the real project, one level down, has
# both. "run factfolio init" is actively wrong advice there — it already ran.
class TestCdHint:
    def test_no_hint_when_nothing_nearby(self, tmp_path, monkeypatch):
        from mybroker.config import cd_hint_if_project_nearby

        monkeypatch.chdir(tmp_path)
        assert cd_hint_if_project_nearby() == ""

    def test_hints_when_a_sibling_project_exists(self, tmp_path, monkeypatch):
        from mybroker.config import cd_hint_if_project_nearby

        nested = tmp_path / "factfolio" / "memory" / "investment_policy.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("x")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "mybroker.config.POLICY_FILE", tmp_path / "memory" / "investment_policy.md"
        )

        assert "cd factfolio" in cd_hint_if_project_nearby()

    def test_no_hint_when_already_inside_that_project(self, tmp_path, monkeypatch):
        """Guard against suggesting `cd factfolio` while already standing
        inside the very folder it would point at."""
        from mybroker.config import cd_hint_if_project_nearby

        nested = tmp_path / "factfolio" / "memory" / "investment_policy.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("x")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("mybroker.config.POLICY_FILE", nested)  # already pointing at it

        assert cd_hint_if_project_nearby() == ""

    def test_missing_equity_error_includes_the_hint(self, tmp_path, monkeypatch):
        nested = tmp_path / "factfolio" / "memory" / "investment_policy.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("x")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "mybroker.config.POLICY_FILE", tmp_path / "memory" / "investment_policy.md"
        )

        with pytest.raises(FileNotFoundError, match="cd factfolio"):
            load_portfolio(
                equity_path=tmp_path / "nope.csv",
                mf_path=tmp_path / "nope_mf.csv",
                inbox_dir=tmp_path / "empty_inbox",
            )

    def test_missing_policy_error_includes_the_hint(self, tmp_path, monkeypatch):
        """policy.py binds POLICY_FILE via its own top-level `from
        mybroker.config import POLICY_FILE` — an independent name, not a
        live reference — so both it and config.py's own copy need
        patching for Policy.load()'s default path and
        cd_hint_if_project_nearby()'s comparison to agree, same as they
        would from the same real os.environ-based resolution un-mocked."""
        from mybroker.portfolio.policy import Policy

        nested = tmp_path / "factfolio" / "memory" / "investment_policy.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("x")
        monkeypatch.chdir(tmp_path)
        missing = tmp_path / "memory" / "investment_policy.md"
        monkeypatch.setattr("mybroker.config.POLICY_FILE", missing)
        monkeypatch.setattr("mybroker.portfolio.policy.POLICY_FILE", missing)

        with pytest.raises(FileNotFoundError, match="cd factfolio"):
            Policy.load()
