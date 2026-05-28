import logging
import sys
import threading
from collections import deque

from .config import settings

# How many recent log records to keep in memory for the dashboard.
# Tiny — each entry is ~200 bytes formatted, so 500 entries = ~100 KB.
_RING_SIZE = 500


class _RingHandler(logging.Handler):
    """Keep the most recent N formatted log lines in a thread-safe deque
    so the /logs/recent endpoint can serve them without re-reading disk."""

    def __init__(self) -> None:
        super().__init__()
        self._buf: deque[dict] = deque(maxlen=_RING_SIZE)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        item = {
            "ts": record.created,
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "line": line,
        }
        with self._lock:
            self._buf.append(item)

    def snapshot(self, n: int = 100) -> list[dict]:
        with self._lock:
            data = list(self._buf)
        return data[-n:] if n and n < len(data) else data


# Singleton — installed once by setup_logging(), read by /logs/recent.
ring_handler = _RingHandler()


def recent_log_records(n: int = 100) -> list[dict]:
    """Return at most `n` most recent log records as plain dicts."""
    return ring_handler.snapshot(n)


def setup_logging() -> None:
    level = getattr(logging, settings.JARVIZ_LOG_LEVEL.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s | %(message)s")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    root.addHandler(handler)
    # Mirror everything into the in-memory ring for the dashboard.
    ring_handler.setFormatter(fmt)
    ring_handler.setLevel(level)
    root.addHandler(ring_handler)
    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.INFO)
