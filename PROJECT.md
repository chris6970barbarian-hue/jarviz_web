# Jarviz Backend — Agent Handoff

A durable orientation note for the next session. Read this first; the
hand-written `README.md` covers user-facing setup, and `CHANGELOG.md` lists
the reverse-chronological changes (read its top to see what just shipped).

## What this project does

Self-hosted Python backend that speaks the [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)
protocol — drop-in replacement for `xiaozhi.me` — for the Jarviz ESP32-S3
reminder appliance (firmware lives in the sibling `../Jarviz_Code/`
directory, separate git repo). Serves the device's `POST /xiaozhi/ota/`
boot handshake and `WS /xiaozhi/v1/` conversation channel, runs ASR
(faster-whisper), an LLM with full tool-use loop (DeepSeek / OpenAI /
Anthropic), TTS (Edge-TTS via ffmpeg), and bridges the device's MCP
tools through the LLM's function-calling.

## Dependencies / runtime requirements

- **Python 3.12** (3.13 has wheel gaps for PyOgg / faster-whisper at the time of this writing; 3.11 also works)
- **ffmpeg** on PATH OR `imageio-ffmpeg` wheel (auto-fallback)
- **libopus** at runtime — PyOgg wheel ships it on Windows; Linux: `apt install libopus0`; macOS: `brew install opus`
- **A DeepSeek key** (or OpenAI / Anthropic) for the LLM step. Set in `.env` (gitignored).
- Pinned Python deps in `requirements.txt` (fastapi, uvicorn[standard], openai, anthropic, edge-tts, faster-whisper, PyOgg, numpy, httpx, websockets, filelock, …).
- **Windows note**: project paths must not contain spaces — the build's stock managed_components emit bare `-L <path>` linker args that the linker tokenizes on spaces. We work around this for the firmware via patches in `../Jarviz_Code/managed_components/`; the backend itself is unaffected.

## How to build and run

```powershell
# First-time setup (Windows native)
cd E:\Etsy\Jarviz Code\Backend
.\scripts\setup-windows.ps1            # scoop -> python312 + ffmpeg + .venv + pip install
notepad .env                            # paste DEEPSEEK_API_KEY=...

# Start (preferred — wraps uvicorn with our WS keepalive defaults)
.\.venv\Scripts\Activate.ps1
python -m server                        # equivalent: uvicorn server.main:app --host 0.0.0.0 --port 8080

# Verify
curl http://localhost:8080/healthz        -> {"ok": true}
curl http://localhost:8080/metrics        -> JSON of counters + latency p50/p95
python scripts\simulate_device.py "what time is it"   # end-to-end smoke without flashing the ESP32
```

Linux/macOS: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && python -m server`. Docker: `docker compose up --build` (Dockerfile already runs `python -m server`).

Pointing the ESP32 at this server: in the firmware repo, `idf.py menuconfig` → Xiaozhi Assistant → OTA URL → `http://<LAN-IP>:8080/xiaozhi/ota/`, then rebuild + flash.

## Code structure

```
Backend/
├─ server/                       FastAPI app + protocol handlers
│   ├─ __main__.py               `python -m server` entry, wraps uvicorn.run() with WS ping/pong baked in
│   ├─ main.py                   FastAPI app, lifespan builds LLM/ASR/TTS + 2 semaphores onto app.state
│   ├─ config.py                 pydantic-settings, reads .env. All JARVIZ_* tunables live here
│   ├─ ota.py                    HTTP routes: POST /xiaozhi/ota/ (handshake), GET /healthz, /devices, /metrics,
│   │                            /sessions/live, /transcripts/recent, /reminders/stats, /network,
│   │                            /logs/recent, /dashboard, /
│   ├─ ws.py                     WS /xiaozhi/v1/ — auth check, cooldown gate, session-cap, hands off to Session
│   ├─ session.py                Per-connection state machine: ASR -> LLM -> TTS, abort handling,
│   │                            per-turn latency log, TTS-text sanitizer (_sanitize_for_tts),
│   │                            echo-guard (drop device's auto-listen events during in-flight TTS).
│   │                            Reports state transitions + completed turns into live_state for the dashboard.
│   ├─ live_state.py             In-memory dashboard state: per-session current state (idle/listening/
│   │                            processing/speaking), ring of last 100 completed turns with transcripts,
│   │                            reminder counters + 48 h hourly bucket series for the chart.
│   ├─ mcp_bridge.py             JSON-RPC client for the device's MCP server; tool discovery + tools/call
│   ├─ audio.py                  Opus encode/decode, resampling, RMS gate. Wire-format constants match firmware
│   ├─ store.py                  devices.json registry. threading.Lock + filelock.FileLock for inter-worker safety
│   ├─ auth.py                   HMAC-signed per-device tokens. Format compatible with xinnan-tech/xiaozhi-esp32-server
│   ├─ cooldown.py               Per-Device-Id rate-limit on OTA + WS (in-memory OrderedDict, 4096-entry cap)
│   ├─ metrics.py                In-memory counters + ring of last 200 turn latencies; serves /metrics JSON
│   ├─ dashboard.py              Operator console: single Python-string HTML/CSS/JS that polls
│   │                            /sessions/live, /transcripts/recent, /reminders/stats, /network,
│   │                            /metrics, /logs/recent every 2s. Served at /dashboard and at /
│   │                            when Accept: text/html. Audiophile mission-control aesthetic
│   │                            (Fraunces serif + VU-amber/CRT-phosphor accents).
│   ├─ log.py                    setup_logging() + an in-memory _RingHandler that backs /logs/recent
│   ├─ asr/whisper_provider.py   faster-whisper, lazy-loaded, serialized via threading.Semaphore (model not thread-safe)
│   ├─ llm/openai_provider.py    DeepSeek / OpenAI-compatible. asyncio.wait_for(timeout=30s) on each round
│   ├─ llm/anthropic_provider.py Claude alternative. Same provider interface + tool-use loop shape
│   └─ tts/edge_provider.py      edge-tts -> ffmpeg -> 24 kHz s16le. Force-kills ffmpeg on early consumer cancel
├─ prompts/jarviz.md             System prompt. Tight: no-emoji, no-disclaimer, greeting-once rules
├─ scripts/
│   ├─ setup-windows.ps1         Scoop-based dev install
│   └─ simulate_device.py        Fake-device protocol client. Use for backend tests without flashing
├─ data/devices.json             Device registry (gitignored)
├─ requirements.txt, .env.example, Dockerfile, docker-compose.yml
├─ README.md                     User-facing setup + env-var cheatsheet (hand-written)
├─ CHANGELOG.md                  Reverse-chronological log of every change
└─ PROJECT.md                    (this file)
```

### Concurrency model — the part to internalize before changing it

Three orthogonal caps gate load:

1. **`JARVIZ_MAX_SESSIONS`** (default 64) — `app.state.session_semaphore`, acquired in `ws.py` before `ws.accept()`. Idle WS connections are cheap; this just prevents one device monopolizing process FDs.
2. **`JARVIZ_MAX_CONCURRENT_TURNS`** (default 8) — `app.state.turn_semaphore`, acquired in `session.py:_process_turn` *after* ASR/discovery but *before* the LLM call, released *after* TTS. Bounds CPU + provider quota.
3. **`JARVIZ_ASR_PARALLELISM`** (default 1) — `threading.Semaphore` *inside* `whisper_provider.py`. Mandatory: faster-whisper's CTranslate2 Generator is not thread-safe per-model. Raising N>1 requires a separate model pool (not implemented).

Plus per-device cooldown (`JARVIZ_DEVICE_COOLDOWN_S`) and WS keepalive (`JARVIZ_WS_PING_INTERVAL_S` / `_TIMEOUT_S`) — both documented in `.env.example`.

### Session lifecycle

Each `WS /xiaozhi/v1/` connection runs one `Session`. `Session.run()` does the hello handshake → kicks off MCP tool discovery in a background task → enters `_main_loop()`. The loop reads `listen start/stop/detect` and binary mic frames; on `listen stop` (or `listen detect`), `_spawn_process()` creates a `process_task` running `_process_turn()` (ASR → LLM → TTS pipeline). Aborts and new turns cancel the in-flight `process_task`. On disconnect, the finally block cancels everything cleanly; metrics counters get bumped at every transition.

## Future possible improvements

In rough priority order — each is its own commit:

1. **Whisper model pool.** Today `JARVIZ_ASR_PARALLELISM=1` is the only safe value because one CTranslate2 model is single-threaded. A small pool of model replicas (configurable N) would let CPU-bound ASR scale with cores. Tradeoff: each replica is ~70 MB (tiny) to ~3 GB (large-v3) RAM.
2. **Sentence-streaming TTS.** Start TTS on the first complete sentence the LLM emits rather than waiting for the full response. Halves perceived latency for long replies. Needs an LLM-streaming refactor in `openai_provider.py` + `anthropic_provider.py` and an `async for sentence in ...` in `_stream_tts`.
3. **Prometheus exposition format.** Add a `/metrics?format=prometheus` mode to `metrics.py` so the JSON endpoint also speaks the line-based prom format. Per-worker still — multi-worker would still need a side-car.
4. **Cross-worker store backend.** `store.py` currently does JSON + `filelock`; OK at single-host single-digit-worker scale, but a real fleet should move to SQLite (or Redis for cooldown + metrics).
5. **Idle-session timeout.** Devices that connect and never send anything still hold a session slot until the WS ping/pong eventually drops them. A per-session inactivity timeout (e.g. close after no message for 5 min) would tighten this.
6. **Per-device system prompt overrides.** `store.py` is currently a leaf store; could extend the record schema with `system_prompt_override` so different devices can have different personas.
7. **httpx-side LLM cancellation propagation.** Verified clean today (~0 ms), but if a future openai SDK upgrade regresses this, add explicit `aclose()` on cancel.
8. **Streaming ASR.** faster-whisper supports a streaming mode; could start LLM on partial transcripts.

The 5-item scaling pass (Sept-Oct 2026 work) is already in. See `CHANGELOG.md` for full per-change rationale and verification.
