# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the S/R terminal (local server + browser).

Build from the repo root:
    pyinstaller packaging/desktop.spec
Output: dist/SR-Terminal.exe  (ONE-FILE — a single shareable binary).

One-file trade-off: the binary self-extracts to a temp dir on every launch, so
startup is a few seconds slower than a one-folder build, and an unsigned single
exe is more likely to trip Windows SmartScreen ("More info -> Run anyway"). The
data store / log still live next to the .exe (via sys.executable), not in the temp
extraction dir, so user data persists across launches.

No pywebview / pythonnet / .NET — the app serves its UI over localhost and opens
the default browser, so there is no clr/winforms dependency to bundle or break.
"""
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# Resolve paths relative to this spec file (packaging/), so the build works
# regardless of the current working directory.
ROOT = os.path.dirname(SPECPATH)  # repo root (parent of packaging/)

# ponytail: plotly only feeds the legacy Streamlit UI now (desktop uses Lightweight
# Charts). charting.py still imports plotly at module level, so its data files stay
# bundled. Drop this line + make that import lazy if you want a smaller exe.
datas = collect_data_files("plotly")
# the UI template + the inlined desktop charting library (TradingView Lightweight Charts)
datas += [(os.path.join(ROOT, "sr", "web", "index.html"), "sr/web")]
datas += [(os.path.join(ROOT, "sr", "web", "vendor", "lightweight-charts.standalone.production.js"), "sr/web/vendor")]
# the live-feed instrument map (resolved frozen-safe by sr/live/paths.py)
datas += [(os.path.join(ROOT, "sr", "live", "instrunments.json"), "sr/live")]

binaries = []
hiddenimports = []
# Flask server stack + openpyxl (the Excel engine — pinned explicitly so reading
# .xlsx never fails on a machine without it). Pure-python; pull submodules + data.
# ...plus the live market-data feed (Lightstreamer client) + Azure auth (MSAL) for the --feed subprocess.
for pkg in ("flask", "werkzeug", "jinja2", "click", "markupsafe", "itsdangerous", "blinker",
            "openpyxl", "lightstreamer", "msal"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += collect_submodules("werkzeug") + collect_submodules("lightstreamer") \
    + collect_submodules("msal") + ["openpyxl"]

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

# ONE-FILE build: fold the binaries + datas INTO the exe (no COLLECT step), so the
# whole app is a single dist/SR-Terminal.exe the user can share directly.
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="SR-Terminal",
    # Windowed: no console — the UI opens as a chromeless desktop window (Edge/Chrome
    # app mode) and closing it quits the app. run_desktop._ensure_streams() redirects
    # stdout/stderr (None in a windowed build) to sr_data_store/sr_terminal.log so
    # print() can't crash the app and startup errors stay diagnosable.
    console=False,
)
