"""Embedded desktop app (pywebview) for the S/R terminal.

The window loads ``web/index.html`` with plotly.js inlined (fully offline) and
exposes a small Python API to the page via ``window.pywebview.api``. All heavy
lifting stays in :mod:`sr.engine`, :mod:`sr.storage`, and :mod:`sr.charting`.

If pywebview is unavailable, :func:`main` falls back to generating a standalone
HTML report and opening it in the default browser, so the app never hard-fails.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

from . import charting, engine, storage


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _jsonsafe(obj):
    """Make stats / records JSON-serializable (Timestamps, numpy scalars, NaN)."""
    if isinstance(obj, dict):
        return {k: _jsonsafe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonsafe(v) for v in obj]
    if isinstance(obj, (pd.Timestamp,)):
        return None if pd.isna(obj) else str(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        return None if pd.isna(obj) else float(obj)
    if obj is pd.NaT:
        return None
    return obj


def _df_records(df: pd.DataFrame):
    if df is None or df.empty:
        return []
    return [_jsonsafe(r) for r in df.to_dict(orient="records")]


def _config_from_settings(settings: dict) -> engine.SRConfig:
    settings = settings or {}
    cfg = engine.SRConfig()
    tfs = settings.get("timeframes")
    if tfs:
        cfg.timeframes = list(tfs)
    for key in ("atr_period", "atr_multiplier", "cluster_atr_multiplier", "min_score",
                "max_distance_atr", "min_zone_width", "lookback", "min_bars", "tick_size"):
        if settings.get(key) is not None:
            setattr(cfg, key, type(getattr(cfg, key))(settings[key]))
    if settings.get("use_close_for_swings") is not None:
        cfg.use_close_for_swings = bool(settings["use_close_for_swings"])
    return cfg


def _resolve_data_dir() -> Path:
    if getattr(sys, "frozen", False):  # packaged .exe -> store beside the binary
        return Path(sys.executable).parent / "sr_data_store"
    return Path(os.environ.get("SR_DATA_DIR", "sr_data_store"))


# ---------------------------------------------------------------------------
# JS API
# ---------------------------------------------------------------------------
class Api:
    def __init__(self):
        self._window = None
        self._pending = {}      # token -> parsed upload awaiting a keep/remove decision
        self._token_seq = 0

    def set_window(self, window):
        self._window = window

    # 1) data ingestion -----------------------------------------------------
    def select_file(self):
        import webview

        if self._window is None:
            return {"path": None}
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Excel files (*.xlsx;*.xls)", "All files (*.*)"),
        )
        if not result:
            return {"path": None}
        path = result[0] if isinstance(result, (list, tuple)) else result
        return {"path": path, "instrument": engine.clean_instrument_name(os.path.basename(path))}

    def update_master(self, path, instrument, sheet="Data"):
        """One-shot ingest (drops invalid OHLC). Kept for back-compat; the UI now
        uses preview_upload + commit_upload so invalid rows can be reviewed."""
        try:
            stats = storage.ingest_excel(path, str(instrument).strip(), sheet or "Data")
            return {"ok": True, "stats": _jsonsafe(stats), "instruments": storage.list_instruments()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "trace": traceback.format_exc()}

    def preview_upload(self, path, instrument, sheet="Data"):
        """Parse the file and report any OHLC-invalid rows WITHOUT writing to the
        master, so the user can decide per-row whether to keep or remove them."""
        try:
            instrument = str(instrument).strip()
            raw = storage.read_upload(path, sheet or "Data")
            n_total = int(len(raw))
            valid, invalid = engine.normalize_ohlcv_split(raw, instrument)
            self._token_seq += 1
            token = f"up{self._token_seq}"
            self._pending[token] = {"instrument": instrument, "valid": valid,
                                    "invalid": invalid, "n_total": n_total}
            invalid_rows = [{
                "key": str(r["datetime"]), "datetime": str(r["datetime"]),
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]),
                "reason": engine.ohlc_invalid_reason(r),
            } for _, r in invalid.iterrows()]
            return {"ok": True, "token": token, "instrument": instrument, "n_total": n_total,
                    "n_valid": int(len(valid)), "n_invalid": int(len(invalid)),
                    "n_parse_dropped": int(n_total - len(valid) - len(invalid)),
                    "invalid_rows": invalid_rows}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "trace": traceback.format_exc()}

    def commit_upload(self, token, keep_keys=None):
        """Finish an upload started by preview_upload, keeping only the invalid rows
        whose keys the user chose to keep."""
        try:
            p = self._pending.pop(token, None)
            if p is None:
                return {"ok": False, "error": "Upload session expired — please re-select the file."}
            keep = set(keep_keys or [])
            invalid = p["invalid"]
            kept = invalid[invalid["datetime"].astype(str).isin(keep)] if (keep and not invalid.empty) else invalid.iloc[0:0]
            combined = pd.concat([p["valid"], kept], ignore_index=True)
            combined = (combined.sort_values(["instrument", "datetime"])
                        .drop_duplicates(["instrument", "datetime"], keep="last").reset_index(drop=True))
            _, stats = storage.merge_into_master(combined, p["instrument"])
            removed = int(len(invalid) - len(kept))
            stats["rows_in_file"] = int(p["n_total"])
            stats["invalid_kept"] = int(len(kept))
            stats["invalid_removed"] = removed
            stats["rows_dropped_bad"] = int(p["n_total"] - len(p["valid"]) - len(invalid)) + removed
            return {"ok": True, "stats": _jsonsafe(stats), "instruments": storage.list_instruments()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "trace": traceback.format_exc()}

    # 2) instruments --------------------------------------------------------
    def list_instruments(self):
        return storage.list_instruments()

    def delete_master(self, instrument):
        return {"ok": storage.delete_master(str(instrument))}

    # 3) compute ------------------------------------------------------------
    def run_sr(self, instrument, settings=None):
        try:
            master = storage.load_master(str(instrument))
            if master is None or master.empty:
                return {"ok": False, "error": f"No master data for {instrument}."}
            cfg = _config_from_settings(settings)
            final_zones, _, _, tf_data, diagnostics = engine.compute_sr(master, cfg)
            inst_key = master["instrument"].iloc[0]
            fig = charting.build_sr_figure(tf_data.get(inst_key, {}), final_zones, inst_key, cfg.lookback, cfg.tick_size)
            atr_by_tf = {}
            if not diagnostics.empty:
                for r in diagnostics[diagnostics["status"] == "used"].to_dict("records"):
                    if r.get("atr") is not None:
                        atr_by_tf[r["timeframe"]] = float(r["atr"])
            return {
                "ok": True,
                "figure": fig.to_json(),
                "zones": _jsonsafe(charting.zones_to_records(final_zones)),
                "summary": _jsonsafe(charting.summarize_zones(final_zones)),
                "diagnostics": _df_records(diagnostics),
                "atr_by_tf": _jsonsafe(atr_by_tf),
                "tick_size": float(cfg.tick_size),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "trace": traceback.format_exc()}


# ---------------------------------------------------------------------------
# page assembly + launch
# ---------------------------------------------------------------------------
def _template_path() -> Path:
    """Locate web/index.html both in source and inside a PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / "sr" / "web" / "index.html"
    return Path(__file__).parent / "web" / "index.html"


def build_page() -> str:
    """index.html with plotly.js inlined for fully-offline rendering."""
    html = _template_path().read_text(encoding="utf-8")
    return html.replace("<!--PLOTLY_JS-->", charting.plotlyjs_script())


def _enable_dpi_awareness():
    """Tell Windows this process handles its own scaling, so the window is rendered
    crisply (not bitmap-stretched/blurred) on high-DPI or fractionally-scaled displays."""
    if sys.platform != "win32":
        return
    import ctypes

    for attempt in (
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),  # per-monitor v2
        lambda: ctypes.windll.user32.SetProcessDPIAware(),       # system DPI fallback
    ):
        try:
            attempt()
            return
        except Exception:
            continue


def main():
    storage.set_base_dir(_resolve_data_dir())
    _enable_dpi_awareness()
    try:
        import webview
    except Exception:  # pywebview missing -> browser fallback
        return _browser_fallback()

    # The page inlines plotly.js (~5 MB). On Windows, pywebview's EdgeChromium
    # backend renders inline `html=` via NavigateToString, which silently fails
    # for content over ~2 MB (-> blank window). Write the page to a temp file and
    # load it by URL, which has no such limit.
    import tempfile

    ui_file = Path(tempfile.gettempdir()) / "sr_terminal_ui.html"
    ui_file.write_text(build_page(), encoding="utf-8")

    api = Api()
    window = webview.create_window(
        "Support / Resistance Terminal",
        url=ui_file.as_uri(), js_api=api,
        width=1440, height=920, min_size=(1024, 720),
    )
    api.set_window(window)
    webview.start()


def _synthetic_ohlc(periods: int = 1200) -> pd.DataFrame:
    """Deterministic synthetic 15-min QH-style frame (for --selftest, no files)."""
    rng = np.random.default_rng(7)
    idx = pd.date_range("2026-01-05 13:30:00", periods=periods, freq="15min")
    close = 100 + np.cumsum(rng.normal(0, 0.15, periods))
    openp = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.1, periods)) + 0.03
    return pd.DataFrame({
        "Date": idx.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "Open": openp.round(2), "High": (np.maximum(openp, close) + spread).round(2),
        "Low": (np.minimum(openp, close) - spread).round(2),
        "Volume": rng.integers(50, 1500, periods), "Close": close.round(2),
    })


def selftest(verbose: bool = True) -> bool:
    """Exercise the whole bundle headlessly (no window): Excel IO -> engine ->
    chart -> strict-JSON payload -> UI template. Used to validate the .exe.
    """
    import json
    import tempfile

    checks: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    try:
        with tempfile.TemporaryDirectory() as d:
            storage.set_base_dir(d)
            xlsx = Path(d) / "selftest.xlsx"
            with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
                _synthetic_ohlc().to_excel(w, index=False, sheet_name="Data")

            stats = storage.ingest_excel(xlsx, "SELFTEST", "Data")
            check("excel ingest -> master", stats["master_rows_after"] > 0, f"{stats['master_rows_after']} rows")
            check("no rows dropped", stats["rows_dropped_bad"] == 0)

            master = storage.load_master("SELFTEST")
            check("load master (CSV round-trip)", master is not None and len(master) > 0)

            final_zones, _, _, tf_data, diag = engine.compute_sr(master, engine.SRConfig())
            check("zones produced", not final_zones.empty, f"{len(final_zones)} zones")
            check("diagnostics produced", not diag.empty)

            # side must be price-relative: support below price, resistance above
            if not final_zones.empty:
                cp = float(final_zones["current_price"].iloc[0])
                sup_ok = (final_zones.loc[final_zones.side == "support", "zone_center"] <= cp).all()
                res_ok = (final_zones.loc[final_zones.side == "resistance", "zone_center"] >= cp).all()
                check("zone sides are price-relative", bool(sup_ok and res_ok),
                      f"support<=cp={bool(sup_ok)}, resistance>=cp={bool(res_ok)}")

            fig = charting.build_sr_figure(tf_data.get("SELFTEST", {}), final_zones, "SELFTEST", 300)
            check("figure has traces", len(fig.data) > 0, f"{len(fig.data)} traces")

            payload = {
                "figure": fig.to_json(),
                "zones": _jsonsafe(charting.zones_to_records(final_zones)),
                "summary": _jsonsafe(charting.summarize_zones(final_zones)),
                "diagnostics": _df_records(diag),
            }
            s = json.dumps(payload, allow_nan=False)  # strict JSON, the pywebview contract
            check("strict-JSON payload", len(s) > 1000, f"{len(s)} bytes")

            page = build_page()
            check("UI template + plotly.js bundled", "<!--PLOTLY_JS-->" not in page and "Plotly" in page)
    except Exception as exc:  # noqa: BLE001
        check(f"EXCEPTION: {exc}", False, traceback.format_exc())

    passed = sum(1 for _, ok, _ in checks if ok)
    if verbose:
        for name, ok, detail in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
        print(f"\nself-test: {passed}/{len(checks)} checks passed")
    return passed == len(checks)


def _browser_fallback():
    """Minimal no-pywebview path: build a report for the first instrument and open it."""
    import tempfile
    import webbrowser

    storage.ensure_dirs()
    instruments = storage.list_instruments()
    if not instruments:
        print("No instrument data found in", storage.master_dir())
        print("Add data with the Streamlit app or sr.storage.ingest_excel(), then re-run.")
        return
    inst = instruments[0]
    master = storage.load_master(inst)
    cfg = engine.SRConfig()
    final_zones, _, _, tf_data, _ = engine.compute_sr(master, cfg)
    fig = charting.build_sr_figure(tf_data.get(inst, {}), final_zones, inst, cfg.lookback)
    out = Path(tempfile.gettempdir()) / f"{storage.safe_name(inst)}_sr.html"
    out.write_text(charting.figure_to_html(fig, include_plotlyjs=True), encoding="utf-8")
    print("pywebview not available — opened a browser report instead:", out)
    webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
