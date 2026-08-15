#!/usr/bin/env python
"""Thin dev-checkout shim — kept for anyone with `python scripts/validate_tickers.py`
muscle memory. The real implementation lives in `mybroker.tickers_validate` so it's
importable (and packaged) regardless of how factfolio was installed; this script
only exists in a git checkout, not in a wheel or a frozen build.

Prefer either of these, which work everywhere:
    factfolio validate
    uv run validate-tickers
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mybroker.tickers_validate import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
