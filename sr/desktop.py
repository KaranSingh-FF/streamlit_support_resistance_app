"""Local-server desktop app for the S/R terminal.

The same offline UI (``web/index.html`` with the charting library inlined) is served from a
localhost HTTP server and opened in the default browser. There is **no GUI
toolkit and no .NET/pythonnet dependency** — the Python backend talks to the page
over plain HTTP/JSON, so it runs on any Windows machine with a browser and is
fully testable headlessly (start the server, hit the endpoints).

All heavy lifting stays in :mod:`sr.engine`, :mod:`sr.storage`, :mod:`sr.charting`.
"""
from __future__ import annotations

import collections
import io
import json
import logging
import math
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
# Logging: one logger feeds two sinks — an in-memory ring (for the in-app
# "Detailed logs" panel via /api/logs) and a stream (console in dev; in the
# frozen windowed build run_desktop redirects stdout to sr_terminal.log, so the
# stream lands on disk with timestamps).
# ---------------------------------------------------------------------------
_LOG_RING: "collections.deque[str]" = collections.deque(maxlen=1000)
_LOG_FMT = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")


class _RingHandler(logging.Handler):
    def emit(self, record):
        try:
            _LOG_RING.append(self.format(record))
        except Exception:  # logging must never crash the app
            pass


log = logging.getLogger("sr")
log.setLevel(logging.INFO)
log.propagate = False
_ring = _RingHandler()
_ring.setFormatter(_LOG_FMT)
log.addHandler(_ring)  # attached at import so requests are captured even in tests


def setup_logging():
    """Add the on-disk/console stream sink. Idempotent. Call after run_desktop has
    redirected stdout (frozen windowed build) so the stream lands in sr_terminal.log."""
    if any(type(h) is logging.StreamHandler for h in log.handlers):
        return
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(_LOG_FMT)
    log.addHandler(sh)


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
        return None if (pd.isna(obj) or not math.isfinite(float(obj))) else float(obj)  # NaN AND inf -> null
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
    cfg.auto = settings.get("auto", True) is not False   # zero-config by default; explicit false to pin everything
    tfs = settings.get("timeframes")
    if tfs:
        cfg.timeframes = list(tfs)
    # the 5 advanced tunables: present in settings => the user pinned it, so auto skips it
    auto_keys = {"min_score", "max_distance_atr", "atr_multiplier", "lookback", "tick_size"}
    for key in ("atr_period", "atr_multiplier", "cluster_atr_multiplier", "min_score",
                "max_distance_atr", "min_zone_width", "lookback", "min_bars", "tick_size"):
        if settings.get(key) is not None:
            setattr(cfg, key, type(getattr(cfg, key))(settings[key]))
            if key in auto_keys:
                cfg.overrides.add(key)
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
            log.exception("preview_upload failed")
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
            log.exception("commit_upload failed")
            return {"ok": False, "error": str(exc), "trace": traceback.format_exc()}

    def run_sr(self, instrument, settings=None):
        try:
            master = storage.load_master(str(instrument))
            if master is None or master.empty:
                return {"ok": False, "error": f"No master data for {instrument}."}
            cfg = _config_from_settings(settings)
            report = {}
            if cfg.auto:   # zero-config: infer un-pinned params from the data, then run once
                cfg, report = engine.infer_config(master, cfg)
            final_zones, _, _, tf_data, diagnostics = engine.compute_sr(master, cfg)
            inst_key = master["instrument"].iloc[0]
            chart_data = charting.chart_payload(tf_data.get(inst_key, {}), final_zones, cfg.lookback)
            dec = charting._tick_decimals(cfg.tick_size)
            atr_by_tf = {}
            if not diagnostics.empty:
                for r in diagnostics[diagnostics["status"] == "used"].to_dict("records"):
                    if r.get("atr") is not None:
                        atr_by_tf[r["timeframe"]] = float(r["atr"])
            result = {
                "ok": True,
                "chart_data": _jsonsafe(chart_data),
                "price_decimals": dec,
                "zones": _jsonsafe(charting.zones_to_records(final_zones)),
                "summary": _jsonsafe(charting.summarize_zones(final_zones, dec)),
                "diagnostics": _df_records(diagnostics),
                "atr_by_tf": _jsonsafe(atr_by_tf),
                "tick_size": float(cfg.tick_size),
                "applied_settings": _jsonsafe({
                    "auto": cfg.auto, "min_score": cfg.min_score, "max_distance_atr": cfg.max_distance_atr,
                    "atr_multiplier": cfg.atr_multiplier, "lookback": cfg.lookback, "tick_size": cfg.tick_size,
                    "inferred": sorted(report.keys()),
                }),
            }
            log.info("run_sr(%s): %d zones across %d timeframe(s); auto=%s inferred=%s", instrument,
                     len(final_zones), len(chart_data.get("timeframes") or []), cfg.auto, sorted(report.keys()))
            return result
        except Exception as exc:  # noqa: BLE001
            log.exception("run_sr(%s) failed", instrument)
            return {"ok": False, "error": str(exc), "trace": traceback.format_exc()}


# ---------------------------------------------------------------------------
# page assembly
# ---------------------------------------------------------------------------
def _template_path() -> Path:
    """Locate web/index.html both in source and inside a PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / "sr" / "web" / "index.html"
    return Path(__file__).parent / "web" / "index.html"


def _lwc_path() -> Path:
    """Locate the vendored TradingView Lightweight Charts bundle (source + bundle)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / "sr" / "web" / "vendor" / "lightweight-charts.standalone.production.js"
    return Path(__file__).parent / "web" / "vendor" / "lightweight-charts.standalone.production.js"


def build_page() -> str:
    """index.html with the charting library inlined for fully-offline rendering."""
    html = _template_path().read_text(encoding="utf-8")
    return html.replace("<!--CHART_JS-->", f"<script>{_lwc_path().read_text(encoding='utf-8')}</script>")


# ---------------------------------------------------------------------------
# HTTP server (Flask)
# ---------------------------------------------------------------------------
def create_app(api: "Api | None" = None, lifecycle=None):
    """Build the Flask app. Assumes storage.set_base_dir() was already called by the caller.
    ``lifecycle`` (optional) drives quit-on-window-close via the page's unload beacon."""
    from flask import Flask, jsonify, request, Response

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # reject uploads larger than 200 MB
    api = api or Api()

    _QUIET = {"/api/logs", "/api/feed/status", "/api/feed/prices", "/api/alerts/recent"}

    @app.after_request
    def _log_request(resp):
        if request.path not in _QUIET:  # don't let 2s UI polling spam the log
            log.info("%s %s -> %s", request.method, request.path, resp.status_code)
        return resp

    @app.get("/")
    def _index():
        if lifecycle:
            lifecycle.ping()   # a page (re)load cancels any pending unload-shutdown
        return Response(build_page(), mimetype="text/html")

    @app.get("/api/logs")
    def _logs():
        return jsonify({"lines": list(_LOG_RING)})

    @app.post("/api/exit")
    def _exit():
        if lifecycle:
            lifecycle.exit()   # window closed -> the page's unload beacon quits the app
        return jsonify({"ok": True})

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

    # ---- live feed + alerts (all disk-backed, so they work with or without a running monitor) ----
    _read_json = storage.read_json_or_none

    @app.get("/api/feed/status")
    def _feed_status():
        from datetime import datetime as _dt, timezone as _tz
        from .live import alerts as _al, config as _cfg, instruments as _ins, paths as _p
        snap = _read_json(_p.snapshot_path("l1"))
        fstat = _read_json(_p.feed_status_path("l1"))
        dev = _read_json(_p.device_code_path())
        stale, reason = _al.is_stale(fstat, snap, _dt.now(_tz.utc))
        cfg = _cfg.read_config()
        auth = {"state": "device_flow"} if dev else {"state": "ok"}
        if dev:
            auth.update({k: dev.get(k) for k in ("user_code", "verification_uri", "message", "expires_at")})
        try:
            mp = _ins.load_instrument_map_from_disk()
        except Exception:  # noqa: BLE001
            mp = {}
        have = set(api.list_instruments())
        monitoring = sorted({n for iid in ((snap or {}).get("latest") or {})
                             if (n := _ins.resolve_name(iid, mp)) and n in have})
        return jsonify(_jsonsafe({
            "status": (fstat or {}).get("status", "DISCONNECTED"), "stale": stale, "reason": reason,
            "disabled": not _cfg.feed_enabled(cfg),   # analysis-only -> show "feed off", not red "DISCONNECTED"
            "sequence": (snap or {}).get("sequence"), "subscribed": (fstat or {}).get("subscribed_contracts"),
            "updated": (fstat or {}).get("updated_contracts"), "last_error": (fstat or {}).get("last_error"),
            "webhook_source": _cfg.webhook_source(cfg), "auth": auth, "monitoring": monitoring,
        }))

    @app.get("/api/feed/prices")
    def _feed_prices():
        from .live import instruments as _ins, paths as _p
        snap = _read_json(_p.snapshot_path("l1")) or {}
        try:
            mp = _ins.load_instrument_map_from_disk()
        except Exception:  # noqa: BLE001
            mp = {}
        have = set(api.list_instruments())
        out = {}
        for iid, rec in (snap.get("latest") or {}).items():
            name = _ins.resolve_name(iid, mp)
            if name and name in have:
                out[name] = {"bid": rec.get("bidPrice"), "ask": rec.get("askPrice"), "trade": rec.get("tradePrice")}
        return jsonify(_jsonsafe({"prices": out}))

    @app.get("/api/alerts/recent")
    def _alerts_recent():
        from .live import alerts as _al
        return jsonify(_jsonsafe({"alerts": _al.read_recent_alerts(request.args.get("n", default=50, type=int))}))

    @app.get("/api/instruments/overview")
    def _overview():
        """Per-instrument data coverage (what's loaded + till when) and the nearest S/R."""
        out = []
        for name in api.list_instruments():
            master = storage.load_master(name)
            if master is None or master.empty or "datetime" not in master.columns:
                continue
            row = {"instrument": name, "rows": int(len(master)),
                   "first": str(master["datetime"].min()), "last": str(master["datetime"].max()),
                   "timeframes": [], "current_price": None,
                   "nearest_support": None, "nearest_resistance": None}
            try:
                cfg = engine.SRConfig(auto=True)
                cfg, _ = engine.infer_config(master, cfg)
                fz, _, _, _, diag = engine.compute_sr(master, cfg)
                summ = charting.summarize_zones(fz, charting._tick_decimals(cfg.tick_size))
                if not diag.empty:
                    row["timeframes"] = [d["timeframe"] for d in diag.to_dict("records") if d.get("status") == "used"]
                row.update({k: summ.get(k) for k in ("current_price", "nearest_support", "nearest_resistance")})
            except Exception:  # noqa: BLE001
                log.exception("overview: S/R failed for %s", name)
            out.append(row)
        return jsonify(_jsonsafe({"instruments": out}))

    @app.get("/api/config")
    def _get_config():
        from .live import config as _cfg
        cfg = _cfg.read_config()
        return jsonify(_jsonsafe({"has_webhook": _cfg.effective_webhook(cfg) is not None,
                                  "webhook_source": _cfg.webhook_source(cfg),
                                  "feed_enabled": bool(cfg.get("feed_enabled", True)),
                                  "monitored": cfg.get("monitored", {}), "monitor": cfg.get("monitor", {})}))

    @app.post("/api/config")
    def _set_config():
        from .live import config as _cfg
        d = request.get_json(force=True, silent=True) or {}
        updates = {}
        if "teams_webhook" in d:
            updates["teams_webhook"] = str(d.get("teams_webhook") or "")
        if "feed_enabled" in d:
            updates["feed_enabled"] = bool(d.get("feed_enabled"))
        if isinstance(d.get("monitored"), dict):
            updates["monitored"] = d["monitored"]
        if isinstance(d.get("monitor"), dict):
            updates["monitor"] = d["monitor"]
        cfg = _cfg.write_config(updates)
        return jsonify(_jsonsafe({"ok": True, "has_webhook": _cfg.effective_webhook(cfg) is not None,
                                  "webhook_source": _cfg.webhook_source(cfg),
                                  "feed_enabled": bool(cfg.get("feed_enabled", True))}))

    return app


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _find_chromium() -> "str | None":
    """Path to an installed Edge or Chrome, or None. Edge ships with Windows 11."""
    import shutil

    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        shutil.which("msedge"), shutil.which("chrome"), shutil.which("google-chrome"),
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def _app_window_cmd(url: str):
    """Chromium 'app mode' command for a chromeless, desktop-style window (no tabs or
    address bar, own taskbar icon) — or None if no Edge/Chrome is found (the caller
    then falls back to the default browser). A dedicated --user-data-dir guarantees a
    NEW browser process whose lifetime tracks the window, so closing the window quits
    the app."""
    exe = _find_chromium()
    if exe is None:
        return None
    profile = _resolve_data_dir() / "_app_window"
    return [exe, f"--app={url}", f"--user-data-dir={profile}",
            "--window-size=1400,900", "--no-first-run", "--no-default-browser-check"]


def _launch_app_window(url: str):
    """Open the UI as a desktop window; return the process handle, or None on fallback."""
    import subprocess

    cmd = _app_window_cmd(url)
    if cmd is None:
        return None
    try:
        return subprocess.Popen(cmd)
    except Exception:
        return None


class _Lifecycle:
    """Drives quit from the page's unload beacon (POST /api/exit). A window close schedules
    a debounced shutdown; a page reload (GET /) cancels it, so refreshing doesn't kill the
    app. This replaces tracking the launcher process — Chromium app-mode often delegates to a
    background broker and the launcher exits immediately, which used to leave an orphan server."""

    def __init__(self):
        self.server = None
        self._timer = None
        self._lock = threading.Lock()
        self._on_exit = []   # callbacks run on real shutdown (stop feed subprocess + monitor)

    def on_exit(self, cb):
        self._on_exit.append(cb)

    def ping(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def exit(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(2.0, self._shutdown)
            self._timer.daemon = True
            self._timer.start()

    def _shutdown(self):
        log.info("window closed (unload beacon); shutting the server down")
        for cb in self._on_exit:
            try:
                cb()
            except Exception:  # noqa: BLE001
                log.exception("on_exit callback failed")
        if self.server:
            self.server.shutdown()


def _is_selftest() -> bool:
    return "--selftest" in sys.argv


def main(port: int | None = None, open_browser: bool = True):
    from werkzeug.serving import make_server

    data_dir = _resolve_data_dir()
    storage.set_base_dir(data_dir)
    setup_logging()
    log.info("starting SR Terminal (frozen=%s, data_dir=%s)", getattr(sys, "frozen", False), data_dir)
    if port is None:
        port = int(os.environ.get("SR_PORT", "0")) or _free_port()
    url = f"http://127.0.0.1:{port}/"
    lifecycle = _Lifecycle()
    server = make_server("127.0.0.1", port, create_app(lifecycle=lifecycle), threaded=True)
    lifecycle.server = server
    log.info("server listening on %s", url)

    # Live L1 feed + alert monitor — tied to the app's lifetime. Never started in tests/selftest
    # (this block lives only in main()), when SR_NO_FEED=1 (the feed subprocess itself), or in
    # analysis-only mode (config feed_enabled=false / --no-feed).
    from .live import config as _cfg
    if not _is_selftest() and _cfg.feed_enabled(_cfg.read_config()):
        try:
            from .live.feed_proc import FeedSupervisor
            from .live.monitor import Monitor
            supervisor = FeedSupervisor(data_dir, "l1")
            lifecycle.on_exit(supervisor.stop)
            supervisor.start()
            monitor = Monitor()
            lifecycle.on_exit(monitor.stop)
            monitor.start()
            log.info("live L1 feed + alert monitor started")
        except Exception:  # noqa: BLE001
            log.exception("could not start live feed/monitor; app continues without alerts")
    else:
        log.info("analysis-only mode: live feed + alerts NOT started")

    def _open():
        if os.environ.get("SR_NO_BROWSER"):
            return
        proc = _launch_app_window(url)
        if proc is None:  # no Edge/Chrome -> degrade to a normal browser tab
            log.info("no Chromium browser found; opening the default browser")
            try:
                webbrowser.open(url)
            except Exception:
                log.warning("could not open a browser automatically; open %s manually", url)
        else:
            log.info("desktop window launched (pid %s)", proc.pid)
        # Quit is driven by the page's unload beacon (/api/exit), NOT by waiting on this
        # launcher process (Chromium app-mode delegates and exits immediately).

    if open_browser:
        t = threading.Timer(1.0, _open)
        t.daemon = True   # don't let the watcher thread keep the process alive on Ctrl-C
        t.start()
    print("\n  Support / Resistance Terminal")
    print(f"  URL (if no window opens):  {url}")
    print("  (A desktop window should open automatically. Close it — or this window — to quit.)\n", flush=True)
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
            cd = charting.chart_payload(tf_data.get("SELFTEST", {}), final_zones, 300)
            check("chart payload has candles", bool(cd["timeframes"]) and bool(cd["candles"]),
                  f"{len(cd['timeframes'])} timeframes")
            page = build_page()
            check("UI template + chart lib bundled", "<!--CHART_JS-->" not in page and "LightweightCharts" in page)

            # --- live HTTP routes (the real bridge) ---
            client = create_app().test_client()
            r = client.get("/")
            check("GET / serves page", r.status_code == 200 and b"LightweightCharts" in r.data)
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
            check("POST /api/run", bool(rj.get("ok")) and "chart_data" in rj and "summary" in rj)
            json.dumps(rj, allow_nan=False)  # strict-JSON over the wire
            check("run payload is strict-JSON", True)
            sm = rj.get("summary", {})
            check("summary has actionable + plain-English + if/then",
                  all(k in sm for k in ("plain_english", "if_then", "top_support_zones",
                                        "top_resistance_zones", "nearest_support", "nearest_resistance")))
            zr = rj.get("zones", [])
            check("zones carry confidence/bucket/volume_at_level",
                  (not zr) or all(k in zr[0] for k in ("confidence", "bucket", "volume_at_level")))
            check("auto inference reported", "applied_settings" in rj and "price_decimals" in rj
                  and "inferred" in rj.get("applied_settings", {}), f"inferred={rj.get('applied_settings',{}).get('inferred')}")
            r = client.get("/api/logs")
            lj = r.get_json()
            check("GET /api/logs", r.status_code == 200 and isinstance(lj.get("lines"), list) and len(lj["lines"]) > 0,
                  f"{len(lj.get('lines', []))} lines")
            check("POST /api/exit no-ops without a lifecycle", client.post("/api/exit").get_json().get("ok") is True)

            # --- live feed / alerts pure seams + route (no network, no subprocess) ---
            from datetime import datetime as _dt, timezone as _tz
            from .live import alerts as _al, hits as _hits
            _z = [{"instrument": "X", "side": "support", "zone_low": 70.10, "zone_high": 70.20,
                   "zone_center": 70.15, "confidence": "High", "bucket": "active", "touches": 5, "score": 12.0}]
            _h = _hits.detect_hits(70.18, 70.20, _z)
            check("live: execution-aware hit detection", len(_h) == 1 and _h[0].side == "support")
            check("live: staleness gate (fresh = not stale)",
                  _al.is_stale({"status": "CONNECTED"}, {"written_at": _dt.now(_tz.utc).isoformat()}, _dt.now(_tz.utc))[0] is False)
            _card = _al.teams_card(_al.build_alert(_h[0], "id", _dt.now(_tz.utc)))
            json.dumps(_card, allow_nan=False)
            check("live: Teams card is strict-JSON", True)
            r = client.get("/api/feed/status")
            check("GET /api/feed/status", r.status_code == 200 and "stale" in r.get_json())
            ov = client.get("/api/instruments/overview").get_json()
            check("GET /api/instruments/overview",
                  isinstance(ov.get("instruments"), list) and all("last" in i and "rows" in i for i in ov["instruments"]))
            cf = client.get("/api/config").get_json()
            check("GET /api/config has feed_enabled", "feed_enabled" in cf and "has_webhook" in cf)
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
