"""Server-side view of the device's MCP server.

The xiaozhi protocol carries MCP messages inside `{"type":"mcp", "payload": <JSONRPC>}`
WS text frames. The device hosts the MCP server (it owns the tools); this
bridge is the client. Flow:

  1. After the WS hello exchange, we send `initialize`, then `tools/list`
     (paginated by `nextCursor` until exhausted).
  2. While conversation runs, the LLM might call a tool: we send `tools/call`
     and await the matching response by id.
  3. We never `notify` from the server side — no need.

The bridge owns no I/O of its own; the session passes it a `send_text`
callable for outgoing frames and routes incoming `mcp` payloads to
`on_message`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from .llm.base import DeviceTool

log = logging.getLogger("jarviz.mcp")

SendText = Callable[[str], Awaitable[None]]

# How many recently-timed-out ids to remember. A late device response
# carrying any of these is logged at DEBUG instead of WARNING — the
# original caller already raised, but the response is no longer a sign
# of a protocol bug, just a slow tool.
_RECENT_TIMEOUT_RING = 64


@dataclass
class _Pending:
    future: asyncio.Future


class MCPBridge:
    def __init__(self, send_text: SendText, session_id: str) -> None:
        self._send_text = send_text
        self._session_id = session_id
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}
        self._timed_out: deque[int] = deque(maxlen=_RECENT_TIMEOUT_RING)
        self._tools: list[DeviceTool] = []

    @property
    def tools(self) -> list[DeviceTool]:
        return list(self._tools)

    async def _send_request(self, method: str, params: dict | None = None, *, timeout: float = 10.0) -> dict:
        rpc_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            payload["params"] = params

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rpc_id] = _Pending(fut)

        envelope = {
            "type": "mcp",
            "session_id": self._session_id,
            "payload": payload,
        }
        await self._send_text(json.dumps(envelope))

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rpc_id, None)
            self._timed_out.append(rpc_id)
            raise RuntimeError(f"MCP {method} timed out after {timeout}s")

    def on_message(self, payload: dict) -> None:
        """Route a device->server MCP payload into the matching pending call."""
        rpc_id = payload.get("id")
        if not isinstance(rpc_id, int):
            log.debug("MCP payload without int id (notification or request from device): %s", payload)
            return
        pending = self._pending.pop(rpc_id, None)
        if pending is None:
            # If we recently timed out this id, the late response is expected
            # — the caller already raised. Otherwise it's a protocol surprise.
            if rpc_id in self._timed_out:
                log.debug("Late MCP response for already-timed-out id=%s", rpc_id)
            else:
                log.warning("MCP response for unknown id=%s", rpc_id)
            return
        if "error" in payload:
            err = payload["error"]
            msg = err.get("message", "unknown MCP error") if isinstance(err, dict) else str(err)
            pending.future.set_exception(RuntimeError(f"MCP error: {msg}"))
        else:
            pending.future.set_result(payload.get("result", {}))

    async def initialize(self) -> None:
        result = await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "jarviz-backend", "version": "0.1.0"},
            },
        )
        log.info("MCP initialize: %s", result)

    async def discover_tools(self) -> list[DeviceTool]:
        cursor = ""
        tools: list[DeviceTool] = []
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self._send_request("tools/list", params)
            for raw in result.get("tools", []):
                tools.append(
                    DeviceTool(
                        name=raw.get("name", ""),
                        description=raw.get("description", ""),
                        input_schema=raw.get("inputSchema", {"type": "object", "properties": {}}),
                    )
                )
            cursor = result.get("nextCursor", "") or ""
            if not cursor:
                break
        log.info("Discovered %d MCP tool(s): %s", len(tools), [t.name for t in tools])
        self._tools = tools
        return tools

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._send_request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=15.0,
        )

    def cancel_all(self, reason: str = "session closed") -> None:
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(RuntimeError(reason))
        self._pending.clear()
