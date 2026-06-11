"""Lightweight in-memory metrics.

Designed for a single uvicorn worker — counters and a ring buffer of the
last N per-turn latencies. The `/metrics` endpoint reports both as JSON
so an operator can see the live picture without standing up Prometheus.

If you later scale to multiple workers, this becomes per-worker (each
worker has its own counters). At that point migrate to prometheus_client
with a multi-process collector, or push to statsd.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

_LATENCY_RING_SIZE = 200  # last N turns kept for p50/p95 stats


@dataclass
class _Counters:
    process_started_at: float = field(default_factory=time.monotonic)

    # Cumulative counts (since process start). Atomic enough under the GIL
    # for the read-modify-write we do; if we ever switch to free-threaded
    # CPython, wrap them in a lock.
    ota_requests_total: int = 0
    ota_cooldown_rejected: int = 0

    ws_connects_accepted: int = 0
    ws_connects_cap_rejected: int = 0
    ws_connects_auth_rejected: int = 0
    ws_connects_cooldown_rejected: int = 0

    sessions_opened: int = 0
    sessions_closed: int = 0

    turns_started: int = 0
    turns_completed: int = 0
    turns_cancelled: int = 0
    turns_overloaded: int = 0  # got the canned overload reply

    # Per-stage failures — lets an operator distinguish a healthy server from
    # one quietly failing every LLM/TTS call (which otherwise only shows as a
    # gap between started and completed). Wire an alert on the rate of these.
    asr_failed: int = 0
    llm_failed: int = 0
    tts_failed: int = 0

    # Most recent N turn latencies (as (total_ms, asr_ms, llm_ms, tts_ms)).
    recent_latencies: deque = field(
        default_factory=lambda: deque(maxlen=_LATENCY_RING_SIZE)
    )


_C = _Counters()
_LOCK = threading.Lock()


def inc(name: str, by: int = 1) -> None:
    """Increment a named counter. Unknown names are silently ignored so
    a typo in caller code can't take the server down."""
    with _LOCK:
        if hasattr(_C, name):
            cur = getattr(_C, name)
            setattr(_C, name, cur + by)


def record_turn_latency(
    *, total_ms: float, asr_ms: float, llm_ms: float, tts_ms: float
) -> None:
    with _LOCK:
        _C.recent_latencies.append((total_ms, asr_ms, llm_ms, tts_ms))


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    # Nearest-rank, good enough for an operator dashboard.
    idx = max(0, min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1)))))
    return s[idx]


def snapshot() -> dict:
    """Return a JSON-serializable picture of the current counters and a
    summary of the recent-latency ring."""
    with _LOCK:
        uptime = time.monotonic() - _C.process_started_at
        lats = list(_C.recent_latencies)
        out: dict = {
            "uptime_s": round(uptime, 1),
            "ota": {
                "requests_total": _C.ota_requests_total,
                "cooldown_rejected": _C.ota_cooldown_rejected,
            },
            "ws": {
                "connects_accepted": _C.ws_connects_accepted,
                "cap_rejected": _C.ws_connects_cap_rejected,
                "auth_rejected": _C.ws_connects_auth_rejected,
                "cooldown_rejected": _C.ws_connects_cooldown_rejected,
            },
            "sessions": {
                "opened_total": _C.sessions_opened,
                "closed_total": _C.sessions_closed,
                "active": _C.sessions_opened - _C.sessions_closed,
            },
            "turns": {
                "started_total": _C.turns_started,
                "completed_total": _C.turns_completed,
                "cancelled_total": _C.turns_cancelled,
                "overloaded_total": _C.turns_overloaded,
                "in_flight": _C.turns_started
                - _C.turns_completed
                - _C.turns_cancelled,
            },
            "failures": {
                "asr": _C.asr_failed,
                "llm": _C.llm_failed,
                "tts": _C.tts_failed,
            },
            "latency_ms": {
                "samples": len(lats),
                "total_p50": _percentile([x[0] for x in lats], 50),
                "total_p95": _percentile([x[0] for x in lats], 95),
                "asr_p50": _percentile([x[1] for x in lats], 50),
                "llm_p50": _percentile([x[2] for x in lats], 50),
                "tts_p50": _percentile([x[3] for x in lats], 50),
                # The dashboard's sparkline reads `recent_totals` directly
                # so it doesn't have to scrape log lines for individual
                # samples. Cap at 50 to keep the response small.
                "recent_totals": [round(x[0]) for x in lats[-50:]],
            },
        }
    return out
