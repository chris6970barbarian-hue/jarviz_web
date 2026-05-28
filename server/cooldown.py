"""Per-device cooldown gate.

A misbehaving device (reboot loop, network flap) can hammer the OTA and
WebSocket endpoints faster than a real user ever would. Without rate
limiting, each reconnect spins up a new session and may fire a turn —
draining LLM quota and burning CPU.

This module keeps a tiny in-memory map of `Device-Id -> last accepted
timestamp`. A request is "allowed" if `now - last_seen >=
JARVIZ_DEVICE_COOLDOWN_S`. The map is pruned opportunistically so it
won't grow unbounded if a fleet rotates many device-ids.

In-memory only. With multiple uvicorn workers the per-worker state is
independent — if you scale workers, move this to Redis or a shared
LRU. For 200 devices on one worker, in-memory is fine.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from .config import settings


# Keep at most this many devices in memory before pruning. 200-device
# deployments will never hit this; it's a safety bound for OTA spam.
_MAX_TRACKED = 4096


class CooldownTracker:
    """Thread-safe most-recent-hit map with a min-interval check."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # OrderedDict so we can drop the oldest entry when we hit the cap.
        self._last_seen: OrderedDict[str, float] = OrderedDict()

    def check_and_mark(self, key: str, *, now: float | None = None) -> bool:
        """Return True if `key` is past the cooldown window (and record the
        new hit). Return False if it's still in cooldown.

        Empty key always passes (we don't want to reject anonymous OTA
        probes from monitoring tools that don't send Device-Id)."""
        if not key:
            return True
        cooldown = settings.JARVIZ_DEVICE_COOLDOWN_S
        if cooldown <= 0:
            return True
        ts = time.monotonic() if now is None else now
        with self._lock:
            prev = self._last_seen.get(key)
            if prev is not None and (ts - prev) < cooldown:
                return False
            self._last_seen[key] = ts
            self._last_seen.move_to_end(key)
            # Bound memory under pathological fleet rotation.
            while len(self._last_seen) > _MAX_TRACKED:
                self._last_seen.popitem(last=False)
        return True


# Two separate trackers so an OTA boot followed immediately by a WS
# open (the legitimate flow) is not punished. Each endpoint cools down
# only against itself.
ota_cooldown = CooldownTracker()
ws_cooldown = CooldownTracker()
