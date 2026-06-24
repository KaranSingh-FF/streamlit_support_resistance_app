"""Post a Microsoft Teams webhook card via the stdlib (no extra dependency). Never raises —
returns True only on a 2xx so the caller records the alert as fired ONLY on confirmed delivery."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def post_teams(url, card, timeout: int = 8, retries: int = 2, _opener=None) -> bool:
    """POST ``card`` (a dict) as JSON to the Teams webhook ``url``. Retries with no delay.
    ``_opener`` injects a stub in tests. Returns True on HTTP 2xx, else False (never raises)."""
    if not url:
        return False
    try:
        body = json.dumps(card, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return False
    opener = _opener or urllib.request.urlopen
    for _ in range(max(1, retries + 1)):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with opener(req, timeout=timeout) as resp:
                code = int(getattr(resp, "status", None) or resp.getcode())
                if 200 <= code < 300:
                    return True
        except Exception:
            pass
    return False
