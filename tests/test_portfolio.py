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

        kind, positions, warnings = extract_positions(path)
        assert kind == "mf"
        assert len(positions) == 7

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

        _, positions, _ = extract_positions(path)
        assert sum(p.invested for p in positions) == pytest.approx(raw_invested, abs=0.01)
        assert sum(p.current_value for p in positions) == pytest.approx(raw_current, abs=1.0)

    def test_load_portfolio_merges_inbox_by_default(self):
        from mybroker.config import HOLDINGS_INBOX_DIR

        if not (HOLDINGS_INBOX_DIR / "holding_mf.xls").exists():
            pytest.skip("holdings_inbox/holding_mf.xls not present")

        merged = load_portfolio()  # include_inbox=True by default
        assert merged.has_mutual_funds
        assert len(merged.mutual_funds) >= 7

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

        kind, positions, _warnings = extract_positions(path)
        assert kind == "equity"
        assert {p.symbol for p in positions} == {"INFY", "TCS"}

    def test_txt_falls_back_to_comma_when_sniffing_fails(self, tmp_path):
        """A single-column file gives the sniffer nothing to distinguish —
        it should default to comma rather than raising."""
        from mybroker.portfolio.importers import _read_txt_grid

        path = tmp_path / "single_column.txt"
        path.write_text("Instrument\nINFY\nTCS\n")

        grid = _read_txt_grid(path)
        assert grid == [["Instrument"], ["INFY"], ["TCS"]]
