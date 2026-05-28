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

    try:
        yield
    finally:
        log.info("Jarviz backend shutting down")


app = FastAPI(title="Jarviz Backend", version="0.1.0", lifespan=lifespan)
app.include_router(ota_router)
app.include_router(make_ws_router())
