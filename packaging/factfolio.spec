# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the standalone factfolio executable.

Run from the repo root:
    uv run --extra build pyinstaller packaging/factfolio.spec --noconfirm

Streamlit has no PyInstaller hook of its own (checked: neither streamlit nor
pyinstaller-hooks-contrib ships one), so its frontend static assets and
dynamically-imported submodules have to be collected explicitly here —
that's the single most fragile part of this build. plotly and pdfplumber get
the same treatment for the same reason (bundled non-.py data / runtime
plugin discovery that PyInstaller's default import analysis won't see).

Two files are bundled as literal data (not compiled into the archive)
because they're opened as real filesystem paths at runtime, not imported as
modules: tickers.yaml (mybroker.config.SRC_DIR / "data" / "tickers.yaml")
and dashboard.py (mybroker.ui.cli.cmd_dashboard, launched via Streamlit's
bootstrap.run() in the frozen-executable branch).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

REPO_ROOT = Path(SPECPATH).resolve().parent
SRC = REPO_ROOT / "src"

datas = []
binaries = []
hiddenimports = []

for pkg in ("streamlit", "plotly", "pdfplumber", "yfinance"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# So `importlib.metadata.version("factfolio")` (src/mybroker/__init__.py,
# backs `factfolio --version`) keeps working once frozen.
datas += copy_metadata("factfolio")

datas += [
    (str(SRC / "mybroker" / "data" / "tickers.yaml"), "mybroker/data"),
    (str(SRC / "mybroker" / "ui" / "dashboard.py"), "mybroker/ui"),
]

a = Analysis(
    [str(REPO_ROOT / "packaging" / "entrypoint.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="factfolio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
