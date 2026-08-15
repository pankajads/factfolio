"""Parse broker exports into normalised position objects.

Handles the Zerodha Kite equity export and an optional mutual-fund export.
Both are tolerated in slightly messy forms — broker exports vary — but a
column that cannot be interpreted is an error, never a silent zero.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from mybroker.config import HOLDINGS_EQUITY, HOLDINGS_INBOX_DIR, HOLDINGS_MF, symbol_meta

# Zerodha's headers, normalised (lowercased, punctuation stripped) → our field.
_EQUITY_COLUMNS = {
    "instrument": "symbol",
    "qty": "quantity",
    "avg cost": "avg_cost",
    "ltp": "ltp",
    "invested": "invested",
    "cur val": "current_value",
    "pl": "pnl",
    "net chg": "net_change_pct",
    "day chg": "day_change_pct",
}


def _norm(header: str) -> str:
    """Normalise a CSV header: lowercase, drop punctuation and whitespace."""
    return header.strip().lower().replace(".", "").replace("&", "").replace("%", "").strip()


def _num(raw: str | float | int | None, *, field_name: str, row: int) -> float:
    """Parse a numeric cell. Empty → 0.0; garbage → explicit error."""
    if raw is None:
        return 0.0
    if isinstance(raw, int | float):
        return float(raw)
    cleaned = raw.strip().replace(",", "").replace("₹", "")
    if cleaned in ("", "-", "--", "NA", "N/A"):
        return 0.0
    try:
        return float(cleaned)
    except ValueError as exc:
        raise ValueError(
            f"Row {row}: cannot parse {field_name}={raw!r} as a number. "
            f"Fix the export rather than letting this default to zero."
        ) from exc


@dataclass
class EquityPosition:
    """One direct-equity holding."""

    symbol: str
    quantity: float
    avg_cost: float
    ltp: float
    invested: float
    current_value: float
    pnl: float
    net_change_pct: float

    # Enriched from tickers.yaml
    name: str = ""
    sector: str = "Unknown"
    tier: str = "unknown"
    bucket: str = "satellite"

    # Optional — only known if the user supplies purchase dates
    purchase_date: date | None = None

    @property
    def pnl_pct(self) -> float:
        return (self.pnl / self.invested * 100) if self.invested else 0.0


@dataclass
class MFPosition:
    """One mutual-fund holding."""

    scheme_name: str
    amfi_code: str
    units: float
    avg_nav: float
    current_nav: float
    invested: float
    current_value: float
    category: str = "Unknown"
    folio: str = ""

    @property
    def pnl(self) -> float:
        return self.current_value - self.invested

    @property
    def pnl_pct(self) -> float:
        return (self.pnl / self.invested * 100) if self.invested else 0.0


@dataclass
class Portfolio:
    """The full portfolio: direct equity plus mutual funds."""

    equity: list[EquityPosition] = field(default_factory=list)
    mutual_funds: list[MFPosition] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ── Totals ───────────────────────────────────────────────────────────────
    @property
    def equity_invested(self) -> float:
        return sum(p.invested for p in self.equity)

    @property
    def equity_value(self) -> float:
        return sum(p.current_value for p in self.equity)

    @property
    def mf_invested(self) -> float:
        return sum(p.invested for p in self.mutual_funds)

    @property
    def mf_value(self) -> float:
        return sum(p.current_value for p in self.mutual_funds)

    @property
    def total_invested(self) -> float:
        return self.equity_invested + self.mf_invested

    @property
    def total_value(self) -> float:
        return self.equity_value + self.mf_value

    @property
    def total_pnl(self) -> float:
        return self.total_value - self.total_invested

    @property
    def total_pnl_pct(self) -> float:
        return (self.total_pnl / self.total_invested * 100) if self.total_invested else 0.0

    @property
    def has_mutual_funds(self) -> bool:
        return bool(self.mutual_funds)


def load_equity(path: Path | None = None) -> tuple[list[EquityPosition], list[str]]:
    """Parse a Zerodha Kite holdings export.

    Returns (positions, warnings). A symbol missing from tickers.yaml is a
    warning rather than a crash so the rest of the portfolio still loads —
    but it is surfaced loudly, because unmapped symbols get no market data.
    """
    path = path or HOLDINGS_EQUITY
    if not path.exists():
        raise FileNotFoundError(
            f"No equity holdings at {path}. Export from Kite → Holdings → download."
        )

    positions: list[EquityPosition] = []
    warnings: list[str] = []

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError(f"{path} is empty.") from exc

        # Map column index → our field name, ignoring the unnamed trailing column.
        idx: dict[str, int] = {}
        for i, raw in enumerate(header):
            key = _EQUITY_COLUMNS.get(_norm(raw))
            if key:
                idx[key] = i

        missing = {"symbol", "quantity", "avg_cost", "invested", "current_value"} - idx.keys()
        if missing:
            raise ValueError(
                f"{path} is missing required column(s): {sorted(missing)}. "
                f"Found headers: {header}"
            )

        for rownum, row in enumerate(reader, start=2):
            if not row or not row[idx["symbol"]].strip():
                continue  # blank line / trailing newline

            symbol = row[idx["symbol"]].strip().upper()

            def get(fname: str, _row=row, _n=rownum) -> float:
                if fname not in idx:
                    return 0.0
                return _num(_row[idx[fname]], field_name=fname, row=_n)

            invested = get("invested")
            current_value = get("current_value")
            # Prefer the broker's P&L; derive it if the column is absent.
            pnl = get("pnl") if "pnl" in idx else current_value - invested

            pos = EquityPosition(
                symbol=symbol,
                quantity=get("quantity"),
                avg_cost=get("avg_cost"),
                ltp=get("ltp"),
                invested=invested,
                current_value=current_value,
                pnl=pnl,
                net_change_pct=get("net_change_pct"),
            )

            try:
                meta = symbol_meta(symbol)
                pos.name = meta.get("name", symbol)
                pos.sector = meta.get("sector", "Unknown")
                pos.tier = meta.get("tier", "unknown")
                pos.bucket = meta.get("bucket", "satellite")
            except KeyError:
                warnings.append(
                    f"{symbol} is not in tickers.yaml — no market data, sector, or "
                    f"policy classification will be available for it."
                )

            positions.append(pos)

    if not positions:
        raise ValueError(f"{path} contained a header but no position rows.")

    return positions, warnings


def load_mutual_funds(path: Path | None = None) -> tuple[list[MFPosition], list[str]]:
    """Parse a mutual-fund export. Absent file is not an error — returns empty."""
    path = path or HOLDINGS_MF
    if not path.exists():
        return [], []

    positions: list[MFPosition] = []
    warnings: list[str] = []

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        norm_fields = {_norm(f): f for f in (reader.fieldnames or [])}

        def col(*names: str) -> str | None:
            for n in names:
                if n in norm_fields:
                    return norm_fields[n]
            return None

        c_name = col("scheme name", "scheme", "instrument", "name")
        c_code = col("amfi code", "amfi", "scheme code", "code")
        c_units = col("units", "qty", "quantity")
        c_avgnav = col("avg nav", "avg cost", "average nav")
        c_curnav = col("current nav", "nav", "ltp")
        c_inv = col("invested", "investment", "cost value")
        c_cur = col("current value", "cur val", "market value")
        c_cat = col("category", "type", "scheme category")
        c_folio = col("folio", "folio no", "folio number")

        if not c_name:
            raise ValueError(
                f"{path}: could not find a scheme-name column. "
                f"Found: {reader.fieldnames}"
            )

        for rownum, row in enumerate(reader, start=2):
            name = (row.get(c_name) or "").strip()
            if not name:
                continue

            def g(c: str | None, _row=row, _n=rownum) -> float:
                return _num(_row.get(c), field_name=c or "?", row=_n) if c else 0.0

            invested = g(c_inv)
            current = g(c_cur)
            units = g(c_units)
            avg_nav = g(c_avgnav)
            cur_nav = g(c_curnav)

            # Derive whichever value is missing, when possible.
            if not current and units and cur_nav:
                current = units * cur_nav
            if not invested and units and avg_nav:
                invested = units * avg_nav

            code = (row.get(c_code) or "").strip() if c_code else ""
            if not code:
                warnings.append(
                    f"{name}: no AMFI code — NAV lookups and overlap analysis "
                    f"will be unavailable for this scheme."
                )

            positions.append(
                MFPosition(
                    scheme_name=name,
                    amfi_code=code,
                    units=units,
                    avg_nav=avg_nav,
                    current_nav=cur_nav,
                    invested=invested,
                    current_value=current,
                    category=(row.get(c_cat) or "Unknown").strip() if c_cat else "Unknown",
                    folio=(row.get(c_folio) or "").strip() if c_folio else "",
                )
            )

    return positions, warnings


def load_portfolio(
    equity_path: Path | None = None,
    mf_path: Path | None = None,
    *,
    inbox_dir: Path | None = None,
    include_inbox: bool = True,
) -> Portfolio:
    """Load the complete portfolio: the legacy holdings.csv/holdings_mf.csv
    pair, plus (by default) every file dropped in holdings_inbox/ — any
    format (csv, xls, xlsx, pdf), equity or mutual fund, sniffed and
    classified by `portfolio.importers`.

    Equity data is required overall, but no longer strictly from
    HOLDINGS_EQUITY alone — a PDF or Excel equity export sitting in the
    inbox satisfies it too. Set `include_inbox=False` to reproduce the old,
    root-files-only behaviour exactly (used by tests pinned to holdings.csv).
    """
    equity: list[EquityPosition] = []
    mfs: list[MFPosition] = []
    warnings: list[str] = []
    equity_path_exists = (equity_path or HOLDINGS_EQUITY).exists()

    if equity_path_exists:
        eq, eq_warn = load_equity(equity_path)
        equity.extend(eq)
        warnings.extend(eq_warn)

    mf_result_present = (mf_path or HOLDINGS_MF).exists()
    mfs_root, mf_warn = load_mutual_funds(mf_path)
    mfs.extend(mfs_root)
    warnings.extend(mf_warn)

    if include_inbox:
        from mybroker.portfolio.importers import discover_inbox_files, extract_positions

        for file in discover_inbox_files(inbox_dir):
            try:
                kind, positions, file_warnings = extract_positions(file)
            except ValueError as exc:
                warnings.append(str(exc))
                continue

            warnings.extend(file_warnings)
            if kind == "equity":
                equity.extend(positions)
                equity_path_exists = True
            else:
                mfs.extend(positions)
                mf_result_present = True

    if not equity:
        raise FileNotFoundError(
            f"No equity holdings found. Export from Kite → Holdings → download "
            f"to {HOLDINGS_EQUITY}, or drop any csv/xls/xlsx/pdf export into "
            f"{HOLDINGS_INBOX_DIR}/."
        )

    if not mfs and not mf_result_present:
        warnings.append(
            "No mutual-fund holdings found. Core/satellite analysis covers direct "
            f"equity only — add holdings_mf.csv, or drop an export into "
            f"{HOLDINGS_INBOX_DIR}/, for a complete picture."
        )

    return Portfolio(equity=equity, mutual_funds=mfs, warnings=warnings)
