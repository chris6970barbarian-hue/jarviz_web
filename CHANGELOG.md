# Backend Changelog

Reverse-chronological. Each entry summarizes the user-visible or operator-visible
change. For details, see the linked commit.

## Unreleased

### Operator console refresh (v2)
- **Audiophile mission-control aesthetic.** `dashboard.py` rewritten from
  the spartan grid into a designed interface: warm near-black panel,
  Fraunces serif for the brand and giant state words (with italic for
  user-quoted speech in the transcript), VU-meter amber + CRT phosphor
  accents, Roman-numeral section labels, pulsing pilot-light state
  indicators sized to be readable across the room.
- **Six panels** wired to the new endpoints:
    I.   Device Status hero       (`/sessions/live`)
    II.  Editorial Transcript     (`/transcripts/recent`)
    III. 48 h Reminders chart     (`/reminders/stats`)
    IV.  Network panel            (`/network`)
    V.   Telemetry strip          (`/metrics`)
    VI.  Console log tail         (`/logs/recent`)
- All panels render gracefully when empty (no devices / no transcripts
  / no reminders) — never a blank frame. Pause button freezes polling
  and dims the transcript to make the frozen state obvious. Mobile
  grid collapses cleanly at 720 / 600 px breakpoints.

### Live state plumbing for the dashboard
- New `server/live_state.py`: per-session device state tracker (idle /
  listening / processing / speaking with `state_age_s`), a 100-entry
  ring of completed conversation turns, cumulative reminder counters,
  and a 48-hour hourly time series for the chart. All in-memory,
  single-worker, thread-safe.
- `session.py` now sets the state at every transition: LISTENING on
  `listen.start mode=manual`, PROCESSING on `listen.stop` and on a
  `listen.detect`, SPEAKING when the TTS phase begins, IDLE at the
  end of every turn. Each completed turn is appended to the transcript
  ring with both sides + tool calls + per-stage latency + cancelled /
  overloaded flags. Reminder tool-calls and `[Reminder]` detect events
  bump their respective counters.
- New JSON endpoints (`server/ota.py`):
  - `GET /sessions/live` — currently-open WS sessions + state
  - `GET /transcripts/recent?n=N` — last N completed turns
  - `GET /reminders/stats` — counters + 48 hourly buckets (zero-filled)
  - `GET /network` — hostname, LAN addresses, WS public URL, peer IPs

### Diagnostic logging (pre-work for the cut-off-mid-sentence task)
- `session.py` now logs every non-MCP RX text message (`RX listen: ...`)
  so the firmware's listen-event timing is visible without a packet
  capture. `_stream_tts` logs a per-stream summary line
  (`TTS streamed N frames (B bytes) over Tms — Ss of audio`) so any
  truncation downstream of the encoder is easy to spot.

### Operator dashboard at /dashboard
- Single-page HTML monitoring UI served from `GET /dashboard` (also at
  `/` when the request's `Accept` header includes `text/html`, so a
  browser visit to the root drops you straight into the dashboard while
  health probes / OTA still see the JSON they expect).
- Polls `/metrics` and `/logs/recent` every 2 seconds. Renders top-line
  stats (active sessions, in-flight turns, latency p50/p95), counters
  for sessions / turns / OTA / WS (with reject categories highlighted
  when non-zero), a sparkline of the last 50 turn-total latencies, and
  a colour-coded live tail of the log (errors red, warnings amber,
  debug grey, info plain).
- New `/logs/recent?n=N` endpoint backed by an in-memory `_RingHandler`
  in `log.py` (cap 500 records, ~100 KB). Survives stdout-only
  deployments where there's no on-disk `backend.log` to tail.
- New `latency_ms.recent_totals` field on `/metrics` exposes the last
  50 turn-total latencies as a flat list, so the dashboard sparkline
  doesn't have to scrape it from log lines.
- Zero build pipeline: HTML+CSS+JS is a Python string in
  `server/dashboard.py`. No StaticFiles mount, no npm. Mobile-friendly,
  dark theme, monospace, pause toggle, reconnect-on-fail status dot.

### Echo-guard — fixes "Jarviz never finishes a sentence"
- **Root cause** identified from backend logs: with the device's
  on-board VAD-driven auto-listen mode active, the speaker's TTS
  output bleeds into the mic, the device VAD trips, and a
  `listen.start mode=auto` -> `listen.stop` arrives at the backend
  while TTS is still streaming. `_spawn_process` then cancels the
  in-flight turn to handle the new "input" (which is just garbage
  speaker echo). Result: every reply gets cut off mid-sentence
  and the LLM is fed a stream of "did I hear you say a-a-a-a?"
  garbage.
- **Fix in `_on_listen`**: while a turn is in flight, drop ALL
  device-initiated listen events EXCEPT a manual BOOT press
  (`mode=manual` on `listen.start`). The matching listen.stop /
  listen.detect of a suppressed start cycle is also dropped via
  a `_state.suppress_listen` flag so it can't accidentally spawn
  an empty turn that cancels the current one. Manual press still
  cancels mid-TTS so users can deliberately interrupt the assistant.
- Verified with a probe that injects `listen.start mode=auto` +
  `listen.stop` during TTS: backend logged `Echo-guard: suppressed`
  and the turn finished `cancelled=False`. Before the fix the same
  probe cancelled the turn mid-stream.
- Bonus: the **mic-buffer-cap log line** now fires once per listen
  cycle instead of once per dropped Opus packet (was producing
  hundreds of warnings per second when an auto-listen session sat
  open past the 30s cap).

### User-name handling
- **No more hardcoded "Chris" anywhere in code.** Every `Assistant ->`
  log line saying "Hello Chris" in earlier sessions was caused by the
  simulator's `jarviz.get_user_name` stub returning `{"name": "Chris", ...}`
  unconditionally. The simulator now mirrors the firmware's NVS-backed
  behavior: a fresh checkout (no `data/sim_user_name.txt`) returns the
  empty string from `get_user_name`, which triggers the LLM's "ask for
  your name -> call set_user_name" branch. `set_user_name(name)` persists
  the value to `data/sim_user_name.txt` (gitignored) for future runs —
  exactly the same UX as a real device persisting to NVS.
- New `--user-name <name>` CLI flag on `scripts/simulate_device.py` to
  pre-seed the persisted name for stress tests that don't want to do the
  conversational onboarding. `--user-name ""` clears it (back to
  fresh-device path).
- Prompt's `[Reminder]` example no longer uses the specific name "Chris";
  it now spells out the substitution slot (`Hey <name>, time to <text>.`)
  and the empty-name fallback (`Hey there, ...`).

### Prompt + TTS hygiene
- **Tightened `prompts/jarviz.md`.** Every recent DeepSeek-V4-flash reply
  emitted at least one waving-hand emoji and self-identified as "I'm a
  reminder appliance/butler" on every off-topic answer. The greeting
  "Good evening, Chris" was prepended to every turn even after tool
  confirmations. Reworked the prompt:
  * Off-topic clause now: "just answer briefly, no disclaimer." Concrete
    example "what is 2+2? -> Four." (not "Four. I'm a reminder appliance.").
  * Greeting rule: only on the FIRST reply of a session; skip for tool-
    confirmation replies and for `[Reminder]` firings (those have their
    own opener).
  * TTS section spells out "NO emoji. None. No 👋, no 🤖, no any."
    explicitly with examples of what NOT to emit.
- **Server-side TTS text sanitizer.** Belt-and-braces in case the model
  ignores the prompt: `_sanitize_for_tts` in session.py strips Unicode
  emoji ranges (pictographs, emoticons, dingbats, supplemental symbols,
  miscellaneous symbols) and markdown emphasis markers (`*`, `_`, `` ` ``)
  before the assistant text reaches Edge-TTS. Idempotent, empty-safe.
  The `Assistant -> ...` log now shows the sanitized text so the line
  matches what the user actually hears. Verified across 9 unit cases
  and 3 live probes: replies dropped from "Chris, four 👋 but I'm just
  a reminder appliance..." to "Four." with no other side effects.

### Scaling
- **`/metrics` endpoint.** New `GET /metrics` returns a JSON snapshot of
  in-memory counters and the p50/p95 of the last 200 turn latencies.
  Tracks: OTA requests + cooldown rejects; WS accepts + cap/auth/cooldown
  rejects; sessions opened/closed/active; turns started/completed/cancelled/
  overloaded + in_flight; per-stage latency percentiles. Per-worker only —
  multi-worker deployments will need to aggregate externally. Counters
  guarded by a single lock for cross-thread safety; latency ring uses
  `collections.deque(maxlen=200)`.
- **Simulator device-id randomized.** `scripts/simulate_device.py` used to
  hard-code `AA:BB:CC:DD:EE:FF`, which meant N parallel simulator runs
  were correctly seen as one device and N-1 got rejected by the per-device
  cooldown. Randomized per-run so stress tests now scale.
- **Interprocess-safe device store.** `store.py` now wraps its writes
  with `filelock.FileLock` in addition to the existing `threading.Lock`.
  Without this, scaling to `uvicorn --workers N > 1` would cause two
  workers to race on `devices.json` and silently lose an update.
  `filelock` was already a transitive dependency (huggingface_hub); now
  pinned explicitly in `requirements.txt`.
- **Per-device cooldown.** New `JARVIZ_DEVICE_COOLDOWN_S` (default 2.0)
  rate-limits the OTA and WS endpoints by Device-Id. A device that
  reconnects faster than this gets HTTP 429 from OTA (with
  `Retry-After`) or WS 1008 from the WebSocket. Protects against
  reboot-loop devices burning LLM/quota budget. In-memory only —
  scaling to multiple workers will need Redis.
- **Separate WS-session cap from turn-pipeline cap.** Previously one
  `JARVIZ_MAX_SESSIONS` (default 8) gated both idle WebSocket sessions
  and concurrent ASR+LLM+TTS pipelines. They have very different costs
  — an idle WS is a buffer per socket; a running turn is several CPU-
  seconds. Split into two: `JARVIZ_MAX_SESSIONS` (now defaults to 64,
  covers idle connections) and `JARVIZ_MAX_CONCURRENT_TURNS` (default
  8, gates active pipelines). Turns over the active cap wait up to
  `JARVIZ_TURN_QUEUE_TIMEOUT_S` (default 5) for a slot; if still full
  they send a polite "I'm overloaded" reply instead of holding the
  device in Listening forever. Verified with a 12-turn-vs-8-slot
  overload probe: 8 ran normally, 4 got the overload reply, 0 hung.
- **ASR thread-safety.** `model.transcribe()` is now guarded by a
  `threading.Semaphore` sized by `JARVIZ_ASR_PARALLELISM` (default 1).
  faster-whisper's CTranslate2 Generator is not thread-safe with a single
  model instance — without this, two concurrent sessions would eventually
  corrupt the model state. The segment generator is fully materialized
  inside the critical section so lazy decoding doesn't escape the guard.

### Diagnostics
- **LLM cancellation verified clean.** Direct probe shows the openai SDK
  + httpx + asyncio cancellation chain propagates in ~0 ms. The previous
  "wasted DeepSeek tokens" hypothesis was a misdiagnosis: cancelled turns
  *do* abort the upstream HTTP request promptly; the symptom was caused by
  slow WS disconnect detection, fixed earlier via configurable
  `JARVIZ_WS_PING_INTERVAL_S` / `JARVIZ_WS_PING_TIMEOUT_S`.

## 2026-05-14 — Initial commit

Snapshot of the prototype after the code-review pass and real-device
end-to-end verification. See the initial commit message for the full
feature list.
