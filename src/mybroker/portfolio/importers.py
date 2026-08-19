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

import re
from pathlib import Path
from typing import Literal

from mybroker.config import HOLDINGS_INBOX_DIR
from mybroker.portfolio.loader import (
    EquityPosition,
    MFPosition,
    _looks_like_a_bond,
    _num,
    _resolve_position,
)

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
        try:
            df = pd.read_excel(path, header=None, dtype=str)
        except ValueError as exc:
            # Plenty of Indian broker/bank "Excel" exports are actually an
            # HTML table saved with an .xls/.xlsx extension, not a real
            # binary workbook. pandas can't identify a format from that
            # content and raises exactly this message rather than guessing
            # — fall back to reading it as HTML instead of treating that as
            # a hard failure. Genuine corruption still raises, just as a
            # (more informative) ValueError from read_html below.
            if "cannot be determined" not in str(exc).lower():
                raise
            return _read_html_grid(path, cause=exc)
    df = df.fillna("")
    return df.astype(str).values.tolist()


def _read_html_grid(path: Path, *, cause: Exception) -> Grid:
    """A holdings file that's actually an HTML table wearing an .xls/.xlsx
    extension — see _read_excel_grid's fallback. pandas.read_html always
    treats a genuine `<th>` header row as the DataFrame's column labels
    rather than an ordinary row, unlike read_excel(header=None); put it
    back as a plain row so extract_positions()'s header-scan finds it the
    same way regardless of which format actually arrived. Concatenate
    every table found rather than guess which one is "the" one — the same
    header-scan already finds the real header amid noise, the way it does
    for a PDF's multiple extracted tables too."""
    import pandas as pd

    try:
        tables = pd.read_html(path, flavor="lxml")
    except ValueError as exc:
        # Not actually HTML either — genuine corruption, not a mislabeled
        # export. Re-raise naming the file, matching every other read_*_grid.
        raise ValueError(
            f"{path}: not a readable Excel file, and no HTML table found "
            f"either ({exc})"
        ) from cause

    grid: Grid = []
    for table in tables:
        grid.append([str(c) for c in table.columns])
        grid.extend(table.fillna("").astype(str).values.tolist())
    return grid


def _pdf_password_for(path: Path) -> str | None:
    """A password to try for an encrypted PDF, if one's available anywhere:

    1. A sidecar file `<filename>.password` next to it — lets different
       files use different passwords, and lives in holdings_inbox/ so it's
       gitignored the same as everything else there.
    2. FACTFOLIO_PDF_PASSWORD, a single default — fine when there's only
       one, or every file happens to share a password (e.g. your own PAN).

    Neither convention is obvious up front: CAMS/KFintech mutual-fund CAS
    statements use your PAN in UPPERCASE; NSDL/CDSL depository CAS
    statements use PAN+date-of-birth (DDMMYYYY) — the exact format is
    always in the email that sent you the statement.
    """
    import os

    sidecar = path.parent / f"{path.name}.password"
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8").strip()
    return os.environ.get("FACTFOLIO_PDF_PASSWORD")


def _open_pdf(path: Path):
    import pdfplumber
    from pdfplumber.utils.exceptions import PdfminerException

    try:
        return pdfplumber.open(path)
    except PdfminerException as exc:
        password = _pdf_password_for(path)
        if password is None:
            raise ValueError(
                f"{path}: couldn't open it — either it's corrupt, or it's "
                f"password-protected and no password was found. If it needs one, "
                f"put it in {path.name}.password next to the file (or set "
                f"FACTFOLIO_PDF_PASSWORD). See _pdf_password_for's docstring for "
                f"the common CAMS/KFintech/NSDL/CDSL password conventions."
            ) from exc
        try:
            return pdfplumber.open(path, password=password)
        except PdfminerException as exc2:
            raise ValueError(
                f"{path}: still couldn't open it with the password from "
                f"{path.name}.password / FACTFOLIO_PDF_PASSWORD — double-check it "
                f"against the email that sent you the statement."
            ) from exc2


def _clean_pdf_cell(cell: str | None) -> str:
    """pdfplumber wraps long cell text across internal lines using literal
    newlines (a narrow "Stock Name"/"Scrip Name" column, say) — fine as a
    rendering detail, but left as-is it turns a one-line company name like
    "AXIS BANK LIMITED" into "AXIS BANK\nLIMITED", which then prints as a
    garbled multi-line mess in every warning, log line, and printed
    position from here on. Collapsing ALL internal whitespace (not just
    leading/trailing, which `.strip()` alone handles) to single spaces
    fixes the display without changing what any of it means —
    _normalize_company_name's own whitespace-tolerant `.split()` already
    treated a wrapped and unwrapped cell identically either way.
    """
    return " ".join((cell or "").split())


def _read_pdf_grid(path: Path) -> Grid:
    rows: Grid = []
    with _open_pdf(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    rows.append([_clean_pdf_cell(c) for c in row])
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
# Column-name variants confirmed across major brokers/DPs (Zerodha, Groww,
# Upstox, Sharekhan, ICICI Direct/depository statements) — different
# vendors, same underlying figure. See docs/USER_GUIDE.md's "holdings
# input" section for the running list this gets extended from as new
# formats show up.
_EQUITY_COL_KEYWORDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "symbol": (("instrument",), ("tradingsymbol",), ("symbol",), ("scrip",)),
    "quantity": (("qty",), ("quantity",)),
    "avg_cost": (
        ("avg", "cost"), ("average", "cost"), ("avg", "price"), ("average", "price"),
        ("avg", "rate"), ("buy", "price"), ("purchase", "price"),
    ),
    "ltp": (("ltp",), ("last", "price"), ("market", "price"), ("current", "price")),
    "invested": (
        ("invested",), ("investment",), ("cost", "value"), ("cost", "acquisition"),
    ),
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

    # invested/current_value each need either their own explicit column, OR
    # enough to derive them (qty*avg_cost, qty*ltp) — the same fallback
    # _rows_to_mf already uses for units*avg_nav/units*current_nav. This
    # matters in practice: some brokers' PDF exports label the cost-basis
    # column something ambiguous like "Holding Value" (could as easily mean
    # current value) rather than "Invested" — deriving it from qty*avg_cost
    # sidesteps guessing what an unfamiliar column name actually means.
    can_derive_invested = {"quantity", "avg_cost"} <= idx.keys()
    can_derive_current = {"quantity", "ltp"} <= idx.keys()
    missing = {"symbol", "quantity", "avg_cost"} - idx.keys()
    if "invested" not in idx and not can_derive_invested:
        missing.add("invested")
    if "current_value" not in idx and not can_derive_current:
        missing.add("current_value")
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
        if _looks_like_a_bond(symbol):
            warnings.append(
                f"{source}: {symbol!r} looks like a bond/gold bond, not equity — excluded."
            )
            continue

        def get(field_name: str, _row=row, _n=rownum) -> float:
            if field_name not in idx:
                return 0.0
            return _num(_cell(_row, idx[field_name]), field_name=field_name, row=_n)

        quantity = get("quantity")
        avg_cost = get("avg_cost")
        ltp = get("ltp")
        invested = get("invested") if "invested" in idx else quantity * avg_cost
        current_value = get("current_value") if "current_value" in idx else quantity * ltp
        pnl = get("pnl") if "pnl" in idx else current_value - invested

        pos = EquityPosition(
            symbol=symbol,
            quantity=quantity,
            avg_cost=avg_cost,
            ltp=ltp,
            invested=invested,
            current_value=current_value,
            pnl=pnl,
            net_change_pct=get("net_change_pct"),
        )

        warning = _resolve_position(pos, source=source)
        if warning:
            warnings.append(warning)

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


_CLEAN_SYMBOL = re.compile(r"^[A-Z0-9&\-]+$")


def _looks_like_a_symbol_column(grid: Grid, header_row: int, col: int) -> bool:
    """True if most non-blank values in `col`, across this table's data
    rows, look like a genuine short trading symbol ('HDFCBANK') rather
    than a full company name ('HDFC Bank Limited') — or, just as
    importantly, rather than a *broken* one: pdfplumber's table extraction
    can wrap a header labelled "SKScripCode" over a narrow column whose
    actual data is full company names (a column-alignment quirk on that
    specific PDF, not a labelling choice), and the header text alone can't
    tell that apart from a genuine code column. This looks at what the
    data actually IS instead of trusting what the header claims — both
    discover_equity_symbols_for_drafting and discover_unmapped_full_names
    key off this rather than the header's own "name"/"symbol" wording, so
    they agree on which case they're in rather than each guessing
    separately and potentially both giving up. Tolerates a stray odd row
    rather than requiring every one to match.
    """
    values = [_cell(r, col).strip().upper() for r in _data_rows(grid, header_row)]
    values = [v for v in values if v and not _looks_like_a_bond(v)]
    if not values:
        return False
    clean = sum(1 for v in values if _CLEAN_SYMBOL.match(v))
    return clean / len(values) >= 0.8


def discover_equity_symbols_for_drafting(path: Path) -> set[str]:
    """Every symbol in `path` that looks like a genuine short trading
    symbol — used to seed DRAFT tickers.yaml entries at `factfolio init`,
    never to build real positions (see extract_positions for that).

    Deliberately narrower than extract_positions: only equity rows whose
    symbol column DATA actually looks like short trading symbols (see
    _looks_like_a_symbol_column) qualify — a column like Sharekhan's
    "Scrip Name" holds a full company name, and there's no reliable way to
    derive the real trading symbol from that text alone. That's exactly
    the kind of guess this project refuses to make, so those rows are left
    to discover_unmapped_full_names / the "not in tickers.yaml" warning
    instead of being drafted.

    Best-effort and silent (in the sense of never raising or blocking
    `init`/`validate`) on any failure — but every outcome, including "found
    nothing to draft", is logged with a reason (see logging_setup.py):
    silent-and-*unexplained* is what turned "this xls has mutual funds in
    it, not equity" into a support request that looked like a missed file.
    """
    from mybroker.logging_setup import get_logger

    logger = get_logger(__name__)

    try:
        grid = read_grid(path)
    except Exception as exc:
        logger.warning("ticker_drafting: %s: couldn't read — %s: %s",
                        path.name, type(exc).__name__, exc)
        return set()

    saw_mf = False
    for i, row in enumerate(grid):
        loose = [_loose_norm(c) for c in row]
        classified = _classify_row(loose)
        if classified != "equity":
            # Unlike extract_positions, this keeps scanning past a
            # non-equity (or unclassified) row rather than stopping at the
            # first one — a file can have other sections before/after its
            # real equity header. Just remember an mf hit for the "found
            # nothing" log message below, in case that's all there ever was.
            saw_mf = saw_mf or classified == "mf"
            continue

        idx = _resolve_columns(loose, _EQUITY_COL_KEYWORDS)
        sym_col = idx.get("symbol")
        if sym_col is None or not _looks_like_a_symbol_column(grid, i, sym_col):
            logger.info("ticker_drafting: %s: equity header found, but the "
                        "symbol column's data doesn't look like genuine "
                        "trading symbols (full company names, e.g. 'Scrip "
                        "Name' — or a header/data column mismatch in this "
                        "file's own table extraction) — can't auto-draft; "
                        "needs `factfolio init`'s AI-assisted resolver or a "
                        "manual tickers.yaml entry", path.name)
            return set()  # not a genuine symbol column — skip

        symbols: set[str] = set()
        for data_row in _data_rows(grid, i):
            raw = _cell(data_row, sym_col).strip().upper()
            if raw and not _looks_like_a_bond(raw) and _CLEAN_SYMBOL.match(raw):
                symbols.add(raw)
        logger.info("ticker_drafting: %s: %d candidate symbol(s) found: %s",
                    path.name, len(symbols), ", ".join(sorted(symbols)) or "(none)")
        return symbols

    if saw_mf:
        logger.info("ticker_drafting: %s: classified as mutual-fund, not "
                    "equity — nothing to draft", path.name)
    else:
        logger.info("ticker_drafting: %s: no equity or mutual-fund header "
                    "row found at all — nothing to draft", path.name)
    return set()


def discover_unmapped_full_names(path: Path) -> list[dict]:
    """The mirror image of discover_equity_symbols_for_drafting: every
    full-company-name-ish holding in `path` — a source like Sharekhan's
    "Scrip Name" column whose DATA doesn't look like genuine trading
    symbols (see _looks_like_a_symbol_column; not just a header that
    literally says "name" — a mislabelled/misaligned column with the same
    problem needs this exact same fallback) — that isn't already
    resolvable against an existing tickers.yaml entry (see
    resolve_symbol_by_name). These can never be auto-drafted (see that
    function's own docstring for why), but `factfolio init`'s
    agent-assisted resolver (agents/ticker_resolver.py) uses this —
    quantity/avg_cost included, not just the name — to reason about
    cross-row duplicates (the same real holding recorded twice at the same
    quantity under a slightly different name, say). Never auto-written
    from this alone; a resolution still has to clear that module's own
    validation gate first.

    Best-effort and silent on any failure, same contract as
    discover_equity_symbols_for_drafting. Returns
    `[{"name": ..., "quantity": ..., "avg_cost": ...}, ...]`.
    """
    from mybroker.config import resolve_symbol_by_name

    try:
        grid = read_grid(path)
    except Exception:
        return []

    for i, row in enumerate(grid):
        loose = [_loose_norm(c) for c in row]
        if _classify_row(loose) != "equity":
            continue
        idx = _resolve_columns(loose, _EQUITY_COL_KEYWORDS)
        sym_col = idx.get("symbol")
        if sym_col is None or _looks_like_a_symbol_column(grid, i, sym_col):
            return []  # a genuine symbol column — discover_equity_symbols_for_drafting's job

        holdings: list[dict] = []
        seen: set[str] = set()
        for rownum, data_row in enumerate(_data_rows(grid, i), start=i + 2):
            raw = _cell(data_row, sym_col).strip()
            if not raw or raw in seen or _looks_like_a_bond(raw):
                continue
            if resolve_symbol_by_name(raw) is not None:
                continue
            seen.add(raw)
            holdings.append({
                "name": raw,
                "quantity": _num(_cell(data_row, idx.get("quantity")), field_name="quantity", row=rownum)
                if idx.get("quantity") is not None else None,
                "avg_cost": _num(_cell(data_row, idx.get("avg_cost")), field_name="avg_cost", row=rownum)
                if idx.get("avg_cost") is not None else None,
            })
        return holdings

    return []


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
