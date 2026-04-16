"""Tests for LLM provider configuration and payload shaping."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from xclaw.llm import (
    OpenAICodexProvider,
    OpenAICompatibleProvider,
    create_provider,
)
from xclaw.llm_types import Message, StopReason, ToolResultBlock, ToolUseBlock


@pytest.mark.asyncio
async def test_openai_provider_uses_custom_base_url_and_options():
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        model="glm-test",
        base_url="https://example.com/api/v3",
        timeout=240.0,
        temperature=0.1,
        thinking=True,
    )
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "model": "glm-test",
    }
    provider._client.post = AsyncMock(return_value=mock_response)

    response = await provider.chat([Message(role="user", content="hello")], max_tokens=128)

    provider._client.post.assert_awaited_once()
    _, kwargs = provider._client.post.await_args
    assert provider._client.post.await_args.args[0] == "https://example.com/api/v3/chat/completions"
    assert kwargs["json"]["temperature"] == 0.1
    assert kwargs["json"]["thinking"] == {"type": "enabled"}
    assert kwargs["json"]["max_tokens"] == 128
    assert response.stop_reason == StopReason.end_turn
    await provider.close()


def test_create_provider_passes_custom_base_url():
    provider = create_provider(
        provider="openai",
        api_key="test-key",
        model="glm-test",
        base_url="https://example.com/api/v3",
        temperature=0.1,
        timeout=240.0,
        thinking=True,
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "https://example.com/api/v3"
    assert provider.temperature == 0.1
    assert provider.thinking is True


def test_openai_serialize_messages_skips_empty_parent_after_tool_result():
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        model="glm-test",
        base_url="https://example.com/api/v3",
    )
    messages = [
        Message(role="assistant", content=[
            ToolUseBlock(id="call_1", name="stock_quote", input={"symbol": "600519"})
        ]),
        Message(role="user", content=[
            ToolResultBlock(tool_use_id="call_1", content="tool failed", is_error=True)
        ]),
    ]

    serialized = provider._serialize_messages(messages)

    assert serialized == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "stock_quote",
                        "arguments": "{\"symbol\": \"600519\"}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "tool failed",
        },
    ]


@pytest.mark.asyncio
async def test_openai_codex_provider_uses_oauth_token_and_responses_endpoint():
    class _FakeManager:
        async def get_access_token(self, *, force_refresh: bool = False) -> str:
            return "oauth-token"

    provider = OpenAICodexProvider(
        credential_manager=_FakeManager(),
        model="gpt-5.4",
    )
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "ok",
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 2},
        "model": "gpt-5.4",
    }
    provider._client.post = AsyncMock(return_value=mock_response)

    response = await provider.chat([Message(role="user", content="hello")], max_tokens=128)

    provider._client.post.assert_awaited_once()
    url = provider._client.post.await_args.args[0]
    _, kwargs = provider._client.post.await_args
    assert url == "https://chatgpt.com/backend-api/responses"
    assert kwargs["headers"]["Authorization"] == "Bearer oauth-token"
    assert kwargs["json"]["model"] == "gpt-5.4"
    assert kwargs["json"]["max_output_tokens"] == 128
    assert response.stop_reason == StopReason.end_turn
    assert response.text() == "ok"
    await provider.close()


def test_create_provider_supports_openai_codex():
    class _FakeManager:
        async def get_access_token(self, *, force_refresh: bool = False) -> str:
            return "oauth-token"

    provider = create_provider(
        provider="openai-codex",
        api_key="",
        model="gpt-5.4",
        oauth_manager=_FakeManager(),
    )

    assert isinstance(provider, OpenAICodexProvider)


@pytest.mark.asyncio
async def test_openai_codex_provider_parses_function_calls_and_tool_results():
    class _FakeManager:
        async def get_access_token(self, *, force_refresh: bool = False) -> str:
            return "oauth-token"

    provider = OpenAICodexProvider(
        credential_manager=_FakeManager(),
        model="gpt-5.4",
    )
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "output": [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "stock_quote",
                "arguments": "{\"symbol\": \"600519\"}",
            }
        ],
        "usage": {"input_tokens": 5, "output_tokens": 3},
        "model": "gpt-5.4",
    }
    provider._client.post = AsyncMock(return_value=mock_response)

    response = await provider.chat(
        [
            Message(role="assistant", content=[ToolUseBlock(id="call_0", name="foo", input={"a": 1})]),
            Message(role="user", content=[ToolResultBlock(tool_use_id="call_0", content="ok")]),
        ]
    )

    _, kwargs = provider._client.post.await_args
    assert kwargs["json"]["input"][0]["type"] == "function_call"
    assert kwargs["json"]["input"][1]["type"] == "function_call_output"
    assert response.stop_reason == StopReason.tool_use
    assert response.tool_uses()[0].name == "stock_quote"
    await provider.close()


@pytest.mark.asyncio
async def test_openai_codex_provider_retries_after_401_with_forced_refresh():
    class _FakeManager:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        async def get_access_token(self, *, force_refresh: bool = False) -> str:
            self.calls.append(force_refresh)
            return "fresh-token" if force_refresh else "stale-token"

    provider = OpenAICodexProvider(
        credential_manager=_FakeManager(),
        model="gpt-5.4",
    )
    unauthorized = MagicMock()
    unauthorized.status_code = 401
    unauthorized.raise_for_status = MagicMock()
    unauthorized.json.return_value = {"error": "unauthorized"}
    ok = MagicMock()
    ok.status_code = 200
    ok.raise_for_status = MagicMock()
    ok.json.return_value = {
        "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}
        ]
    }
    provider._client.post = AsyncMock(side_effect=[unauthorized, ok])

    response = await provider.chat([Message(role="user", content="hello")])

    assert response.text() == "ok"
    headers_first = provider._client.post.await_args_list[0].kwargs["headers"]
    headers_second = provider._client.post.await_args_list[1].kwargs["headers"]
    assert headers_first["Authorization"] == "Bearer stale-token"
    assert headers_second["Authorization"] == "Bearer fresh-token"
    await provider.close()
