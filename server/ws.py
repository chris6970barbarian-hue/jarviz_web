"""WebSocket endpoint. Wires the request to a Session and runs it."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, status

from .auth import auth_enabled, verify_token
from .cooldown import ws_cooldown
from .metrics import inc as metrics_inc
from .session import Session

log = logging.getLogger("jarviz.ws")


def _extract_bearer(ws: WebSocket) -> str:
    auth = ws.headers.get("authorization", "")
    if not auth:
        return ""
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return auth.strip()  # be lenient: some firmware sends the raw token


def make_router() -> APIRouter:
    r = APIRouter()

    @r.websocket("/xiaozhi/v1/")
    async def xiaozhi_v1(ws: WebSocket) -> None:
        device_id = ws.headers.get("device-id", "")
        client_id = ws.headers.get("client-id", "")
        user_agent = ws.headers.get("user-agent", "")
        protocol_version = ws.headers.get("protocol-version", "1")

        if auth_enabled():
            token = _extract_bearer(ws)
            if not verify_token(client_id, device_id, token):
                metrics_inc("ws_connects_auth_rejected")
                log.warning(
                    "WS reject: bad/missing token for device_id=%s", device_id
                )
                await ws.close(code=status.WS_1008_POLICY_VIOLATION)
                return

        # Per-device cooldown gate: a device in a reboot loop will be
        # bounced here rather than getting a session slot only to drop
        # the connection seconds later.
        if device_id and not ws_cooldown.check_and_mark(device_id):
            metrics_inc("ws_connects_cooldown_rejected")
            log.warning("WS cooldown rejected device_id=%s", device_id)
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # Pull providers + semaphores that lifespan built. Doing this here
        # (instead of via closure at startup) means a hot reload picks up
        # new providers without rebuilding the router.
        app_state = ws.app.state
        llm = app_state.llm
        asr = app_state.asr
        tts = app_state.tts
        sem = app_state.session_semaphore
        turn_sem = app_state.turn_semaphore

        # Reject (rather than queue) when at capacity. Use a near-zero
        # wait_for on acquire so the check-and-grab is atomic — a prior
        # `if sem.locked()` check had a TOCTOU race where two connections
        # could both see the semaphore as free and over-subscribe by one.
        try:
            await asyncio.wait_for(sem.acquire(), timeout=0.001)
        except asyncio.TimeoutError:
            metrics_inc("ws_connects_cap_rejected")
            log.warning("WS reject: session cap reached, device_id=%s", device_id)
            await ws.close(code=status.WS_1013_TRY_AGAIN_LATER)
            return

        try:
            metrics_inc("ws_connects_accepted")
            log.info(
                "WS connect device_id=%s client_id=%s proto_v=%s",
                device_id,
                client_id,
                protocol_version,
            )
            await ws.accept()
            session = Session(
                ws=ws,
                device_id=device_id,
                client_id=client_id,
                user_agent=user_agent,
                llm=llm,
                asr=asr,
                tts=tts,
                turn_semaphore=turn_sem,
            )
            await session.run()
        finally:
            sem.release()

    return r
