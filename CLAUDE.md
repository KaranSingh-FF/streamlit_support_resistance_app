# CLAUDE.md

Multi-timeframe support/resistance toolkit. Two thin UIs over one pure engine.
User-facing usage lives in `README.md`; this file is the engineering map.

## Where the code is

| Path | What | Note |
|------|------|------|
| `sr/engine.py` | Pure S/R math (DataFrame in → DataFrame out). No IO, no UI. | The verified core. Most logic changes go here. |
| `sr/charting.py` | Plotly figure + `summarize_zones` (UI cards) + `zones_to_records`. | |
| `sr/storage.py` | Per-instrument master data on disk (CSV), dedup/merge. | |
| `sr/desktop.py` | **Primary UI**: Flask server + HTTP/JSON API, opens `sr/web/index.html`. | |
| `sr/web/index.html` | The whole desktop front-end (HTML/CSS/JS), plotly.js inlined at serve time. | Single file. |
| `run_desktop.py` | Exe entry point (`--port`, `--selftest`, `--version`). | |
| `streamlit_sr_app.py` | **Legacy** Streamlit UI. Kept as fallback; does NOT use `summarize_zones`. | |
| `tests/` | pytest. `conftest.py` has the fixtures/helpers. | |
| `packaging/desktop.spec` | PyInstaller build. | |
| **`dist/`** | **PyInstaller build output (vendored deps). Gitignored. IGNORE when searching** — globbing `**/*.py` returns hundreds of irrelevant vendored files. | |

## Engine pipeline (`run_sr` / `compute_sr`)

`resample_ohlcv` → `add_atr` → `detect_swings` → `extract_levels` → `cluster_levels`
(per timeframe) → **`score_and_merge`** (across timeframes, the important one).

`score_and_merge` does: classify side, merge same-side zones, score, tick-snap, filter.

## Invariants — don't break these (all test-enforced)

- **Side is PRICE-RELATIVE, not swing-type.** `side ∈ {support, resistance}` only;
  support center ≤ current_price, resistance center ≥ price. A former swing-low now
  above price is resistance. Adding a third side value breaks `test_sides_are_valid`
  and the price-relative regression tests.
- **Zone merge** in `score_and_merge` merges same-side zones when centers are close
  OR `[zone_low, zone_high]` ranges overlap. The overlap sweep requires the group be
  **sorted by `zone_low`** (running-max-high invariant) — not by center.
- **Nearest cards** (`summarize_zones`) measure to the zone's near EDGE, and a zone
  the price sits *inside* is excluded from nearest S/R and reported as `current_zone`.
- **Scoring has no proximity boost** — distance only ever subtracts a far-penalty.
  Don't add inverse-distance scoring.
- **HTTP bridge is strict-JSON**: every `/api/*` payload must pass
  `json.dumps(..., allow_nan=False)`. Route data through `desktop._jsonsafe` (maps
  NaN/NaT/numpy → JSON-safe). New numeric fields that can be NaN must be handled.

## Commands (Windows / PowerShell)

```
python -m pytest tests/ -q                                   # tests
python -c "from sr import desktop; desktop.selftest()"        # headless engine+HTTP selftest
python run_desktop.py                                         # run the app (dev)
python -m PyInstaller --noconfirm --clean packaging\desktop.spec   # build exe (NOT `pyinstaller` — not on PATH)
```

After build: `dist\SR-Terminal\SR-Terminal.exe`. Zip with
`Compress-Archive -Path dist\SR-Terminal -DestinationPath dist\SR-Terminal-vX.Y.Z.zip`.
Version is in `sr/__init__.py`.

## Test helpers (`tests/conftest.py`)

`normalized(interval, periods)`, `descending(n)` → synthetic OHLCV frames;
`qh_excel_frame(periods)` → raw QH-format frame; `tmp_store` fixture → isolated data dir.
Pattern for non-trivial logic: add an `assert`-based unit test, no heavy fixtures.

## Desktop window behavior (recent)

The exe opens a **chromeless Edge/Chrome app-mode window** (own taskbar icon), not a
browser tab; closing it stops the server. Falls back to default browser if no Chromium
browser. Build is **windowed** (`console=False`), so `run_desktop._ensure_streams()`
redirects `None` stdout/stderr to `sr_data_store/sr_terminal.log` — otherwise `print()`
would crash the app. `SR_NO_BROWSER` env var suppresses auto-open (used by tests/CLI).

## Gotchas

- `dist/`, `sr_data_store/`, `*_master.csv` are gitignored — real trading data is never committed.
- Platform is Windows, Python 3.14. Bash tool is Git Bash (POSIX); PowerShell for builds/native.
- No pywebview/.NET/clr — it was removed (crashed on target machines). Don't reintroduce.
