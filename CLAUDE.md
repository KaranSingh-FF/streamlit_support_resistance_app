# CLAUDE.md

Multi-timeframe support/resistance toolkit. Two thin UIs over one pure engine.
User-facing usage lives in `README.md`; this file is the engineering map.

## Where the code is

| Path | What | Note |
|------|------|------|
| `sr/engine.py` | Pure S/R math (DataFrame in → DataFrame out). No IO, no UI. | The verified core. Most logic changes go here. |
| `sr/charting.py` | `chart_payload` (desktop candle/price JSON) + `summarize_zones` (UI cards) + `zones_to_records`; `build_sr_figure` (Plotly) is **legacy Streamlit only**. | |
| `sr/storage.py` | Per-instrument master data on disk (CSV), dedup/merge. | |
| `sr/desktop.py` | **Primary UI**: Flask server + HTTP/JSON API, opens `sr/web/index.html`. | |
| `sr/web/index.html` | The whole desktop front-end (HTML/CSS/JS). Renders with **TradingView Lightweight Charts** (`sr/web/vendor/`, inlined at serve time): single candle pane + TF selector, nearest-N S/R price lines (client-side stepper), crosshair readout, click-to-copy. | Single file. |
| `run_desktop.py` | Exe entry point (`--port`, `--selftest`, `--version`, `--feed l1`). | |
| `sr/live/` | **Live market-data feed → bars → S/R → Teams alerts.** Pure seams (`bars`,`hits`,`alerts`,`instruments`,`config`,`zones`) + isolated I/O (`feed_proc`,`auth`,`monitor`,`bar_builder`). See "Live feed" below. | `new_thing/` is the user's reference feed (gitignored). |
| `streamlit_sr_app.py` | **Legacy** Streamlit UI. Kept as fallback; does NOT use `summarize_zones`. | |
| `tests/` | pytest. `conftest.py` has the fixtures/helpers. | |
| `packaging/desktop.spec` | PyInstaller build. | |
| **`dist/`** | **PyInstaller build output (vendored deps). Gitignored. IGNORE when searching** — globbing `**/*.py` returns hundreds of irrelevant vendored files. | |

## Engine pipeline (`run_sr` / `compute_sr`)

`resample_ohlcv` → `add_atr` → `detect_swings` → `extract_levels` → `cluster_levels`
(per timeframe) → **`score_and_merge`** (across timeframes, the important one) → `_finalize_zones`.

`score_and_merge` does: classify side, merge same-side zones, **split zones wider than
`MAX_ZONE_ATR`**, score, tick-snap, filter, then a **per-instrument** enrichment pass adding
`days_since_touch`, `recency_weight`, `confidence_score`, `bucket` (active/nearby/historical).
`_finalize_zones` adds `volume_at_level` (coarsest TF) + the `confidence` High/Med/Low label.

**Zero-config:** `desktop.run_sr` sets `cfg.auto=True` → `engine.infer_config` infers `tick_size`,
`lookback`, `atr_multiplier`, `min_score` (score percentile), `max_distance_atr` from the data
(one permissive probe pass), skipping any key the user pinned via `cfg.overrides`. **`SRConfig.auto`
defaults False** so the engine/tests are byte-identical unless auto is set. `/api/run` returns
`applied_settings{...,inferred:[...]}`, `price_decimals`, and a `summary` with actionable nearest
S/R, top zones per side, a plain-English blurb, and an if/then map.

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

After build (now **one-file**): `dist\SR-Terminal.exe` — a single shareable binary, no
zip needed. Validate it with `dist\SR-Terminal.exe --selftest` (prints `21/21`).
Trade-off: one-file self-extracts to temp each launch (slower start, SmartScreen may warn).
Version is in `sr/__init__.py`.

## Test helpers (`tests/conftest.py`)

`normalized(interval, periods)`, `descending(n)` → synthetic OHLCV frames;
`qh_excel_frame(periods)` → raw QH-format frame; `tmp_store` fixture → isolated data dir.
Pattern for non-trivial logic: add an `assert`-based unit test, no heavy fixtures.

## Desktop window behavior (recent)

The exe opens a **chromeless Edge/Chrome app-mode window** (own taskbar icon), not a
browser tab. Falls back to default browser if no Chromium browser. Build is **windowed**
(`console=False`), so `run_desktop._ensure_streams()` redirects `None` stdout/stderr to
`sr_data_store/sr_terminal.log` — otherwise `print()` would crash the app. `SR_NO_BROWSER`
env var suppresses auto-open (used by tests/CLI).

**Window lifetime:** quit is driven by the page's **unload beacon** — `index.html` POSTs
`/api/exit` on `pagehide`; `_Lifecycle.exit()` schedules a **debounced** `server.shutdown()` (2s)
that a page reload (GET `/` → `_Lifecycle.ping()`) cancels, so a refresh doesn't kill the app.
This replaced an older `proc.wait()` heuristic that orphaned the server whenever Chromium
app-mode delegated to a background broker (Edge "startup boost") and the launcher exited at once.

**Logging:** the `sr` logger (`desktop.py`) feeds two sinks — an in-memory ring
(`/api/logs` → the in-app "Detailed logs" panel) and a stream (console in dev; the
redirected `sr_terminal.log` when frozen). Each HTTP request + every API error
(`log.exception`) is logged with timestamps.

## Live feed → bars → S/R → Teams alerts (`sr/live/`)

On app launch (`desktop.main`, gated by `SR_NO_FEED` + `_is_selftest`) two things start, both tied
to `_Lifecycle.on_exit`: a **`FeedSupervisor`** subprocess (`run_desktop.py --feed l1`, the exe
re-invokes itself) and the **alert `Monitor`** thread. The feed: MSAL silent-auth (`auth`, device
code only first run) → Lightstreamer connect → each update appends a tick (JSONL, `ticks`) + writes
`live/l1_latest.json`; a 15 s timer rolls **closed** 1-min bars into the master via
`storage.merge_into_master` (`bars`+`bar_builder`, offset cursor + dedupe = idempotent). The monitor:
read snapshot → **`alerts.is_stale` gate (never fire on stale)** → per mapped instrument-with-master,
`hits.detect_hits` (execution-aware: support `ask≤zone_high`, resistance `bid≥zone_low`, non-Low only)
→ dedupe one-per-zone-per-UTC-day (`alerts`, atomic state, restart-safe) → `teams.post_teams` (records
fired only on 2xx). Instrument name = `instruments.load_instrument_map` (`"Brent N26"`). Config +
webhook in `sr_data_store/config.json` (secret; env `SR_TEAMS_WEBHOOK` overrides). **Live Lightstreamer/
Azure I/O is untestable in dev** — it lives only in `feed_proc`/`auth`; everything else is pure +
covered by `tests/test_live.py`. Routes: `/api/feed/status|prices`, `/api/alerts/recent`, `/api/config`.

## Gotchas

- `dist/`, `sr_data_store/`, `*_master.csv`, `new_thing/` are gitignored — real trading data + the live
  feed's secrets/snapshots (under `sr_data_store/`) are never committed. `sr/live/instrunments.json` IS
  committed (bundled into the exe).
- Platform is Windows, Python 3.14. Bash tool is Git Bash (POSIX); PowerShell for builds/native.
- No pywebview/.NET/clr — it was removed (crashed on target machines). Don't reintroduce.
- **Spreads (FLY/1MS) are ~65% of the feed universe and legitimately quote NEGATIVE or ZERO.** Live
  price validity (`sr/live/bars.bar_price`, `sr/live/hits.detect_hits`) uses **finiteness, not `> 0`** —
  a `> 0` gate silently makes every spread go dark (no bars, no alerts). Engine already supports negatives
  (`ohlc_invalid_mask`: "Negative-price and zero-range bars are valid"). `sr/live/instrunments.json` must
  stay byte-identical to the authoritative `ls/instrunments.json`. `ls/` is gitignored (real snapshots).
