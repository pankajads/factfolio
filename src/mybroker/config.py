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
# SRC_DIR is where THIS PACKAGE's own bundled data lives (tickers.yaml) —
# always relative to config.py's own location, correct no matter how the
# package got onto disk: an editable dev checkout, a wheel installed from
# PyPI, or a PyInstaller build (which bundles data/tickers.yaml at the
# matching path, so this keeps resolving correctly there too).
#
# PROJECT_ROOT is where the USER's data lives — holdings, policy, memory,
# reports, logs, cache. It used to be hardcoded to "wherever this package is
# installed", which only ever worked for a git-checkout dev install (`uv run
# factfolio` from the repo root); a `pip install factfolio` or a standalone
# executable run from anywhere else would silently read/write inside the
# install location instead of the user's own directory. The documented
# workflow (docs/USER_GUIDE.md) has always been "cd into your portfolio
# folder, run factfolio commands there" — the same model git/terraform use —
# so PROJECT_ROOT now really is that: the current working directory,
# overridable via FACTFOLIO_HOME for cron jobs or scripts that launch from
# elsewhere.
SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.environ.get("FACTFOLIO_HOME", ".")).resolve()

HOLDINGS_EQUITY = PROJECT_ROOT / "holdings.csv"
HOLDINGS_MF = PROJECT_ROOT / "holdings_mf.csv"
ASSETS_FILE = PROJECT_ROOT / "assets.yaml"

# Drop any broker export here — csv, xls, xlsx, pdf, or txt, equity or mutual-fund,
# any filename. portfolio/importers.py sniffs each file's header row and
# content to classify and parse it; load_portfolio() merges everything found
# here with the legacy HOLDINGS_EQUITY/HOLDINGS_MF files above.
HOLDINGS_INBOX_DIR = PROJECT_ROOT / "holdings_inbox"

TICKERS_FILE = SRC_DIR / "data" / "tickers.yaml"

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

# Directories the agent is permitted to write to. Enforced by security hooks.
WRITABLE_DIRS = (MEMORY_DIR, REPORTS_DIR, LOGS_DIR, CACHE_DIR)


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
    """Load and cache the symbol → ticker map."""
    with TICKERS_FILE.open() as fh:
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
