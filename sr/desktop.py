"""Local-server desktop app for the S/R terminal.

The same offline UI (``web/index.html`` with plotly.js inlined) is served from a
localhost HTTP server and opened in the default browser. There is **no GUI
toolkit and no .NET/pythonnet dependency** — the Python backend talks to the page
over plain HTTP/JSON, so it runs on any Windows machine with a browser and is
fully testable headlessly (start the server, hit the endpoints).

All heavy lifting stays in :mod:`sr.engine`, :mod:`sr.storage`, :mod:`sr.charting`.
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import numpy as np
import pandas as pd

from . import charting, engine, storage


# ---------------------------------------------------------------------------
# JSON helpers
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


def _read_any(source, sheet) -> pd.DataFrame:
    """Read an Excel upload from raw bytes (HTTP upload) or a path (tests/CLI)."""
    if isinstance(source, (bytes, bytearray)):
        bio = io.BytesIO(bytes(source))
        try:
            return pd.read_excel(bio, sheet_name=sheet or "Data", engine="openpyxl")
        except Exception:
            bio.seek(0)
            return pd.read_excel(bio, sheet_name=0, engine="openpyxl")
    return storage.read_upload(source, sheet or "Data")


# ---------------------------------------------------------------------------
# Operations (pure: source -> JSON-safe dict). Shared by HTTP routes + tests.
# ---------------------------------------------------------------------------
class Api:
    def __init__(self):
        self._pending = {}      # token -> parsed upload awaiting a keep/remove decision
        self._token_seq = 0
        self._lock = threading.Lock()   # guard _pending/_token_seq across threaded requests
        self._max_pending = 16          # bound memory: evict oldest un-committed uploads

    def list_instruments(self):
        return storage.list_instruments()

    def delete_master(self, instrument):
        return {"ok": storage.delete_master(str(instrument))}

    def preview_upload(self, source, instrument, sheet="Data"):
        """Parse the upload and report any OHLC-invalid rows WITHOUT writing to the
        master, so the user can decide per-row whether to keep or remove them.
        ``source`` is raw bytes (HTTP) or a path (tests)."""
        try:
            instrument = str(instrument).strip()
            if not instrument:
                return {"ok": False, "error": "Instrument name is required."}
            raw = _read_any(source, sheet)
            n_total = int(len(raw))
            valid, invalid = engine.normalize_ohlcv_split(raw, instrument)
            with self._lock:
                self._token_seq += 1
                token = f"up{self._token_seq}"
                self._pending[token] = {"instrument": instrument, "valid": valid,
                                        "invalid": invalid, "n_total": n_total}
                while len(self._pending) > self._max_pending:  # drop oldest stale upload
                    self._pending.pop(next(iter(self._pending)))
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
            with self._lock:
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
# page assembly
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


# ---------------------------------------------------------------------------
# HTTP server (Flask)
# ---------------------------------------------------------------------------
def create_app(api: "Api | None" = None):
    """Build the Flask app. Assumes storage.set_base_dir() was already called by the caller."""
    from flask import Flask, jsonify, request, Response

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # reject uploads larger than 200 MB
    api = api or Api()

    @app.get("/")
    def _index():
        return Response(build_page(), mimetype="text/html")

    @app.get("/api/instruments")
    def _instruments():
        return jsonify(api.list_instruments())

    @app.post("/api/preview")
    def _preview():
        f = request.files.get("file")
        if f is None or not (f.filename or "").strip():
            return jsonify({"ok": False, "error": "Please choose an Excel file."})
        data = f.read()
        if not data:
            return jsonify({"ok": False, "error": "The selected file is empty."})
        return jsonify(api.preview_upload(data, request.form.get("instrument", ""),
                                          request.form.get("sheet", "Data")))

    @app.post("/api/commit")
    def _commit():
        d = request.get_json(force=True, silent=True) or {}
        return jsonify(api.commit_upload(d.get("token"), d.get("keep_keys")))

    @app.post("/api/run")
    def _run():
        d = request.get_json(force=True, silent=True) or {}
        return jsonify(api.run_sr(d.get("instrument"), d.get("settings")))

    @app.post("/api/delete")
    def _delete():
        d = request.get_json(force=True, silent=True) or {}
        return jsonify(api.delete_master(d.get("instrument", "")))

    return app


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main(port: int | None = None, open_browser: bool = True):
    from werkzeug.serving import make_server

    storage.set_base_dir(_resolve_data_dir())
    if port is None:
        port = int(os.environ.get("SR_PORT", "0")) or _free_port()
    url = f"http://127.0.0.1:{port}/"

    def _open():
        try:
            webbrowser.open(url)
        except Exception:
            print(f"  (Could not open a browser automatically — please open {url} manually.)", flush=True)

    if open_browser and not os.environ.get("SR_NO_BROWSER"):
        threading.Timer(1.5, _open).start()
    print("\n  Support / Resistance Terminal")
    print(f"  Open in your browser:  {url}")
    print("  (Your browser should open automatically. Keep this window open; close it to quit.)\n", flush=True)
    server = make_server("127.0.0.1", port, create_app(), threaded=True)
    server.serve_forever()


# ---------------------------------------------------------------------------
# self-test (headless: engine + chart + HTTP routes)
# ---------------------------------------------------------------------------
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


def _synthetic_xlsx_bytes() -> bytes:
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as w:
        _synthetic_ohlc().to_excel(w, index=False, sheet_name="Data")
    return bio.getvalue()


def selftest(verbose: bool = True) -> bool:
    """Exercise the whole bundle headlessly (no browser): Excel IO -> engine ->
    chart -> strict-JSON, AND the live HTTP routes via Flask's test client."""
    import tempfile

    checks: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    try:
        with tempfile.TemporaryDirectory() as d:
            storage.set_base_dir(d)

            # --- engine / chart path ---
            stats = storage.ingest_excel(
                _write_tmp_xlsx(Path(d) / "selftest.xlsx"), "SELFTEST", "Data")
            check("excel ingest -> master", stats["master_rows_after"] > 0, f"{stats['master_rows_after']} rows")
            master = storage.load_master("SELFTEST")
            check("load master (CSV round-trip)", master is not None and len(master) > 0)
            final_zones, _, _, tf_data, diag = engine.compute_sr(master, engine.SRConfig())
            check("zones produced", not final_zones.empty, f"{len(final_zones)} zones")
            if not final_zones.empty:
                cp = float(final_zones["current_price"].iloc[0])
                sup_ok = (final_zones.loc[final_zones.side == "support", "zone_center"] <= cp).all()
                res_ok = (final_zones.loc[final_zones.side == "resistance", "zone_center"] >= cp).all()
                check("zone sides are price-relative", bool(sup_ok and res_ok))
            fig = charting.build_sr_figure(tf_data.get("SELFTEST", {}), final_zones, "SELFTEST", 300)
            check("figure has traces", len(fig.data) > 0, f"{len(fig.data)} traces")
            page = build_page()
            check("UI template + plotly.js bundled", "<!--PLOTLY_JS-->" not in page and "Plotly" in page)

            # --- live HTTP routes (the real bridge) ---
            client = create_app().test_client()
            r = client.get("/")
            check("GET / serves page", r.status_code == 200 and b"Plotly" in r.data)
            r = client.post("/api/preview", content_type="multipart/form-data", data={
                "instrument": "ST2", "sheet": "Data",
                "file": (io.BytesIO(_synthetic_xlsx_bytes()), "x.xlsx")})
            pv = r.get_json()
            check("POST /api/preview", bool(pv and pv.get("ok")), f"{pv and pv.get('n_total')} rows")
            r = client.post("/api/commit", json={"token": pv["token"], "keep_keys": []})
            check("POST /api/commit", bool(r.get_json().get("ok")))
            r = client.get("/api/instruments")
            check("GET /api/instruments", "ST2" in (r.get_json() or []))
            r = client.post("/api/run", json={"instrument": "ST2",
                                              "settings": {"timeframes": ["15min", "1h", "4h", "1D"]}})
            rj = r.get_json()
            check("POST /api/run", bool(rj.get("ok")) and "figure" in rj and "summary" in rj)
            json.dumps(rj, allow_nan=False)  # strict-JSON over the wire
            check("run payload is strict-JSON", True)
    except Exception as exc:  # noqa: BLE001
        check(f"EXCEPTION: {exc}", False, traceback.format_exc())

    passed = sum(1 for _, ok, _ in checks if ok)
    if verbose:
        for name, ok, detail in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
        print(f"\nself-test: {passed}/{len(checks)} checks passed")
    return passed == len(checks)


def _write_tmp_xlsx(path: Path) -> Path:
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        _synthetic_ohlc().to_excel(w, index=False, sheet_name="Data")
    return path


if __name__ == "__main__":
    main()
