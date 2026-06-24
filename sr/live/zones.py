"""Per-instrument S/R zone cache for the alert monitor. Zones are computed with the SAME
auto-config path the desktop UI uses (so alerts match what you see), throttled and invalidated
on master change."""
from __future__ import annotations

import time

from .. import engine, storage

_ZCOLS = ["instrument", "side", "zone_low", "zone_high", "zone_center",
          "confidence", "bucket", "touches", "score"]


def cached_zones_from_frame(final_zones) -> list:
    """final_zones DataFrame -> list of non-Low zone dicts (the monitor input). Pure."""
    if final_zones is None or final_zones.empty:
        return []
    df = final_zones
    if "confidence" in df.columns:
        df = df[df["confidence"] != "Low"]
    out = []
    for _, z in df.iterrows():
        rec = {}
        for c in _ZCOLS:
            v = z[c] if c in df.columns else None
            if c in ("zone_low", "zone_high", "zone_center", "score"):
                rec[c] = float(v) if v is not None else None
            elif c == "touches":
                rec[c] = int(v) if v is not None else 0
            else:
                rec[c] = str(v) if v is not None else None
        out.append(rec)
    return out


def compute_zones_for(name: str):
    """Auto-config S/R for one instrument's master -> (non-Low zone dicts, inferred tick). The tick
    is the SAME engine.infer_tick_size the zone edges were snapped to, so the alert dedupe key
    snaps identically (no duplicate/missed alerts on fine-tick spreads). ([], 0.01) if no data."""
    master = storage.load_master(name)
    if master is None or master.empty:
        return [], 0.01
    try:
        cfg = engine.SRConfig(auto=True)
        cfg, _ = engine.infer_config(master, cfg)
        final_zones = engine.compute_sr(master, cfg)[0]
        tick = float(cfg.tick_size) if cfg.tick_size and cfg.tick_size > 0 else 0.01
    except Exception:
        return [], 0.01
    return cached_zones_from_frame(final_zones), tick


class ZoneCache:
    """Recompute per instrument at most every ``recompute_sec`` AND whenever its master file
    changes (new bars merged by the feed). ``get`` returns (zones, tick). Confined to the monitor."""

    def __init__(self, recompute_sec: int = 300):
        self.recompute_sec = recompute_sec
        self._cache: dict[str, dict] = {}

    def get(self, name: str):
        mp = storage.master_path_for_instrument(name)
        mtime = mp.stat().st_mtime if mp.exists() else 0.0
        ent = self._cache.get(name)
        now = time.monotonic()
        if ent and (now - ent["at"]) < self.recompute_sec and ent["mtime"] == mtime:
            return ent["zones"], ent["tick"]
        zones, tick = compute_zones_for(name)
        self._cache[name] = {"zones": zones, "tick": tick, "at": now, "mtime": mtime}
        return zones, tick
