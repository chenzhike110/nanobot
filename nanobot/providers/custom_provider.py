"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import json_repair
from openai import AsyncOpenAI

from nanobot.providers.base import LLMProvider, LLMResponse, LLMStreamEvent, ToolCallRequest


class CustomProvider(LLMProvider):

    def __init__(self, api_key: str = "no-key", api_base: str = "http://localhost:8000/v1", default_model: str = "default"):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        # Keep affinity stable for this provider instance to improve backend cache locality.
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            default_headers={"x-session-affinity": uuid.uuid4().hex},
        )

    @staticmethod
    def _accumulate_tool_call_delta(buffers: dict[int, dict[str, Any]], tool_delta: Any) -> None:
        index = getattr(tool_delta, "index", None)
        try:
            idx = int(index if index is not None else len(buffers))
        except Exception:
            idx = len(buffers)
        buf = buffers.setdefault(idx, {"id": None, "name": None, "arguments": ""})
        if getattr(tool_delta, "id", None):
            buf["id"] = tool_delta.id
        function = getattr(tool_delta, "function", None)
        name = getattr(function, "name", None)
        if name:
            buf["name"] = name
        arguments = getattr(function, "arguments", None)
        if isinstance(arguments, str) and arguments:
            buf["arguments"] += arguments

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
                   reasoning_effort: str | None = None,
                   tool_choice: str | dict[str, Any] | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs.update(tools=tools, tool_choice=tool_choice or "auto")
        try:
            return self._parse(await self._client.chat.completions.create(**kwargs))
        except Exception as e:
            return LLMResponse(content=f"Error: {e}", finish_reason="error")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
            "stream": True,
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs.update(tools=tools, tool_choice=tool_choice or "auto")

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_buffers: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"

        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                choices = chunk.choices or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.delta
                text_delta = getattr(delta, "content", None)
                if isinstance(text_delta, str) and text_delta:
                    content_parts.append(text_delta)
                    yield LLMStreamEvent(kind="text_delta", delta=text_delta)
                reasoning_delta = getattr(delta, "reasoning_content", None)
                if isinstance(reasoning_delta, str) and reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    yield LLMStreamEvent(kind="reasoning_delta", delta=reasoning_delta)
                for tool_delta in (getattr(delta, "tool_calls", None) or []):
                    self._accumulate_tool_call_delta(tool_buffers, tool_delta)
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        except Exception as e:
            yield LLMStreamEvent(kind="response", response=LLMResponse(content=f"Error: {e}", finish_reason="error"))
            return

        tool_calls = [
            ToolCallRequest(
                id=buf.get("id") or f"call_{idx}",
                name=buf.get("name") or "unknown_tool",
                arguments=json_repair.loads(buf.get("arguments") or "{}"),
            )
            for idx, buf in sorted(tool_buffers.items())
        ]
        yield LLMStreamEvent(
            kind="response",
            response=LLMResponse(
                content="".join(content_parts) or None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                reasoning_content="".join(reasoning_parts).strip() or None,
            ),
        )

    def _parse(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        msg = choice.message
        tool_calls = [
            ToolCallRequest(id=tc.id, name=tc.function.name,
                            arguments=json_repair.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments)
            for tc in (msg.tool_calls or [])
        ]
        u = response.usage
        return LLMResponse(
            content=msg.content, tool_calls=tool_calls, finish_reason=choice.finish_reason or "stop",
            usage={"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens, "total_tokens": u.total_tokens} if u else {},
            reasoning_content=getattr(msg, "reasoning_content", None) or None,
        )

    def get_default_model(self) -> str:
        return self.default_model

