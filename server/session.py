"""Per-connection session: ties the WebSocket, the MCP bridge, and the
ASR/LLM/TTS providers into one state machine.

State model (simplified — see firmware application.cc for the device side):
  - hello exchange + MCP tool discovery
  - idle: waits for a `listen` message
  - on `listen start manual`: collect Opus mic frames into a buffer
  - on `listen stop`: ASR -> LLM -> TTS, back to idle
  - on `listen detect text=...`: skip ASR, run LLM on the text directly
    (this is the proactive reminder firing path)
  - on `abort`: cancel any in-flight turn

The connection is single-threaded by virtue of asyncio; only one
listen->process->tts cycle runs at a time. If a new event arrives mid-cycle
(rare; usually only `abort`), we cancel the current task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .audio import (
    FRAME_DURATION_MS,
    MIC_SAMPLE_RATE,
    OpusMicDecoder,
    OpusTtsEncoder,
    TTS_FRAME_SAMPLES,
)
from .asr.base import ASRProvider
from .config import settings
from . import live_state
from .llm.base import LLMProvider
from .mcp_bridge import MCPBridge
from .metrics import inc as metrics_inc, record_turn_latency
from .store import touch_device
from .tts.base import TTSProvider

log = logging.getLogger("jarviz.session")

REMINDER_PREFIX = "[Reminder]"

# Cap a single mic capture. Past this we force-stop ASR. 30 seconds is
# generous for "remind me to..." style utterances.
_MAX_MIC_BUFFER_BYTES = MIC_SAMPLE_RATE * 2 * 30


# --- TTS text sanitization ----------------------------------------------
# Belt-and-braces for the system prompt's "no emoji, no markdown" rule.
# Even with the prompt, LLMs sprinkle emoji (a recurring 👋 in DeepSeek-V4
# replies) and markdown asterisks. Edge-TTS silently skips emoji, so the
# user never hears them — but we waste model tokens and the asterisks
# would be vocalized as "asterisk" in some voices. Strip both server-side.

# Unicode emoji + pictographs + symbols. Doesn't catch every codepoint but
# hits the common offenders we see in practice (face/hand/object emoji,
# pictographs, dingbats). Plain text + standard punctuation passes through.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"  # alchemical
    "\U0001F780-\U0001F7FF"  # geometric extended
    "\U0001F800-\U0001F8FF"  # supplemental arrows-c
    "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"  # chess + symbols
    "\U0001FA70-\U0001FAFF"  # symbols and pictographs extended-a
    "\U00002600-\U000026FF"  # miscellaneous symbols (sun, snowflake, etc.)
    "\U00002700-\U000027BF"  # dingbats
    "]+",
    flags=re.UNICODE,
)

# Strip markdown emphasis markers and code fences. Headers and lists are
# usually fine — they collapse to plain words once `*` is removed.
_MARKDOWN_RE = re.compile(r"[*_`]")


def _sanitize_for_tts(text: str) -> str:
    """Remove emoji and markdown noise the LLM sometimes emits despite the
    system prompt. Idempotent. Returns the (possibly-trimmed) plain text."""
    if not text:
        return text
    cleaned = _EMOJI_RE.sub("", text)
    cleaned = _MARKDOWN_RE.sub("", cleaned)
    # Collapse the runs of whitespace that emoji removal can leave behind
    # (e.g. "fresh 👋 here" -> "fresh  here" -> "fresh here").
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _log_text(text: str | None) -> str:
    """Redact free-text in logs when JARVIZ_LOG_MESSAGE_TEXT is off, so a
    production log (and the in-memory ring behind /logs/recent) carries no
    user/assistant PII. The length is kept so lines stay debuggable."""
    if text is None:
        return ""
    if settings.JARVIZ_LOG_MESSAGE_TEXT:
        return text
    # A protocol-violating device could send a non-string `text` (e.g. an int).
    # Don't call len() on it — that TypeError would otherwise escape the RX-log
    # line and tear down the whole session on the redaction path.
    if not isinstance(text, str):
        return f"<redacted {type(text).__name__}>"
    return f"<redacted {len(text)} chars>"


def _redact_msg(msg: dict) -> dict:
    """A copy of an inbound device message with its free-text field redacted
    (for the RX log line) when JARVIZ_LOG_MESSAGE_TEXT is off."""
    if settings.JARVIZ_LOG_MESSAGE_TEXT or "text" not in msg:
        return msg
    clone = dict(msg)
    clone["text"] = _log_text(clone.get("text"))
    return clone


@dataclass
class _SessionState:
    listening: bool = False
    listening_mode: str = "manual"
    mic_buffer: bytearray = field(default_factory=bytearray)
    mic_cap_notified: bool = False  # we send the system event at most once per turn
    history: list[dict] = field(default_factory=list)
    # The provider class that produced `history`. If the user switches
    # JARVIZ_LLM_PROVIDER mid-session the shape of history will not match
    # the new provider — drop it instead of replaying garbage.
    history_owner: type | None = None
    process_task: asyncio.Task | None = None
    closed: bool = False
    discovery_done: asyncio.Event = field(default_factory=asyncio.Event)
    # Echo-guard: when an auto-mode listen.start arrives while a turn is
    # in flight (likely the device hearing its own speaker output through
    # its mic), we silently drop it AND its matching listen.stop/detect.
    # This flag carries the "we suppressed start" decision through to the
    # next stop/detect on the same listen cycle.
    suppress_listen: bool = False


class Session:
    def __init__(
        self,
        ws: WebSocket,
        device_id: str,
        client_id: str,
        user_agent: str,
        llm: LLMProvider,
        asr: ASRProvider,
        tts: TTSProvider,
        turn_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._ws = ws
        self._device_id = device_id
        self._client_id = client_id
        self._user_agent = user_agent
        self._llm = llm
        self._asr = asr
        self._tts = tts
        # Process-wide cap on simultaneously-running ASR+LLM+TTS pipelines.
        # Optional so existing tests can construct a Session without one;
        # when None we skip the gate (effectively unbounded).
        self._turn_sem = turn_semaphore
        self._session_id = uuid.uuid4().hex
        self._state = _SessionState()
        self._mic_decoder = OpusMicDecoder()
        self._tts_encoder = OpusTtsEncoder()
        self._mcp = MCPBridge(self._send_text, self._session_id)
        self._send_lock = asyncio.Lock()  # serialize WS sends

    # ---- low-level send helpers ----

    async def _send_text(self, text: str) -> None:
        async with self._send_lock:
            if self._state.closed or self._ws.application_state != WebSocketState.CONNECTED:
                return
            try:
                await self._ws.send_text(text)
            except Exception as e:  # noqa: BLE001
                log.warning("send_text failed: %s", e)
                self._state.closed = True

    async def _send_bytes(self, data: bytes) -> None:
        async with self._send_lock:
            if self._state.closed or self._ws.application_state != WebSocketState.CONNECTED:
                return
            try:
                await self._ws.send_bytes(data)
            except Exception as e:  # noqa: BLE001
                log.warning("send_bytes failed: %s", e)
                self._state.closed = True

    async def _send_json(self, obj: dict) -> None:
        await self._send_text(json.dumps(obj))

    # ---- public entry point ----

    async def run(self) -> None:
        touch_device(self._device_id, client_id=self._client_id, user_agent=self._user_agent)
        metrics_inc("sessions_opened")
        # Register in the live-state registry so the dashboard's
        # `/sessions/live` shows us with state=idle from the moment we
        # accept the WS, before the device even sends `hello`.
        peer_ip = ""
        try:
            if self._ws.client is not None:
                peer_ip = self._ws.client.host or ""
        except Exception:
            pass
        live_state.session_opened(
            session_id=self._session_id,
            device_id=self._device_id,
            client_id=self._client_id,
            user_agent=self._user_agent,
            peer_ip=peer_ip,
        )
        log.info("Session %s opened for device=%s", self._session_id, self._device_id)

        try:
            await self._handshake()
        except Exception as e:  # noqa: BLE001
            log.exception("Handshake failed: %s", e)
            await self._safe_close()
            return

        # Discover tools in the background; conversation can proceed even if
        # discovery is still going (tool-use rounds will wait briefly).
        discover_task = asyncio.create_task(self._discover_tools())

        try:
            await self._main_loop()
        except WebSocketDisconnect:
            log.info("Session %s: client disconnected", self._session_id)
        except Exception as e:  # noqa: BLE001
            log.exception("Session %s: fatal error: %s", self._session_id, e)
        finally:
            self._state.closed = True
            discover_task.cancel()
            self._mcp.cancel_all("session closed")
            if self._state.process_task and not self._state.process_task.done():
                self._state.process_task.cancel()
            await self._safe_close()
            metrics_inc("sessions_closed")
            live_state.session_closed(self._session_id)

    async def _safe_close(self) -> None:
        if self._ws.application_state in (WebSocketState.CONNECTED, WebSocketState.CONNECTING):
            try:
                await self._ws.close()
            except Exception:
                pass

    # ---- handshake ----

    async def _handshake(self) -> None:
        # Wait for the device's hello (10s budget). The device also has a 10s
        # timeout waiting for our hello.
        try:
            raw = await asyncio.wait_for(self._ws.receive_text(), timeout=10.0)
        except asyncio.TimeoutError as e:
            raise RuntimeError("Timed out waiting for client hello") from e
        msg = json.loads(raw)
        if msg.get("type") != "hello":
            raise RuntimeError(f"Expected hello, got {msg.get('type')!r}")
        log.info("Client hello: version=%s features=%s audio=%s",
                 msg.get("version"), msg.get("features"), msg.get("audio_params"))
        await self._send_json(
            {
                "type": "hello",
                "transport": "websocket",
                "session_id": self._session_id,
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 24000,
                    "frame_duration": 60,
                },
            }
        )

    async def _discover_tools(self) -> None:
        try:
            await self._mcp.initialize()
            await self._mcp.discover_tools()
        except asyncio.TimeoutError as e:
            # Client that doesn't respond to MCP `initialize` (e.g. the test
            # simulator, or a real device that disconnected mid-handshake).
            # Not an internal error — log without a traceback.
            log.warning("Tool discovery timed out: %s", e)
        except asyncio.CancelledError:
            # Session was torn down before discovery completed; not an error.
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Tool discovery failed: %s", e)
        finally:
            # Always release the gate so a turn doesn't hang forever if
            # discovery errored out — better an empty tool list than a stall.
            self._state.discovery_done.set()

    # ---- main loop ----

    async def _main_loop(self) -> None:
        while not self._state.closed:
            message = await self._ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            if "bytes" in message and message["bytes"] is not None:
                self._on_audio_in(message["bytes"])
            elif "text" in message and message["text"] is not None:
                try:
                    await self._on_text_in(json.loads(message["text"]))
                except json.JSONDecodeError:
                    log.warning("Non-JSON text frame: %r", message["text"][:200])

    def _on_audio_in(self, packet: bytes) -> None:
        if not self._state.listening:
            return  # device may briefly send audio after stop; ignore
        try:
            pcm = self._mic_decoder.decode(packet)
        except Exception as e:  # noqa: BLE001
            log.debug("opus decode failed: %s", e)
            return
        if len(self._state.mic_buffer) + len(pcm) > _MAX_MIC_BUFFER_BYTES:
            # Log + notify only once per listen cycle. Without this the log
            # flooded with one warning per dropped 60ms Opus packet — easily
            # hundreds per second when an auto-listen session sat open.
            if not self._state.mic_cap_notified:
                log.warning("Mic buffer cap reached (%ds); dropping remaining audio for this turn", 30)
                self._state.mic_cap_notified = True
                asyncio.create_task(
                    self._send_json(
                        {"type": "system", "event": "mic_cap_hit", "max_seconds": 30}
                    )
                )
            return
        self._state.mic_buffer.extend(pcm)

    async def _on_text_in(self, msg: dict) -> None:
        mtype = msg.get("type")
        # Diagnostic trace of every non-MCP text message from the device.
        # MCP traffic is its own logger and would drown out the listen events
        # we care about for the cut-off-mid-sentence diagnosis.
        if mtype != "mcp":
            log.info("RX %s: %s", mtype, _redact_msg(msg))
        if mtype == "listen":
            await self._on_listen(msg)
        elif mtype == "abort":
            await self._on_abort(msg)
        elif mtype == "mcp":
            payload = msg.get("payload")
            if isinstance(payload, dict):
                self._mcp.on_message(payload)
        elif mtype == "hello":
            log.warning("Unexpected second hello, ignoring")
        else:
            log.debug("Unhandled message type: %s", mtype)

    def _is_turn_active(self) -> bool:
        """True iff a `_process_turn` task is still running (ASR/LLM/TTS
        in progress)."""
        return (
            self._state.process_task is not None
            and not self._state.process_task.done()
        )

    async def _on_listen(self, msg: dict) -> None:
        state = msg.get("state")
        mode = msg.get("mode", "")

        # ---- Echo-guard --------------------------------------------------
        # The device's auto-listen mode runs VAD on the mic. While Jarviz's
        # TTS is playing through the speaker, the mic picks up the speaker
        # output, VAD trips, and the device sends listen.start -> audio ->
        # listen.stop. That listen.stop would cancel the in-flight TTS via
        # _spawn_process, cutting the sentence off mid-word and starting a
        # new ASR pass on garbage echo. The result is "Jarviz never finishes
        # a sentence."
        #
        # Rule: while a turn is in flight, drop ALL device-initiated listen
        # events EXCEPT a manual BOOT press. A manual press is the user
        # explicitly choosing to interrupt — that one we honor (the
        # subsequent listen.stop cancels the current turn cleanly).
        if self._is_turn_active():
            is_manual_press = state == "start" and mode == "manual"
            if not is_manual_press:
                if state == "start":
                    # Mark the cycle so the matching stop/detect (which
                    # arrives later) is also dropped instead of spawning
                    # an empty turn that cancels the current one.
                    self._state.suppress_listen = True
                    log.info(
                        "Echo-guard: suppressed listen.start mode=%s during in-flight turn",
                        mode or "?",
                    )
                elif state in ("stop", "detect") and self._state.suppress_listen:
                    self._state.suppress_listen = False
                    log.debug(
                        "Echo-guard: dropped listen.%s (matching suppressed start)", state
                    )
                else:
                    # Unmatched stop/detect during in-flight turn (rare —
                    # would happen if the start arrived before the turn
                    # began). Drop it too — the outer turn is finishing
                    # and a redundant cancel just adds churn.
                    log.info("Echo-guard: dropped unmatched listen.%s during in-flight turn", state)
                return

        if state == "start":
            self._state.listening = True
            self._state.listening_mode = mode or "manual"
            self._state.mic_buffer = bytearray()
            self._state.mic_cap_notified = False
            self._state.suppress_listen = False
            log.info("Listen start mode=%s", self._state.listening_mode)
            live_state.set_state(self._session_id, live_state.STATE_LISTENING)
        elif state == "stop":
            self._state.listening = False
            buf = bytes(self._state.mic_buffer)
            self._state.mic_buffer = bytearray()
            log.info("Listen stop, %d bytes captured", len(buf))
            # ASR/LLM/TTS pipeline starts now → dashboard shows "processing".
            live_state.set_state(self._session_id, live_state.STATE_PROCESSING)
            self._spawn_process(captured_pcm=buf, source_text=None)
        elif state == "detect":
            text = (msg.get("text") or "").strip()
            log.info("Listen detect: %r", _log_text(text))
            if text.startswith(REMINDER_PREFIX):
                # Bump the dashboard's "reminder fired" counter — this is
                # the proactive path where ReminderManager::Tick on the
                # device fired a stored reminder.
                live_state.record_reminder_fired()
            live_state.set_state(self._session_id, live_state.STATE_PROCESSING)
            # Proactive utterance from the device (e.g. reminder firing). We
            # treat it as the user input directly, no ASR.
            self._spawn_process(captured_pcm=None, source_text=text)
        else:
            log.debug("Unknown listen.state=%s", state)

    async def _on_abort(self, msg: dict) -> None:
        log.info("Abort received: %s", msg.get("reason"))
        if self._state.process_task and not self._state.process_task.done():
            self._state.process_task.cancel()

    # ---- per-turn pipeline: ASR -> LLM -> TTS ----

    def _spawn_process(self, captured_pcm: bytes | None, source_text: str | None) -> None:
        # Cancel any in-flight turn before starting a new one.
        if self._state.process_task and not self._state.process_task.done():
            self._state.process_task.cancel()
        # Clean slate for the echo-guard: a suppressed-start flag must never
        # leak across a turn boundary and swallow the next genuine listen.stop.
        self._state.suppress_listen = False
        self._state.process_task = asyncio.create_task(
            self._process_turn(captured_pcm, source_text)
        )

    async def _process_turn(self, captured_pcm: bytes | None, source_text: str | None) -> None:
        # `_stream_tts` owns the `tts stop` send: its `finally` runs even when
        # the turn is cancelled mid-stream. The outer guard here only fires
        # when we cancel BEFORE entering `_stream_tts` AND we had already
        # told the device `tts start`. In that case the device is in
        # Speaking state with no closing message coming — without this send
        # the device hangs.
        cancelled = False
        tts_entered = False
        # Per-stage timing so regressions in any one stage are visible at
        # a glance in the logs. Stages we didn't enter stay at 0.0.
        turn_started = time.monotonic()
        t_asr_ms = t_llm_ms = t_tts_ms = 0.0
        metrics_inc("turns_started")
        # Transcript fields populated as the turn progresses; captured into
        # live_state at the end so the dashboard renders the back-and-forth.
        user_text_for_log = ""
        assistant_text_for_log = ""
        tool_calls_for_log: list[str] = []
        overloaded = False
        try:
            user_text: str
            if source_text is not None:
                user_text = source_text
            elif captured_pcm:
                t0 = time.monotonic()
                try:
                    user_text = await self._asr.transcribe(captured_pcm, MIC_SAMPLE_RATE)
                except Exception as e:  # noqa: BLE001
                    log.exception("ASR failed: %s", e)
                    metrics_inc("asr_failed")
                    user_text = ""
                finally:
                    # In `finally` so cancellation mid-ASR still reports the
                    # elapsed time rather than silently logging 0ms.
                    t_asr_ms = (time.monotonic() - t0) * 1000.0
                if user_text:
                    await self._send_json({"type": "stt", "text": user_text})
            else:
                user_text = ""

            user_text_for_log = user_text
            if not user_text.strip():
                log.info("Empty user_text; skipping LLM call")
                return

            # Wait for tool discovery to finish before invoking the LLM.
            # Without this, the very first turn races with the discovery
            # task and the LLM gets an empty tool list, so it can't do
            # anything useful (e.g. it'll ask for the user's name in plain
            # text instead of calling jarviz.get_user_name).
            try:
                await asyncio.wait_for(self._state.discovery_done.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("Tool discovery did not finish in 5s; proceeding without tools")

            # History across turns is provider-specific in shape; if the
            # backing provider has changed since we last stored history
            # (e.g. someone edited JARVIZ_LLM_PROVIDER and reloaded), drop
            # the old replay rather than feed an Anthropic-shaped message
            # list to OpenAI or vice versa.
            llm_type = type(self._llm)
            if self._state.history and self._state.history_owner is not llm_type:
                log.info(
                    "LLM provider changed (%s -> %s); clearing history",
                    self._state.history_owner.__name__ if self._state.history_owner else None,
                    llm_type.__name__,
                )
                self._state.history = []
            self._state.history_owner = llm_type

            # Acquire a process-wide turn slot. This is the only place we
            # gate LLM+TTS load. ASR has its own (thread-level) semaphore.
            # When the cap is hit, we wait briefly; if still no slot, we
            # short-circuit to a polite overload reply instead of holding
            # the device in Listening for an unbounded amount of time.
            slot_acquired = False
            # `overloaded` is hoisted to the function-top scope so the
            # final transcript record can read it from the outer finally.
            if self._turn_sem is not None:
                queue_t0 = time.monotonic()
                try:
                    await asyncio.wait_for(
                        self._turn_sem.acquire(),
                        timeout=settings.JARVIZ_TURN_QUEUE_TIMEOUT_S,
                    )
                    slot_acquired = True
                    queued_ms = (time.monotonic() - queue_t0) * 1000.0
                    if queued_ms > 50:
                        log.info("Turn queued for %.0fms before slot freed", queued_ms)
                except asyncio.TimeoutError:
                    log.warning(
                        "Turn-queue full after %ss; sending overload reply",
                        settings.JARVIZ_TURN_QUEUE_TIMEOUT_S,
                    )
                    overloaded = True
                    metrics_inc("turns_overloaded")

            try:
                if overloaded:
                    assistant_text = (
                        "Sorry, I'm a bit overloaded right now. Please try again in a moment."
                    )
                    new_history = self._state.history
                else:
                    tools = self._mcp.tools
                    t0 = time.monotonic()
                    # Wrap the MCP call so we can both (a) record reminder-
                    # related tool calls in the live-state counters used by
                    # the dashboard chart, and (b) append the tool name to
                    # the transcript record.
                    async def _traced_invoke(name: str, args: dict):
                        tool_calls_for_log.append(name)
                        live_state.record_tool_call(name)
                        return await self._mcp.call_tool(name, args)
                    try:
                        assistant_text, new_history = await self._llm.respond(
                            user_text=user_text,
                            tools=tools,
                            invoke_tool=_traced_invoke,
                            history=self._state.history,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.exception("LLM failed: %s", e)
                        metrics_inc("llm_failed")
                        assistant_text = "Sorry, something went wrong on my end."
                        new_history = self._state.history
                    finally:
                        # Capture in `finally` so cancellation mid-LLM still reports a
                        # real elapsed time. Otherwise CancelledError skips the line
                        # and the latency log shows llm=0ms misleadingly.
                        t_llm_ms = (time.monotonic() - t0) * 1000.0

                self._state.history = new_history
                # Sanitize before logging + speaking so the log line shows
                # exactly what the user will hear. We deliberately do NOT
                # sanitize the copy that goes into history — keeping the
                # original text in context lets the model see its own
                # earlier (sometimes emoji-laden) outputs verbatim for any
                # future-turn reference, but it never reaches the TTS path.
                spoken_text = _sanitize_for_tts(assistant_text)
                if spoken_text != assistant_text:
                    log.debug("TTS sanitizer trimmed %d -> %d chars",
                              len(assistant_text), len(spoken_text))
                log.info("Assistant -> %r", _log_text(spoken_text))
                assistant_text_for_log = spoken_text

                t0 = time.monotonic()
                tts_entered = True
                live_state.set_state(self._session_id, live_state.STATE_SPEAKING)
                try:
                    frames = await self._stream_tts(spoken_text)
                    # We had text to say but no audio reached the device — an
                    # upstream TTS failure (e.g. Edge outage). Count it so the
                    # operator sees a TTS error rate, not just silence.
                    if spoken_text.strip() and not frames:
                        metrics_inc("tts_failed")
                except Exception as e:  # noqa: BLE001
                    log.exception("TTS streaming failed at session level: %s", e)
                    metrics_inc("tts_failed")
                finally:
                    # Captured in `finally` so a mid-TTS abort still records the
                    # time spent streaming rather than reporting 0ms.
                    t_tts_ms = (time.monotonic() - t0) * 1000.0
            finally:
                # Release the slot the moment LLM+TTS are done, even if we
                # were cancelled. The slot must NOT be held across the
                # post-TTS reminder-close sleep below.
                if slot_acquired and self._turn_sem is not None:
                    self._turn_sem.release()

            # If the turn was triggered by a `detect` (proactive reminder),
            # close the channel cleanly. The device's WakeWordInvoke flow
            # leaves it in auto-listening mode otherwise, which would sit
            # with the mic open forever.
            if source_text and source_text.startswith(REMINDER_PREFIX):
                # With real-time pacing, up to JARVIZ_TTS_JITTER_BUFFER_MS of
                # audio is still buffered on the device when _stream_tts
                # returns. Closing the channel doesn't clear the device's
                # queue (it drains regardless of state), but wait out the
                # cushion plus a margin so the close can never race the tail.
                drain_s = settings.JARVIZ_TTS_JITTER_BUFFER_MS / 1000.0 + 0.3
                await asyncio.sleep(drain_s)
                await self._safe_close()
        except asyncio.CancelledError:
            log.info("Turn cancelled mid-flight")
            cancelled = True
            raise
        finally:
            # `_stream_tts` always sends its own `tts stop` from its `finally`
            # block (even on cancel), so emit a stop here only when we
            # cancelled BEFORE entering `_stream_tts`. Sending one in both
            # places used to duplicate the message on every cancel-during-TTS.
            if cancelled and not tts_entered:
                try:
                    await self._send_json({"type": "tts", "state": "stop"})
                except Exception:
                    pass
            total_ms = (time.monotonic() - turn_started) * 1000.0
            log.info(
                "turn_latency total=%.0fms asr=%.0fms llm=%.0fms tts=%.0fms cancelled=%s",
                total_ms,
                t_asr_ms,
                t_llm_ms,
                t_tts_ms,
                cancelled,
            )
            metrics_inc("turns_cancelled" if cancelled else "turns_completed")
            record_turn_latency(
                total_ms=total_ms,
                asr_ms=t_asr_ms,
                llm_ms=t_llm_ms,
                tts_ms=t_tts_ms,
            )
            # Push the completed turn into the dashboard transcript ring.
            # Only when we actually had user text — empty/skipped turns
            # would just be noise.
            if user_text_for_log.strip() or assistant_text_for_log.strip():
                live_state.add_transcript(
                    device_id=self._device_id,
                    session_id=self._session_id,
                    user_text=user_text_for_log,
                    assistant_text=assistant_text_for_log,
                    tool_calls=tool_calls_for_log,
                    latency_ms={
                        "total": round(total_ms),
                        "asr": round(t_asr_ms),
                        "llm": round(t_llm_ms),
                        "tts": round(t_tts_ms),
                    },
                    cancelled=cancelled,
                    overloaded=overloaded,
                )
            # The pipeline is done; device is back to idle from the
            # dashboard's perspective. (`session_closed` clears the row
            # entirely if/when the WS drops.)
            live_state.set_state(self._session_id, live_state.STATE_IDLE)

    async def _stream_tts(self, text: str) -> int:
        """Stream `text` as paced Opus frames. Returns the number of audio
        frames sent (0 means nothing was synthesized — e.g. an upstream TTS
        failure — which the caller treats as a TTS error)."""
        if not text.strip():
            return 0
        await self._send_json(
            {"type": "tts", "state": "sentence_start", "text": text}
        )
        await self._send_json({"type": "tts", "state": "start"})

        # Buffer PCM into 60ms-aligned blocks before encoding so opus encoder
        # always gets a full frame.
        bytes_per_frame = TTS_FRAME_SAMPLES * 2
        leftover = bytearray()
        frames_sent = 0
        bytes_sent = 0
        tts_t0 = time.monotonic()

        # --- Real-time pacing -------------------------------------------------
        # Without this we'd flush the whole reply (encoded faster than
        # real-time) at the device in one burst. The firmware's decode queue
        # only holds ~2.4s and drops everything past that, so a long reply gets
        # cut off mid-sentence. Gate each frame so we never run more than
        # `lead_seconds` ahead of its real-time playout point: the first
        # `lead_seconds` of audio burst out to prime the device's jitter
        # buffer, then we settle to one frame per `frame_seconds`. The schedule
        # is absolute (off `tts_t0`) so per-frame send cost can't accumulate
        # into drift.
        pace = settings.JARVIZ_TTS_PACING_ENABLED
        frame_seconds = FRAME_DURATION_MS / 1000.0
        lead_seconds = max(0.0, settings.JARVIZ_TTS_JITTER_BUFFER_MS / 1000.0)

        async def _emit(opus: bytes) -> None:
            nonlocal frames_sent, bytes_sent
            # Pace BEFORE the send. The sleep is a cancellation point, so an
            # abort / barge-in stops the stream within ~one frame instead of
            # after the full flood. It is also outside `_send_bytes` so it
            # never holds the WS send lock.
            if pace:
                target = tts_t0 + frames_sent * frame_seconds - lead_seconds
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            await self._send_bytes(opus)
            frames_sent += 1
            bytes_sent += len(opus)

        try:
            async for pcm_chunk in self._tts.synthesize(text):
                leftover.extend(pcm_chunk)
                while len(leftover) >= bytes_per_frame:
                    frame = bytes(leftover[:bytes_per_frame])
                    del leftover[:bytes_per_frame]
                    for opus in self._tts_encoder.encode_pcm(frame):
                        await _emit(opus)
            if leftover:
                # Encode the trailing partial frame (encoder pads internally).
                for opus in self._tts_encoder.encode_pcm(bytes(leftover)):
                    await _emit(opus)
        except Exception as e:  # noqa: BLE001
            log.exception("TTS streaming failed: %s", e)
        finally:
            elapsed = (time.monotonic() - tts_t0) * 1000.0
            log.info(
                "TTS streamed %d frames (%d bytes) over %.0fms — %.1fs of audio",
                frames_sent, bytes_sent, elapsed,
                frames_sent * 60 / 1000.0,
            )
            await self._send_json({"type": "tts", "state": "stop"})
        return frames_sent
