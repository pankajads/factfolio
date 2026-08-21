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
  4. Map columns by keyword-containment (falling back to a learned
     AI-assisted mapping — see "Column-map cache" below — before this
     module's own deterministic heuristics give up) and build the same
     EquityPosition / MFPosition dataclasses loader.py uses everywhere
     else downstream.

A column or row this cannot confidently interpret is skipped with a warning
— never silently zeroed, matching loader.py's rule. A file this cannot
classify at all raises, naming the file, rather than pretending it found
nothing.

## Column-map cache

The keyword-based matching above is fast, free, and right most of the
time — it stays the first thing tried, always. But it's still a fixed set
of rules, and every genuinely new broker/DP export format hit in practice
so far needed another one (a keyword reorder, a whitespace-collapsing
fallback for PDF-wrapped headers, a candidate-symbol scan for a swapped
column) — correct for the file that prompted it, but a code change for
every future format nobody's seen yet. That doesn't scale to formats this
project's author can't predict.

When the deterministic path can't resolve a file's required columns,
agents/schema_resolver.py can be asked (interactively, at `factfolio
validate` — never automatically, and never from load_portfolio()'s own
call path, so `status` stays exactly as deterministic as it's always been)
to read the header and real sample rows and propose a mapping, the same
"propose it, then verify it against real data before trusting it" pattern
already used for ticker-name resolution (agents/ticker_resolver.py). A
mapping that passes validate_schema's grounding check gets written to
config.COLUMN_MAP_CACHE, keyed by header signature — not filename — so
the SAME broker/DP format is recognised immediately in any FUTURE file
too, without ever asking again or needing another code change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

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
    given sets, trying sets in order (most specific first).

    Falls back to a whitespace-collapsed re-check of the SAME keyword sets
    before giving up — a PDF's own table extraction wraps a single word
    across a narrow header cell ("InvestmentValue" → "Investme" + " " +
    "ntValue"), which breaks a plain substring check even though the
    header, read as a human would read it, obviously says "Investment
    Value"; collapsing internal whitespace within one header cell before
    re-testing repairs exactly that, without ever merging separate
    columns into each other (each cell is still checked on its own). Tried
    only as a fallback, after every keyword set has already had a fair
    exact-text shot across every column — a genuinely two-word keyword
    match ("scheme", "name") on an UNWRAPPED, correctly-spelled header
    still needs both real words present in the exact check; the fallback
    exists for wrap damage, not to make matching looser in general.
    """
    for kws in keyword_sets:
        for i, cell in enumerate(header_loose):
            if all(kw in cell for kw in kws):
                return i
    for kws in keyword_sets:
        tight_kws = ["".join(kw.split()) for kw in kws]
        for i, cell in enumerate(header_loose):
            tight_cell = "".join(cell.split())
            if all(kw in tight_cell for kw in tight_kws):
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
    import warnings

    import pandas as pd

    # Two independent noise sources here, neither a real signal, both
    # swallowed the same way any other "harmless but alarming-looking"
    # library chatter is treated elsewhere in this file:
    #   - xlrd `print()`s (not `warnings.warn`s — stdout, not filterable
    #     via the warnings module) a benign notice, "WARNING *** file size
    #     ... not 512 + multiple of sector size", for real-world .xls
    #     exports whose trailing sector is short. Parses fine regardless.
    #   - openpyxl `warnings.warn()`s "Workbook contains no default style"
    #     for a real-world .xlsx that's missing an optional style table —
    #     also parses fine, and — because this file gets read multiple
    #     times per `factfolio validate`/`init` run (classification,
    #     drafting, extraction all read it independently) — printed
    #     six-plus times in a row for one command, which is what actually
    #     made a real user's terminal output feel broken, not the data.
    # Every call in this file goes through here, so both are caught
    # regardless of which specific read triggered them.
    with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
        warnings.simplefilter("ignore")
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
    # ("investment",) tried before the bare ("invested",): a real broker
    # column literally called "DivReinvested" contains "invested" as a
    # plain substring ("re" + "invested") — no wrap damage needed for that
    # false match, just bad luck with an ambiguous single-word keyword.
    # "investment" doesn't have that problem (no MF/equity column spells a
    # word containing "investment" as an accidental substring the way
    # "reinvested" does for "invested"), so it goes first; the bare form
    # stays as the last-resort fallback, not the first guess.
    "invested": (
        ("investment",), ("cost", "value"), ("cost", "acquisition"), ("invested",),
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
    "avg_nav": (("avg", "nav"), ("average", "nav"), ("avg", "cost"), ("average", "cost")),
    "current_nav": (("current", "nav"),),
    "invested": (("investment",), ("cost", "value"), ("invested",)),
    "current_value": (("current", "value"), ("market", "value"), ("cur", "val")),
    "category": (("category",), ("scheme", "type")),
}

# The ground truth for what fields exist and which are load-bearing —
# agents/schema_resolver.py imports these directly rather than keeping its
# own copy, so the AI-assisted fallback path always asks about (and
# requires) exactly the same fields the deterministic path does.
EQUITY_REQUIRED = ("symbol", "quantity", "avg_cost", "invested", "current_value")
EQUITY_ALL_FIELDS = tuple(_EQUITY_COL_KEYWORDS)  # dict preserves insertion order

MF_REQUIRED = ("scheme_name", "invested", "current_value")
MF_ALL_FIELDS = tuple(_MF_COL_KEYWORDS)


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


# ── AI-assisted column-map cache ────────────────────────────────────────────
# See this module's own docstring ("Column-map cache") for the full picture.
# Everything below is pure data plumbing and validation — the actual AI call
# lives in agents/schema_resolver.py; nothing here ever talks to a model.

def _header_signature(header_loose: list[str]) -> str:
    """A stable, human-inspectable key for one header row's exact shape —
    the loose-normalized cells joined, not a hash, so
    config.COLUMN_MAP_CACHE stays as readable/greppable as tickers.yaml
    is, not an opaque lookup table."""
    return "|".join(header_loose)


def load_column_map_cache() -> dict[str, dict]:
    import json

    from mybroker.config import COLUMN_MAP_CACHE

    if not COLUMN_MAP_CACHE.exists():
        return {}
    try:
        data = json.loads(COLUMN_MAP_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_column_map(
    header: list[str], *, kind: str, columns: dict[str, int | None],
    source: str, confidence: str, reasoning: str,
) -> None:
    """Persist a column mapping — call only after validate_schema has
    already passed it; this function itself does no checking, it just
    writes whatever it's given. `header` is the RAW header row (as read
    from the file, same shape find_unresolvable_files hands a caller) —
    normalized internally so callers never need this module's own private
    _loose_norm."""
    import json
    from datetime import UTC, datetime

    from mybroker.config import COLUMN_MAP_CACHE

    header_loose = [_loose_norm(c) for c in header]
    cache = load_column_map_cache()
    cache[_header_signature(header_loose)] = {
        "kind": kind,
        "columns": columns,
        "confidence": confidence,
        "reasoning": reasoning,
        "learned_from": source,
        "learned_at": datetime.now(UTC).isoformat(),
    }
    COLUMN_MAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    COLUMN_MAP_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _cached_mapping_for_row(header_loose: list[str]) -> tuple[Kind, dict[str, int | None]] | None:
    """Whether THIS ONE row's exact header signature matches a previously
    learned-and-validated mapping — checked for every candidate row
    extract_positions' table scan considers, before falling back to
    keyword classification, so a format already resolved once (a human
    confirming an AI-assisted mapping, at `factfolio validate`) is
    recognised instantly in any future file sharing that header, without
    asking again or needing another code change."""
    cache = load_column_map_cache()
    entry = cache.get(_header_signature(header_loose))
    if entry:
        return entry["kind"], entry["columns"]
    return None


def _looks_numeric(value: str) -> bool:
    """Same tolerant cleanup as loader.py's _num, but returning a bool
    rather than raising or parsing — used only to check whether an
    agent's claim about a column holds up, never to read a real value."""
    if not value:
        return False
    cleaned = value.strip().replace(",", "").replace("₹", "")
    if cleaned in ("-", "--", "NA", "N/A"):
        return True  # a legitimate "blank" numeric cell, not text
    try:
        float(cleaned)
        return True
    except ValueError:
        return False


_NUMERIC_FIELDS = {
    "quantity", "avg_cost", "ltp", "invested", "current_value", "pnl",
    "net_change_pct", "day_change_pct", "units", "avg_nav", "current_nav",
}
_TEXT_FIELDS = {"symbol", "scheme_name"}


def validate_schema(
    kind: str | None, columns: dict[str, int | None], grid: Grid,
    header_row: int | None,
) -> tuple[bool, list[str]]:
    """The actual gate an agent-proposed column mapping must clear before
    anything here trusts it — checked against the file's own REAL data
    (every data row, not just the handful of sample rows the agent saw),
    the same "a claim must be grounded in real evidence" discipline
    agents/ticker_resolver.py's _validate applies to a search result,
    applied here to column semantics instead. Returns (ok, problems);
    problems is always populated, human-readably, when ok is False.

    header_row now comes from the agent's OWN read of the file (see
    agents/schema_resolver.py — it isn't told where the header is any
    more than a human would be) rather than being known in advance, so
    it's validated here too: null (the agent couldn't find one at all)
    or out of range is rejected the same as any other ungrounded claim.
    """
    problems: list[str] = []

    if kind not in ("equity", "mf"):
        return False, [f"unrecognised kind {kind!r} (must be 'equity' or 'mf')"]

    if not isinstance(header_row, int) or header_row < 0 or header_row >= len(grid):
        return False, [f"header_row {header_row!r} is not a valid row index"]

    n_cols = len(grid[header_row])
    data_rows = _data_rows(grid, header_row)
    if not data_rows:
        return False, ["no data rows to validate the claimed mapping against"]

    for field_name, col in columns.items():
        if col is None:
            continue
        if not isinstance(col, int) or col < 0 or col >= n_cols:
            problems.append(f"{field_name}: column index {col!r} is out of range")
            continue

        values = [_cell(r, col).strip() for r in data_rows]
        values = [v for v in values if v]
        if not values:
            problems.append(
                f"{field_name}: column {col} ({grid[header_row][col]!r}) is "
                f"empty in every data row"
            )
            continue

        if field_name in _NUMERIC_FIELDS:
            bad = [v for v in values if not _looks_numeric(v)]
            # Tolerate a stray blank/dash/footnote — reject only if the
            # column is predominantly non-numeric.
            if len(bad) > max(1, len(values) * 0.2):
                problems.append(
                    f"{field_name}: column {col} ({grid[header_row][col]!r}) "
                    f"doesn't look numeric — e.g. {bad[0]!r}"
                )
        elif field_name in _TEXT_FIELDS and all(_looks_numeric(v) for v in values):
            problems.append(
                f"{field_name}: column {col} ({grid[header_row][col]!r}) "
                f"looks purely numeric, not a name/symbol"
            )

    required = EQUITY_REQUIRED if kind == "equity" else MF_REQUIRED
    missing = [f for f in required if columns.get(f) is None]
    if missing:
        problems.append(f"missing required field(s) for {kind}: {list(missing)}")

    return not problems, problems


def _rows_to_equity(
    grid: Grid, header_row: int, *, source: str,
    column_override: dict[str, int | None] | None = None,
) -> tuple[list[EquityPosition], list[str]]:
    # column_override — a mapping already learned via AI-assisted schema
    # resolution and validated against this file's own data (see
    # validate_schema/load_column_map) — replaces keyword matching
    # entirely for this file when present, rather than merging with it:
    # a human/agent already looked at the real data and settled this, so
    # there's nothing left for the keyword heuristics to add.
    if column_override is not None:
        idx = {k: v for k, v in column_override.items() if v is not None}
    else:
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
    grid: Grid, header_row: int, *, source: str,
    column_override: dict[str, int | None] | None = None,
) -> tuple[list[MFPosition], list[str]]:
    # See _rows_to_equity's own comment on column_override — same idea.
    if column_override is not None:
        idx = {k: v for k, v in column_override.items() if v is not None}
    else:
        header_loose = [_loose_norm(c) for c in grid[header_row]]
        idx = _resolve_columns(header_loose, _MF_COL_KEYWORDS)
    if "scheme_name" not in idx:
        raise ValueError(
            f"{source}: could not find a scheme-name column in the detected "
            f"mutual-fund header {grid[header_row]!r}."
        )

    # Same rule loader.py states up front for the whole module: a column
    # that can't be interpreted is an error, never a silent zero. Without
    # this check, a header this couldn't map "invested" or "current_value"
    # (directly, or derivable from units × nav) for — the exact failure
    # mode a badly PDF-wrapped header produces — used to build every
    # position anyway, just with invested=0 and current_value=0: real
    # holdings worth real money, silently reported as worthless, with
    # nothing anywhere saying why.
    can_derive_invested = {"units", "avg_nav"} <= idx.keys()
    can_derive_current = {"units", "current_nav"} <= idx.keys()
    missing = set()
    if "invested" not in idx and not can_derive_invested:
        missing.add("invested")
    if "current_value" not in idx and not can_derive_current:
        missing.add("current_value")
    if missing:
        raise ValueError(
            f"{source}: could not find column(s) {sorted(missing)} (or enough "
            f"to derive them from units × NAV) in the detected mutual-fund "
            f"header {grid[header_row]!r}."
        )

    positions: list[MFPosition] = []
    warnings: list[str] = []
    no_amfi_code = 0

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
            no_amfi_code += 1

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

    if no_amfi_code:
        # One line, not one per scheme — a direct-plan investor's own
        # statement never carries an AMFI code at all (there's no
        # distributor/broker to populate one), so this fires for EVERY
        # scheme, every time, permanently. Repeating an identical warning
        # 19 times drowns out everything else in the output for a state
        # that isn't a data problem to go fix; it's just how a direct
        # investor's statement looks. folio (see metrics.py's snapshot,
        # which uses it to disambiguate same-name holdings) is the actual
        # working substitute for "which holding is this" — AMFI code is
        # specifically about NAV lookups/overlap analysis, which folio
        # genuinely can't provide, so this still says so honestly, just
        # once.
        plural = "" if no_amfi_code == 1 else "s"
        warnings.append(
            f"{source}: {no_amfi_code} scheme{plural} with no AMFI code — "
            f"expected for direct-plan holdings (no distributor to supply "
            f"one). NAV lookups and overlap analysis will be unavailable "
            f"for {'it' if no_amfi_code == 1 else 'them'}; folio number is "
            f"used to tell holdings apart instead."
        )

    return positions, warnings


# ── Entry point ──────────────────────────────────────────────────────────────
def _extract_all_tables(
    grid: Grid, source: str,
) -> tuple[list[EquityPosition], list[MFPosition], list[str]]:
    """Scan the WHOLE file for every holdings table it contains — not just
    the first. A consolidated broker/DP statement can legitimately hold
    BOTH an equity table and a mutual-fund table as separate sections of
    the same file; stopping at the first classifiable header (the old
    behaviour) silently dropped whatever came after it entirely, with no
    warning anywhere in the whole system — real holdings, real money,
    invisible. Real users hit exactly this.

    Each candidate header row found is resolved via the column-map cache
    first (see _cached_mapping_for_row), falling back to keyword
    classification — same priority as before, just applied per-table
    instead of once for the whole file. One table failing to resolve its
    columns does not block another, different table elsewhere in the same
    file from still being extracted — its error becomes a warning
    alongside whatever DID succeed, not a hard failure, unless NOTHING in
    the entire file could be read at all (still a ValueError, same as
    before, so find_unresolvable_files/AI-assisted schema resolution
    still applies to that case).
    """
    equity: list[EquityPosition] = []
    mfs: list[MFPosition] = []
    warnings: list[str] = []
    table_errors: list[str] = []
    tables_found = 0

    i = 0
    while i < len(grid):
        loose = [_loose_norm(c) for c in grid[i]]
        cached = _cached_mapping_for_row(loose)
        kind: Kind | None = cached[0] if cached else _classify_row(loose)

        if not kind:
            i += 1
            continue

        tables_found += 1
        columns = cached[1] if cached else None
        try:
            if kind == "equity":
                positions, w = _rows_to_equity(
                    grid, i, source=source, column_override=columns
                )
                equity.extend(positions)
            else:
                positions, w = _rows_to_mf(
                    grid, i, source=source, column_override=columns
                )
                mfs.extend(positions)
            warnings.extend(w)
        except ValueError as exc:
            table_errors.append(str(exc))

        # Resume scanning right after this table's own data rows, so its
        # data can't be mistaken for a second header further down.
        i += 1 + len(_data_rows(grid, i))

    if tables_found == 0:
        raise ValueError(
            f"{source}: could not find a recognisable equity or mutual-fund "
            f"header row. Expected keywords like 'Instrument'/'Qty' (equity) "
            f"or 'Folio'/'Scheme Name' (mutual fund) somewhere in the file."
        )

    if not equity and not mfs:
        # Every table found failed to resolve — nothing usable came out of
        # this file at all, so this stays a hard failure (same as always),
        # not a warning nobody would ever see.
        raise ValueError("; ".join(table_errors))

    warnings.extend(table_errors)
    return equity, mfs, warnings


def extract_positions(
    path: Path,
) -> tuple[list[EquityPosition], list[MFPosition], list[str]]:
    """Sniff, classify and parse every holdings table in one file — see
    _extract_all_tables for why this is plural, not singular. Raises
    ValueError if nothing in the file could be read at all."""
    grid = read_grid(path)
    return _extract_all_tables(grid, path.name)


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


_MAX_CANDIDATE_SYMBOL_LEN = 15


def _looks_like_a_wrapped_symbol(value: str) -> bool:
    """True if `value` plausibly reads as ONE genuine trading symbol split
    across at most two PDF-wrapped lines (a narrow column wrapping
    "HDFCBANK" into "HDFCBAN\\nK", collapsed by _clean_pdf_cell's
    whitespace-preserving join into "HDFCBAN K") — as opposed to a full
    company name that also happens to contain spaces. Blindly stripping
    every space and re-checking _CLEAN_SYMBOL alone would treat "AXIS BANK
    LIMITED" (→ "AXISBANKLIMITED") as equally "clean"; the token-count and
    length limits below are what keep those apart — a real wrapped symbol
    is at most two fragments and stays short even joined, while a company
    name is usually three-plus words and/or too long once joined.
    """
    tokens = value.split()
    if not tokens or len(tokens) > 2:
        return False
    joined = "".join(tokens)
    return (
        len(joined) <= _MAX_CANDIDATE_SYMBOL_LEN
        and bool(_CLEAN_SYMBOL.match(joined))
        and any(ch.isalpha() for ch in joined)
    )


def _find_candidate_symbol_column(
    grid: Grid, header_row: int, exclude: set[int]
) -> int | None:
    """A column OTHER than the one already identified as the name column
    whose data plausibly holds a genuine short trading symbol per holding
    — a HINT only, never auto-drafted or auto-written anywhere (see
    discover_unmapped_full_names, its only caller, and contrast with
    discover_equity_symbols_for_drafting's deliberately unmoved "give up
    rather than guess" gate). Exists because a real broker PDF's own table
    extraction can put the actual code in a column whose HEADER doesn't
    match any of _EQUITY_COL_KEYWORDS at all — one real statement's column
    literally labelled "Stock Name" holds "AXISBANK", while the column
    that keyword-matches as the symbol column ("SKScrip Code") holds the
    full company name instead. The code exists in the file, just under a
    header nothing here goes looking for — discarding it entirely (the old
    behaviour) sent every holding through AI-assisted name resolution for
    a symbol that was already sitting one column over, unused. Surfacing
    it as `candidate_symbol` lets the resolver (or a human, interactively)
    verify and use it directly instead.

    Stricter than _looks_like_a_symbol_column on purpose: also requires at
    least one letter (rules out a numeric internal row/customer ID, which
    would otherwise pass the same character-class check just as easily as
    a real symbol) and that most rows differ from each other (rules out a
    repeated account ID or a constant Y/N flag column — both of which are
    "clean" by character shape alone). Returns the column only if it's the
    SOLE one satisfying all of this; multiple candidates is genuine
    ambiguity, not a hint worth surfacing.
    """
    candidates: list[int] = []
    for col in range(len(grid[header_row])):
        if col in exclude:
            continue
        values = [_cell(r, col).strip().upper() for r in _data_rows(grid, header_row)]
        values = [v for v in values if v and not _looks_like_a_bond(v)]
        if not values:
            continue
        clean = sum(1 for v in values if _looks_like_a_wrapped_symbol(v))
        if clean / len(values) < 0.8:
            continue
        if len(set(values)) / len(values) < 0.5:
            continue
        candidates.append(col)
    return candidates[0] if len(candidates) == 1 else None


def _row_as_dict(grid: Grid, header_row: int, data_row: list[str]) -> dict[str, str]:
    """`data_row` as a plain {header label: value} dict — the RAW label
    from the file itself, not loose-normalized, and every column, not
    just the ones _EQUITY_COL_KEYWORDS happens to recognize. This is the
    actual fix for only ever handing downstream resolution a single
    Python-guessed column: rather than this module deciding which one
    column might matter and discarding the rest, the whole row goes to
    whoever resolves the name (an AI agent, or a human in an interactive
    prompt) so THEY can read it and notice what a fixed set of shape
    heuristics might not — a differently-named code column, an ISIN, a
    BSE scrip code, anything. A header repeated across columns (a broker
    export with two same-named columns) gets suffixed so neither silently
    overwrites the other in the dict.
    """
    out: dict[str, str] = {}
    seen: dict[str, int] = {}
    for col, header in enumerate(grid[header_row]):
        label = header.strip()
        if not label:
            continue
        n = seen.get(label, 0)
        seen[label] = n + 1
        key = label if n == 0 else f"{label} ({n + 1})"
        out[key] = _cell(data_row, col).strip()
    return out


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
    discover_equity_symbols_for_drafting. Returns `[{"name": ...,
    "quantity": ..., "avg_cost": ..., "candidate_symbol": ..., "row_data":
    {...}}, ...]`. candidate_symbol is a cheap, free, no-agent-call hint —
    present only when _find_candidate_symbol_column found exactly one
    unambiguous shape-match elsewhere in the row, and deliberately narrow
    (a fixed statistical heuristic will always miss real-world layouts it
    wasn't tuned against). row_data is the actual fix for that ceiling:
    every column of the row, raw, unfiltered — so the agent (or a human,
    interactively) can read the whole thing and use real judgement, not
    just whatever this module's own heuristics happened to notice.
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

        candidate_col = _find_candidate_symbol_column(
            grid, i, exclude={sym_col} if sym_col is not None else set()
        )

        holdings: list[dict] = []
        seen: set[str] = set()
        for rownum, data_row in enumerate(_data_rows(grid, i), start=i + 2):
            raw = _cell(data_row, sym_col).strip()
            if not raw or raw in seen or _looks_like_a_bond(raw):
                continue
            if resolve_symbol_by_name(raw) is not None:
                continue
            seen.add(raw)
            entry = {
                "name": raw,
                "quantity": _num(_cell(data_row, idx.get("quantity")), field_name="quantity", row=rownum)
                if idx.get("quantity") is not None else None,
                "avg_cost": _num(_cell(data_row, idx.get("avg_cost")), field_name="avg_cost", row=rownum)
                if idx.get("avg_cost") is not None else None,
                "row_data": _row_as_dict(grid, i, data_row),
            }
            if candidate_col is not None:
                hint = "".join(_cell(data_row, candidate_col).split()).upper()
                if hint:
                    entry["candidate_symbol"] = hint
            holdings.append(entry)
        return holdings

    return []


def discover_inbox_files(inbox_dir: Path | None = None) -> list[Path]:
    """Every supported-format file directly inside the inbox dir, sorted for
    deterministic ordering. Missing directory → empty list, not an error."""
    if inbox_dir is None:
        # Deliberately a lazy import, not a module-level one — this used
        # to bind HOLDINGS_INBOX_DIR once at first import of this module,
        # which then never noticed `factfolio init` repointing
        # PROJECT_ROOT mid-process, or a test's config.set_project_root()
        # — every caller relying on this default silently kept scanning
        # the FIRST project root this module ever saw, not the current
        # one. Every other reader of config.HOLDINGS_INBOX_DIR in this
        # codebase (portfolio/loader.py's load_portfolio, ticker_seeding
        # .py's holdings_present) already imports it fresh inside the
        # function for the same reason.
        from mybroker.config import HOLDINGS_INBOX_DIR

        inbox_dir = HOLDINGS_INBOX_DIR
    if not inbox_dir.exists():
        return []
    return sorted(
        p for p in inbox_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )


_SCHEMA_EXCERPT_ROWS = 30


def find_unresolvable_files(inbox_dir: Path | None = None) -> list[dict]:
    """Every holdings_inbox file _extract_all_tables can't get ANYTHING
    out of at all — whether because no header row could even be
    recognised by keyword (a genuinely novel export using different
    terminology throughout, not just an unfamiliar column name) or
    because every table it did find failed to resolve its required
    columns. Both are exactly what AI-assisted schema resolution
    (agents/schema_resolver.py) exists for: it isn't told where the
    header is any more than a human opening the file for the first time
    would be — it reads the raw excerpt and figures out the whole
    structure itself, so it isn't limited to only the narrower of the two
    failure modes the way a pre-picked-header design would be. A file
    with ONE good table and ONE bad one isn't flagged here at all — the
    good table's data is real and already extracted; see the returned
    warning list from load_portfolio for the bad one instead, the same
    partial-success handling every other holdings source gets.

    Returns `[{"path": Path, "grid": Grid, "grid_excerpt": Grid, "error":
    str}, ...]` — everything a caller (cli.py) needs to both show the
    problem to a human and hand the raw material straight to the resolver
    without re-reading the file. grid_excerpt is capped at the first
    _SCHEMA_EXCERPT_ROWS rows — plenty for any real statement's header
    plus a few data rows, without shipping an entire large file to the
    agent.
    """
    out: list[dict] = []
    for path in discover_inbox_files(inbox_dir):
        try:
            grid = read_grid(path)
        except Exception:
            continue  # a different failure mode — not what this is for

        try:
            _extract_all_tables(grid, path.name)
        except ValueError as exc:
            out.append({
                "path": path,
                "grid": grid,
                "grid_excerpt": grid[:_SCHEMA_EXCERPT_ROWS],
                "error": str(exc),
            })
    return out
