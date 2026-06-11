"""Operator-console / telemetry auth gate (JARVIZ_DASHBOARD_TOKEN). Offline.

When the token is set, the dashboard + telemetry read surface requires it
(query / header / cookie); /healthz and the OTA device route stay open.
"""

from __future__ import annotations

import pytest

import server.main as main
from server.config import settings
from conftest import StubASR, StubLLM, StubTTS

TELEMETRY = ["/metrics", "/transcripts/recent", "/logs/recent",
             "/sessions/live", "/network", "/reminders/stats", "/devices"]


def _mk_client(monkeypatch, token):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_build_providers",
                        lambda: (StubLLM(), StubASR(), StubTTS(n_frames=2)))
    monkeypatch.setattr(settings, "JARVIZ_AUTH_SECRET", "")
    monkeypatch.setattr(settings, "JARVIZ_DEVICE_COOLDOWN_S", 0)
    monkeypatch.setattr(settings, "JARVIZ_DASHBOARD_TOKEN", token)
    return TestClient(main.app)


def test_telemetry_open_when_no_token(monkeypatch):
    with _mk_client(monkeypatch, "") as c:
        for path in TELEMETRY:
            assert c.get(path).status_code == 200, path


def test_telemetry_requires_token_when_set(monkeypatch):
    with _mk_client(monkeypatch, "op-secret") as c:
        for path in TELEMETRY:
            assert c.get(path).status_code == 401, path


def test_token_accepted_via_query_and_header(monkeypatch):
    with _mk_client(monkeypatch, "op-secret") as c:
        assert c.get("/metrics?token=op-secret").status_code == 200
        assert c.get("/metrics", headers={"X-Dashboard-Token": "op-secret"}).status_code == 200
        assert c.get("/metrics?token=wrong").status_code == 401


def test_dashboard_sets_cookie_then_polls_authenticate(monkeypatch):
    with _mk_client(monkeypatch, "op-secret") as c:
        # No token -> 401 login hint page.
        r = c.get("/dashboard")
        assert r.status_code == 401 and "token" in r.text.lower()
        # Correct token -> 200 and a cookie is dropped...
        r = c.get("/dashboard?token=op-secret")
        assert r.status_code == 200
        assert "jarviz_dash" in c.cookies
        # ...so subsequent same-origin polls authenticate via the cookie alone.
        assert c.get("/metrics").status_code == 200
        assert c.get("/transcripts/recent").status_code == 200


def test_healthz_and_probe_root_never_gated(monkeypatch):
    with _mk_client(monkeypatch, "op-secret") as c:
        assert c.get("/healthz").status_code == 200
        r = c.get("/", headers={"accept": "application/json"})
        assert r.status_code == 200 and r.json()["status"] == "ok"


def test_ota_device_route_never_gated(monkeypatch):
    # Devices must always reach OTA regardless of the operator token.
    with _mk_client(monkeypatch, "op-secret") as c:
        r = c.post("/xiaozhi/ota/", headers={"Device-Id": "aa:bb", "Client-Id": "c"},
                   content="{}")
        assert r.status_code == 200
        assert "websocket" in r.json()
