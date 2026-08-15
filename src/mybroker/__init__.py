"""FactFolio — open-source, evidence-based portfolio advisory for Indian
equity & mutual funds. Every number traceable to a tool call, never a guess.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth is pyproject.toml's [project].version — this
    # reads whatever was actually installed (editable dev checkout, a wheel
    # from PyPI). The PyInstaller build passes `--copy-metadata factfolio` so
    # the frozen dist-info is bundled too and this keeps working there.
    __version__ = version("factfolio")
except PackageNotFoundError:  # pragma: no cover - only when wholly unbuilt
    __version__ = "0.0.0+unknown"
