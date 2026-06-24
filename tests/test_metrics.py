"""In-memory metrics counters + latency ring. Offline.

The module holds a global singleton; tests assert on deltas / structure rather
than absolute values so ordering between tests doesn't matter.
"""

from __future__ import annotations

from server import metrics


def test_unknown_counter_is_ignored():
    # Must not raise — a typo'd counter name can't take the server down.
    metrics.inc("this_counter_does_not_exist")
    metrics.inc("nope", by=5)


def test_known_counter_increments():
    before = metrics.snapshot()["turns"]["started_total"]
    metrics.inc("turns_started")
    metrics.inc("turns_started", by=2)
    after = metrics.snapshot()["turns"]["started_total"]
    assert after - before == 3


def test_in_flight_derivation():
    s0 = metrics.snapshot()["turns"]
    base = s0["started_total"] - s0["completed_total"] - s0["cancelled_total"]
    metrics.inc("turns_started")
    metrics.inc("turns_started")
    metrics.inc("turns_completed")
    s1 = metrics.snapshot()["turns"]
    in_flight = s1["started_total"] - s1["completed_total"] - s1["cancelled_total"]
    assert in_flight == base + 1
    assert s1["in_flight"] == in_flight


def test_record_turn_latency_and_snapshot_shape():
    # Clear the latency ring first — other test modules (e.g. test_hardening)
    # exercise _process_turn end-to-end against stubbed providers that complete
    # instantly, populating the global ring with (0, 0, 0, 0) tuples. With
    # those still present, p50 across the sorted samples lands on a zero and
    # the assertion below ("total_p50 > 0") flakes based on test ordering.
    metrics._C.recent_latencies.clear()
    metrics.record_turn_latency(total_ms=120, asr_ms=30, llm_ms=70, tts_ms=20)
    snap = metrics.snapshot()
    lat = snap["latency_ms"]
    assert lat["samples"] >= 1
    assert lat["total_p50"] > 0
    assert isinstance(lat["recent_totals"], list)
    assert lat["recent_totals"][-1] == 120
    # required top-level structure
    for key in ("uptime_s", "ota", "ws", "sessions", "turns", "latency_ms"):
        assert key in snap


def test_percentile_pure_function():
    vals = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert metrics._percentile([], 50) == 0.0
    assert metrics._percentile([42], 95) == 42
    p50 = metrics._percentile(vals, 50)
    p95 = metrics._percentile(vals, 95)
    assert 40 <= p50 <= 60
    assert p95 >= p50
    assert p95 <= 100
