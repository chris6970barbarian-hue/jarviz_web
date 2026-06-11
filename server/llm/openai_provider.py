"""OpenAI-compatible LLM provider with full tool-use loop.

Works for any service that speaks the OpenAI Chat Completions API:
DeepSeek, OpenAI, Together, Groq, Fireworks, etc. The base URL and model
id are wired in from settings; this class doesn't know which it is.

Tool-call loop mirrors the Anthropic provider:
    user -> assistant (with tool_calls?)
       if tool_calls: invoke each on the device, append `tool` messages, loop
       else: extract content, return

History across turns is the OpenAI message list MINUS the system prompt
(we re-attach the system prompt at the start of every call).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from openai import AsyncOpenAI

from ..config import settings
from .base import DeviceTool, LLMProvider, ToolInvoker

log = logging.getLogger("jarviz.llm")

_MAX_TOOL_ROUNDS = 6
_LLM_CALL_TIMEOUT_S = 30.0


def _load_system_prompt() -> str:
    p: Path = settings.system_prompt_path
    if not p.exists():
        return "You are Jarviz, a helpful voice assistant."
    return p.read_text(encoding="utf-8").strip()


class OpenAICompatLLM(LLMProvider):
    def __init__(self, *, api_key: str, base_url: str | None, model: str) -> None:
        if not api_key:
            raise RuntimeError(
                "API key is empty. Set DEEPSEEK_API_KEY (or OPENAI_API_KEY) in .env."
            )
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        # Retry transient failures (429 / 5xx / connection resets) with the
        # SDK's exponential backoff (honors Retry-After). A routine rate-limit
        # blip under load no longer wastes the whole turn on the canned error.
        kwargs["max_retries"] = settings.JARVIZ_LLM_MAX_RETRIES
        self._client = AsyncOpenAI(**kwargs)
        self._model = model
        self._max_tokens = settings.JARVIZ_LLM_MAX_TOKENS
        self._system = _load_system_prompt()

    @staticmethod
    def _safe_name(name: str) -> str:
        # OpenAI-compatible APIs (DeepSeek included) require tool names to
        # match `^[a-zA-Z0-9_-]+$`. Our MCP tools use `jarviz.X` — translate
        # dots to underscores on the way out.
        return name.replace(".", "_")

    @classmethod
    def _tool_schemas(cls, tools: list[DeviceTool]) -> tuple[list[dict], dict[str, str]]:
        """Returns (schema list, safe_name -> original_name map)."""
        out: list[dict] = []
        name_map: dict[str, str] = {}
        for t in tools:
            safe = cls._safe_name(t.name)
            name_map[safe] = t.name
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": safe,
                        "description": t.description,
                        "parameters": t.input_schema or {"type": "object", "properties": {}},
                    },
                }
            )
        return out, name_map

    @staticmethod
    def _flatten_mcp_result(mcp_result: dict) -> str:
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
        msgs: list[dict] = [{"role": "system", "content": self._system}]
        if history:
            msgs.extend(history)
        msgs.append({"role": "user", "content": user_text})

        tool_schemas, name_map = self._tool_schemas(tools)
        final_text = ""

        for round_idx in range(_MAX_TOOL_ROUNDS):
            kwargs: dict = {
                "model": self._model,
                "messages": msgs,
                "max_tokens": self._max_tokens,
            }
            if tool_schemas:
                kwargs["tools"] = tool_schemas
                kwargs["tool_choice"] = "auto"

            try:
                resp = await asyncio.wait_for(
                    self._client.chat.completions.create(**kwargs),
                    timeout=_LLM_CALL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.warning("LLM call timed out after %ss", _LLM_CALL_TIMEOUT_S)
                final_text = "Sorry, that took too long. Could you try again?"
                break
            choice = resp.choices[0]
            assistant = choice.message
            tool_calls = assistant.tool_calls or []
            usage = resp.usage
            log.info(
                "LLM round %d finish=%s tool_calls=%d tokens=%s/%s",
                round_idx,
                choice.finish_reason,
                len(tool_calls),
                getattr(usage, "prompt_tokens", "?"),
                getattr(usage, "completion_tokens", "?"),
            )

            # Round-trip the assistant message via model_dump so vendor-
            # specific fields (DeepSeek thinking models include
            # `reasoning_content` and require it on the next request) are
            # preserved verbatim.
            assistant_msg = assistant.model_dump(exclude_none=True)
            assistant_msg["role"] = "assistant"
            if "content" not in assistant_msg:
                assistant_msg["content"] = ""
            msgs.append(assistant_msg)

            if not tool_calls:
                final_text = (assistant.content or "").strip()
                break

            for tc in tool_calls:
                safe_name = tc.function.name
                # Translate the LLM's safe name back to the device's MCP tool
                # name (e.g. `jarviz_get_user_name` -> `jarviz.get_user_name`).
                original_name = name_map.get(safe_name, safe_name)
                raw_args = tc.function.arguments or "{}"
                parse_err: str | None = None
                try:
                    args = json.loads(raw_args)
                    if not isinstance(args, dict):
                        parse_err = "tool arguments must be a JSON object"
                        args = {}
                except json.JSONDecodeError as e:
                    parse_err = f"invalid JSON args: {e.msg} at pos {e.pos}"
                    args = {}

                if parse_err is not None:
                    # Surface the error back to the LLM as a tool result so
                    # the next round can self-correct, instead of silently
                    # running the tool with empty args.
                    log.warning("Tool %s args parse failed: %s raw=%r",
                                original_name, parse_err, raw_args)
                    text = f"tool error: {parse_err}. Got: {raw_args!r}"
                else:
                    log.info("LLM -> tool %s args=%s", original_name, args)
                    try:
                        mcp_result = await invoke_tool(original_name, args)
                        text = self._flatten_mcp_result(mcp_result)
                    except Exception as e:  # noqa: BLE001
                        log.exception("Tool %s raised", original_name)
                        text = f"tool error: {e}"
                msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": text,
                    }
                )
        else:
            log.warning("LLM tool-use loop exceeded %d rounds; truncating", _MAX_TOOL_ROUNDS)
            # Prefer the most recent non-empty assistant text over the canned
            # apology — at least the user hears something coherent the model
            # actually said.
            for m in reversed(msgs):
                if m.get("role") == "assistant":
                    text = (m.get("content") or "").strip()
                    if text:
                        final_text = text
                        break
            if not final_text:
                final_text = "Sorry, I got stuck. Could you try again?"

        if not final_text:
            final_text = "Okay."

        # History to return: drop the system prompt; we re-attach next turn.
        return_history = [m for m in msgs if m.get("role") != "system"]
        return final_text, return_history
