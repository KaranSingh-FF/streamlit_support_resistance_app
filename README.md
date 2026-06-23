# Multi-Timeframe Support & Resistance Terminal

A desktop tool for traders: drop in an instrument's OHLC Excel export, maintain
a clean per-instrument history, and get **scored, multi-timeframe support &
resistance zones** drawn on interactive candlestick charts.

- **Load history once per instrument**, then append the latest day daily — old
  rows are auto-deduplicated by `instrument + datetime` (latest upload wins).
- **Per-instrument timeframe adaptation** — the engine detects each instrument's
  native bar interval and skips timeframes finer than it, so a 15-min view of an
  hourly feed can't double-count the same level.
- **Price-relative zones** — every zone is classified by where it sits *now*:
  supports below the current price, resistances above it (a broken support flips
  to resistance). The table, chart bands, and summary cards always agree.
- **Validation with review** — bars that fail OHLC logic (High ≥ Low, High ≥
  max(O,C), Low ≤ min(O,C)) are caught at ingest; the desktop app shows them in a
  dialog so you choose per-row whether to keep or remove them before anything is
  written to the master.
- **Interactive visuals** — stacked candlestick panels per timeframe with
  strength-graded S/R bands (green = support, red = resistance), swing-high/low
  markers, a current-price line, a crosshair, and range buttons. Hover any band
  for its score / touches / timeframes / distance; click the legend to toggle
  Support, Resistance, or swing markers across all panels. Summary cards show the
  current price and nearest support/resistance at a glance. It runs in your
  browser, so it scales crisply and never crops at any window size or DPI.

Two interchangeable front-ends over one shared engine:

| Interface | Command | Notes |
|---|---|---|
| **Desktop app** (primary) | `python run_desktop.py` | Local server opened in your default browser. Packaged to a standalone Windows app (`.exe`). |
| **Streamlit** (legacy) | `streamlit run streamlit_sr_app.py` | Browser UI; same engine and charts. |

---

## Quick start

```bash
pip install -r requirements.txt        # core + desktop
python run_desktop.py                  # launch the desktop terminal
```

**Daily workflow:**
1. **Select Excel file** → set/confirm the instrument name → **Update master data**.
   First file per instrument can be full history; after that, just drop the latest
   day — overlapping rows are deduplicated automatically. If any candle fails an
   OHLC sanity check, a dialog lets you keep or remove those rows before saving.
2. Pick the instrument, adjust settings if needed, click **▶ Run S/R Engine**.
3. Read the levels off the chart, the summary cards, and the sortable zone table.

**Chart controls:** hover any S/R band for its score / touches / timeframes /
distance; click **Support**, **Resistance**, or the swing markers in the legend
to toggle them across all panels; use the **7d / 1m / 3m / all** range buttons and
scroll to zoom.

No real data on hand? A synthetic sample is included:

```bash
python scripts/make_sample_data.py     # writes sample_data/SAMPLE_15min.xlsx
```

## Build a standalone .exe (Windows)

```bat
packaging\build_exe.bat
```

Produces `dist\SR-Terminal\SR-Terminal.exe` (one-folder build) that bundles its
own Python runtime — **no Python install needed on the target machine**. It
stores data in a `sr_data_store\` folder next to the executable.

Verify the built binary headlessly — exercises Excel IO, the engine, charting,
and the live HTTP routes end-to-end, no browser needed:

```bat
dist\SR-Terminal\SR-Terminal.exe --selftest
```

It prints a PASS/FAIL checklist and exits `0` on success.

> Built and tested on **Python 3.14 / pandas 3.0**. The app runs a tiny local
> server on `127.0.0.1` and opens your **default browser** (Edge / Chrome /
> Firefox) — no extra GUI runtime (and no .NET / WebView2) is required. A console
> window shows the local URL; keep it open while using the app and close it to
> quit.

## Expected Excel format

The QH export is supported directly:

```
Date, Open, High, Low, Volume, Close, BuyVolume, SellVolume, isNewCandle
```

`Date` may be ISO-8601 UTC (`2026-02-18T13:30:00.000Z`). Common lowercase
variants (`datetime/date/time`, `open/high/low/close/volume`) also work.
Negative prices (e.g. CL–BZ spreads) and zero-range bars are valid. Rows whose
date/OHLC can't be parsed are dropped and reported; rows that parse but fail OHLC
logic are surfaced in the keep/remove dialog rather than silently dropped.

## How the engine works

1. **Normalize & validate** the upload (parse, OHLC sanity checks) and merge into
   the instrument's master (CSV), keeping one row per `instrument + datetime`.
2. **Detect the native bar interval** and select effective timeframes (drop any
   finer than native to avoid duplicate levels).
3. Per timeframe: **resample → ATR → swing highs/lows → cluster** nearby swings
   into zones.
4. **Score & merge** zones across timeframes using timeframe weight, touch count,
   recency, and distance-to-price; multi-timeframe confluence boosts the score.
5. **Classify by price** (support below / resistance above) and keep zones above
   `min_score` and within `max_distance_atr` of the current price.

A **diagnostics** panel shows exactly which timeframes were used vs skipped (and
why), so nothing is dropped silently.

## Project layout

```
sr/
  engine.py          # pure S/R math (no IO / no UI)
  storage.py         # per-instrument master CSV: merge, dedup, list, delete
  charting.py        # Plotly multi-panel candlestick + zone figures
  desktop.py         # local Flask server + JSON API (Api ops) + --selftest
  web/index.html     # browser UI (plotly.js inlined; talks to /api/* via fetch)
run_desktop.py       # entry point (--selftest / --version / --port flags)
streamlit_sr_app.py  # legacy Streamlit UI (same engine)
scripts/make_sample_data.py   # synthetic OHLC generator
packaging/           # desktop.spec (PyInstaller) + build_exe.bat
tests/               # conftest + engine / charting / storage / desktop suites
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q                                   # full suite (67 tests)
python run_desktop.py --selftest            # end-to-end smoke test, exits 0/1
```

The suite covers ISO-UTC parsing and normalization edge cases (missing/lowercase/
bad-date/duplicate/negative-price), **OHLC sanity validation** and the valid/
invalid split, instrument naming, native-interval detection and the adaptive-
timeframe rule, the dedup/overwrite/append master logic, the **price-relative
side** invariant, engine **determinism**, the preview/commit keep-or-remove flow,
chart building for empty/constant/single-timeframe inputs, and the desktop API —
including the strict-JSON contract the HTTP/JSON bridge relies on (the self-test
drives the live Flask routes in-process). `--selftest` additionally asserts sides
are price-relative.

## Troubleshooting

- **First launch is slow.** A one-folder PyInstaller app unpacks on first run —
  give it a few seconds. A console window opens with the local URL, then your
  browser opens automatically.
- **Browser didn't open.** The console window prints the address
  (`http://127.0.0.1:…`); open it in any browser. Keep the console window open
  while using the app — closing it quits the app.
- **Nothing happens / an error.** Confirm the engine and routes are fine with
  `SR-Terminal.exe --selftest` (PASS/FAIL checklist; needs no browser or network).
- **"No date/datetime column" or "Missing OHLC columns".** The selected sheet
  isn't the data sheet, or the headers differ — check the **Sheet name** field
  (default `Data`) and the expected columns above.

## Data & privacy

Real market data is **excluded from git** (`sr_data_store/`, `*_master.csv`,
`*.parquet` are gitignored). Only the synthetic `sample_data/` is tracked.

## License

[MIT](LICENSE) © 2026 Deepanshu Goyal
