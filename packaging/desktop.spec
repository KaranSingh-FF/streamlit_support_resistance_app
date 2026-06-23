# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the S/R terminal (local server + browser).

Build from the repo root:
    pyinstaller packaging/desktop.spec
Output: dist/SR-Terminal/SR-Terminal.exe  (one-folder; faster startup than one-file)

No pywebview / pythonnet / .NET — the app serves its UI over localhost and opens
the default browser, so there is no clr/winforms dependency to bundle or break.
"""
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# Resolve paths relative to this spec file (packaging/), so the build works
# regardless of the current working directory.
ROOT = os.path.dirname(SPECPATH)  # repo root (parent of packaging/)

# plotly ships its offline JS bundle as package data (needed by get_plotlyjs()).
datas = collect_data_files("plotly")
# the UI template
datas += [(os.path.join(ROOT, "sr", "web", "index.html"), "sr/web")]

binaries = []
hiddenimports = []
# Flask server stack + openpyxl (the Excel engine — pinned explicitly so reading
# .xlsx never fails on a machine without it). Pure-python; pull submodules + data.
for pkg in ("flask", "werkzeug", "jinja2", "click", "markupsafe", "itsdangerous", "blinker", "openpyxl"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += collect_submodules("werkzeug") + ["openpyxl"]

a = Analysis(
    [os.path.join(ROOT, "run_desktop.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    # Keep the bundle free of the GUI/.NET stack and other unused heavyweights.
    excludes=["streamlit", "matplotlib", "tkinter",
              "webview", "clr", "pythonnet", "clr_loader"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="SR-Terminal",
    console=True,   # console shows the local URL and acts as the quit control
)
coll = COLLECT(exe, a.binaries, a.datas, name="SR-Terminal")
