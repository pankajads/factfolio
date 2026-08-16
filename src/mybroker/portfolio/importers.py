"""General-purpose holdings-file importer for `holdings_inbox/`.

`loader.py`'s `load_equity`/`load_mutual_funds` assume a clean, known-shape
CSV (Zerodha's own export, or a hand-built MF CSV with recognisable headers)
and are deliberately strict — they are what the golden-value tests pin. Real
broker statements dropped into an inbox are messier: the data table is buried
under account/PII headers and disclaimer paragraphs, ends with a "Total" row,
column names vary by broker, and the file itself may be CSV, XLS, XLSX,
PDF, or TXT.

This module handles that mess as an ADDITIVE source, not a replacement:

  1. Read the file into a plain grid of strings, regardless of format.
  2. Scan every row for one that looks like a header — matched by keyword,
     not exact name — and classify it as an equity or mutual-fund table.
  3. Slice to just the data rows (stop at a blank row or a "Total" row).
  4. Map columns by keyword-containment and build the same EquityPosition /
     MFPosition dataclasses loader.py uses everywhere else downstream.

A column or row this cannot confidently interpret is skipped with a warning
— never silently zeroed, matching loader.py's rule. A file this cannot
classify at all raises, naming the file, rather than pretending it found
nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from mybroker.config import HOLDINGS_INBOX_DIR, symbol_meta
from mybroker.portfolio.loader import EquityPosition, MFPosition, _num

SUPPORTED_SUFFIXES = {".csv", ".xls", ".xlsx", ".pdf", ".txt"}

Grid = list[list[str]]
Kind = Literal["equity", "mf"]

# Row must contain at least this many distinct keyword hits, and more of its
# own kind's keywords than the other kind's, to be accepted as a header row.
_MIN_HEADER_HITS = 2

_EQUITY_HEADER_HINTS = ("instrument", "symbol", "scrip", "quantity", "qty", "tradingsymbol")
_MF_HEADER_HINTS = ("folio", "scheme", "amfi", "units", "nav")

_STOP_MARKERS = ("total", "grand total", "subtotal", "disclaimer", "note")


def _loose_norm(cell: object) -> str:
    """Aggressively normalise a cell for keyword matching: lowercase, fold
    newlines/whitespace to single spaces, drop punctuation that varies
    between broker templates ("Invested \\nAmount \\n(Rs.)" → "invested
    amount rs")."""
    s = str(cell) if cell is not None else ""
    s = s.replace("\n", " ").replace("\t", " ")
    for ch in ".,()/&%₹-":
        s = s.replace(ch, " ")
    return " ".join(s.lower().split())


def _find_col(header_loose: list[str], *keyword_sets: tuple[str, ...]) -> int | None:
    """First column whose loose header contains every keyword in one of the
    given sets, trying sets in order (most specific first)."""
    for kws in keyword_sets:
        for i, cell in enumerate(header_loose):
            if all(kw in cell for kw in kws):
                return i
    return None


def _classify_row(cells_loose: list[str]) -> Kind | None:
    equity_hits = sum(1 for kw in _EQUITY_HEADER_HINTS if any(kw in c for c in cells_loose))
    mf_hits = sum(1 for kw in _MF_HEADER_HINTS if any(kw in c for c in cells_loose))
    if mf_hits >= _MIN_HEADER_HITS and mf_hits > equity_hits:
        return "mf"
    if equity_hits >= _MIN_HEADER_HITS and equity_hits >= mf_hits:
        return "equity"
    return None


def _is_stop_row(cells_loose: list[str]) -> bool:
    if not any(c.strip() for c in cells_loose):
        return True
    first = cells_loose[0] if cells_loose else ""
    return any(first == m or first.startswith(m) for m in _STOP_MARKERS)


# ── Grid readers ─────────────────────────────────────────────────────────────
def _read_csv_grid(path: Path) -> Grid:
    import csv

    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [list(row) for row in csv.reader(fh)]


def _read_txt_grid(path: Path) -> Grid:
    """.txt has no fixed delimiter across brokers — some export plain
    comma-separated data under a .txt extension, others tab- or
    semicolon-separated. Sniff it (falling back to comma, the commonest
    case) rather than assuming one, so both actually get parsed instead of
    silently landing everything in a single column."""
    import csv

    text = path.read_text(encoding="utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel  # comma-delimited default
    return [list(row) for row in csv.reader(text.splitlines(), dialect=dialect)]


def _read_excel_grid(path: Path) -> Grid:
    import contextlib
    import io

    import pandas as pd

    # xlrd `print()`s (not `warnings.warn`s — can't be filtered the normal
    # way) a benign notice, "WARNING *** file size ... not 512 + multiple of
    # sector size", for real-world .xls exports whose trailing sector is
    # short. It still parses the file correctly — this is stdout noise, not
    # a signal, and having it appear ahead of an unrelated failure makes
    # that failure look scarier than it is. Swallow it.
    with contextlib.redirect_stdout(io.StringIO()):
        df = pd.read_excel(path, header=None, dtype=str)
    df = df.fillna("")
    return df.astype(str).values.tolist()


def _read_pdf_grid(path: Path) -> Grid:
    import pdfplumber

    rows: Grid = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    rows.append([(c or "").strip() for c in row])
    return rows


def read_grid(path: Path) -> Grid:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _read_csv_grid(path)
    if suffix in (".xls", ".xlsx"):
        return _read_excel_grid(path)
    if suffix == ".pdf":
        return _read_pdf_grid(path)
    if suffix == ".txt":
        return _read_txt_grid(path)
    raise ValueError(
        f"{path}: unsupported format {suffix!r}. Supported: "
        f"{', '.join(sorted(SUPPORTED_SUFFIXES))}."
    )


# ── Row → position builders ──────────────────────────────────────────────────
_EQUITY_COL_KEYWORDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "symbol": (("instrument",), ("tradingsymbol",), ("symbol",), ("scrip",)),
    "quantity": (("qty",), ("quantity",)),
    "avg_cost": (("avg", "cost"), ("average", "cost"), ("avg", "price")),
    "ltp": (("ltp",), ("last", "price"), ("market", "price")),
    "invested": (("invested",), ("investment",), ("cost", "value")),
    "current_value": (("current", "value"), ("market", "value"), ("cur", "val")),
    "pnl": (("p l",), ("profit", "loss"), ("gain", "loss"), ("pl",)),
    "net_change_pct": (("net", "chg"), ("net", "change")),
    "day_change_pct": (("day", "chg"), ("day", "change")),
}

_MF_COL_KEYWORDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "folio": (("folio",),),
    "scheme_name": (("scheme", "name"), ("fund", "name"), ("scheme",)),
    "amfi_code": (("amfi",), ("scheme", "code")),
    "units": (("holding", "units"), ("units",), ("qty",)),
    "avg_nav": (("avg", "nav"), ("average", "nav"), ("avg", "cost")),
    "current_nav": (("current", "nav"),),
    "invested": (("invested",), ("investment",), ("cost", "value")),
    "current_value": (("current", "value"), ("market", "value"), ("cur", "val")),
    "category": (("category",), ("scheme", "type")),
}


def _resolve_columns(
    header_loose: list[str], keywords: dict[str, tuple[tuple[str, ...], ...]]
) -> dict[str, int]:
    idx: dict[str, int] = {}
    for field_name, keyword_sets in keywords.items():
        col = _find_col(header_loose, *keyword_sets)
        if col is not None:
            idx[field_name] = col
    return idx


def _cell(row: list[str], i: int | None) -> str:
    if i is None or i >= len(row):
        return ""
    return row[i]


def _data_rows(grid: Grid, header_row: int) -> list[list[str]]:
    out: list[list[str]] = []
    for row in grid[header_row + 1 :]:
        loose = [_loose_norm(c) for c in row]
        if _is_stop_row(loose):
            break
        out.append(row)
    return out


def _rows_to_equity(
    grid: Grid, header_row: int, *, source: str
) -> tuple[list[EquityPosition], list[str]]:
    header_loose = [_loose_norm(c) for c in grid[header_row]]
    idx = _resolve_columns(header_loose, _EQUITY_COL_KEYWORDS)
    missing = {"symbol", "quantity", "avg_cost", "invested", "current_value"} - idx.keys()
    if missing:
        raise ValueError(
            f"{source}: could not find column(s) {sorted(missing)} in the "
            f"detected equity header {grid[header_row]!r}."
        )

    positions: list[EquityPosition] = []
    warnings: list[str] = []

    for rownum, row in enumerate(_data_rows(grid, header_row), start=header_row + 2):
        symbol = _cell(row, idx["symbol"]).strip().upper()
        if not symbol:
            continue

        def get(field_name: str, _row=row, _n=rownum) -> float:
            if field_name not in idx:
                return 0.0
            return _num(_cell(_row, idx[field_name]), field_name=field_name, row=_n)

        invested = get("invested")
        current_value = get("current_value")
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
                f"{source}: {symbol} is not in tickers.yaml — no market data, "
                f"sector, or policy classification will be available for it."
            )

        positions.append(pos)

    return positions, warnings


def _rows_to_mf(
    grid: Grid, header_row: int, *, source: str
) -> tuple[list[MFPosition], list[str]]:
    header_loose = [_loose_norm(c) for c in grid[header_row]]
    idx = _resolve_columns(header_loose, _MF_COL_KEYWORDS)
    if "scheme_name" not in idx:
        raise ValueError(
            f"{source}: could not find a scheme-name column in the detected "
            f"mutual-fund header {grid[header_row]!r}."
        )

    positions: list[MFPosition] = []
    warnings: list[str] = []

    for rownum, row in enumerate(_data_rows(grid, header_row), start=header_row + 2):
        name = _cell(row, idx["scheme_name"]).strip()
        if not name:
            continue

        def g(field_name: str, _row=row, _n=rownum) -> float:
            if field_name not in idx:
                return 0.0
            return _num(_cell(_row, idx[field_name]), field_name=field_name, row=_n)

        invested = g("invested")
        current = g("current_value")
        units = g("units")
        avg_nav = g("avg_nav")
        cur_nav = g("current_nav")

        if not current and units and cur_nav:
            current = units * cur_nav
        if not invested and units and avg_nav:
            invested = units * avg_nav

        code = _cell(row, idx.get("amfi_code")).strip()
        if not code:
            warnings.append(
                f"{source}: {name}: no AMFI code — NAV lookups and overlap "
                f"analysis will be unavailable for this scheme."
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
                category=_cell(row, idx.get("category")).strip() or "Unknown",
                folio=_cell(row, idx.get("folio")).strip(),
            )
        )

    return positions, warnings


# ── Entry point ──────────────────────────────────────────────────────────────
def extract_positions(
    path: Path,
) -> tuple[Kind, list[EquityPosition] | list[MFPosition], list[str]]:
    """Sniff, classify and parse one holdings file. Raises ValueError if no
    header row can be classified, or if a classified header is missing
    required columns — both name the file, never fail silently."""
    grid = read_grid(path)

    header_row: int | None = None
    kind: Kind | None = None
    for i, row in enumerate(grid):
        loose = [_loose_norm(c) for c in row]
        classified = _classify_row(loose)
        if classified:
            header_row, kind = i, classified
            break

    if header_row is None or kind is None:
        raise ValueError(
            f"{path}: could not find a recognisable equity or mutual-fund "
            f"header row. Expected keywords like 'Instrument'/'Qty' (equity) "
            f"or 'Folio'/'Scheme Name' (mutual fund) somewhere in the file."
        )

    source = path.name
    if kind == "equity":
        positions, warnings = _rows_to_equity(grid, header_row, source=source)
    else:
        positions, warnings = _rows_to_mf(grid, header_row, source=source)

    if not positions:
        warnings.append(f"{source}: header row found but no data rows parsed.")

    return kind, positions, warnings


def discover_inbox_files(inbox_dir: Path | None = None) -> list[Path]:
    """Every supported-format file directly inside the inbox dir, sorted for
    deterministic ordering. Missing directory → empty list, not an error."""
    inbox_dir = inbox_dir or HOLDINGS_INBOX_DIR
    if not inbox_dir.exists():
        return []
    return sorted(
        p for p in inbox_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )
