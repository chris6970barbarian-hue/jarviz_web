"""End-to-end HTTP + WebSocket surface via FastAPI TestClient.

Providers are stubbed by monkeypatching server.main._build_providers so the
app boots without faster-whisper / ffmpeg / an LLM key. Offline.
"""

from __future__ import annotations

import json

import pytest
from starlette.websockets import WebSocketDisconnect

import server.main as main
from server import auth
from server.config import settings
from conftest import StubASR, StubLLM, StubTTS


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "_build_providers", lambda: (StubLLM(), StubASR(), StubTTS(n_frames=2)))
    # Auth off by default for the plain-surface tests.
    monkeypatch.setattr(settings, "JARVIZ_AUTH_SECRET", "")
    monkeypatch.setattr(settings, "JARVIZ_DEVICE_COOLDOWN_S", 0)  # don't fight cooldown across tests
    with TestClient(main.app) as c:
        yield c


HELLO = {"type": "hello", "version": 1,
         "audio_params": {"format": "opus", "sample_rate": 16000, "frame_duration": 60}}


# ------------------------------- HTTP ------------------------------------

def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_metrics_shape(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    for key in ("uptime_s", "ws", "sessions", "turns", "latency_ms"):
        assert key in body


def test_root_json_for_probes(client):
    r = client.get("/", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"service": "jarviz-backend", "status": "ok"}


def test_root_html_for_browsers(client):
    r = client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<" in r.text and len(r.text) > 200


# ----------------------------- WebSocket ---------------------------------

def test_ws_hello_handshake(client):
    with client.websocket_connect(
        "/xiaozhi/v1/", headers={"device-id": "dev-hello", "client-id": "c1"}
    ) as ws:
        ws.send_text(json.dumps(HELLO))
        msg = ws.receive_json()
        assert msg["type"] == "hello"
        assert msg["transport"] == "websocket"
        assert msg["audio_params"]["sample_rate"] == 24000
        assert msg["audio_params"]["frame_duration"] == 60


def test_ws_rejects_missing_token_when_auth_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "JARVIZ_AUTH_SECRET", "topsecret")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/xiaozhi/v1/", headers={"device-id": "dev-noauth", "client-id": "c1"}
        ) as ws:
            ws.receive_text()


def test_ws_accepts_valid_token_when_auth_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "JARVIZ_AUTH_SECRET", "topsecret")
    monkeypatch.setattr(settings, "JARVIZ_AUTH_TOKEN_TTL_S", 3600)
    monkeypatch.setattr(settings, "JARVIZ_AUTH_ALLOWED_DEVICES", "")
    tok = auth.mint_token("c1", "dev-auth-ok")
    assert tok
    with client.websocket_connect(
        "/xiaozhi/v1/",
        headers={"device-id": "dev-auth-ok", "client-id": "c1",
                 "authorization": f"Bearer {tok}"},
    ) as ws:
        ws.send_text(json.dumps(HELLO))
        msg = ws.receive_json()
        assert msg["type"] == "hello"
