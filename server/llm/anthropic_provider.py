"""Claude implementation of LLMProvider with full tool-use loop.

Why Anthropic for the prototype:
  - Strong, deterministic function calling. The xiaozhi.me failure mode was
    the model dropping tool calls; Sonnet does not do that on this prompt.
  - Prompt caching cuts the per-turn cost: the system prompt + tool schemas
    are static across all turns of all sessions, so we mark them as
    `cache_control` ephemeral. The first turn pays full price; subsequent
    turns hit the cache.

Migration to DeepSeek/OpenAI is a sibling file implementing the same
LLMProvider protocol — the session loop is provider-agnostic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from anthropic import AsyncAnthropic

from ..config import settings
from .base import DeviceTool, LLMProvider, ToolInvoker

log = logging.getLogger("jarviz.llm")

_MAX_TOOL_ROUNDS = 6  # safety cap; in practice 1-2 rounds per turn
_LLM_CALL_TIMEOUT_S = 30.0


def _load_system_prompt() -> str:
    p: Path = settings.system_prompt_path
    if not p.exists():
        return "You are Jarviz, a helpful voice assistant."
    return p.read_text(encoding="utf-8").strip()


class AnthropicLLM(LLMProvider):
    def __init__(self) -> None:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is empty. Set it in .env before starting the server."
            )
        self._client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._model = settings.JARVIZ_LLM_MODEL
        self._max_tokens = settings.JARVIZ_LLM_MAX_TOKENS
        self._system = _load_system_prompt()

    @staticmethod
    def _tool_schemas(tools: list[DeviceTool]) -> list[dict]:
        out: list[dict] = []
        for t in tools:
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema or {"type": "object", "properties": {}},
                }
            )
        # Mark the last tool with cache_control so the entire tool-list block
        # is cached. Anthropic caches up to the marker, inclusive.
        if out:
            out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
        return out

    @staticmethod
    def _flatten_mcp_result(mcp_result: dict) -> str:
        """Convert MCP `result.content` into a plain string for the LLM.

        Most of our tools return a single text item; a few return JSON-encoded
        text. We just concat everything text-typed.
        """
        if not isinstance(mcp_result, dict):
            return json.dumps(mcp_result)
        content = mcp_result.get("content")
        if not isinstance(content, list):
            return json.dumps(mcp_result)
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item))
        return "\n".join(parts) if parts else "(empty result)"

    async def respond(
        self,
        *,
        user_text: str,
        tools: list[DeviceTool],
        invoke_tool: ToolInvoker,
        history: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        msgs: list[dict] = list(history) if history else []
        msgs.append({"role": "user", "content": user_text})

        tool_schemas = self._tool_schemas(tools)
        # No cache_control on the system block — it's <1024 tokens so the
        # ephemeral marker is a no-op and only confuses future readers. The
        # tool-list block in _tool_schemas() carries the marker that does
        # actually activate the cache.
        system_blocks = [{"type": "text", "text": self._system}]

        final_text = ""
        for round_idx in range(_MAX_TOOL_ROUNDS):
            try:
                resp = await asyncio.wait_for(
                    self._client.messages.create(
                        model=self._model,
                        max_tokens=self._max_tokens,
                        system=system_blocks,
                        tools=tool_schemas if tool_schemas else None,
                        messages=msgs,
                    ),
                    timeout=_LLM_CALL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.warning("LLM call timed out after %ss", _LLM_CALL_TIMEOUT_S)
                final_text = "Sorry, that took too long. Could you try again?"
                break

            log.info(
                "LLM round %d stop=%s in=%d out=%d cache_r=%d cache_w=%d",
                round_idx,
                resp.stop_reason,
                resp.usage.input_tokens,
                resp.usage.output_tokens,
                getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
            )

            assistant_blocks = [b.model_dump() for b in resp.content]
            msgs.append({"role": "assistant", "content": assistant_blocks})

            if resp.stop_reason != "tool_use":
                final_text = "".join(
                    b.text for b in resp.content if getattr(b, "type", None) == "text"
                ).strip()
                break

            tool_results: list[dict] = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_name = block.name
                tool_args = block.input or {}
                log.info("LLM -> tool %s args=%s", tool_name, tool_args)
                try:
                    mcp_result = await invoke_tool(tool_name, tool_args)
                    is_error = bool(mcp_result.get("isError"))
                    text = self._flatten_mcp_result(mcp_result)
                except Exception as e:  # noqa: BLE001
                    log.exception("Tool %s raised", tool_name)
                    text = f"tool error: {e}"
                    is_error = True
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                        "is_error": is_error,
                    }
                )
            msgs.append({"role": "user", "content": tool_results})
        else:
            log.warning("LLM tool-use loop exceeded %d rounds; truncating", _MAX_TOOL_ROUNDS)
            # Prefer the last non-empty assistant text the model emitted so
            # the user hears something coherent instead of the canned apology.
            for m in reversed(msgs):
                if m.get("role") != "assistant":
                    continue
                content = m.get("content") or []
                text = "".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
                if text:
                    final_text = text
                    break
            if not final_text:
                final_text = "Sorry, I got stuck. Could you try again?"

        if not final_text:
            final_text = "Okay."
        return final_text, msgs
