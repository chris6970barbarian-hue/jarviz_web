"""FastAPI entry point.

Run locally:
    uvicorn server.main:app --host 0.0.0.0 --port 8080

Or via the Docker compose file at the repo root.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .log import setup_logging
from .ota import router as ota_router
from .ws import make_router as make_ws_router

setup_logging()
log = logging.getLogger("jarviz.main")


def _build_providers():
    from .asr.whisper_provider import FasterWhisperASR
    from .tts.edge_provider import EdgeTTS

    asr_name = settings.JARVIZ_ASR_PROVIDER.lower()
    if asr_name == "faster_whisper":
        asr = FasterWhisperASR()
    else:
        raise RuntimeError(f"Unknown ASR provider: {settings.JARVIZ_ASR_PROVIDER}")

    llm_name = settings.JARVIZ_LLM_PROVIDER.lower()
    if llm_name == "deepseek":
        from .llm.openai_provider import OpenAICompatLLM
        llm = OpenAICompatLLM(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            model=settings.JARVIZ_LLM_MODEL,
        )
    elif llm_name == "openai":
        # Generic OpenAI-compatible. Prefer the dedicated OPENAI_* settings;
        # fall back to the DeepSeek pair so prototypes that only set one
        # set of keys keep working. Leave base_url unset (None) to default
        # to https://api.openai.com.
        from .llm.openai_provider import OpenAICompatLLM
        api_key = settings.OPENAI_API_KEY or settings.DEEPSEEK_API_KEY or ""
        base_url = settings.OPENAI_BASE_URL or None
        llm = OpenAICompatLLM(
            api_key=api_key,
            base_url=base_url,
            model=settings.JARVIZ_LLM_MODEL,
        )
    elif llm_name == "anthropic":
        from .llm.anthropic_provider import AnthropicLLM
        llm = AnthropicLLM()
    else:
        raise RuntimeError(f"Unknown LLM provider: {settings.JARVIZ_LLM_PROVIDER}")

    return llm, asr, EdgeTTS()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Jarviz backend starting on %s:%s", settings.JARVIZ_HTTP_HOST, settings.JARVIZ_HTTP_PORT)
    log.info("WS public URL announced to devices: %s", settings.JARVIZ_WS_PUBLIC_URL)
    log.info("TZ offset minutes: %s", settings.JARVIZ_TZ_OFFSET_MINUTES)

    # Build providers in startup so a missing key or ffmpeg shows up as a
    # clean lifespan error rather than a stack trace at import time.
    llm, asr, tts = _build_providers()
    app.state.llm = llm
    app.state.asr = asr
    app.state.tts = tts
    app.state.session_semaphore = asyncio.Semaphore(settings.JARVIZ_MAX_SESSIONS)
    # Separate from the session semaphore: this caps how many turns can
    # be in the ASR+LLM+TTS pipeline at any one moment. See config docs.
    app.state.turn_semaphore = asyncio.Semaphore(settings.JARVIZ_MAX_CONCURRENT_TURNS)
    log.info("Session concurrency cap: %d  |  Turn concurrency cap: %d",
             settings.JARVIZ_MAX_SESSIONS, settings.JARVIZ_MAX_CONCURRENT_TURNS)
    log.info("Providers initialized: llm=%s asr=%s tts=%s",
             type(llm).__name__, type(asr).__name__, type(tts).__name__)

    # Security-posture guard. Two independent open-by-default surfaces:
    #   (1) device auth — fails *open* on a routable interface when
    #       JARVIZ_AUTH_SECRET is empty;
    #   (2) the dashboard/telemetry read surface — open unless
    #       JARVIZ_DASHBOARD_TOKEN is set (it leaks transcripts, device IDs,
    #       peer IPs, logs).
    # Either is fine on a trusted LAN but a data-leak / paid-LLM-quota faucet if
    # the port is reachable from an untrusted network. Warn loudly on each, since
    # both are invisible on a developer's laptop otherwise.
    from .auth import auth_enabled  # local import: avoids a cycle at module load
    host = settings.JARVIZ_HTTP_HOST
    loopback = host in ("127.0.0.1", "localhost", "::1")
    # Fail closed when the operator has opted in: refuse to boot rather than
    # silently come up with device auth disabled in a deployment that demands it.
    if settings.JARVIZ_REQUIRE_AUTH and not auth_enabled():
        raise RuntimeError(
            "JARVIZ_REQUIRE_AUTH is set but JARVIZ_AUTH_SECRET is empty — "
            "refusing to start with device auth disabled. Set a secret or "
            "clear JARVIZ_REQUIRE_AUTH for an intentionally open LAN deploy."
        )
    if not auth_enabled():
        if loopback:
            log.warning(
                "Device auth is DISABLED (JARVIZ_AUTH_SECRET empty). OK for "
                "loopback-only dev; set a secret before exposing this server."
            )
        else:
            log.warning(
                "SECURITY: device auth is DISABLED and the server is bound to a "
                "non-loopback interface (%s). Any host that can reach port %s can "
                "open sessions (burning LLM quota) and read the unauthenticated "
                "/dashboard, /transcripts/recent and /logs/recent surface. Set "
                "JARVIZ_AUTH_SECRET, or bind JARVIZ_HTTP_HOST=127.0.0.1, before "
                "deploying on an untrusted network.",
                host, settings.JARVIZ_HTTP_PORT,
            )
    # Independent of device auth: the operator console + telemetry read surface
    # is open unless JARVIZ_DASHBOARD_TOKEN is set. Warn when it's open on a
    # routable bind (this fires even when device auth IS configured).
    if not settings.JARVIZ_DASHBOARD_TOKEN and not loopback:
        log.warning(
            "SECURITY: operator dashboard + telemetry (/dashboard, "
            "/transcripts/recent, /logs/recent, /sessions/live, /network) are "
            "UNAUTHENTICATED and bound to a non-loopback interface (%s). Anyone "
            "who can reach port %s can read conversation transcripts, device "
            "IDs, peer IPs and logs. Set JARVIZ_DASHBOARD_TOKEN to require an "
            "operator token.",
            host, settings.JARVIZ_HTTP_PORT,
        )

    try:
        yield
    finally:
        log.info("Jarviz backend shutting down")


app = FastAPI(title="Jarviz Backend", version="0.1.0", lifespan=lifespan)
app.include_router(ota_router)
app.include_router(make_ws_router())
