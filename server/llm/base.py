"""LLM provider abstraction.

The session loop hands the provider:
  - the user's transcribed utterance,
  - the device's MCP tool list (already in JSON-Schema form on the wire),
  - a callback for invoking a tool on the device and awaiting its result.

The provider returns the LLM's final text response after running any
tool-use round trips. We deliberately keep it linear (no streaming token
delivery to the device) — the device only cares about the final text we
send to TTS.

Subclass this to swap in DeepSeek, OpenAI, etc. The Anthropic implementation
is the one we use end-to-end; others are slot-ins for the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


@dataclass
class DeviceTool:
    """One tool, as discovered from the device's MCP `tools/list`."""

    name: str
    description: str
    input_schema: dict


ToolInvoker = Callable[[str, dict], Awaitable[dict]]
"""Async callable: (tool_name, arguments) -> MCP `result` dict.

The dict mirrors what the device returned — usually
`{"content": [{"type": "text", "text": "..."}], "isError": false}`.
The provider is responsible for flattening that to a string before passing
it back to the LLM.
"""


class LLMProvider(Protocol):
    async def respond(
        self,
        *,
        user_text: str,
        tools: list[DeviceTool],
        invoke_tool: ToolInvoker,
        history: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        """Run a single user-turn -> assistant-turn exchange.

        Returns (assistant_text, updated_history). The history shape is
        provider-specific; the caller treats it as opaque and just hands it
        back on the next turn so the provider can maintain context.
        """
        ...
