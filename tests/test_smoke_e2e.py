"""End-to-end protocol smoke test over a real WebSocket via FastAPI TestClient.

This is the deterministic, offline equivalent of scripts/simulate_device.py:
it plays the device side of the xiaozhi protocol — hello handshake, MCP tool
discovery, then a `listen.detect` (the proactive/reminder path that skips ASR)
— and asserts the backend drives LLM -> TTS and streams real Opus audio frames
followed by a clean `tts stop`. Providers are stubbed (no network / LLM key).
"""

from __future__ import annotations

import json

import pytest

import server.main as main
from server.config import settings
from conftest import StubASR, StubLLM, StubTTS


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        main, "_build_providers",
        lambda: (StubLLM(reply="It is nine o'clock."), StubASR(), StubTTS(n_frames=8)),
    )
    monkeypatch.setattr(settings, "JARVIZ_AUTH_SECRET", "")
    monkeypatch.setattr(settings, "JARVIZ_DEVICE_COOLDOWN_S", 0)
    with TestClient(main.app) as c:
        yield c


HELLO = {"type": "hello", "version": 1,
         "audio_params": {"format": "opus", "sample_rate": 16000, "frame_duration": 60}}


def _answer_mcp(ws, payload):
    """Play the device's MCP server: answer initialize + tools/list so the
    backend's discovery completes promptly (otherwise it waits out a timeout)."""
    method = payload.get("method")
    rid = payload.get("id")
    if method == "initialize":
        ws.send_text(json.dumps({"type": "mcp", "payload": {
            "jsonrpc": "2.0", "id": rid,
            "result": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "serverInfo": {"name": "sim-device", "version": "1.0"}}}}))
    elif method == "tools/list":
        ws.send_text(json.dumps({"type": "mcp", "payload": {
            "jsonrpc": "2.0", "id": rid, "result": {"tools": []}}}))


def test_full_pipeline_streams_paced_audio_then_stop(client):
    with client.websocket_connect(
        "/xiaozhi/v1/", headers={"device-id": "dev-smoke", "client-id": "c-smoke"}
    ) as ws:
        ws.send_text(json.dumps(HELLO))
        assert ws.receive_json()["type"] == "hello"

        # Proactive utterance (reminder firing path): skips ASR, runs LLM->TTS.
        ws.send_text(json.dumps({"type": "listen", "state": "detect",
                                 "text": "what time is it"}))

        audio_bytes = 0
        audio_frames = 0
        sentence_text = None
        saw_start = saw_stop = False

        for _ in range(500):
            m = ws.receive()
            if m["type"] == "websocket.close":
                break
            if m.get("bytes") is not None:
                audio_frames += 1
                audio_bytes += len(m["bytes"])
                continue
            txt = m.get("text")
            if not txt:
                continue
            obj = json.loads(txt)
            if obj.get("type") == "mcp":
                _answer_mcp(ws, obj.get("payload", {}))
            elif obj.get("type") == "tts":
                st = obj.get("state")
                if st == "sentence_start":
                    sentence_text = obj.get("text")
                elif st == "start":
                    saw_start = True
                elif st == "stop":
                    saw_stop = True
                    break

        # The backend spoke a full reply: control framing + real Opus audio.
        assert saw_start and saw_stop, (saw_start, saw_stop)
        assert sentence_text == "It is nine o'clock."
        assert audio_frames == 8           # StubTTS produced 8 full 60 ms frames
        assert audio_bytes > 0             # ...as real (non-empty) Opus packets
