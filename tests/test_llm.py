"""Tests for LLM provider configuration and payload shaping."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from xclaw.llm import OpenAICompatibleProvider, create_provider
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
