"""The operational log — every command's activity, not just uncaught
crashes.

Before this, `logs/` held exactly one thing: errors.log (see errors.py),
and only ever got an entry when an exception escaped all the way to the
top of a command — never for the routine trail of what actually happened
along the way (which files were found in holdings_inbox/, what was parsed
from each, what got skipped and why, whether the AI-assisted ticker
resolver ran or silently fell back). All of that only ever existed as
stdout, gone the moment the terminal scrolled or closed — which is exactly
how "the PDF didn't get picked up" turned into a mystery with no trail to
diagnose it from.

`get_logger()` gives every module in this package a logger that writes to
logs/factfolio.log — plain text, one line per event, human-scannable with
`tail`/`grep`, append-only and capped by rotation so it never grows
unbounded. errors.log still gets the full tracebacks (see errors.py);
factfolio.log is the higher-level narrative, including a one-line pointer
to errors.log whenever something lands there.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FILE_NAME = "factfolio.log"
_ROOT_LOGGER_NAME = "mybroker"

# Which LOGS_DIR the root logger's handler currently points at — compared
# against the *current* mybroker.config.LOGS_DIR on every get_logger() call
# so this stays correct even when PROJECT_ROOT changes mid-process (e.g.
# `factfolio init` calling config.set_project_root(), or test isolation).
# See config.py's own docstring on why PROJECT_ROOT-relative names can't
# just be imported once at module load time.
_configured_dir: Path | None = None


def get_logger(name: str = _ROOT_LOGGER_NAME) -> logging.Logger:
    """A logger under the `mybroker` hierarchy, writing to
    logs/factfolio.log. Safe to call from anywhere, any number of times —
    the handler is (re)configured at most once per distinct LOGS_DIR, never
    duplicated onto the same one.
    """
    global _configured_dir
    from mybroker.config import LOGS_DIR, ensure_dirs

    root = logging.getLogger(_ROOT_LOGGER_NAME)
    if _configured_dir != LOGS_DIR:
        ensure_dirs()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

        handler = RotatingFileHandler(
            LOGS_DIR / _LOG_FILE_NAME, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
        ))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        # Never hand INFO/WARNING volume to the root Python logger (and
        # whatever handlers some embedding process — the MCP server, a
        # test runner — may have installed there); this package's log is
        # self-contained in its own file.
        root.propagate = False
        _configured_dir = LOGS_DIR

    return logging.getLogger(name)
