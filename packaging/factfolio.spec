# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the standalone factfolio executable.

Run from the repo root:
    uv run --extra build pyinstaller packaging/factfolio.spec --noconfirm

factfolio is a pure terminal tool — no GUI, no bundled frontend — so this
spec only needs to worry about the handful of packages that do their own
runtime data/plugin discovery PyInstaller's default import analysis won't
see: pdfplumber (bundled fonts/CMaps) and yfinance (bundled data files).

One file is bundled as literal data (not compiled into the archive) because
it's opened as a real filesystem path at runtime, not imported as a module:
tickers.yaml (mybroker.config.SRC_DIR / "data" / "tickers.yaml").
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

REPO_ROOT = Path(SPECPATH).resolve().parent
SRC = REPO_ROOT / "src"

datas = []
binaries = []
hiddenimports = []

for pkg in ("pdfplumber", "yfinance"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# So `importlib.metadata.version("factfolio")` (src/mybroker/__init__.py,
# backs `factfolio --version`) keeps working once frozen.
datas += copy_metadata("factfolio")

datas += [
    (str(SRC / "mybroker" / "data" / "tickers.yaml"), "mybroker/data"),
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
