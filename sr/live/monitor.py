"""Background alert monitor: read the L1 snapshot, gate on staleness (feed-level AND per-instrument),
and fire execution-aware Teams alerts (one per zone per UTC day, persisted). Every fire decision is a
pure + tested seam; this class only orchestrates and persists. NEVER fires on stale data. The /api
routes read alert state from disk, so this class exposes no read getters — only ``stop``."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from .. import storage
from . import alerts, config, hits, instruments, paths, teams, zones

log = logging.getLogger("sr")


def _read_json(path):
    return storage.read_json_or_none(path)


def _dec(v):
    return 2 if (v is None or abs(v) >= 10) else 4


def _record_age_sec(rec: dict, snapshot, now: datetime) -> float:
    """Age of THIS instrument's quote. Prefer the per-record receive stamp (so a frozen quote for a
    quiet contract is caught even while a chatty contract keeps the snapshot fresh); fall back to the
    snapshot's written_at. A present-but-unparseable stamp -> inf (suppress, don't fire on garbage)."""
    rt = rec.get("_recv_ts")
    if rt is None:
        rt = (snapshot or {}).get("written_at")
        if rt is None:
            return 0.0
    try:
        wt = datetime.fromisoformat(str(rt).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return float("inf")
    if wt.tzinfo is None:
        wt = wt.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - wt.astimezone(timezone.utc)).total_seconds()


class Monitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="sr-alert-monitor")
        self._stop = threading.Event()
        try:
            self.mapping = instruments.load_instrument_map_from_disk()
        except Exception:
            log.exception("monitor: could not load instrument map; alerts disabled")
            self.mapping = {}
        self.zone_cache = zones.ZoneCache()
        self.state = alerts.load_state()

    def stop(self):
        self._stop.set()

    def run(self):
        log.info("alert monitor started")
        while not self._stop.is_set():
            poll = 2.0
            try:
                poll = self._tick()
            except Exception:
                log.exception("monitor tick failed")
            self._stop.wait(max(0.5, poll))
        log.info("alert monitor stopped")

    def _tick(self) -> float:
        cfg = config.read_config()
        mon = cfg.get("monitor", {}) or {}
        poll = float(mon.get("poll_sec", 2) or 2)
        if not mon.get("enabled", True):
            return poll
        self.zone_cache.recompute_sec = int(mon.get("zone_recompute_sec", 300) or 300)
        max_age = int(mon.get("max_age_sec", 30) or 30)

        snapshot = _read_json(paths.snapshot_path("l1"))
        feed_status = _read_json(paths.feed_status_path("l1"))
        now = datetime.now(timezone.utc)
        if alerts.is_stale(feed_status, snapshot, now, max_age)[0]:
            return poll                                   # feed-level stale -> never fire
        webhook = config.effective_webhook(cfg)
        if webhook is None:
            return poll                                   # nothing to deliver to; leave un-fired

        have_master = set(storage.list_instruments())
        monitored_cfg = cfg.get("monitored", {}) or {}
        day = alerts.session_day(now)
        for iid, rec in ((snapshot or {}).get("latest", {}) or {}).items():
            name = instruments.resolve_name(iid, self.mapping)
            if not name or name not in have_master:
                continue
            if monitored_cfg and not monitored_cfg.get(name, True):   # explicit opt-out
                continue
            if _record_age_sec(rec, snapshot, now) > max_age:         # per-instrument staleness
                continue
            zlist, tick = self.zone_cache.get(name)
            for hit in hits.detect_hits(rec.get("bidPrice"), rec.get("askPrice"), zlist):
                key = alerts.zone_key(hit.instrument, hit.side, hit.zone_low, hit.zone_high, tick=tick)
                if not alerts.should_fire(self.state, day, key):
                    continue
                alert = alerts.build_alert(hit, iid, now)
                if not teams.post_teams(webhook, alerts.teams_card(alert, _dec(hit.edge))):
                    log.warning("Teams post failed for %s %s @ %s; will retry next tick", name, hit.side, hit.edge)
                    continue
                candidate = alerts.record_fire(self.state, day, key, alert)
                alerts.save_state(candidate)              # persist BEFORE advancing in-memory state
                self.state = candidate                    # (so a save failure -> retry, never a silent re-fire)
                alerts.append_alert_log(alert)
                log.info("ALERT %s %s edge=%.4f hit=%.4f conf=%s", name, hit.side,
                         hit.edge, hit.hit_price, hit.confidence)
        return poll
