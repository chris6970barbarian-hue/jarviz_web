"""Resilience + observability hardening: PII-log redaction, LLM retry wiring,
fail-closed startup, per-stage failure metrics, echo-guard cross-turn reset.
Offline.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from server import metrics, session as session_mod
from conftest import StubASR, StubLLM, StubTTS, make_session


def _disable_pacing(monkeypatch):
    monkeypatch.setattr(session_mod.settings, "JARVIZ_TTS_PACING_ENABLED", False)


# ----------------------------- PII log redaction -------------------------

def test_log_text_redacts_when_disabled(monkeypatch):
    monkeypatch.setattr(session_mod.settings, "JARVIZ_LOG_MESSAGE_TEXT", True)
    assert session_mod._log_text("call mom") == "call mom"
    assert session_mod._log_text(None) == ""

    monkeypatch.setattr(session_mod.settings, "JARVIZ_LOG_MESSAGE_TEXT", False)
    assert session_mod._log_text("call mom") == "<redacted 8 chars>"


def test_redact_msg_strips_text_field(monkeypatch):
    monkeypatch.setattr(session_mod.settings, "JARVIZ_LOG_MESSAGE_TEXT", False)
    msg = {"type": "listen", "state": "detect", "text": "secret"}
    red = session_mod._redact_msg(msg)
    assert red["text"] == "<redacted 6 chars>"
    assert red["state"] == "detect"           # non-text fields preserved
    # No text field -> returned unchanged (same object).
    no_text = {"type": "abort"}
    assert session_mod._redact_msg(no_text) is no_text


def test_redact_msg_noop_when_enabled(monkeypatch):
    monkeypatch.setattr(session_mod.settings, "JARVIZ_LOG_MESSAGE_TEXT", True)
    msg = {"type": "listen", "text": "hello"}
    assert session_mod._redact_msg(msg) is msg


# ----------------------------- LLM retry wiring --------------------------

def test_llm_client_gets_configured_max_retries(monkeypatch):
    from server.llm import openai_provider
    monkeypatch.setattr(openai_provider.settings, "JARVIZ_LLM_MAX_RETRIES", 5)
    llm = openai_provider.OpenAICompatLLM(api_key="sk-test", base_url=None, model="m")
    assert llm._client.max_retries == 5


# ----------------------------- fail-closed startup -----------------------

def test_require_auth_refuses_to_boot_without_secret(monkeypatch):
    import server.main as main
    from fastapi.testclient import TestClient
    from conftest import StubASR, StubLLM

    monkeypatch.setattr(main, "_build_providers",
                        lambda: (StubLLM(), StubASR(), StubTTS(n_frames=1)))
    monkeypatch.setattr(main.settings, "JARVIZ_REQUIRE_AUTH", True)
    monkeypatch.setattr(main.settings, "JARVIZ_AUTH_SECRET", "")
    with pytest.raises(RuntimeError, match="refusing to start"):
        with TestClient(main.app):
            pass


def test_require_auth_boots_with_secret(monkeypatch):
    import server.main as main
    from fastapi.testclient import TestClient
    from conftest import StubASR, StubLLM

    monkeypatch.setattr(main, "_build_providers",
                        lambda: (StubLLM(), StubASR(), StubTTS(n_frames=1)))
    monkeypatch.setattr(main.settings, "JARVIZ_REQUIRE_AUTH", True)
    monkeypatch.setattr(main.settings, "JARVIZ_AUTH_SECRET", "a-secret")
    with TestClient(main.app) as c:
        assert c.get("/healthz").status_code == 200


# ----------------------------- failure metrics ---------------------------

def test_failures_block_in_snapshot():
    before = metrics.snapshot()["failures"]
    metrics.inc("llm_failed")
    metrics.inc("tts_failed", by=2)
    after = metrics.snapshot()["failures"]
    assert after["llm"] == before["llm"] + 1
    assert after["tts"] == before["tts"] + 2
    assert "asr" in after


@pytest.mark.asyncio
async def test_llm_failure_increments_metric_and_still_speaks(monkeypatch):
    _disable_pacing(monkeypatch)

    class BoomLLM:
        async def respond(self, *, user_text, tools, invoke_tool, history):
            raise RuntimeError("provider 500")

    sess = make_session(llm=BoomLLM(), tts=StubTTS(n_frames=1))
    sess._state.discovery_done.set()
    before = metrics.snapshot()["failures"]["llm"]

    await sess._process_turn(captured_pcm=None, source_text="hello there")

    assert metrics.snapshot()["failures"]["llm"] == before + 1
    # The user still hears the graceful apology (not dead air).
    assert any(m.get("state") == "sentence_start" for m in sess._ws.sent_json)


@pytest.mark.asyncio
async def test_tts_silent_failure_increments_metric(monkeypatch):
    _disable_pacing(monkeypatch)

    class SilentTTS:
        async def synthesize(self, text):
            return
            yield b""  # pragma: no cover — async generator that yields nothing

    sess = make_session(tts=SilentTTS())
    sess._state.discovery_done.set()
    before = metrics.snapshot()["failures"]["tts"]

    await sess._process_turn(captured_pcm=None, source_text="what time is it")

    # Had text to say but zero audio frames reached the device -> counted.
    assert metrics.snapshot()["failures"]["tts"] == before + 1


# ----------------------------- echo-guard reset --------------------------

@pytest.mark.asyncio
async def test_spawn_process_clears_suppress_listen(monkeypatch):
    _disable_pacing(monkeypatch)
    sess = make_session(tts=StubTTS(n_frames=1))
    sess._state.discovery_done.set()
    sess._state.suppress_listen = True  # stale flag from a prior turn

    sess._spawn_process(captured_pcm=None, source_text="hi")
    assert sess._state.suppress_listen is False
    await asyncio.wait_for(sess._state.process_task, timeout=5.0)


# ---- review fix: _log_text must not crash on a non-string `text` field ----

def test_log_text_tolerates_non_string(monkeypatch):
    monkeypatch.setattr(session_mod.settings, "JARVIZ_LOG_MESSAGE_TEXT", False)
    # A protocol-violating device could send text=42; redaction must not raise.
    assert session_mod._log_text(42) == "<redacted int>"
    red = session_mod._redact_msg({"type": "listen", "state": "detect", "text": 42})
    assert red["text"] == "<redacted int>"


# ---- review fix: startup warns when telemetry is open on a routable bind ----

def _boot(monkeypatch, *, token, host, secret="devsecret", ws_url="ws://x/xiaozhi/v1/"):
    import server.main as main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_build_providers",
                        lambda: (StubLLM(), StubASR(), StubTTS(n_frames=1)))
    monkeypatch.setattr(main.settings, "JARVIZ_AUTH_SECRET", secret)
    monkeypatch.setattr(main.settings, "JARVIZ_REQUIRE_AUTH", False)
    monkeypatch.setattr(main.settings, "JARVIZ_DASHBOARD_TOKEN", token)
    monkeypatch.setattr(main.settings, "JARVIZ_HTTP_HOST", host)
    monkeypatch.setattr(main.settings, "JARVIZ_WS_PUBLIC_URL", ws_url)
    return main, TestClient(main.app)


def _has_dash_warning(records):
    return any("operator dashboard" in r.getMessage() and "UNAUTHENTICATED" in r.getMessage()
               for r in records)


def test_open_telemetry_on_nonloopback_warns_even_with_device_auth(monkeypatch, caplog):
    # device auth ON but dashboard token EMPTY + routable bind -> must warn.
    _, client = _boot(monkeypatch, token="", host="0.0.0.0")
    with caplog.at_level(logging.WARNING, logger="jarviz.main"):
        with client:
            pass
    assert _has_dash_warning(caplog.records)


def test_dashboard_token_set_suppresses_warning(monkeypatch, caplog):
    _, client = _boot(monkeypatch, token="op-secret", host="0.0.0.0")
    with caplog.at_level(logging.WARNING, logger="jarviz.main"):
        with client:
            pass
    assert not _has_dash_warning(caplog.records)


def test_loopback_open_telemetry_does_not_warn(monkeypatch, caplog):
    _, client = _boot(monkeypatch, token="", host="127.0.0.1")
    with caplog.at_level(logging.WARNING, logger="jarviz.main"):
        with client:
            pass
    assert not _has_dash_warning(caplog.records)


# ---- review fix: dashboard cookie gets Secure on a TLS (wss) deployment ----

def test_dashboard_cookie_secure_on_wss_deploy(monkeypatch):
    _, client = _boot(monkeypatch, token="op-secret", host="0.0.0.0",
                      ws_url="wss://jarviz.example.com/xiaozhi/v1/")
    with client as c:
        sc = c.get("/dashboard?token=op-secret").headers.get("set-cookie", "")
    assert "jarviz_dash=" in sc and "Secure" in sc


def test_dashboard_cookie_not_secure_on_http_lan(monkeypatch):
    _, client = _boot(monkeypatch, token="op-secret", host="0.0.0.0",
                      ws_url="ws://192.168.1.50:8080/xiaozhi/v1/")
    with client as c:
        sc = c.get("/dashboard?token=op-secret").headers.get("set-cookie", "")
    assert "jarviz_dash=" in sc and "Secure" not in sc
