"""Local app config (sr_data_store/config.json): Teams webhook + monitored instruments +
monitor cadence. The webhook secret lives here (gitignored); env SR_TEAMS_WEBHOOK overrides."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .. import storage
from . import paths

DEFAULTS = {
    "teams_webhook": "",
    "feed_enabled": True,   # False = analysis-only (no live feed + no alerts); applies on next launch
    "monitored": {},        # name -> bool. Empty dict = monitor every instrument that has a master.
    "monitor": {"enabled": True, "poll_sec": 2, "zone_recompute_sec": 300, "max_age_sec": 30},
}


def feed_enabled(cfg: dict) -> bool:
    """Live feed + alerts run unless analysis-only is set or SR_NO_FEED=1 in the env."""
    if os.environ.get("SR_NO_FEED") == "1":
        return False
    return bool((cfg or {}).get("feed_enabled", True))


def read_config(path: "Path | None" = None) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    p = path or paths.config_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for k, v in data.items():
                if k in ("monitor", "monitored") and isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k] = {**cfg[k], **v}
                else:
                    cfg[k] = v
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def write_config(updates: dict, path: "Path | None" = None) -> dict:
    p = path or paths.config_path()
    cfg = read_config(p)
    for k, v in (updates or {}).items():
        if k in ("monitor", "monitored") and isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = {**cfg[k], **v}
        else:
            cfg[k] = v
    storage.write_json_atomic(p, cfg)
    return cfg


def effective_webhook(cfg: dict) -> "str | None":
    """Env override wins over the stored config; '' / missing -> None (alerts disabled)."""
    env = os.environ.get("SR_TEAMS_WEBHOOK")
    if env and env.strip():
        return env.strip()
    wh = (cfg or {}).get("teams_webhook")
    return wh.strip() if isinstance(wh, str) and wh.strip() else None


def webhook_source(cfg: dict) -> str:
    env = os.environ.get("SR_TEAMS_WEBHOOK")
    if env and env.strip():
        return "env"
    wh = (cfg or {}).get("teams_webhook")
    return "config" if isinstance(wh, str) and wh.strip() else "none"
