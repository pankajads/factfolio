"""Parse broker exports into normalised position objects.

Handles the Zerodha Kite equity export and an optional mutual-fund export.
Both are tolerated in slightly messy forms — broker exports vary — but a
column that cannot be interpreted is an error, never a silent zero.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from mybroker.config import (
    HOLDINGS_EQUITY,
    HOLDINGS_INBOX_DIR,
    HOLDINGS_MF,
    cd_hint_if_project_nearby,
    resolve_symbol_by_name,
    symbol_meta,
)

# RBI/government bonds and Sovereign Gold Bonds sometimes show up in a demat
# "holdings" export alongside actual equity (e.g. "2.50% JAN29 SERIES X FY
# 2020-2", "2.50%GOLDBONDS2029SR-IX") — this tool only analyses equity and
# mutual funds (see README). Counting a bond as an unclassified
# "Unknown"-sector stock would misstate both its own P&L context and the
# portfolio's sector concentration, so it's excluded (with a warning naming
# it), not silently mis-typed.
_BOND_LIKE = re.compile(r"^\d+(\.\d+)?%|GOLD\s*BOND|\bSGB\b", re.IGNORECASE)


def _looks_like_a_bond(display_text: str) -> bool:
    return bool(_BOND_LIKE.search(display_text))

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


def _resolve_position(pos: EquityPosition, *, source: str = "") -> str | None:
    """Attach tickers.yaml metadata to `pos`, falling back to matching its
    full company name (see resolve_symbol_by_name) when an exact-symbol
    lookup misses — some sources (a demat holdings PDF, say) only have a
    display name, not a trading symbol, to key off of. Canonicalizes
    `pos.symbol` to the resolved tickers.yaml key either way, so the same
    stock recorded under different display names/lots still merges into
    one position later (see _merge_same_symbol_lots). Returns a warning
    string if nothing resolved, else None.
    """
    prefix = f"{source}: " if source else ""
    try:
        meta = symbol_meta(pos.symbol)
    except KeyError:
        resolved = resolve_symbol_by_name(pos.symbol)
        if resolved is None:
            return (
                f"{prefix}{pos.symbol} is not in tickers.yaml — no market data, "
                f"sector, or policy classification will be available for it. "
                f"Run `factfolio init` to try automatic resolution, or add it "
                f"to tickers.yaml yourself."
            )
        pos.symbol = resolved
        meta = symbol_meta(resolved)

    pos.name = meta.get("name", pos.symbol)
    pos.sector = meta.get("sector", "Unknown")
    pos.tier = meta.get("tier", "unknown")
    pos.bucket = meta.get("bucket", "satellite")
    return None


def _merge_same_symbol_lots(positions: list[EquityPosition]) -> list[EquityPosition]:
    """Combine multiple lots of the same resolved symbol into one position.

    A stock split across two demat accounts, or recorded as separate lots
    by a DP's own back office (a corporate action, a batch of trades), is
    still one real exposure — max_position_pct and concentration (HHI) are
    about total exposure to a stock, not how many statements/accounts it
    happens to be spread across. Leaving lots unmerged can let a real
    breach hide beneath the cap in every individual row.

    Symbols that never resolved (still a raw, unmapped display name) merge
    too, but only with an exact-string match of that same raw name — two
    different unmapped spellings of the same company stay separate until
    tickers.yaml actually maps them, same "never guess" rule as everywhere
    else here.
    """
    merged: dict[str, EquityPosition] = {}
    for p in positions:
        if p.symbol not in merged:
            merged[p.symbol] = replace(p)
            continue
        existing = merged[p.symbol]
        existing.quantity += p.quantity
        existing.invested += p.invested
        existing.current_value += p.current_value
        existing.pnl += p.pnl
        existing.avg_cost = (
            existing.invested / existing.quantity if existing.quantity else existing.avg_cost
        )
        existing.ltp = p.ltp or existing.ltp
        # net_change_pct/day_change_pct are point-in-time broker figures,
        # not additive across lots — deliberately left as the first lot's,
        # not summed into something that isn't a percentage of anything real.
    return list(merged.values())


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
            if _looks_like_a_bond(symbol):
                warnings.append(f"{symbol!r} looks like a bond/gold bond, not equity — excluded.")
                continue

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

            warning = _resolve_position(pos)
            if warning:
                warnings.append(warning)

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
    format (csv, xls, xlsx, pdf, txt), equity or mutual fund, sniffed and
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
        from mybroker.logging_setup import get_logger
        from mybroker.portfolio.importers import discover_inbox_files, extract_positions

        logger = get_logger(__name__)
        inbox_files = discover_inbox_files(inbox_dir)
        logger.info("holdings_inbox: %d file(s) found: %s", len(inbox_files),
                     ", ".join(f.name for f in inbox_files) or "(none)")

        for file in inbox_files:
            try:
                kind, positions, file_warnings = extract_positions(file)
            except ValueError as exc:
                # Never silent: this is exactly the "couldn't read/pick up
                # this file" failure mode, and previously left no trace
                # anywhere once it scrolled off the terminal.
                logger.error("holdings_inbox: %s: could not parse — %s", file.name, exc)
                warnings.append(str(exc))
                continue

            logger.info("holdings_inbox: %s: parsed as %s, %d position(s), "
                        "%d warning(s)", file.name, kind, len(positions), len(file_warnings))
            for w in file_warnings:
                logger.warning("holdings_inbox: %s", w)

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
            f"to {HOLDINGS_EQUITY}, or drop any csv/xls/xlsx/pdf/txt export into "
            f"{HOLDINGS_INBOX_DIR}/.{cd_hint_if_project_nearby()}"
        )

    # Merge lots of the same resolved symbol — real for anyone holding a
    # stock across multiple demat accounts/brokers, or whose DP recorded it
    # as separate lots after a corporate action. See
    # _merge_same_symbol_lots's own docstring for why this has to happen
    # before weights/concentration are computed, not just for display.
    equity = _merge_same_symbol_lots(equity)

    if not mfs and not mf_result_present:
        warnings.append(
            "No mutual-fund holdings found. Core/satellite analysis covers direct "
            f"equity only — add holdings_mf.csv, or drop an export into "
            f"{HOLDINGS_INBOX_DIR}/, for a complete picture."
        )

    return Portfolio(equity=equity, mutual_funds=mfs, warnings=warnings)
