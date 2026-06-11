"""MCP JSON-RPC bridge: request/response correlation, errors, timeouts,
pagination, and late/unknown responses. Offline.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.mcp_bridge import MCPBridge


def _make():
    frames = []

    async def send_text(s: str) -> None:
        frames.append(json.loads(s))

    return MCPBridge(send_text, "sess-1"), frames


async def _last_id(frames):
    await asyncio.sleep(0)  # let the pending _send_request emit its frame
    env = frames[-1]
    assert env["type"] == "mcp" and env["session_id"] == "sess-1"
    return env["payload"]["id"], env["payload"]


@pytest.mark.asyncio
async def test_call_tool_roundtrip():
    bridge, frames = _make()
    task = asyncio.create_task(bridge.call_tool("jarviz.get_current_time", {"tz": "x"}))
    rid, payload = await _last_id(frames)
    assert payload["method"] == "tools/call"
    assert payload["params"] == {"name": "jarviz.get_current_time", "arguments": {"tz": "x"}}
    bridge.on_message({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"text": "9am"}]}})
    assert await task == {"content": [{"text": "9am"}]}


@pytest.mark.asyncio
async def test_error_response_raises():
    bridge, frames = _make()
    task = asyncio.create_task(bridge._send_request("initialize", {}))
    rid, _ = await _last_id(frames)
    bridge.on_message({"jsonrpc": "2.0", "id": rid, "error": {"message": "boom"}})
    with pytest.raises(RuntimeError, match="boom"):
        await task


@pytest.mark.asyncio
async def test_timeout_raises_and_is_remembered():
    bridge, frames = _make()
    with pytest.raises(RuntimeError, match="timed out"):
        await bridge._send_request("slow", {}, timeout=0.02)
    rid = frames[-1]["payload"]["id"]
    assert rid in bridge._timed_out
    # A late response for the timed-out id must not blow up (logged at DEBUG).
    bridge.on_message({"jsonrpc": "2.0", "id": rid, "result": {}})


@pytest.mark.asyncio
async def test_discover_tools_paginates():
    bridge, frames = _make()
    task = asyncio.create_task(bridge.discover_tools())

    rid1, _ = await _last_id(frames)
    bridge.on_message({
        "jsonrpc": "2.0", "id": rid1,
        "result": {"tools": [{"name": "a", "description": "", "inputSchema": {}}],
                   "nextCursor": "page2"},
    })
    rid2, payload2 = await _last_id(frames)
    assert payload2.get("params", {}).get("cursor") == "page2"
    bridge.on_message({
        "jsonrpc": "2.0", "id": rid2,
        "result": {"tools": [{"name": "b"}]},
    })

    tools = await task
    assert [t.name for t in tools] == ["a", "b"]
    assert [t.name for t in bridge.tools] == ["a", "b"]  # cached on the bridge


@pytest.mark.asyncio
async def test_unknown_and_notification_messages_are_ignored():
    bridge, _ = _make()
    bridge.on_message({"id": 9999, "result": {}})  # no pending call
    bridge.on_message({"jsonrpc": "2.0", "method": "ping"})  # device notification, no id


@pytest.mark.asyncio
async def test_cancel_all_fails_pending_calls():
    bridge, frames = _make()
    task = asyncio.create_task(bridge._send_request("x", {}, timeout=5))
    await _last_id(frames)
    bridge.cancel_all("session closed")
    with pytest.raises(RuntimeError, match="session closed"):
        await task
