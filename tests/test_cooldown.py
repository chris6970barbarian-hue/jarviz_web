"""Per-device cooldown rate limiter. Offline, deterministic (injected clock)."""

from __future__ import annotations

import pytest

from server import cooldown
from server.cooldown import CooldownTracker


@pytest.fixture(autouse=True)
def _set_cooldown(monkeypatch):
    monkeypatch.setattr(cooldown.settings, "JARVIZ_DEVICE_COOLDOWN_S", 2.0)


def test_first_hit_allowed_second_blocked_then_allowed():
    t = CooldownTracker()
    assert t.check_and_mark("dev", now=100.0) is True
    assert t.check_and_mark("dev", now=101.0) is False   # within 2s window
    assert t.check_and_mark("dev", now=102.0) is True     # exactly at window
    assert t.check_and_mark("dev", now=102.5) is False


def test_empty_key_always_allowed():
    t = CooldownTracker()
    assert t.check_and_mark("", now=1.0) is True
    assert t.check_and_mark("", now=1.0) is True


def test_zero_cooldown_disables_gate(monkeypatch):
    monkeypatch.setattr(cooldown.settings, "JARVIZ_DEVICE_COOLDOWN_S", 0)
    t = CooldownTracker()
    assert t.check_and_mark("dev", now=1.0) is True
    assert t.check_and_mark("dev", now=1.0) is True


def test_distinct_devices_independent():
    t = CooldownTracker()
    assert t.check_and_mark("a", now=1.0) is True
    assert t.check_and_mark("b", now=1.0) is True   # different device, not blocked


def test_memory_bounded_under_fleet_rotation():
    t = CooldownTracker()
    # Insert well over the cap with ever-increasing keys/timestamps.
    for i in range(cooldown._MAX_TRACKED + 500):
        t.check_and_mark(f"dev-{i}", now=float(i))
    assert len(t._last_seen) <= cooldown._MAX_TRACKED


def test_module_level_trackers_are_independent():
    # An OTA hit must not consume the WS device's cooldown budget.
    assert cooldown.ota_cooldown.check_and_mark("dev", now=1.0) is True
    assert cooldown.ws_cooldown.check_and_mark("dev", now=1.0) is True
