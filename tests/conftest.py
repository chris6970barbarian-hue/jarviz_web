"""Shared fixtures + stub providers for the jarviz_web test suite.

These tests are designed to run fully OFFLINE — no network, no LLM API key,
no whisper model download. The real ASR/LLM/TTS providers are replaced with
deterministic stubs; the WebSocket is a recording fake.

libopus must be loadable at import time (server.audio binds it via ctypes).
On macOS with Homebrew opus, run pytest with:
    DYLD_LIBRARY_PATH=/opt/homebrew/lib pytest
The conftest also adds that path defensively for child code paths.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest

# Make `import server.*` work when pytest is run from the repo root.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Defensive: help ctypes find Homebrew opus if the caller forgot the env var.
# (Only affects child processes / late dlopens; the primary load still needs
# DYLD_LIBRARY_PATH set before the interpreter starts on macOS.)
os.environ.setdefault("DYLD_LIBRARY_PATH", "/opt/homebrew/lib")

from server.audio import TTS_FRAME_SAMPLES  # noqa: E402

TTS_BYTES_PER_FRAME = TTS_FRAME_SAMPLES * 2


# --------------------------------------------------------------------------
# Stub providers (match the Protocol interfaces the Session calls)
# --------------------------------------------------------------------------

class StubASR:
    """ASRProvider stub: returns a fixed transcript."""

    def __init__(self, text: str = "hello jarviz") -> None:
        self.text = text
        self.calls = 0

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.calls += 1
        return self.text


class StubLLM:
    """LLMProvider stub: echoes a canned assistant reply, no tool use."""

    def __init__(self, reply: str = "Sure, done.") -> None:
        self.reply = reply
        self.calls = 0

    async def respond(self, *, user_text, tools, invoke_tool, history):
        self.calls += 1
        new_history = list(history) + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": self.reply},
        ]
        return self.reply, new_history


class StubTTS:
    """TTSProvider stub: yields `n_frames` worth of non-silent 24 kHz PCM,
    split into a few chunks (like the real ffmpeg reader does)."""

    def __init__(self, n_frames: int = 100, chunks: int = 5) -> None:
        self.n_frames = n_frames
        self.chunks = max(1, chunks)
        self.calls = 0

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        self.calls += 1
        total = self.n_frames * TTS_BYTES_PER_FRAME
        # A simple non-zero ramp so frames aren't pure silence.
        buf = bytes((i % 251) for i in range(total))
        step = max(1, total // self.chunks)
        for off in range(0, total, step):
            yield buf[off:off + step]


class FakeWebSocket:
    """Records everything sent; lets tests drive received messages.

    Implements just enough of the starlette WebSocket surface that Session's
    low-level send helpers and (optionally) _main_loop need.
    """

    def __init__(self) -> None:
        from starlette.websockets import WebSocketState
        self.application_state = WebSocketState.CONNECTED
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed = False
        self._inbox: list[dict] = []

    # ---- send side (recorded) ----
    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        from starlette.websockets import WebSocketState
        self.application_state = WebSocketState.DISCONNECTED

    # ---- json helpers (the suite reads these) ----
    @property
    def sent_json(self) -> list[dict]:
        import json
        out = []
        for t in self.sent_text:
            try:
                out.append(json.loads(t))
            except Exception:
                pass
        return out


def make_session(ws=None, *, llm=None, asr=None, tts=None, turn_semaphore=None):
    """Construct a real Session wired to stubs/fakes."""
    from server.session import Session
    ws = ws or FakeWebSocket()
    return Session(
        ws=ws,
        device_id="aa:bb:cc:dd:ee:ff",
        client_id="client-123",
        user_agent="pytest",
        llm=llm or StubLLM(),
        asr=asr or StubASR(),
        tts=tts or StubTTS(),
        turn_semaphore=turn_semaphore,
    )


@pytest.fixture
def fake_ws():
    return FakeWebSocket()


@pytest.fixture
def session(fake_ws):
    return make_session(fake_ws)
