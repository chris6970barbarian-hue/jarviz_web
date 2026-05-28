"""Live operational state for the dashboard.

Three things kept here (all in-memory, single-worker):

1.  Per-session device state — what each connected Jarviz is doing
    *right now* (idle / listening / processing / speaking). Set by the
    session as it walks the WS protocol.
2.  Conversation transcripts — a ring of recent completed turns so the
    dashboard can render the recent back-and-forth without scraping logs.
3.  Reminder counters + a per-hour time series — enough data for the
    dashboard's reminders chart without bringing in a real timeseries db.

All updates take the module lock, so callers from the asyncio loop and
from worker threads (ASR) cooperate safely. Reads are cheap copies; the
endpoints serialize directly.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

# How many completed turns the dashboard shows.
_TRANSCRIPT_RING = 100
# Hourly buckets retained for the reminder chart. 48 = 2 days of context.
_HOURLY_BUCKETS = 48


# ---- Per-session device state -----------------------------------------------

# These names match what the firmware's StateMachine emits; the dashboard
# just shows them verbatim with a colour-coded chip.
STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_PROCESSING = "processing"  # backend running ASR + LLM after listen.stop
STATE_SPEAKING = "speaking"


@dataclass
class _SessionInfo:
    session_id: str
    device_id: str
    client_id: str
    user_agent: str
    peer_ip: str
    opened_at: float
    state: str = STATE_IDLE
    state_changed_at: float = field(default_factory=time.time)


_lock = threading.Lock()
_sessions: dict[str, _SessionInfo] = {}  # session_id -> info
_transcripts: deque[dict] = deque(maxlen=_TRANSCRIPT_RING)

# Reminder activity. Cumulative counters + a rolling per-hour series.
# Bucket key = `hour_floor_unix_seconds` (e.g. 1700000000 // 3600 * 3600).
_reminder_counts = {
    "created_total": 0,        # any of create_reminder / create_reminder_relative
    "deleted_total": 0,        # delete_reminder
    "listed_total": 0,         # list_reminders
    "fired_total": 0,          # device sent listen.detect with [Reminder] prefix
}
_reminder_buckets: dict[int, dict[str, int]] = {}


def _bucket_for(ts: float) -> int:
    return int(ts) - (int(ts) % 3600)


def _trim_buckets() -> None:
    """Drop buckets older than _HOURLY_BUCKETS hours so memory stays bounded."""
    cutoff = _bucket_for(time.time()) - _HOURLY_BUCKETS * 3600
    stale = [k for k in _reminder_buckets if k < cutoff]
    for k in stale:
        del _reminder_buckets[k]


# ---- Session lifecycle --------------------------------------------------

def session_opened(
    *,
    session_id: str,
    device_id: str,
    client_id: str,
    user_agent: str,
    peer_ip: str,
) -> None:
    info = _SessionInfo(
        session_id=session_id,
        device_id=device_id,
        client_id=client_id,
        user_agent=user_agent,
        peer_ip=peer_ip,
        opened_at=time.time(),
    )
    with _lock:
        _sessions[session_id] = info


def session_closed(session_id: str) -> None:
    with _lock:
        _sessions.pop(session_id, None)


def set_state(session_id: str, new_state: str) -> None:
    with _lock:
        info = _sessions.get(session_id)
        if info is None:
            return
        if info.state == new_state:
            return
        info.state = new_state
        info.state_changed_at = time.time()


def live_sessions() -> list[dict]:
    with _lock:
        out = []
        now = time.time()
        for info in _sessions.values():
            out.append({
                "session_id": info.session_id,
                "device_id": info.device_id,
                "client_id": info.client_id,
                "user_agent": info.user_agent,
                "peer_ip": info.peer_ip,
                "opened_at": info.opened_at,
                "session_age_s": round(now - info.opened_at, 1),
                "state": info.state,
                "state_age_s": round(now - info.state_changed_at, 1),
            })
    out.sort(key=lambda x: x["opened_at"])
    return out


# ---- Transcripts --------------------------------------------------------

def add_transcript(
    *,
    device_id: str,
    session_id: str,
    user_text: str,
    assistant_text: str,
    tool_calls: list[str],
    latency_ms: dict,
    cancelled: bool,
    overloaded: bool,
) -> None:
    entry = {
        "ts": time.time(),
        "device_id": device_id,
        "session_id": session_id,
        "user_text": (user_text or "").strip(),
        "assistant_text": (assistant_text or "").strip(),
        "tool_calls": list(tool_calls or []),
        "latency_ms": dict(latency_ms or {}),
        "cancelled": bool(cancelled),
        "overloaded": bool(overloaded),
    }
    with _lock:
        _transcripts.append(entry)


def recent_transcripts(n: int = 50) -> list[dict]:
    n = max(1, min(int(n), _TRANSCRIPT_RING))
    with _lock:
        return list(_transcripts)[-n:]


# ---- Reminder activity --------------------------------------------------

def record_tool_call(name: str) -> None:
    """Called from the LLM provider's tool-invocation path with the
    canonical tool name (e.g. `jarviz.create_reminder_relative`)."""
    if not name:
        return
    bucket = _bucket_for(time.time())
    with _lock:
        slot = _reminder_buckets.setdefault(
            bucket, {"created": 0, "deleted": 0, "listed": 0, "fired": 0}
        )
        if name in ("jarviz.create_reminder", "jarviz.create_reminder_relative"):
            _reminder_counts["created_total"] += 1
            slot["created"] += 1
        elif name == "jarviz.delete_reminder":
            _reminder_counts["deleted_total"] += 1
            slot["deleted"] += 1
        elif name == "jarviz.list_reminders":
            _reminder_counts["listed_total"] += 1
            slot["listed"] += 1
        _trim_buckets()


def record_reminder_fired() -> None:
    """Called when the device sends a listen.detect with the [Reminder]
    prefix — i.e. ReminderManager::Tick fired a stored reminder."""
    bucket = _bucket_for(time.time())
    with _lock:
        slot = _reminder_buckets.setdefault(
            bucket, {"created": 0, "deleted": 0, "listed": 0, "fired": 0}
        )
        _reminder_counts["fired_total"] += 1
        slot["fired"] += 1
        _trim_buckets()


def reminder_snapshot() -> dict:
    """Counters + a flat hourly series spanning the last N hours, with
    zero-fill so the chart can render straight bars without gaps."""
    with _lock:
        counts = dict(_reminder_counts)
        now_bucket = _bucket_for(time.time())
        series: list[dict] = []
        for i in range(_HOURLY_BUCKETS - 1, -1, -1):
            b = now_bucket - i * 3600
            slot = _reminder_buckets.get(
                b, {"created": 0, "deleted": 0, "listed": 0, "fired": 0}
            )
            series.append({"bucket": b, **slot})
    return {"counters": counts, "hourly": series}
