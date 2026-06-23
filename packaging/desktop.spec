# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the S/R desktop terminal.

Build from the repo root:
    pyinstaller packaging/desktop.spec
Output: dist/SR-Terminal/SR-Terminal.exe  (one-folder; faster startup than one-file)
"""
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files

# Resolve paths relative to this spec file (packaging/), so the build works
# regardless of the current working directory.
ROOT = os.path.dirname(SPECPATH)  # repo root (parent of packaging/)

# plotly ships its offline JS bundle as package data (needed by get_plotlyjs()).
datas = collect_data_files("plotly")
# the desktop UI template
datas += [(os.path.join(ROOT, "sr", "web", "index.html"), "sr/web")]

binaries = []
hiddenimports = []
# pywebview + its Windows backend (pythonnet/clr) — pull everything it needs.
for pkg in ("webview", "clr_loader"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += ["clr", "webview.platforms.winforms", "webview.platforms.edgechromium"]

a = Analysis(
    [os.path.join(ROOT, "run_desktop.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=["streamlit", "matplotlib", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="SR-Terminal",
    console=False,          # set True temporarily if the window won't open (shows tracebacks)
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="SR-Terminal")
