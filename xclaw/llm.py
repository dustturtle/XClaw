"""LLM Provider abstraction + Anthropic and OpenAI-Compatible implementations."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

import httpx

from xclaw.llm_types import (
    ContentBlock,
    DoneEvent,
    LLMEvent,
    LLMResponse,
    Message,
    StopReason,
    TextBlock,
    TextDeltaEvent,
    ToolDefinition,
    ToolUseBlock,
    UsageStats,
)
from xclaw.oauth import OAuthNotAuthenticatedError


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse: ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[LLMEvent]: ...


# ── Anthropic implementation ──────────────────────────────────────────────────

class AnthropicProvider(LLMProvider):
    """Direct httpx implementation of the Anthropic Messages API."""

    BASE_URL = "https://api.anthropic.com/v1/messages"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-4-5",
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout)

    def _build_payload(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        system: str | None,
        max_tokens: int,
        stream: bool = False,
    ) -> dict[str, Any]:
        serialized_messages: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg.content, str):
                serialized_messages.append({"role": msg.role, "content": msg.content})
            else:
                blocks: list[dict[str, Any]] = []
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        blocks.append({"type": "text", "text": block.text})
                    elif isinstance(block, ToolUseBlock):
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            }
                        )
                    else:  # ToolResultBlock
                        blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.tool_use_id,
                                "content": block.content,
                                "is_error": block.is_error,
                            }
                        )
                serialized_messages.append({"role": msg.role, "content": blocks})

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": serialized_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in tools
            ]
        if stream:
            payload["stream"] = True
        return payload

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        content: list[ContentBlock] = []
        for block in data.get("content", []):
            if block["type"] == "text":
                content.append(TextBlock(text=block["text"]))
            elif block["type"] == "tool_use":
                content.append(
                    ToolUseBlock(id=block["id"], name=block["name"], input=block["input"])
                )

        usage_data = data.get("usage", {})
        return LLMResponse(
            stop_reason=StopReason(data.get("stop_reason", "end_turn")),
            content=content,
            usage=UsageStats(
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
            ),
            model=data.get("model", self.model),
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        payload = self._build_payload(messages, tools, system, max_tokens)
        response = await self._client.post(
            self.BASE_URL,
            json=payload,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": self.ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )
        response.raise_for_status()
        return self._parse_response(response.json())

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[LLMEvent]:
        payload = self._build_payload(messages, tools, system, max_tokens, stream=True)
        async with self._client.stream(
            "POST",
            self.BASE_URL,
            json=payload,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": self.ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        ) as resp:
            resp.raise_for_status()
            # Simple SSE parsing – collect full response and emit done event
            full_text = ""
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event_data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event_type = event_data.get("type", "")
                    if event_type == "content_block_delta":
                        delta = event_data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            full_text += text
                            yield TextDeltaEvent(text=text)
            # Emit a final done event (simplified – no full LLMResponse reconstruction)
            yield DoneEvent(
                response=LLMResponse(
                    stop_reason=StopReason.end_turn,
                    content=[TextBlock(text=full_text)],
                )
            )

    async def close(self) -> None:
        await self._client.aclose()


# ── OpenAI-compatible implementation ─────────────────────────────────────────

class OpenAICompatibleProvider(LLMProvider):
    """Provider for OpenAI-compatible APIs (OpenAI, DeepSeek, Ollama, etc.)."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
        temperature: float | None = None,
        thinking: bool | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.thinking = thinking
        self._client = httpx.AsyncClient(timeout=timeout)

    def _serialize_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg.content, str):
                result.append({"role": msg.role, "content": msg.content})
            else:
                # Flatten blocks into a single string for OpenAI
                parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                saw_tool_result = False
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append(
                            {
                                "id": block.id,
                                "type": "function",
                                "function": {
                                    "name": block.name,
                                    "arguments": json.dumps(block.input),
                                },
                            }
                        )
                    else:  # ToolResultBlock
                        saw_tool_result = True
                        result.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.tool_use_id,
                                "content": block.content,
                            }
                        )
                        continue

                # Tool results are emitted as standalone ``tool`` messages.
                # Do not append an extra parent message with ``content=None``,
                # which breaks OpenAI-compatible APIs such as Volcengine Ark.
                if saw_tool_result and not parts and not tool_calls:
                    continue

                entry: dict[str, Any] = {"role": msg.role, "content": "\n".join(parts) or None}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                result.append(entry)
        return result

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        choice = data["choices"][0]
        message = choice["message"]
        content: list[ContentBlock] = []

        if message.get("content"):
            content.append(TextBlock(text=message["content"]))

        for tc in message.get("tool_calls") or []:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            content.append(ToolUseBlock(id=tc["id"], name=fn["name"], input=args))

        finish = choice.get("finish_reason", "stop")
        stop_reason = StopReason.tool_use if finish == "tool_calls" else StopReason.end_turn

        usage_data = data.get("usage", {})
        return LLMResponse(
            stop_reason=stop_reason,
            content=content,
            usage=UsageStats(
                input_tokens=usage_data.get("prompt_tokens", 0),
                output_tokens=usage_data.get("completion_tokens", 0),
            ),
            model=data.get("model", self.model),
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(self._serialize_messages(messages))

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": all_messages,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.thinking is not None:
            payload["thinking"] = {
                "type": "enabled" if self.thinking else "disabled",
            }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
            payload["tool_choice"] = "auto"

        response = await self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        return self._parse_response(response.json())

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[LLMEvent]:
        # Simplified: just call chat and yield a DoneEvent
        response = await self.chat(messages, tools, system, max_tokens)
        for block in response.content:
            if isinstance(block, TextBlock):
                yield TextDeltaEvent(text=block.text)
        yield DoneEvent(response=response)

    async def close(self) -> None:
        await self._client.aclose()


class OpenAICodexProvider(LLMProvider):
    """Provider for ChatGPT/Codex OAuth-backed requests."""

    def __init__(
        self,
        *,
        credential_manager: Any,
        model: str = "gpt-5.4",
        base_url: str = "https://chatgpt.com/backend-api",
        timeout: float = 120.0,
    ) -> None:
        self.credential_manager = credential_manager
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    def _serialize_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg.content, str):
                result.append(
                    {
                        "type": "message",
                        "role": msg.role,
                        "content": [{"type": "input_text", "text": msg.content}],
                    }
                )
                continue

            text_parts: list[str] = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    result.append(
                        {
                            "type": "function_call",
                            "call_id": block.id,
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        }
                    )
                else:
                    result.append(
                        {
                            "type": "function_call_output",
                            "call_id": block.tool_use_id,
                            "output": block.content,
                        }
                    )
            if text_parts:
                result.append(
                    {
                        "type": "message",
                        "role": msg.role,
                        "content": [{"type": "input_text", "text": "\n".join(text_parts)}],
                    }
                )
        return result

    def _build_payload(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        system: str | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": self._serialize_messages(messages),
            "max_output_tokens": max_tokens,
        }
        if system:
            payload["instructions"] = system
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                }
                for t in tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        content: list[ContentBlock] = []
        for item in data.get("output", []):
            item_type = item.get("type")
            if item_type == "message":
                for part in item.get("content", []):
                    if part.get("type") in {"output_text", "text"} and part.get("text"):
                        content.append(TextBlock(text=part["text"]))
            elif item_type == "function_call":
                try:
                    args = json.loads(item.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                content.append(
                    ToolUseBlock(
                        id=item.get("call_id") or item.get("id") or "",
                        name=item.get("name", ""),
                        input=args,
                    )
                )

        stop_reason = StopReason.end_turn
        if any(isinstance(block, ToolUseBlock) for block in content):
            stop_reason = StopReason.tool_use

        incomplete = data.get("incomplete_details") or {}
        if incomplete.get("reason") == "max_output_tokens":
            stop_reason = StopReason.max_tokens

        usage_data = data.get("usage", {})
        return LLMResponse(
            stop_reason=stop_reason,
            content=content,
            usage=UsageStats(
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
            ),
            model=data.get("model", self.model),
        )

    async def _post_with_auth(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = await self.credential_manager.get_access_token()
        response = await self._client.post(
            f"{self.base_url}/responses",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        if response.status_code == 401:
            token = await self.credential_manager.get_access_token(force_refresh=True)
            response = await self._client.post(
                f"{self.base_url}/responses",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        response.raise_for_status()
        return response.json()

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        if self.credential_manager is None:
            raise OAuthNotAuthenticatedError("OpenAI Codex OAuth manager is not configured.")
        payload = self._build_payload(messages, tools, system, max_tokens)
        data = await self._post_with_auth(payload)
        return self._parse_response(data)

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[LLMEvent]:
        response = await self.chat(messages, tools, system, max_tokens)
        for block in response.content:
            if isinstance(block, TextBlock):
                yield TextDeltaEvent(text=block.text)
        yield DoneEvent(response=response)

    async def close(self) -> None:
        await self._client.aclose()


# ── Factory ───────────────────────────────────────────────────────────────────

def create_provider(
    provider: str,
    api_key: str,
    model: str,
    base_url: str | None = None,
    temperature: float | None = None,
    timeout: float = 120.0,
    thinking: bool | None = None,
    oauth_manager: Any | None = None,
) -> LLMProvider:
    """Factory that returns the correct LLMProvider based on config."""
    if provider == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model, timeout=timeout)
    if provider == "openai-codex":
        return OpenAICodexProvider(
            credential_manager=oauth_manager,
            model=model,
            base_url=(base_url or "https://chatgpt.com/backend-api"),
            timeout=timeout,
        )
    # OpenAI-compatible providers
    urls = {
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "ollama": "http://localhost:11434/v1",
    }
    resolved_url = base_url or urls.get(provider, "https://api.openai.com/v1")
    return OpenAICompatibleProvider(
        api_key=api_key,
        model=model,
        base_url=resolved_url,
        timeout=timeout,
        temperature=temperature,
        thinking=thinking,
    )
