"""Azure AD (MSAL) device-flow auth with a persisted token cache, so day-to-day startup is
silent: ``acquire_token_silent`` refreshes from the cached refresh token; the device code is
shown only on first run or when the refresh token is gone. The access token is the LS password.

``msal`` is imported lazily so the rest of the app never depends on it."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from .. import storage
from . import paths

log = logging.getLogger("sr")

# from new_thing/LS_authenticate.py — the registered public client + tenant
CLIENT_ID = "2ca7995a-d52e-4a8a-af2d-34ddb23c6594"
TENANT_ID = "0753c1a4-2be6-4a86-8763-32ae847e1186"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["User.Read"]


def _load_cache():
    import msal
    cache = msal.SerializableTokenCache()
    p = paths.msal_cache_path()
    try:
        if p.exists():
            cache.deserialize(p.read_text(encoding="utf-8"))
    except OSError:
        log.exception("could not read MSAL cache")
    return cache


def _save_cache(cache) -> None:
    if not cache.has_state_changed:
        return
    try:
        paths.auth_dir().mkdir(parents=True, exist_ok=True)
        p = paths.msal_cache_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(cache.serialize(), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        log.exception("could not persist MSAL cache")


def _write_device_code(flow: dict) -> None:
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(flow.get("expires_in", 600)))
    storage.write_json_atomic(paths.device_code_path(), {
        "user_code": flow.get("user_code"),
        "verification_uri": flow.get("verification_uri"),
        "message": flow.get("message"),
        "expires_at": expires.isoformat(),
    })


def clear_device_code() -> None:
    try:
        paths.device_code_path().unlink()
    except OSError:
        pass


def get_token(on_device_code=None, silent_only=False):
    """Return (access_token, username) or (None, None). Silent-first; device-flow only when the
    cache has no usable refresh token. ``silent_only=True`` (the periodic refresh from the feed
    worker) NEVER starts an interactive device flow — it returns (None, None) on failure so the
    worker can't block. Surfaces the device code to ``auth/device_code.json`` + log."""
    try:
        import msal
    except Exception as exc:  # noqa: BLE001
        log.error("msal not available: %s", exc)
        return None, None

    cache = _load_cache()
    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result or "access_token" not in result:
        if silent_only:
            _save_cache(cache)
            return None, None
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            log.error("device flow init failed: %s", flow)
            _save_cache(cache)
            return None, None
        _write_device_code(flow)
        log.info("AUTH required — go to %s and enter code %s",
                 flow.get("verification_uri"), flow.get("user_code"))
        if on_device_code:
            try:
                on_device_code(flow)
            except Exception:  # noqa: BLE001
                pass
        result = app.acquire_token_by_device_flow(flow)   # blocks until completed or expired

    _save_cache(cache)
    if result and "access_token" in result:
        clear_device_code()
        user = (result.get("id_token_claims") or {}).get("preferred_username")
        return result["access_token"], user
    log.error("auth failed: %s", (result or {}).get("error_description") or (result or {}).get("error"))
    return None, None
