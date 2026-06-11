"""Edge-TTS provider resilience. Offline (uses local ffmpeg via imageio-ffmpeg;
no network — the Edge stream itself is stubbed).

Regression for the "silent TTS on Edge failure" finding: an upstream stream
error must be surfaced (logged), not swallowed into a fake-successful empty
synthesis.
"""

from __future__ import annotations

import logging

import pytest

from server.tts import edge_provider


def _ffmpeg_available() -> bool:
    try:
        edge_provider._resolve_ffmpeg()
        return True
    except Exception:
        return False


# These tests spawn a real ffmpeg to exercise the decode pipeline (the Edge
# stream itself is stubbed). Skip cleanly where ffmpeg isn't installed — it is
# always present in the production Docker image (apt install ffmpeg).
pytestmark = pytest.mark.skipif(
    not _ffmpeg_available(), reason="ffmpeg not installed in this environment"
)


@pytest.mark.asyncio
async def test_upstream_stream_failure_is_logged_not_swallowed(monkeypatch, caplog):
    class FailingComm:
        def __init__(self, *a, **k):
            pass

        async def stream(self):
            raise RuntimeError("403 Forbidden from Edge CDN")
            yield {}  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(edge_provider.edge_tts, "Communicate", FailingComm)

    tts = edge_provider.EdgeTTS()
    chunks = []
    with caplog.at_level(logging.WARNING, logger="jarviz.tts"):
        async for c in tts.synthesize("hello world"):
            chunks.append(c)

    # No audio produced, no exception leaked to the caller...
    assert chunks == []
    # ...but the failure is now observable to operators (was silently passed).
    assert any("Edge-TTS stream failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_empty_text_yields_nothing(monkeypatch):
    tts = edge_provider.EdgeTTS()
    out = [c async for c in tts.synthesize("   ")]
    assert out == []
