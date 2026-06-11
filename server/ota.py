"""HTTP endpoints the firmware hits before opening the WebSocket.

The firmware POSTs to `/xiaozhi/ota/` once per boot. Our response tells the
device:
  - the current wall-clock time + tz offset (so localtime() is right and
    reminders fire at the right hour),
  - the WebSocket URL it should connect to,
  - (optionally) a firmware update URL.

We deliberately omit the `activation` block — the firmware skips activation
entirely when it's absent, which is what we want for a prototype where every
device is implicitly trusted on the LAN. Real deployments can add a challenge
flow here later.
"""

from __future__ import annotations

import hmac
import logging
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from .auth import mint_token
from .config import settings
from .cooldown import ota_cooldown
from .dashboard import DASHBOARD_HTML
from .live_state import (
    live_sessions,
    recent_transcripts,
    reminder_snapshot,
)
from .log import recent_log_records
from .metrics import inc as metrics_inc, snapshot as metrics_snapshot
from .store import list_devices, touch_device

log = logging.getLogger("jarviz.ota")
router = APIRouter()

# --- Operator-console / telemetry auth ------------------------------------
# When JARVIZ_DASHBOARD_TOKEN is set, the dashboard + telemetry read surface
# (which exposes transcripts, device IDs, peer IPs, and logs) requires the
# token. The token is accepted as `?token=`, an `X-Dashboard-Token` header,
# or the `jarviz_dash` cookie that the dashboard page sets on a good token —
# so the browser's same-origin polling fetches authenticate via the cookie
# with no client-side code. /healthz and the device OTA/WS routes are NEVER
# gated by this (probes + devices must reach them freely).
_DASH_COOKIE = "jarviz_dash"


def _dashboard_token_ok(request: Request) -> bool:
    configured = settings.JARVIZ_DASHBOARD_TOKEN
    if not configured:
        return True  # open (LAN-prototype default)
    supplied = (
        request.query_params.get("token")
        or request.headers.get("x-dashboard-token")
        or request.cookies.get(_DASH_COOKIE)
        or ""
    )
    return bool(supplied) and hmac.compare_digest(supplied, configured)


async def dashboard_guard(request: Request) -> None:
    """FastAPI dependency for the JSON telemetry endpoints."""
    if not _dashboard_token_ok(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="operator token required (set ?token=, X-Dashboard-Token, or visit /dashboard?token=...)",
            headers={"WWW-Authenticate": "Bearer"},
        )


_LOGIN_HINT = (
    "<!doctype html><meta charset=utf-8><title>Jarviz — auth required</title>"
    "<body style='font-family:system-ui;background:#16130f;color:#e8dcc8;"
    "padding:3rem;max-width:40rem;margin:auto'>"
    "<h1>Operator token required</h1><p>This console is protected. Open it as "
    "<code>/dashboard?token=YOUR_TOKEN</code> (the value of "
    "<code>JARVIZ_DASHBOARD_TOKEN</code>).</p></body>"
)


def _serve_dashboard(request: Request) -> HTMLResponse:
    """Serve the console; on a valid token, drop a cookie so the page's
    same-origin polling fetches authenticate without extra client code."""
    if not _dashboard_token_ok(request):
        return HTMLResponse(content=_LOGIN_HINT, status_code=status.HTTP_401_UNAUTHORIZED)
    resp = HTMLResponse(content=DASHBOARD_HTML)
    if settings.JARVIZ_DASHBOARD_TOKEN:
        # Set Secure on a TLS deployment so the token cookie is never sent over
        # plaintext, while the LAN-over-HTTP prototype (the default) still works.
        # Detect TLS from the request scheme OR a wss:// public URL (covers a
        # TLS-terminating proxy that forwards plain HTTP to us).
        secure = (
            request.url.scheme == "https"
            or settings.JARVIZ_WS_PUBLIC_URL.lower().startswith("wss")
        )
        resp.set_cookie(
            _DASH_COOKIE,
            settings.JARVIZ_DASHBOARD_TOKEN,
            max_age=86400,
            httponly=True,
            samesite="strict",
            secure=secure,
            path="/",
        )
    return resp


@router.get("/")
async def root(request: Request):
    """Browsers (Accept: text/html) get the operator dashboard; bots /
    health probes (Accept: */* or json) get the simple JSON status the
    OTA layer used to return at this path."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return _serve_dashboard(request)
    return {"service": "jarviz-backend", "status": "ok"}


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@router.get("/devices", dependencies=[Depends(dashboard_guard)])
async def devices() -> dict:
    """Read-only view of registered devices. Handy during prototyping."""
    return list_devices()


@router.get("/metrics", dependencies=[Depends(dashboard_guard)])
async def metrics() -> dict:
    """In-memory operator metrics — counters since process start and a
    p50/p95 of the most recent turn latencies. Per-worker; if you run
    multiple uvicorn workers you'll need to aggregate externally."""
    return metrics_snapshot()


@router.get("/logs/recent", dependencies=[Depends(dashboard_guard)])
async def logs_recent(n: int = 200) -> dict:
    """Last `n` log records from the in-memory ring (formatted line +
    structured fields). Used by the operator dashboard to render a live
    log tail without re-reading the on-disk log file (which may not
    exist when running on stdout)."""
    n = max(1, min(int(n), 500))
    return {"records": recent_log_records(n)}


@router.get("/sessions/live", dependencies=[Depends(dashboard_guard)])
async def sessions_live() -> dict:
    """Current open WebSocket sessions with their per-device state
    (idle / listening / processing / speaking) and connection metadata.
    Drives the dashboard's "what is Jarviz doing right now" hero panel."""
    return {"sessions": live_sessions()}


@router.get("/transcripts/recent", dependencies=[Depends(dashboard_guard)])
async def transcripts_recent(n: int = 50) -> dict:
    """Last N completed conversation turns with both sides + tool calls."""
    n = max(1, min(int(n), 100))
    return {"transcripts": recent_transcripts(n)}


@router.get("/reminders/stats", dependencies=[Depends(dashboard_guard)])
async def reminders_stats() -> dict:
    """Cumulative counters + per-hour series for the dashboard chart.
    `hourly` always spans the last 48 hours with zero-fill so the chart
    has stable, gap-free bars."""
    return reminder_snapshot()


@router.get("/network", dependencies=[Depends(dashboard_guard)])
async def network() -> dict:
    """Server's own LAN addresses, the configured WS public URL handed
    to devices on OTA, the listen port, and the peer IPs of currently-
    connected sessions. Lets the dashboard show 'where do I connect?'
    plus 'who's connected right now?' without any extra plumbing."""
    import socket
    addrs: list[str] = []
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    try:
        # All v4 addrs for this host (skip 127.0.0.1; that's never useful
        # for a device on a different machine to dial in to).
        for info in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            ip = info[4][0]
            if ip and ip != "127.0.0.1" and ip not in addrs:
                addrs.append(ip)
    except Exception:
        pass
    sess = live_sessions()
    return {
        "hostname": hostname,
        "listen_host": settings.JARVIZ_HTTP_HOST,
        "listen_port": settings.JARVIZ_HTTP_PORT,
        "lan_addresses": addrs,
        "ws_public_url": settings.JARVIZ_WS_PUBLIC_URL,
        "connected_peers": [
            {
                "device_id": s["device_id"],
                "peer_ip": s["peer_ip"],
                "state": s["state"],
                "age_s": s["session_age_s"],
            }
            for s in sess
        ],
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Single-page operator dashboard. Polls the telemetry endpoints every
    2s. When JARVIZ_DASHBOARD_TOKEN is set, requires `?token=...` (and then
    sets a cookie so the polling fetches authenticate). No build pipeline;
    the HTML/CSS/JS is one Python string in server/dashboard.py."""
    return _serve_dashboard(request)


def _server_time() -> dict:
    return {
        "timestamp": int(time.time() * 1000),
        "timezone_offset": settings.JARVIZ_TZ_OFFSET_MINUTES,
    }


@router.post("/xiaozhi/ota/")
async def ota_check(
    request: Request,
    device_id: str | None = Header(None, alias="Device-Id"),
    client_id: str | None = Header(None, alias="Client-Id"),
    user_agent: str | None = Header(None, alias="User-Agent"),
) -> dict:
    metrics_inc("ota_requests_total")
    # Per-device cooldown: bail early if this device just hit OTA. Cheaper
    # than running touch_device + the response build for a reboot-loop
    # storm. 429 maps cleanly to the firmware's retry behavior.
    if device_id and not ota_cooldown.check_and_mark(device_id):
        metrics_inc("ota_cooldown_rejected")
        log.warning("OTA cooldown rejected device_id=%s", device_id)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="device cooldown active",
            headers={"Retry-After": str(int(settings.JARVIZ_DEVICE_COOLDOWN_S) or 1)},
        )

    if device_id:
        touch_device(
            device_id,
            client_id=client_id or "",
            user_agent=user_agent or "",
        )
    log.info("OTA check from device_id=%s client_id=%s ua=%s", device_id, client_id, user_agent)

    # The firmware rejects an empty body for some methods, but `CheckVersion`
    # doesn't actually inspect what it sent us — it just parses the response.
    # `token` is forwarded by the device as Authorization: Bearer <token> when
    # it opens the WebSocket. Empty string = no auth (LAN dev default); a
    # non-empty `JARVIZ_AUTH_SECRET` switches us to signed per-device tokens.
    return {
        "server_time": _server_time(),
        "websocket": {
            "url": settings.JARVIZ_WS_PUBLIC_URL,
            "version": 1,
            "token": mint_token(client_id or "", device_id or ""),
        },
        "firmware": {
            # Reporting the same version the device says it has makes the device
            # treat itself as up-to-date. Once we want to push firmware, set
            # `version` to a higher value and `url` to a binary endpoint.
            "version": "0.0.0",
            "url": "",
        },
    }


@router.post("/xiaozhi/ota/activate")
async def ota_activate(request: Request) -> dict:
    # We never set an `activation` block in the OTA response, so the firmware
    # should not call this. If it ever does (e.g. a device flashed against a
    # different server first), accept the call so we don't 404 the device.
    return {"ok": True}
