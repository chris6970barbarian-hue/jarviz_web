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

import logging
import time

from fastapi import APIRouter, Header, HTTPException, Request, status
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


@router.get("/")
async def root(request: Request):
    """Browsers (Accept: text/html) get the operator dashboard; bots /
    health probes (Accept: */* or json) get the simple JSON status the
    OTA layer used to return at this path."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return HTMLResponse(content=DASHBOARD_HTML)
    return {"service": "jarviz-backend", "status": "ok"}


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@router.get("/devices")
async def devices() -> dict:
    """Read-only view of registered devices. Handy during prototyping."""
    return list_devices()


@router.get("/metrics")
async def metrics() -> dict:
    """In-memory operator metrics — counters since process start and a
    p50/p95 of the most recent turn latencies. Per-worker; if you run
    multiple uvicorn workers you'll need to aggregate externally."""
    return metrics_snapshot()


@router.get("/logs/recent")
async def logs_recent(n: int = 200) -> dict:
    """Last `n` log records from the in-memory ring (formatted line +
    structured fields). Used by the operator dashboard to render a live
    log tail without re-reading the on-disk log file (which may not
    exist when running on stdout)."""
    n = max(1, min(int(n), 500))
    return {"records": recent_log_records(n)}


@router.get("/sessions/live")
async def sessions_live() -> dict:
    """Current open WebSocket sessions with their per-device state
    (idle / listening / processing / speaking) and connection metadata.
    Drives the dashboard's "what is Jarviz doing right now" hero panel."""
    return {"sessions": live_sessions()}


@router.get("/transcripts/recent")
async def transcripts_recent(n: int = 50) -> dict:
    """Last N completed conversation turns with both sides + tool calls."""
    n = max(1, min(int(n), 100))
    return {"transcripts": recent_transcripts(n)}


@router.get("/reminders/stats")
async def reminders_stats() -> dict:
    """Cumulative counters + per-hour series for the dashboard chart.
    `hourly` always spans the last 48 hours with zero-fill so the chart
    has stable, gap-free bars."""
    return reminder_snapshot()


@router.get("/network")
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
async def dashboard() -> HTMLResponse:
    """Single-page operator dashboard. Polls /metrics and /logs/recent
    every 2s and renders counters + recent latency + a live log tail.
    No build pipeline; the HTML/CSS/JS is one Python string in
    server/dashboard.py."""
    return HTMLResponse(content=DASHBOARD_HTML)


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
