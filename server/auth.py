"""HMAC-signed per-device tokens.

The OTA endpoint mints a token for the device on each boot; the device
forwards it as `Authorization: Bearer <token>` on the WebSocket open;
the WS endpoint verifies it.

The token format mirrors xinnan-tech/xiaozhi-esp32-server so a Jarviz
firmware built against either backend interoperates:

    <base64url(hmac_sha256(secret, "client_id|device_id|ts"))>.<ts>

where `ts` is the unix-epoch second the token was minted and verification
accepts the token while `now - ts <= JARVIZ_AUTH_TOKEN_TTL_S`.

When `JARVIZ_AUTH_SECRET` is empty the server runs unauthenticated and
both `mint_token` and `verify_token` short-circuit — this preserves the
LAN-only prototype behavior the rest of the system was built against.

`JARVIZ_AUTH_ALLOWED_DEVICES` (comma-separated) acts as a bypass list:
devices on it get an empty token from OTA and skip the WS check.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

from .config import settings

log = logging.getLogger("jarviz.auth")


def _sign(client_id: str, device_id: str, issue_ts: int, secret: str) -> str:
    content = f"{client_id}|{device_id}|{issue_ts}".encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), content, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def auth_enabled() -> bool:
    return bool(settings.JARVIZ_AUTH_SECRET)


def _allowlist() -> set[str]:
    raw = settings.JARVIZ_AUTH_ALLOWED_DEVICES or ""
    return {d.strip() for d in raw.split(",") if d.strip()}


def device_allowed(device_id: str) -> bool:
    """True if `device_id` is on the bypass allowlist."""
    if not device_id:
        return False
    return device_id in _allowlist()


def mint_token(client_id: str, device_id: str) -> str:
    """Return a token for this device, or "" when auth is disabled or the
    device is on the bypass allowlist."""
    if not auth_enabled():
        return ""
    if not device_id:
        return ""
    if device_allowed(device_id):
        return ""
    issue_ts = int(time.time())
    sig = _sign(client_id or "", device_id, issue_ts, settings.JARVIZ_AUTH_SECRET)
    return f"{sig}.{issue_ts}"


def verify_token(client_id: str, device_id: str, token: str) -> bool:
    """Return True iff `token` was minted for this (client_id, device_id)
    pair and hasn't expired.

    When auth is disabled this always returns True so existing devices keep
    working. Devices on the bypass allowlist also pass without a token.
    """
    if not auth_enabled():
        return True
    if device_allowed(device_id):
        return True
    if not device_id or not token:
        return False
    try:
        sig, ts_s = token.split(".", 1)
        issue_ts = int(ts_s)
    except (ValueError, AttributeError):
        return False
    age = int(time.time()) - issue_ts
    if age < 0 or age > int(settings.JARVIZ_AUTH_TOKEN_TTL_S):
        return False
    expected = _sign(client_id or "", device_id, issue_ts, settings.JARVIZ_AUTH_SECRET)
    return hmac.compare_digest(expected, sig)
