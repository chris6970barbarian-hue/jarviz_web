"""Headless device simulator. Lets you exercise the backend end-to-end
without flashing real hardware.

What it does:
  1. POST /xiaozhi/ota/ to fetch the WebSocket URL.
  2. Open the WS, do the hello handshake.
  3. Respond to MCP `initialize` and `tools/list` with a tiny in-memory
     stub of the Jarviz tools (enough for the LLM to plausibly call them).
  4. Send a `listen detect` with a hard-coded user utterance.
  5. Print every JSON message and audio frame size that comes back.

Usage:
    python scripts/simulate_device.py "remind me to call mom in 30 seconds"
    python scripts/simulate_device.py --reminder "take medicine"

The text is treated as a fresh user utterance unless --reminder is set,
in which case it's wrapped in '[Reminder] ...' to simulate the firing path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets


# Randomize the synthetic device-id per process. Hard-coding it meant two
# parallel simulator runs got rejected by the per-device cooldown after the
# first; randomizing lets stress tests open N independent "devices".
def _random_mac() -> str:
    parts = uuid.uuid4().hex[:12]
    return ":".join(parts[i:i+2].upper() for i in range(0, 12, 2))


FAKE_DEVICE_ID = _random_mac()
FAKE_CLIENT_ID = str(uuid.uuid4())


# Mirror the firmware's NVS behavior: a fresh device has no user_name. The
# LLM's first reply should ask the user for their name, then a
# `jarviz.set_user_name(name)` tool call persists it for future sessions.
# We persist that state to a tiny file so subsequent simulator runs see
# the saved name — exactly how a real ESP32 sees it from NVS on reboot.
_SIM_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "sim_user_name.txt"
)


def _sim_get_user_name() -> str:
    """Read the simulated persisted name. Empty string if never set —
    same as the firmware's `jarviz.get_user_name` on a fresh NVS."""
    try:
        return _SIM_STATE_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def _sim_set_user_name(name: str) -> None:
    """Persist a name. Mirrors `Settings::SetString("user_name", name)`
    on the firmware. Bounded to match the firmware's 64-char cap."""
    name = (name or "").strip()[:64]
    _SIM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SIM_STATE_PATH.write_text(name, encoding="utf-8")

STUB_TOOLS: list[dict[str, Any]] = [
    {
        "name": "jarviz.get_user_name",
        "description": "Bootstrap. Returns name, local_time, unix_timestamp, pending_reminders.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "jarviz.set_user_name",
        "description": "Persist first name.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "jarviz.create_reminder_relative",
        "description": "Schedule reminder N seconds from now.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seconds_from_now": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["seconds_from_now", "text"],
        },
    },
    {
        "name": "jarviz.create_reminder",
        "description": "Schedule reminder at absolute unix timestamp.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "unix_timestamp": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["unix_timestamp", "text"],
        },
    },
    {
        "name": "jarviz.list_reminders",
        "description": "List pending reminders.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "jarviz.delete_reminder",
        "description": "Delete a reminder by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    },
]


# The simulator emulates the device's clock. The real firmware (ota.cc)
# folds the server-pushed `timezone_offset` into settimeofday(), so the
# device's epoch is local-wall-clock seconds, and its reminder timestamps
# are in that frame. We mirror that here so the dashboard's countdown
# (which corrects back by the same offset) lines up. Set from the OTA
# response in main().
_SIM_TZ_OFFSET_MIN = 0
# In-process reminder store, keyed by id — stands in for the device's NVS
# so get_user_name / list_reminders return real pending reminders (the
# previous always-empty stub would wrongly wipe the server's mirror).
_SIM_REMINDERS: dict[int, dict] = {}
_SIM_NEXT_ID = 1


def _sim_device_now() -> int:
    """Device-frame epoch seconds (true UTC + the OTA timezone offset),
    matching what the firmware's clock holds after settimeofday()."""
    return int(time.time()) + _SIM_TZ_OFFSET_MIN * 60


def _sim_local_time(unix_ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(unix_ts))


def _sim_reminder_list() -> list[dict]:
    return [
        {
            "id": r["id"],
            "unix_timestamp": r["unix_timestamp"],
            "local_time": _sim_local_time(r["unix_timestamp"]),
            "text": r["text"],
        }
        for r in sorted(_SIM_REMINDERS.values(), key=lambda x: x["unix_timestamp"])
    ]


def _stub_tool_response(name: str, args: dict) -> dict:
    """Return a minimal MCP `result` dict for the given tool call."""
    global _SIM_NEXT_ID
    if name == "jarviz.get_user_name":
        # NO hardcoded name — read whatever the simulator last persisted.
        # First run on a fresh checkout returns "" so the LLM exercises
        # its "ask for the name" branch exactly like a real fresh device.
        now = _sim_device_now()
        text = json.dumps(
            {
                "name": _sim_get_user_name(),
                "local_time": _sim_local_time(now),
                "unix_timestamp": now,
                "pending_reminders": _sim_reminder_list(),
            }
        )
    elif name == "jarviz.set_user_name":
        # Persist for future runs, so the user only has to teach Jarviz
        # their name once — same UX as the real device's NVS.
        _sim_set_user_name(args.get("name", ""))
        text = "true"
    elif name == "jarviz.get_current_time":
        now = _sim_device_now()
        text = json.dumps({"local_time": _sim_local_time(now), "unix_timestamp": now})
    elif name == "jarviz.create_reminder_relative":
        rid = _SIM_NEXT_ID
        _SIM_NEXT_ID += 1
        ts = _sim_device_now() + int(args.get("seconds_from_now", 0))
        _SIM_REMINDERS[rid] = {"id": rid, "unix_timestamp": ts, "text": args.get("text", "")}
        text = json.dumps(
            {"id": rid, "unix_timestamp": ts, "local_time": _sim_local_time(ts), "text": args.get("text", "")}
        )
    elif name == "jarviz.create_reminder":
        rid = _SIM_NEXT_ID
        _SIM_NEXT_ID += 1
        ts = int(args.get("unix_timestamp", 0))
        _SIM_REMINDERS[rid] = {"id": rid, "unix_timestamp": ts, "text": args.get("text", "")}
        text = json.dumps(
            {"id": rid, "unix_timestamp": ts, "local_time": _sim_local_time(ts), "text": args.get("text", "")}
        )
    elif name == "jarviz.list_reminders":
        text = json.dumps(_sim_reminder_list())
    elif name == "jarviz.delete_reminder":
        rid = args.get("id")
        try:
            _SIM_REMINDERS.pop(int(rid), None)
        except (TypeError, ValueError):
            pass
        text = "true"
    else:
        text = f"unknown tool {name}"
    return {"content": [{"type": "text", "text": text}], "isError": False}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("utterance", nargs="?", default="what time is it")
    parser.add_argument(
        "--reminder",
        action="store_true",
        help="Wrap the utterance in '[Reminder] ...' to simulate firing.",
    )
    parser.add_argument(
        "--ota-url",
        default="http://127.0.0.1:8080/xiaozhi/ota/",
        help="Where to fetch the WS URL from. Defaults to local dev.",
    )
    parser.add_argument(
        "--idle-after",
        type=float,
        default=15.0,
        help="Seconds of idle after TTS stop before we close.",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=45.0,
        help="Hard ceiling: exit if nothing arrives in this many seconds (catches "
             "the case where the backend silently skips the turn, e.g. empty input).",
    )
    parser.add_argument(
        "--user-name",
        default=None,
        help="Pre-seed the simulator's persisted user name (mirrors a device "
             "that's already done the 'what's your name?' handshake). Stored in "
             "data/sim_user_name.txt and reused on every future run. Pass an "
             "empty string ('') to forget the saved name and exercise the "
             "first-run path again.",
    )
    args = parser.parse_args()

    # Apply --user-name BEFORE opening the WS so the backend's first
    # `jarviz.get_user_name` call sees the seeded value.
    if args.user_name is not None:
        _sim_set_user_name(args.user_name)
        if args.user_name == "":
            print("[sim] cleared persisted user name (fresh-device mode)")
        else:
            print(f"[sim] seeded persisted user name: {args.user_name!r}")

    text = f"[Reminder] {args.utterance}" if args.reminder else args.utterance

    # 1. OTA
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            args.ota_url,
            headers={
                "Device-Id": FAKE_DEVICE_ID,
                "Client-Id": FAKE_CLIENT_ID,
                "User-Agent": "jarviz-simulator/0.1",
                "Content-Type": "application/json",
            },
            content="{}",
        )
        r.raise_for_status()
        ota = r.json()
    print("OTA response:", json.dumps(ota, indent=2))
    ws_url = ota["websocket"]["url"]

    # Mirror the firmware: fold the server-pushed timezone offset into our
    # emulated device clock so reminder timestamps land in the same frame
    # the real device produces (ota.cc adds it to settimeofday()).
    global _SIM_TZ_OFFSET_MIN
    try:
        _SIM_TZ_OFFSET_MIN = int(ota["server_time"]["timezone_offset"])
        print(f"[sim] device clock offset: {_SIM_TZ_OFFSET_MIN} min")
    except (KeyError, TypeError, ValueError):
        _SIM_TZ_OFFSET_MIN = 0

    # The OTA URL hands out the LAN IP so the real device can reach us. When
    # running the simulator on the same host, force the WS host back to
    # whatever host we used for OTA. Avoids Windows hairpin-NAT quirks where
    # localhost can't talk to its own LAN IP.
    from urllib.parse import urlparse, urlunparse
    ota_host = urlparse(args.ota_url).hostname or "127.0.0.1"
    ws_parsed = urlparse(ws_url)
    if ws_parsed.hostname not in (ota_host, "127.0.0.1", "localhost"):
        rewritten = ws_parsed._replace(netloc=f"{ota_host}:{ws_parsed.port}")
        ws_url = urlunparse(rewritten)
        print("Rewriting WS host to match OTA host ->", ws_url)

    # 2. WS
    headers = {
        "Device-Id": FAKE_DEVICE_ID,
        "Client-Id": FAKE_CLIENT_ID,
        "Protocol-Version": "1",
    }
    # websockets 13.x default `connect` is the legacy asyncio client, which
    # takes `extra_headers` (not `additional_headers`).
    async with websockets.connect(ws_url, extra_headers=headers, max_size=2**22) as ws:
        # Hello
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "version": 1,
                    "features": {"mcp": True},
                    "transport": "websocket",
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
        )
        server_hello = json.loads(await ws.recv())
        print("Server hello:", server_hello)
        session_id = server_hello.get("session_id", "")

        # 3. Send the utterance as a `detect`
        await ws.send(
            json.dumps(
                {
                    "type": "listen",
                    "state": "detect",
                    "text": text,
                    "session_id": session_id,
                }
            )
        )

        # 4. Loop: handle MCP requests, log everything until tts.stop, then idle.
        last_tts_stop = None
        audio_frames = 0
        audio_bytes = 0
        last_msg_at = time.time()
        try:
            while True:
                if last_tts_stop and (time.time() - last_tts_stop) > args.idle_after:
                    break
                if (time.time() - last_msg_at) > args.max_wait:
                    print(f"[sim] no message for {args.max_wait}s, giving up "
                          "(backend may have skipped this turn)")
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    if last_tts_stop:
                        break
                    continue
                last_msg_at = time.time()
                if isinstance(raw, bytes):
                    audio_frames += 1
                    audio_bytes += len(raw)
                    continue
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "mcp":
                    payload = msg.get("payload") or {}
                    method = payload.get("method")
                    rpc_id = payload.get("id")
                    if method == "initialize":
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "mcp",
                                    "session_id": session_id,
                                    "payload": {
                                        "jsonrpc": "2.0",
                                        "id": rpc_id,
                                        "result": {
                                            "protocolVersion": "2024-11-05",
                                            "capabilities": {"tools": {}},
                                            "serverInfo": {"name": "jarviz-sim", "version": "0.1"},
                                        },
                                    },
                                }
                            )
                        )
                    elif method == "tools/list":
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "mcp",
                                    "session_id": session_id,
                                    "payload": {
                                        "jsonrpc": "2.0",
                                        "id": rpc_id,
                                        "result": {"tools": STUB_TOOLS},
                                    },
                                }
                            )
                        )
                    elif method == "tools/call":
                        params = payload.get("params") or {}
                        name = params.get("name", "")
                        arguments = params.get("arguments") or {}
                        print(f"[device] tool call: {name}({arguments})")
                        result = _stub_tool_response(name, arguments)
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "mcp",
                                    "session_id": session_id,
                                    "payload": {
                                        "jsonrpc": "2.0",
                                        "id": rpc_id,
                                        "result": result,
                                    },
                                }
                            )
                        )
                    else:
                        print("Unknown MCP method:", method)
                elif mtype == "stt":
                    print(f"[stt] {msg.get('text')!r}")
                elif mtype == "tts":
                    state = msg.get("state")
                    if state == "sentence_start":
                        print(f"[tts.sentence] {msg.get('text')!r}")
                    elif state == "start":
                        print("[tts] start")
                    elif state == "stop":
                        print(f"[tts] stop  (received {audio_frames} opus frames, {audio_bytes} bytes)")
                        last_tts_stop = time.time()
                else:
                    print("[server]", msg)
        except websockets.ConnectionClosed:
            print("Server closed connection.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
