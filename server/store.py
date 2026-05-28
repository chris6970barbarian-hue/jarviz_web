"""Tiny JSON-file persistence for device records.

Keys are device MAC addresses (the `Device-Id` header sent on every OTA + WS
request). Each device record holds whatever per-device state the server cares
about — for now, just a `last_seen` timestamp; later we can extend with
per-device system prompt overrides, owner email, etc.

Two layers of locking:
- `_THREAD_LOCK` serializes calls within one process (cheap; same as before).
- `FileLock(devices.json.lock)` serializes calls across processes so this
  module stays correct under `uvicorn --workers N`. Without it, two workers
  doing a read-modify-write race and one update is silently lost.

For 200 devices the file is ~30 KB; atomic-rename writes keep readers from
seeing partial data. SQLite is still the right long-term answer once this
schema grows beyond `last_seen` / `client_id`.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from filelock import FileLock

from .config import settings


# Inside one process, serialize calls via threading.Lock so we don't acquire
# the file lock on every concurrent request (file-locking has syscall cost).
_THREAD_LOCK = threading.Lock()
# Inter-process serialization. The lockfile is co-located with the data
# file so a moved data_dir takes its lock with it.
_FILE_LOCK_PATH: Path | None = None
_FILE_LOCK: FileLock | None = None


def _file_lock() -> FileLock:
    """Lazy-construct the lock so importing this module doesn't try to
    create directories during e.g. unit-test collection."""
    global _FILE_LOCK_PATH, _FILE_LOCK
    target = settings.data_dir / "devices.json.lock"
    if _FILE_LOCK is None or _FILE_LOCK_PATH != target:
        _FILE_LOCK_PATH = target
        _FILE_LOCK = FileLock(str(target), timeout=10.0)
    return _FILE_LOCK


def _path() -> Path:
    return settings.data_dir / "devices.json"


def _load() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, Any]) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def touch_device(device_id: str, client_id: str = "", user_agent: str = "") -> dict[str, Any]:
    """Record that a device contacted us. Creates the record if new."""
    with _THREAD_LOCK, _file_lock():
        data = _load()
        rec = data.setdefault(device_id, {"first_seen": int(time.time())})
        rec["last_seen"] = int(time.time())
        if client_id:
            rec["client_id"] = client_id
        if user_agent:
            rec["user_agent"] = user_agent
        _save(data)
        return rec


def get_device(device_id: str) -> dict[str, Any] | None:
    # Reads don't need the file lock — atomic-rename writes mean a partial
    # file is never visible — but the thread lock cheaply prevents tearing.
    with _THREAD_LOCK:
        return _load().get(device_id)


def list_devices() -> dict[str, Any]:
    with _THREAD_LOCK:
        return _load()
