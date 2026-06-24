"""Alert dedupe (one per zone per UTC trading day, persisted), staleness gating, and the alert
record + Teams card builders. Dedupe state is written atomically so a mid-day restart never
re-fires; the day rolls -> levels re-arm for the new session."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from .. import storage
from . import paths


def session_day(now_utc: datetime) -> str:
    return now_utc.astimezone(timezone.utc).strftime("%Y-%m-%d")


def zone_key(instrument, side, low, high, tick: float = 0.01) -> str:
    """Stable key for one zone, tick-snapped so per-recompute jitter maps to the same key."""
    t = tick if tick and tick > 0 else 0.01
    return f"{instrument}|{side}|{round(low / t) * t:.6f}|{round(high / t) * t:.6f}"


def load_state(path: "Path | None" = None) -> dict:
    p = path or paths.alert_state_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            return {"day": d.get("day"), "fired": dict(d.get("fired") or {})}
    except (OSError, json.JSONDecodeError):
        pass
    return {"day": None, "fired": {}}


def should_fire(state: dict, day: str, key: str) -> bool:
    if state.get("day") != day:           # new UTC day -> everything re-arms
        return True
    return key not in (state.get("fired") or {})


def record_fire(state: dict, day: str, key: str, alert: dict) -> dict:
    """Return the NEW state after recording a fire (re-armed if the day rolled)."""
    fired = {} if state.get("day") != day else dict(state.get("fired") or {})
    fired[key] = {"ts": alert.get("ts"), "bid": alert.get("bid"),
                  "ask": alert.get("ask"), "hit_price": alert.get("hit_price")}
    return {"day": day, "fired": fired}


def save_state(state: dict, path: "Path | None" = None) -> None:
    storage.write_json_atomic(path or paths.alert_state_path(), state)


def is_stale(feed_status, snapshot, now_utc: datetime, max_age_sec: int = 30):
    """(stale, reason). NEVER fire when stale: feed must be CONNECTED and the snapshot fresh
    (written_at within max_age_sec and parseable)."""
    if not feed_status or str(feed_status.get("status", "")).upper() != "CONNECTED":
        return True, "feed not connected"
    if not snapshot:
        return True, "no snapshot"
    written = snapshot.get("written_at")
    try:
        wt = datetime.fromisoformat(str(written).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True, "unparseable snapshot time"
    if wt.tzinfo is None:
        wt = wt.replace(tzinfo=timezone.utc)
    age = (now_utc.astimezone(timezone.utc) - wt.astimezone(timezone.utc)).total_seconds()
    if age > max_age_sec:
        return True, f"stale {int(age)}s"
    return False, ""


def _f(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def build_alert(hit, instrument_id, now_utc: datetime) -> dict:
    """The single alert record (schema=1). All numbers finite-or-None -> strict-JSON safe."""
    return {
        "schema": 1,
        "ts": now_utc.astimezone(timezone.utc).isoformat(),
        "instrument": hit.instrument,
        "instrument_id": str(instrument_id),
        "side": hit.side,
        "edge": _f(hit.edge),
        "hit_price": _f(hit.hit_price),
        "bid": _f(hit.bid),
        "ask": _f(hit.ask),
        "zone_low": _f(hit.zone_low),
        "zone_high": _f(hit.zone_high),
        "zone_center": _f(hit.center),
        "confidence": hit.confidence,
        "bucket": hit.bucket,
        "touches": int(hit.touches),
        "score": _f(hit.score),
    }


def teams_card(alert: dict, decimals: int = 2) -> dict:
    """Legacy MessageCard (works with classic Incoming Webhooks AND the Workflows connector)."""
    side = alert.get("side")
    color = "2EB67D" if side == "support" else "E01E5A"
    fmt = lambda v: "—" if v is None else f"{v:.{decimals}f}"
    trig = "ask" if side == "support" else "bid"
    title = f"{alert.get('instrument')} — {str(side).upper()} hit @ {fmt(alert.get('edge'))}"
    facts = [
        {"name": "Instrument", "value": str(alert.get("instrument"))},
        {"name": "Side", "value": str(side)},
        {"name": "Level (edge)", "value": fmt(alert.get("edge"))},
        {"name": "Hit price", "value": f"{fmt(alert.get('hit_price'))} ({trig})"},
        {"name": "Bid / Ask", "value": f"{fmt(alert.get('bid'))} / {fmt(alert.get('ask'))}"},
        {"name": "Zone", "value": f"{fmt(alert.get('zone_low'))} – {fmt(alert.get('zone_high'))}"},
        {"name": "Confidence", "value": f"{alert.get('confidence')} ({alert.get('bucket')})"},
        {"name": "Touches / Score", "value": f"{alert.get('touches')} / {fmt(alert.get('score'))}"},
        {"name": "Time (UTC)", "value": str(alert.get("ts"))},
    ]
    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": color,
        "summary": title,
        "sections": [{"activityTitle": title, "facts": facts, "markdown": False}],
    }


def append_alert_log(alert: dict, path: "Path | None" = None) -> None:
    p = path or paths.alerts_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(alert, allow_nan=False) + "\n").encode("utf-8")
    with open(p, "ab") as fh:
        fh.write(line)


def read_recent_alerts(n: int = 50, path: "Path | None" = None) -> list:
    p = path or paths.alerts_log_path()
    if not Path(p).exists():
        return []
    with open(p, "rb") as fh:
        lines = fh.read().splitlines()
    out = []
    for raw in lines[-max(1, n):]:
        try:
            out.append(json.loads(raw.decode("utf-8", "replace")))
        except json.JSONDecodeError:
            pass
    return list(reversed(out))
