"""Central paths and configuration.

Everything that touches the filesystem resolves through here so the security
hooks have a single, unambiguous definition of what is inside the project.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# ── Paths ────────────────────────────────────────────────────────────────────
# Two different "roots", deliberately kept separate:
#
# SRC_DIR is where THIS PACKAGE's own bundled data lives — always relative
# to config.py's own location, correct no matter how the package got onto
# disk: an editable dev checkout, a wheel installed from PyPI, or a
# PyInstaller build. That last one is exactly why DEFAULT_TICKERS_FILE below
# is only ever a *seed*, never read directly at runtime: PyInstaller's
# onefile mode re-extracts the whole bundle into a fresh temp directory
# every launch and deletes it on exit, so a path under SRC_DIR is not a
# place anyone can durably edit — see TICKERS_FILE.
#
# PROJECT_ROOT is where the USER's data lives — holdings, policy, memory,
# reports, logs, cache, and (see below) their own tickers.yaml. See
# _apply_root()/set_project_root() below for how it's actually decided.
SRC_DIR = Path(__file__).resolve().parent

DEFAULT_TICKERS_FILE = SRC_DIR / "data" / "tickers.yaml"

# The generic, committed llm.yaml every project's own copy is seeded from —
# see llm_config.py. Kept in sync with llm_config.render_llm_yaml("claude_local")
# by test_llm_config.py, same idea as DEFAULT_TICKERS_FILE above.
DEFAULT_LLM_FILE = SRC_DIR / "data" / "llm.yaml"


def _apply_root(root: Path) -> None:
    """(Re)derive every PROJECT_ROOT-relative constant from `root`.

    Called once below for the normal cwd-based default, and again by
    set_project_root() when `factfolio init` resolves a fresh project
    folder to create. Only affects lazy `from mybroker.config import X`
    lookups made *after* the call, within this same process — modules that
    bind one of these names at their own top-level import time (e.g.
    portfolio/loader.py, portfolio/policy.py) keep whatever value they
    first saw. That's fine in practice: every `factfolio <command>` after
    `init` is its own fresh process, so it re-derives everything from
    `Path.cwd()` once you've `cd`ed into the project folder init printed.
    """
    global PROJECT_ROOT, HOLDINGS_EQUITY, HOLDINGS_MF, ASSETS_FILE
    global HOLDINGS_INBOX_DIR, MEMORY_DIR, THESES_DIR, REPORTS_DIR, LOGS_DIR
    global CACHE_DIR, CACHE_DB, TOOL_LOG, CRON_LOG, LEDGER_FILE, POLICY_FILE
    global RESOLVED_TICKERS, WRITABLE_DIRS, TICKERS_FILE, COLUMN_MAP_CACHE
    global LLM_FILE

    PROJECT_ROOT = root

    HOLDINGS_EQUITY = PROJECT_ROOT / "holdings.csv"
    HOLDINGS_MF = PROJECT_ROOT / "holdings_mf.csv"
    ASSETS_FILE = PROJECT_ROOT / "assets.yaml"

    # Drop any broker export here — csv, xls, xlsx, pdf, or txt, equity or
    # mutual-fund, any filename. portfolio/importers.py sniffs each file's
    # header row and content to classify and parse it; load_portfolio()
    # merges everything found here with the legacy HOLDINGS_EQUITY/
    # HOLDINGS_MF files above.
    HOLDINGS_INBOX_DIR = PROJECT_ROOT / "holdings_inbox"

    MEMORY_DIR = PROJECT_ROOT / "memory"
    THESES_DIR = MEMORY_DIR / "theses"
    REPORTS_DIR = PROJECT_ROOT / "reports"
    LOGS_DIR = PROJECT_ROOT / "logs"
    CACHE_DIR = PROJECT_ROOT / ".cache"

    CACHE_DB = CACHE_DIR / "market_data.db"
    TOOL_LOG = LOGS_DIR / "tool_calls.jsonl"
    CRON_LOG = LOGS_DIR / "cron.jsonl"
    LEDGER_FILE = MEMORY_DIR / "decision_journal.md"
    POLICY_FILE = MEMORY_DIR / "investment_policy.md"
    RESOLVED_TICKERS = CACHE_DIR / "resolved_tickers.json"

    # A holdings_inbox file's column layout, once an AI-assisted resolution
    # (agents/schema_resolver.py) has mapped it and that mapping passed
    # grounding against the file's own real data — keyed by header
    # signature (see portfolio/importers.py's _header_signature), not
    # filename, so the SAME broker/DP export format is recognised
    # immediately in any future file, never re-asking for a format
    # already learned once. The deterministic keyword-based column
    # matching stays the fast, free, always-tried-first path; this is
    # only ever consulted for a header it didn't already resolve.
    COLUMN_MAP_CACHE = CACHE_DIR / "column_maps.json"

    # Your own symbol map, seeded from DEFAULT_TICKERS_FILE at `factfolio
    # init` and yours to edit from then on — see load_tickers().
    TICKERS_FILE = PROJECT_ROOT / "tickers.yaml"

    # Which LLM provider every agent uses, seeded by `factfolio init` and
    # yours to hand-edit from then on — see llm_config.py.
    LLM_FILE = PROJECT_ROOT / "llm.yaml"

    # Directories the agent is permitted to write to. Enforced by security hooks.
    WRITABLE_DIRS = (MEMORY_DIR, REPORTS_DIR, LOGS_DIR, CACHE_DIR)


# PROJECT_ROOT is where the USER's data lives — holdings, policy, memory,
# reports, logs, cache. It used to be hardcoded to "wherever this package is
# installed", which only ever worked for a git-checkout dev install (`uv run
# factfolio` from the repo root); a `pip install factfolio` or a standalone
# executable run from anywhere else would silently read/write inside the
# install location instead of the user's own directory. It defaults to the
# current working directory, overridable via FACTFOLIO_HOME for cron jobs or
# scripts that launch from elsewhere — `factfolio init` is what actually
# decides, once, which directory that should be (see cli._resolve_init_target).
_apply_root(Path(os.environ.get("FACTFOLIO_HOME", ".")).resolve())


def set_project_root(root: Path) -> None:
    """Repoint every PROJECT_ROOT-relative constant at `root`, in-process.

    Used by `factfolio init` right after it creates (or reuses) the project
    folder, so the rest of *this* process sees it. See _apply_root's
    docstring for the one place that doesn't apply."""
    _apply_root(root.resolve())


def ensure_dirs() -> None:
    """Create the runtime directories. Safe to call repeatedly."""
    for d in (MEMORY_DIR, THESES_DIR, REPORTS_DIR, LOGS_DIR, CACHE_DIR, HOLDINGS_INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ── Models ───────────────────────────────────────────────────────────────────
# Worker agents run on cheaper tiers; only orchestration and adversarial
# refutation justify Opus. Overridable via env for cost experiments.
MODEL_ORCHESTRATOR = os.getenv("MYBROKER_MODEL_ORCHESTRATOR", "opus")
MODEL_WORKER = os.getenv("MYBROKER_MODEL_WORKER", "sonnet")
MODEL_CHEAP = os.getenv("MYBROKER_MODEL_CHEAP", "haiku")
MODEL_ADVERSARY = os.getenv("MYBROKER_MODEL_ADVERSARY", "opus")

# ── Graph theory / risk (M3) ──────────────────────────────────────────────────
# Two different history thresholds, deliberately distinct:
#   - min_history_days (tickers.yaml, per-symbol): is THIS symbol's own series
#     long enough to trust on its own (checked once, at ticker-validation time).
#   - MIN_CORRELATION_OVERLAP_DAYS (here, per-pair): is the OVERLAP between two
#     symbols' series long enough to trust a correlation computed from it. This
#     must be lower than min_history_days, or two genuinely short-but-valid
#     series (TMCV, TMPV — both post-demerger, Oct 2025) could never correlate
#     with each other despite being the pair the MST most needs to connect.
MIN_CORRELATION_OVERLAP_DAYS = 30

# ── Tax (India, FY25-26 rules for listed equity & equity MF) ─────────────────
STCG_RATE = 0.20  # < 12 months
LTCG_RATE = 0.125  # >= 12 months, above the exemption
LTCG_EXEMPTION = 125_000  # per financial year, in rupees
LTCG_HOLDING_DAYS = 365  # >= this many days qualifies as long term
STT_RATE = 0.001  # securities transaction tax on delivery sell


# ── Ticker map ───────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def load_tickers() -> dict[str, Any]:
    """Load and cache the symbol → ticker map: your project's own
    tickers.yaml (seeded by `factfolio init`, yours to add symbols to) if
    it exists, the bundled defaults otherwise — e.g. before your first
    `init`, or if TICKERS_FILE was deleted."""
    path = TICKERS_FILE if TICKERS_FILE.exists() else DEFAULT_TICKERS_FILE
    with path.open() as fh:
        return yaml.safe_load(fh)


def symbol_meta(symbol: str) -> dict[str, Any]:
    """Metadata for one Zerodha symbol. Raises KeyError if unmapped.

    An unmapped symbol is a hard error, never a silent fallback to
    f"{symbol}.NS" — that assumption is exactly what breaks on renames
    and demergers.
    """
    symbols = load_tickers()["symbols"]
    if symbol not in symbols:
        raise KeyError(
            f"Symbol {symbol!r} is not in tickers.yaml. Add it with an explicit "
            f"candidate list — do not rely on a .NS suffix guess."
        )
    return symbols[symbol]


_LEGAL_SUFFIXES = frozenset({
    "LIMITED", "LTD", "LTDS", "PVT", "PRIVATE", "CO", "COMPANY", "INDIA",
})


def _normalize_company_name(name: str) -> str:
    """Canonicalize a company name for cross-statement matching: case,
    punctuation, and legal-entity suffixes all vary between brokers/DPs for
    the exact same company — "HDFC Bank Ltd" vs "HDFC Bank Ltd." vs "HDFC
    BANK LIMITED" should all reduce to the same thing."""
    s = name.upper()
    for ch in ".,()&/-":
        s = s.replace(ch, " ")
    tokens = [t for t in s.split() if t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)


def resolve_symbol_by_name(display_name: str) -> str | None:
    """Best-effort: the tickers.yaml symbol whose own `name:` field (or
    recorded `aliases:`) normalizes to the same thing as `display_name`.

    Needed for holdings sources whose only identifying column is a full
    company name rather than a trading symbol — a demat "Consolidated
    Holding" PDF, say — where the same company can be spelled differently
    across rows or statements ("HDFC BANK LTD" vs "HDFC BANK LTD."), so an
    exact tickers.yaml key lookup misses a real match.

    `name:` matching alone has a real ceiling: a PDF's own table extraction
    routinely wraps a single word across a narrow column, splitting it into
    two tokens ("HINDUSTA N COPPER LTD" for "Hindustan Copper Ltd") that
    normalize to something a clean, correctly-spelled `name:` field will
    never equal, no matter how good the normalization is — the raw source
    text is simply broken in a way _normalize_company_name can't repair
    after the fact, and a broker's own abbreviations ("TATA POWER CO LTD"
    for "The Tata Power Company Limited") compound the same problem.
    Rather than chase an ever-growing set of string-repair heuristics for
    every corruption/abbreviation pattern a real file might contain,
    `aliases:` records the EXACT raw text a name was already resolved
    from — see agents/ticker_resolver.py's SYSTEM_PROMPT and cli.py's
    _agent_resolved_entry/_reviewed_entry and ticker_seeding's
    backfill_draft_entry, all of which write one — so a holding already
    proven to mean this symbol is recognised as such immediately next
    time, exact match, no fuzzy guessing needed at all.

    Deliberately conservative otherwise, matching symbol_meta()'s own
    "never guess" rule: the name-based fallback resolves only when EXACTLY
    one entry's name matches after normalization. Zero or multiple
    candidates comes back as unresolved (None) rather than picking one —
    an ambiguous or unmapped company is reported as such, never silently
    assigned to the wrong stock.
    """
    target = _normalize_company_name(display_name)
    if not target:
        return None

    normalized_display = " ".join(display_name.split()).strip().upper()
    for sym, meta in load_tickers()["symbols"].items():
        aliases = meta.get("aliases") or []
        if any(" ".join(str(a).split()).strip().upper() == normalized_display for a in aliases):
            return sym

    matches = [
        sym for sym, meta in load_tickers()["symbols"].items()
        if _normalize_company_name(meta.get("name", "")) == target
    ]
    return matches[0] if len(matches) == 1 else None


def cd_hint_if_project_nearby() -> str:
    """A "you probably need to `cd factfolio` first" suffix for a "no
    holdings"/"no policy" error message — empty string when nothing
    nearby suggests that, so callers can just append it unconditionally.

    Catches one specific, easy-to-hit mistake: `factfolio init` ran from
    directory X, creating `X/factfolio/`, and a later `factfolio
    <command>` runs from X itself rather than the folder init actually
    printed and created — e.g. `cd ~/Desktop && factfolio init` makes
    `~/Desktop/factfolio/`, then `factfolio status` from `~/Desktop`
    (forgetting the `cd`) hits "no holdings"/"no policy" even though the
    real project, one level down, has both. The generic error/advice to
    "run factfolio init" is actively wrong here — it'd just silently reuse
    the existing folder and land you right back at this same error.
    """
    nested = Path.cwd() / "factfolio" / "memory" / "investment_policy.md"
    if nested.exists() and nested.resolve() != POLICY_FILE.resolve():
        return (
            " Looks like you need to `cd factfolio` first — that's the "
            "project folder `factfolio init` already set up."
        )
    return ""


def suggest_ticker_for_name(display_name: str) -> str | None:
    """Best-guess NSE/BSE ticker for `display_name`, via yfinance's own
    search — a SUGGESTION for a human to verify and add to tickers.yaml
    themselves, never written automatically anywhere. This exists because
    real accounts showed it's necessary, not just a nice-to-have: the same
    search can return a London GDR or a Brazilian BDR alongside the actual
    NSE/BSE listing for a genuinely Indian company (filtered out below),
    and returns nothing at all for a company that's since been renamed —
    concrete examples in docs/USER_GUIDE.md. NSE (`.NS`) is preferred over
    BSE (`.BO`) when both turn up, matching every hand-written
    tickers.yaml entry's own candidate ordering.

    None on no match or any error — a network hiccup or rate limit here
    must never block whatever called this, and nothing beyond what
    yfinance's own search actually returned is ever guessed at.
    """
    try:
        import yfinance as yf

        quotes = yf.Search(display_name, max_results=8).quotes
    except Exception:
        return None

    # NSI is yfinance's own exchange code for the NSE — not a typo.
    candidates = [
        q["symbol"] for q in quotes
        if q.get("quoteType") == "EQUITY" and q.get("exchange") in ("NSI", "BSE")
    ]
    for suffix in (".NS", ".BO"):
        for c in candidates:
            if c.endswith(suffix):
                return c
    return None
