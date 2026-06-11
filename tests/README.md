# Tests

Offline, deterministic test suite for the Jarviz backend. No network, no LLM
API key, and no Whisper model download are required — the ASR/LLM/TTS providers
are replaced with stubs and the WebSocket is a recording fake (see
`conftest.py`).

## Running

Requires **Python ≥ 3.10** (the app uses `str | None` route annotations that
FastAPI evaluates at import time; 3.9 cannot import it). CI/prod target is 3.12.

`server/audio.py` binds libopus via ctypes at import, so libopus must be on the
loader path. On macOS with Homebrew opus:

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/lib python -m pytest tests/
```

On Linux (libopus0 installed) the env var is unnecessary:

```bash
python -m pytest tests/
```

## What's covered (64 tests)

| File | Focus |
|------|-------|
| `test_pacing.py` | **TTS pacing fix** — drives the real `_stream_tts` under a virtual clock; asserts the paced schedule stays under the firmware's 2.4 s decode-queue cap, that pacing-off reproduces the flood, and that `tts stop` is always sent on error/cancel |
| `test_smoke_e2e.py` | Full protocol over a real WebSocket (hello → MCP discovery → detect → LLM → paced Opus frames → stop) with stub providers |
| `test_session.py` | Session state machine: echo-guard suppression, listen→spawn, abort, turn-semaphore release on completion **and** cancellation |
| `test_auth.py` | HMAC device-token mint/verify: tamper, expiry, future-date, allowlist, auth-disabled |
| `test_cooldown.py` | Per-device rate limiter: window, empty key, disable, memory bound |
| `test_audio.py` | Opus 24 kHz encode→decode round-trip + frame slicing + PCM helpers |
| `test_mcp_bridge.py` | JSON-RPC correlation, error/timeout/late-id, pagination, cancel-all |
| `test_metrics.py` | Counters, derived in-flight, latency ring, percentiles |
| `test_http_ws.py` | FastAPI TestClient: `/healthz`, `/metrics`, root HTML/JSON, WS hello + auth accept/reject |
| `test_sanitize.py` | TTS text sanitizer (emoji/markdown stripping, idempotence) |
| `test_edge_tts.py` | Edge-TTS resilience (upstream failure surfaced, not swallowed). **Skips if ffmpeg is absent**; always present in the Docker image |
