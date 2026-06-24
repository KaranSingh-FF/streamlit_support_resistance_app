"""The ``--feed l1`` subprocess body + a supervisor that the app spawns on startup.

run_feed_l1: silent-auth -> Lightstreamer connect -> on each update capture a tick (append-only
JSONL) + write the latest snapshot; a timer rolls closed 1-min bars into the master and another
refreshes the token. All network/credential I/O lives here (isolated). The app never imports this
except inside the spawned child; if deps/creds/network are absent the feed writes a DISCONNECTED
status and exits, leaving the app + S/R + uploads fully functional."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import storage
from . import alerts, auth, bar_builder, instruments, paths, ticks

log = logging.getLogger("sr")

BUILD_EVERY_SEC = 15
TOKEN_REFRESH_SEC = 30 * 60
LS_SERVER = os.getenv("LS_SERVER", "https://ls.prod-live.hertshtengroup.com/")


def _load_subscription_instruments(instruments_json_path: Path) -> list:
    with open(instruments_json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    ids = []
    for prod in (data.get("products", {}) or {}).values():
        if not prod.get("enabled"):
            continue
        for entry in prod.get("instruments", []):
            if not entry.get("enabled"):
                continue
            vid = str(entry.get("instrument_id", "")).strip()
            if not vid or vid.lower() == "none":
                continue
            ids.append(vid if "-" in vid else f"{vid}-2")
    return ids


def _write_status(status: str, error=None, subscribed=0, updated=0) -> None:
    storage.write_json_atomic(paths.feed_status_path("l1"), {
        "status": status,
        "last_update_ts": datetime.now(timezone.utc).isoformat(),
        "subscribed_contracts": int(subscribed),
        "updated_contracts": int(updated),
        "last_error": error,
    })


def run_feed_l1(level: str = "l1") -> int:
    """Body of `run_desktop.py --feed l1`. Returns a process exit code."""
    from ..desktop import _resolve_data_dir  # late import (avoids cycle at module load)
    storage.set_base_dir(_resolve_data_dir())
    paths.ensure_live_dirs()

    try:
        mapping = instruments.load_instrument_map_from_disk()
    except Exception:
        log.exception("feed: cannot load instrument map")
        _write_status("DISCONNECTED", error="instrument map missing")
        return 0

    token, user = auth.get_token()
    if not token:
        _write_status("DISCONNECTED", error="authentication failed")
        return 0
    user = os.getenv("LS_USER", user or "deepanshu.goyal")

    try:
        from lightstreamer.client import (ClientListener, LightstreamerClient,
                                          Subscription, SubscriptionListener)
    except Exception as exc:  # noqa: BLE001
        log.error("feed: lightstreamer client not available: %s", exc)
        _write_status("DISCONNECTED", error=f"feed deps missing: {exc}")
        return 0

    sub_ids = _load_subscription_instruments(paths.instruments_json_path())
    latest: dict = {}
    state = {"sequence": 0, "updates": 0, "status": "DISCONNECTED"}
    builder = bar_builder.BarBuilder(mapping=mapping)

    def write_snapshot():
        state["sequence"] += 1
        storage.write_json_atomic(paths.snapshot_path("l1"), {
            "sequence": state["sequence"],
            "written_at": datetime.now(timezone.utc).isoformat(),
            "latest": latest,
        })

    client = LightstreamerClient(LS_SERVER, "MARKET_DATA")
    client.connectionOptions.setFirstRetryMaxDelay(5000)
    client.connectionOptions.setRetryDelay(5000)
    client.connectionDetails.setUser(user)
    client.connectionDetails.setPassword(token)

    class _CL(ClientListener):
        def onStatusChange(self, status):
            state["status"] = "CONNECTED" if "CONNECTED" in status.upper() else "DISCONNECTED"
            _write_status(state["status"], subscribed=len(sub_ids), updated=state["updates"])

        def onServerError(self, code, message):
            log.error("feed server error %s: %s", code, message)

    class _SL(SubscriptionListener):
        def onItemUpdate(self, update):
            cleaned = {f: ticks.clean_value(f, update.getValue(f)) for f in ticks.FIELDS}
            iid = str(cleaned.get("instrumentId") or cleaned.get("key") or update.getItemName() or "").split("-")[0]
            if not iid:
                return
            cleaned["instrumentId"] = iid
            now = datetime.now(timezone.utc)
            rec = ticks.tick_record(cleaned, now.isoformat())
            name = mapping.get(iid)
            if name:   # capture ticks only for mapped instruments (those we can build bars for)
                try:
                    ticks.append_tick(paths.tick_log_path(storage.safe_name(name), alerts.session_day(now)), rec)
                except OSError:
                    log.exception("feed: tick append failed for %s", name)
            cleaned["_recv_ts"] = now.isoformat()   # per-instrument freshness, so the monitor can
            latest[iid] = cleaned                   # catch a frozen quote even if the snapshot looks fresh
            state["updates"] += 1
            write_snapshot()

    client.addListener(_CL())
    sub = Subscription("MERGE", sub_ids, ticks.FIELDS)
    sub.setDataAdapter("MDS")
    sub.setRequestedSnapshot("yes")
    sub.setRequestedMaxFrequency(1)
    sub.addListener(_SL())

    _write_status("CONNECTING", subscribed=len(sub_ids))
    client.connect()
    time.sleep(8)
    client.subscribe(sub)
    log.info("feed l1 subscribed to %d instruments as %s", len(sub_ids), user)

    stop = threading.Event()

    def _worker():
        last_build = 0.0
        last_refresh = time.monotonic()   # first refresh is TOKEN_REFRESH_SEC after connect, not now
        while not stop.is_set():
            now = time.monotonic()
            if now - last_build >= BUILD_EVERY_SEC:
                last_build = now
                try:
                    builder.build_once(now_utc=datetime.now(timezone.utc))
                except Exception:
                    log.exception("feed: bar build failed")
            if now - last_refresh >= TOKEN_REFRESH_SEC:
                last_refresh = now
                try:
                    new_token, _ = auth.get_token(silent_only=True)   # NEVER block on device flow here
                    if new_token:
                        client.connectionDetails.setPassword(new_token)
                        log.info("feed: token refreshed")
                    else:
                        log.warning("feed: silent token refresh failed; reconnect will re-auth")
                except Exception:
                    log.exception("feed: token refresh failed")
            stop.wait(1.0)

    t = threading.Thread(target=_worker, daemon=True, name="sr-feed-worker")
    t.start()
    try:
        while not stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        _write_status("DISCONNECTED", subscribed=len(sub_ids), updated=state["updates"])
    return 0


class FeedSupervisor:
    """Spawn the feed as a child process and keep it alive (bounded-backoff restart). Lifetime is
    tied to the app via _Lifecycle.on_exit(self.stop)."""

    def __init__(self, data_dir, level: str = "l1"):
        self.data_dir = str(data_dir)
        self.level = level
        self._proc: "subprocess.Popen | None" = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: "threading.Thread | None" = None

    def _cmd(self):
        if getattr(sys, "frozen", False):
            return [sys.executable, "--feed", self.level]           # the exe re-invokes itself
        return [sys.executable, str(Path("run_desktop.py").resolve()), "--feed", self.level]

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="sr-feed-supervisor")
        self._thread.start()

    @staticmethod
    def _terminate(p):
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass

    def _run(self):
        backoff = 2.0
        env = dict(os.environ, SR_DATA_DIR=self.data_dir, SR_NO_FEED="1")  # child must not recurse-spawn
        while not self._stop.is_set():
            try:
                log.info("starting feed subprocess: %s", " ".join(self._cmd()))
                proc = subprocess.Popen(self._cmd(), env=env)
                with self._lock:
                    self._proc = proc
                if self._stop.is_set():        # stop() raced in during the spawn -> kill it now
                    self._terminate(proc)
                    break
                proc.wait()
            except Exception:
                log.exception("feed supervisor: spawn failed")
            if self._stop.is_set():
                break
            log.warning("feed subprocess exited; restarting in %.0fs", backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def stop(self):
        self._stop.set()
        with self._lock:
            p = self._proc
        self._terminate(p)
