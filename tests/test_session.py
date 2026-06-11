"""Session state machine: echo-guard, listen handling, abort, and the
turn-semaphore release contract. Offline (stub providers + fake WebSocket).
"""

from __future__ import annotations

import asyncio

import pytest

from server import live_state, session as session_mod
from conftest import FakeWebSocket, StubTTS, make_session


def _disable_pacing(monkeypatch):
    # Keep TTS instant so full-turn tests don't wait on the paced schedule.
    monkeypatch.setattr(session_mod.settings, "JARVIZ_TTS_PACING_ENABLED", False)


async def _stuck_task():
    await asyncio.Event().wait()


# ----------------------------- echo-guard --------------------------------

@pytest.mark.asyncio
async def test_echo_guard_suppresses_auto_listen_during_turn():
    sess = make_session()
    sess._state.process_task = asyncio.create_task(_stuck_task())
    try:
        assert sess._is_turn_active() is True

        await sess._on_listen({"type": "listen", "state": "start", "mode": "auto"})
        # The auto-listen start is swallowed: not treated as a real listen.
        assert sess._state.suppress_listen is True
        assert sess._state.listening is False
        held = sess._state.process_task

        await sess._on_listen({"type": "listen", "state": "stop"})
        # Matching stop is dropped and the flag cleared; the in-flight turn is
        # NOT cancelled/replaced.
        assert sess._state.suppress_listen is False
        assert sess._state.process_task is held
        assert not held.done()
    finally:
        sess._state.process_task.cancel()


@pytest.mark.asyncio
async def test_echo_guard_honors_manual_press_during_turn():
    sess = make_session()
    sess._state.process_task = asyncio.create_task(_stuck_task())
    try:
        await sess._on_listen({"type": "listen", "state": "start", "mode": "manual"})
        # A deliberate BOOT press is honored: it starts a real listen cycle.
        assert sess._state.listening is True
        assert sess._state.listening_mode == "manual"
        assert sess._state.suppress_listen is False
    finally:
        sess._state.process_task.cancel()


# ----------------------------- listen -> spawn ---------------------------

@pytest.mark.asyncio
async def test_listen_stop_spawns_turn(monkeypatch):
    _disable_pacing(monkeypatch)
    sess = make_session(tts=StubTTS(n_frames=2))
    sess._state.discovery_done.set()

    await sess._on_listen({"type": "listen", "state": "start", "mode": "manual"})
    assert sess._state.listening is True

    await sess._on_listen({"type": "listen", "state": "stop"})
    assert sess._state.listening is False
    assert sess._state.process_task is not None
    # Empty mic buffer -> turn returns fast without an LLM call.
    await asyncio.wait_for(sess._state.process_task, timeout=5.0)


@pytest.mark.asyncio
async def test_detect_reminder_records_and_spawns():
    sess = make_session()
    fired_before = live_state.reminder_snapshot()["counters"]["fired_total"]

    await sess._on_listen(
        {"type": "listen", "state": "detect", "text": "[Reminder] call mom"}
    )
    # A turn was spawned for the proactive utterance...
    assert sess._state.process_task is not None
    # ...and the reminder-fired counter advanced. Cancel before the heavy
    # pipeline actually runs.
    sess._state.process_task.cancel()

    fired_after = live_state.reminder_snapshot()["counters"]["fired_total"]
    assert fired_after == fired_before + 1


# ----------------------------- abort -------------------------------------

@pytest.mark.asyncio
async def test_abort_cancels_inflight_turn():
    sess = make_session()
    sess._state.process_task = asyncio.create_task(_stuck_task())
    await sess._on_abort({"type": "abort", "reason": "user"})
    await asyncio.sleep(0)
    assert sess._state.process_task.cancelled() or sess._state.process_task.done()


# ----------------------- turn-semaphore release contract -----------------

@pytest.mark.asyncio
async def test_turn_semaphore_released_after_normal_turn(monkeypatch):
    _disable_pacing(monkeypatch)
    sem = asyncio.Semaphore(1)
    sess = make_session(turn_semaphore=sem, tts=StubTTS(n_frames=2))
    sess._state.discovery_done.set()

    await sess._process_turn(captured_pcm=None, source_text="what time is it")
    # Slot must be returned even though a full LLM+TTS turn ran.
    assert sem._value == 1
    # And the assistant actually spoke.
    assert any(m.get("state") == "sentence_start" for m in sess._ws.sent_json)


@pytest.mark.asyncio
async def test_turn_semaphore_released_on_cancellation(monkeypatch):
    _disable_pacing(monkeypatch)
    sem = asyncio.Semaphore(1)

    class SlowLLM:
        async def respond(self, *, user_text, tools, invoke_tool, history):
            await asyncio.Event().wait()  # never returns -> we cancel it
            return "", history

    sess = make_session(llm=SlowLLM(), turn_semaphore=sem, tts=StubTTS(n_frames=1))
    sess._state.discovery_done.set()

    task = asyncio.create_task(
        sess._process_turn(captured_pcm=None, source_text="hello there")
    )
    # Let it acquire the slot and block inside the LLM call.
    for _ in range(10):
        await asyncio.sleep(0)
        if sem._value == 0:
            break
    assert sem._value == 0  # slot is held mid-turn

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The finally-block must return the slot on cancellation too.
    assert sem._value == 1
