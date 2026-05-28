---
title: Jarviz Backend
short_description: Self-hosted backend + operator console for the Jarviz ESP32 voice reminder appliance.
sdk: docker
app_port: 8080
pinned: false
license: mit
---

# Jarviz Web

The cloud half of the [Jarviz](https://github.com/chris6970barbarian-hue/Jarviz) project — a butler-style ESP32 voice reminder appliance. This repo is the **web side** only: the FastAPI backend that speaks the `xiaozhi-esp32` device protocol (drop-in replacement for `xiaozhi.me`) plus the single-page operator console (`/dashboard`).

The device firmware lives in a separate repo: [chris6970barbarian-hue/Jarviz](https://github.com/chris6970barbarian-hue/Jarviz).

## What this serves

- `POST /xiaozhi/ota/` — device boot handshake (returns time + WS URL + signed token)
- `WS  /xiaozhi/v1/` — per-device conversation channel: hello, audio in (Opus), MCP tool calls, audio out (Opus)
- `GET /dashboard` (also `/` for browsers) — live operator UI: device state, transcript, reminders chart, network, telemetry, log tail
- JSON endpoints: `/healthz`, `/metrics`, `/sessions/live`, `/transcripts/recent`, `/reminders/stats`, `/network`, `/logs/recent`, `/devices`

The console polls the JSON endpoints every 2 s and renders six panels (device status, transcript, reminders, network, telemetry, log). No JS framework, no build step — one HTML string in `server/dashboard.py`.

## Stack

- **Python 3.12**, FastAPI, uvicorn with WS keepalive
- **ASR**: `faster-whisper` (default model `tiny`, CPU, int8)
- **LLM**: DeepSeek-V3 by default via the OpenAI-compatible client. Anthropic Claude and any other OpenAI-compatible provider are drop-in.
- **TTS**: Edge-TTS streamed through `ffmpeg` into 24 kHz Opus frames
- **Storage**: JSON device registry with `filelock` for multi-worker safety
- **Auth**: HMAC-signed per-device tokens, wire-compatible with `xinnan-tech/xiaozhi-esp32-server`

## Deploy

### Hugging Face Spaces (this repo's primary target)

1. Create a new Space → SDK: Docker. Point it at this GitHub repo (HF supports auto-sync) or `git push` to the Space's git remote.
2. In **Settings → Variables and secrets** set:
   - `DEEPSEEK_API_KEY` (Secret) — required, get one from <https://platform.deepseek.com>
   - `JARVIZ_WS_PUBLIC_URL` (Variable) — `wss://<your-space>.hf.space/xiaozhi/v1/` (HF Spaces serve on HTTPS/WSS)
3. The Space builds the `Dockerfile` and serves on port `8080` (declared in the frontmatter above as `app_port: 8080`).
4. Open `https://<your-space>.hf.space/` to load the operator console. Point your ESP32 firmware's OTA URL at `https://<your-space>.hf.space/xiaozhi/ota/`.

HF Spaces gives 2 vCPU + 16 GB RAM on the free CPU tier, never sleeps, has WebSocket support — comfortably fits the whole stack including `faster-whisper`.

### Local development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                                 # then paste DEEPSEEK_API_KEY=sk-...
python -m server                                     # http://localhost:8080/
```

End-to-end smoke without flashing the ESP32:

```bash
python scripts/simulate_device.py --user-name Jacob "remind me to drink water in 30 seconds"
```

### Docker

```bash
docker build -t jarviz-web .
docker run --rm -p 8080:8080 --env-file .env jarviz-web
```

## Pointing the real device at the deployed backend

In the firmware repo (`Jarviz`):

```powershell
idf.py menuconfig    # Xiaozhi Assistant > OTA URL > https://<your-space>.hf.space/xiaozhi/ota/
idf.py build flash monitor
```

Re-flash and the device's onboard RGB will scroll blue (boot) → green-blink (activating with the Space) → off (idle, awaiting BOOT press). Press BOOT, speak, and the Space's `/dashboard` will show the live state chip flip through `listening` → `processing` → `speaking` in real time.

## Operator console

`https://<your-space>.hf.space/` (browsers) or `/dashboard` (everyone). Polls the six JSON endpoints every 2 seconds. Six panels:

| # | Panel | Source |
|---|---|---|
| I | Device Status hero | `/sessions/live` |
| II | Transcript | `/transcripts/recent` |
| III | Reminders (48 h chart) | `/reminders/stats` |
| IV | Network | `/network` |
| V | Telemetry | `/metrics` |
| VI | Console log tail | `/logs/recent` |

Aesthetic: audiophile mission-control — warm near-black panel, Fraunces serif (italic for user-quoted speech in the transcript), VU-meter amber and CRT-phosphor accents.

## Configuration cheatsheet

All `JARVIZ_*` env vars are documented in `.env.example`. The ones worth knowing for fleet deployment:

| Variable | Purpose |
|---|---|
| `DEEPSEEK_API_KEY` | Required when `JARVIZ_LLM_PROVIDER=deepseek` (default) |
| `JARVIZ_WS_PUBLIC_URL` | The URL the device dials in to. Must match where the Space is reachable. |
| `JARVIZ_MAX_SESSIONS` | Idle WebSocket cap (default 64) |
| `JARVIZ_MAX_CONCURRENT_TURNS` | Active ASR+LLM+TTS cap (default 8) |
| `JARVIZ_DEVICE_COOLDOWN_S` | Per-device reconnect rate-limit in seconds (default 2.0) |
| `JARVIZ_AUTH_SECRET` | Set to enable HMAC-signed device tokens. Empty = open (LAN dev default) |
| `JARVIZ_WS_PING_INTERVAL_S` / `_TIMEOUT_S` | WebSocket keepalive (default 20/20) |

## Documentation

- [`PROJECT.md`](PROJECT.md) — agent-handoff orientation: code structure, concurrency model, future-work backlog
- [`CHANGELOG.md`](CHANGELOG.md) — reverse-chronological notes on every change

## License

MIT. The upstream `xiaozhi-esp32` protocol is also MIT.
