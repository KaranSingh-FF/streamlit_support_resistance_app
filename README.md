# Multi-Timeframe Support & Resistance Terminal

A desktop tool for traders: drop in an instrument's OHLC Excel export, maintain
a clean per-instrument history, and get **scored, multi-timeframe support &
resistance zones** drawn on interactive candlestick charts.

- **Load history once per instrument**, then append the latest day daily — old
  rows are auto-deduplicated by `instrument + datetime` (latest upload wins).
- **Per-instrument timeframe adaptation** — the engine detects each instrument's
  native bar interval and skips timeframes finer than it, so a 15-min view of an
  hourly feed can't double-count the same level.
- **Interactive visuals** — stacked candlestick panels per timeframe with
  strength-graded S/R bands (green = support, red = resistance), a current-price
  line, and hover details (score, touches, contributing timeframes, distance).

Two interchangeable front-ends over one shared engine:

| Interface | Command | Notes |
|---|---|---|
| **Desktop app** (primary) | `python run_desktop.py` | Embedded window (pywebview). Packageable to a single `.exe`. |
| **Streamlit** (legacy) | `streamlit run streamlit_sr_app.py` | Browser UI; same engine and charts. |

---

## Quick start

```bash
pip install -r requirements.txt        # core + desktop
python run_desktop.py                  # launch the desktop terminal
```

Then in the app:
1. **Select Excel file** → set/confirm the instrument name → **Update master data**.
   (First file per instrument can be full history; later files just the last day.)
2. Pick the instrument, adjust settings if needed, click **▶ Run S/R Engine**.
3. Read the zones off the chart and the sortable table.

No real data on hand? A synthetic sample is included:

```bash
python scripts/make_sample_data.py     # writes sample_data/SAMPLE_15min.xlsx
```

## Build a standalone .exe (Windows)

```bat
packaging\build_exe.bat
```

Produces `dist\SR-Terminal\SR-Terminal.exe` (one-folder build). It stores its
data in a `sr_data_store\` folder next to the executable. If the window fails to
open, edit `packaging/desktop.spec` and set `console=True` to see tracebacks.

> Built and tested on **Python 3.14 / pandas 3.0**. pywebview uses the Windows
> WebView2 runtime (preinstalled on Windows 11).

## Expected Excel format

The QH export is supported directly:

```
Date, Open, High, Low, Volume, Close, BuyVolume, SellVolume, isNewCandle
```

`Date` may be ISO-8601 UTC (`2026-02-18T13:30:00.000Z`). Common lowercase
variants (`datetime/date/time`, `open/high/low/close/volume`) also work. Rows
whose date or OHLC can't be parsed are dropped and reported, never silently lost.

## How the engine works

1. **Normalize** the upload and merge into the instrument's master (CSV), keeping
   one row per `instrument + datetime`.
2. **Detect the native bar interval** and select effective timeframes (drop any
   finer than native to avoid duplicate levels).
3. Per timeframe: **resample → ATR → swing highs/lows → cluster** nearby swings
   into zones.
4. **Score & merge** zones across timeframes using timeframe weight, touch count,
   recency, and distance-to-price; multi-timeframe confluence boosts the score.
5. Keep zones above `min_score` and within `max_distance_atr` of current price.

A **diagnostics** panel shows exactly which timeframes were used vs skipped (and
why), so nothing is dropped silently.

## Project layout

```
sr/
  engine.py      # pure S/R math (no IO / no UI)
  storage.py     # per-instrument master CSV: merge, dedup, list, delete
  charting.py    # Plotly multi-panel candlestick + zone figures
  desktop.py     # pywebview app + JS API bridge
  web/index.html # desktop UI (plotly.js injected at runtime)
run_desktop.py   # desktop entry point
streamlit_sr_app.py  # legacy Streamlit UI (same engine)
scripts/make_sample_data.py  # synthetic OHLC generator
packaging/       # PyInstaller spec + build_exe.bat
tests/           # pytest suite (engine + dedup + adaptive timeframes)
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

Covers ISO-UTC parsing, instrument naming, native-interval detection, the
adaptive-timeframe rule, and the dedup/overwrite/append master logic.

## Data & privacy

Real market data is **excluded from git** (`sr_data_store/`, `*_master.csv`,
`*.parquet` are gitignored). Only the synthetic `sample_data/` is tracked.

## License

[MIT](LICENSE) © 2026 Deepanshu Goyal
