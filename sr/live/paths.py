"""Single source of truth for every live-feed path. All derive from
``storage.get_base_dir()`` so ``storage.set_base_dir(tmp)`` (the test fixture) redirects
them too. The bundled instruments map is resolved frozen-safe (``sys._MEIPASS``)."""
from __future__ import annotations

import sys
from pathlib import Path

from .. import storage


def base() -> Path:
    return storage.get_base_dir()


def live_dir() -> Path:
    return base() / "live"


def snapshot_path(level: str = "l1") -> Path:
    return live_dir() / f"{level}_latest.json"


def feed_status_path(level: str = "l1") -> Path:
    return live_dir() / ("feed_status.json" if level == "l1" else "feed_status2.json")


def ticks_dir() -> Path:
    return base() / "ticks"


def tick_log_path(safe_instrument: str, day: str) -> Path:
    return ticks_dir() / f"{safe_instrument}_{day}.jsonl"


def bars_cursor_path() -> Path:
    return base() / "bars" / "cursor.json"


def alerts_dir() -> Path:
    return base() / "alerts"


def alert_state_path() -> Path:
    return alerts_dir() / "alert_state.json"


def alerts_log_path() -> Path:
    return alerts_dir() / "alerts_log.jsonl"


def auth_dir() -> Path:
    return base() / "auth"


def msal_cache_path() -> Path:
    return auth_dir() / "msal_cache.bin"


def device_code_path() -> Path:
    return auth_dir() / "device_code.json"


def config_path() -> Path:
    return base() / "config.json"


def instruments_json_path() -> Path:
    """The bundled product/instrument map (same idiom as desktop._template_path)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / "sr" / "live" / "instrunments.json"
    return Path(__file__).parent / "instrunments.json"


def ensure_live_dirs() -> None:
    for d in (live_dir(), ticks_dir(), bars_cursor_path().parent, alerts_dir(), auth_dir()):
        d.mkdir(parents=True, exist_ok=True)
