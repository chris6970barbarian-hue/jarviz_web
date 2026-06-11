"""Regression tests for the real-time TTS pacing fix (the mid-sentence cutoff).

These drive the REAL Session._stream_tts under a virtual clock: we patch
session.time.monotonic + session.asyncio.sleep so the pacing schedule is
exercised deterministically and instantly (no wall-clock waiting).

The bug: frames were flushed faster than real-time; the firmware's ~2.4 s
decode queue overflowed and dropped the tail -> speech cut off. The fix paces
each frame so we never run more than JARVIZ_TTS_JITTER_BUFFER_MS ahead of
real-time playout, keeping device buffer occupancy well under the 2.4 s cap.
"""

from __future__ import annotations

import asyncio

import pytest

from server import session as session_mod
from server.audio import FRAME_DURATION_MS, TTS_FRAME_SAMPLES

from conftest import FakeWebSocket, StubTTS, make_session

FRAME_S = FRAME_DURATION_MS / 1000.0      # 0.06
DEVICE_CAP_S = 2400 / FRAME_DURATION_MS * FRAME_S  # 2.4 s firmware decode-queue cap


class _ClockWS(FakeWebSocket):
    """Records the virtual time at which each binary frame is sent."""

    def __init__(self, clock):
        super().__init__()
        self._clock = clock
        self.send_times = []

    async def send_bytes(self, data: bytes) -> None:
        self.send_times.append(self._clock[0])
        await super().send_bytes(data)


@pytest.fixture
def virtual_clock(monkeypatch):
    clock = [1000.0]

    def mono():
        return clock[0]

    async def fake_sleep(delay, *a, **k):
        if delay and delay > 0:
            clock[0] += delay

    monkeypatch.setattr(session_mod.time, "monotonic", mono)
    monkeypatch.setattr(session_mod.asyncio, "sleep", fake_sleep)
    return clock


async def _run_stream(clock, n_frames=100, jitter_ms=800, pacing=True, monkeypatch=None):
    monkeypatch.setattr(session_mod.settings, "JARVIZ_TTS_PACING_ENABLED", pacing)
    monkeypatch.setattr(session_mod.settings, "JARVIZ_TTS_JITTER_BUFFER_MS", jitter_ms)
    ws = _ClockWS(clock)
    sess = make_session(ws, tts=StubTTS(n_frames=n_frames, chunks=5))
    t0 = clock[0]
    await sess._stream_tts("a long spoken reply")
    rel = [t - t0 for t in ws.send_times]
    return ws, rel


@pytest.mark.asyncio
async def test_control_message_order_and_frame_count(virtual_clock, monkeypatch):
    ws, rel = await _run_stream(virtual_clock, n_frames=50, monkeypatch=monkeypatch)
    # 50 full frames in, 50 opus packets out.
    assert len(ws.sent_bytes) == 50
    # Control messages: sentence_start, start, ... stop (in that order).
    states = [(m.get("type"), m.get("state")) for m in ws.sent_json]
    assert states[0] == ("tts", "sentence_start")
    assert states[1] == ("tts", "start")
    assert states[-1] == ("tts", "stop")


@pytest.mark.asyncio
async def test_pacing_keeps_device_buffer_under_cap(virtual_clock, monkeypatch):
    n = 100
    ws, rel = await _run_stream(virtual_clock, n_frames=n, jitter_ms=800, monkeypatch=monkeypatch)
    assert len(rel) == n

    # Initial burst: every frame whose playout point is within the lead window
    # ships immediately at t=0 (primes the jitter buffer).
    lead_s = 0.8
    expected_burst = sum(1 for i in range(n) if i * FRAME_S <= lead_s)
    burst = [i for i, t in enumerate(rel) if t == 0.0]
    assert burst == list(range(expected_burst))

    # After the burst, the schedule is absolute & drift-free: target = i*frame - lead.
    for i in range(expected_burst, n):
        assert rel[i] == pytest.approx(i * FRAME_S - lead_s, abs=1e-9)

    # The device is never asked to hold more than the jitter buffer (+1 frame)
    # ahead of real-time -> always far below the 2.4 s firmware drop cap.
    max_ahead = max((i + 1) * FRAME_S - rel[i] for i in range(n))
    assert max_ahead < DEVICE_CAP_S
    assert max_ahead == pytest.approx(lead_s + FRAME_S, abs=1e-9)

    # And the whole stream takes ~ (audio duration - lead) of wall-clock, i.e.
    # it is genuinely paced, not flooded.
    assert rel[-1] == pytest.approx((n - 1) * FRAME_S - lead_s, abs=1e-9)


@pytest.mark.asyncio
async def test_pacing_disabled_reproduces_the_flood(virtual_clock, monkeypatch):
    # The pre-fix behaviour: with pacing off, every frame is sent instantly.
    ws, rel = await _run_stream(virtual_clock, n_frames=100, pacing=False, monkeypatch=monkeypatch)
    assert len(rel) == 100
    assert all(t == 0.0 for t in rel)  # entire reply flung out at t=0 (the bug)


@pytest.mark.asyncio
async def test_zero_jitter_is_strict_realtime(virtual_clock, monkeypatch):
    n = 30
    ws, rel = await _run_stream(virtual_clock, n_frames=n, jitter_ms=0, monkeypatch=monkeypatch)
    # frame i plays at i*frame; with no lead the server runs exactly one frame ahead.
    max_ahead = max((i + 1) * FRAME_S - rel[i] for i in range(n))
    assert max_ahead == pytest.approx(FRAME_S, abs=1e-9)


@pytest.mark.asyncio
async def test_stop_is_sent_even_when_synthesis_errors(virtual_clock, monkeypatch):
    class BoomTTS:
        async def synthesize(self, text):
            raise RuntimeError("edge-tts/ffmpeg blew up")
            yield b""  # pragma: no cover (makes this an async generator)

    monkeypatch.setattr(session_mod.settings, "JARVIZ_TTS_PACING_ENABLED", True)
    ws = FakeWebSocket()
    sess = make_session(ws, tts=BoomTTS())
    # Must not raise — the error is caught and a clean tts stop is still sent.
    await sess._stream_tts("hello")
    assert ("tts", "stop") in [(m.get("type"), m.get("state")) for m in ws.sent_json]
    assert len(ws.sent_bytes) == 0


@pytest.mark.asyncio
async def test_stop_is_sent_on_cancellation_midstream(virtual_clock, monkeypatch):
    # Simulate an abort/barge-in: the WS send raises CancelledError partway.
    # The pacing sleep is a cancellation point in production; here we inject it
    # at the send to assert the finally still emits tts stop.
    monkeypatch.setattr(session_mod.settings, "JARVIZ_TTS_PACING_ENABLED", True)
    ws = FakeWebSocket()
    sess = make_session(ws, tts=StubTTS(n_frames=50, chunks=1))

    sent = {"n": 0}
    orig = ws.send_bytes

    async def cancel_after_three(data):
        sent["n"] += 1
        if sent["n"] == 3:
            raise asyncio.CancelledError()
        await orig(data)

    ws.send_bytes = cancel_after_three  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        await sess._stream_tts("interrupt me")
    # finally-block still closed out the device cleanly.
    assert ("tts", "stop") in [(m.get("type"), m.get("state")) for m in ws.sent_json]
