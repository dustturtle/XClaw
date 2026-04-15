from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from xclaw.channels.qq import QQAdapter


class FakeQQClient:
    def __init__(self, *, account_key: str) -> None:
        self.account_key = account_key
        self.gateway_url = "wss://gateway.example.com"
        self.access_token = "token-1"
        self.c2c_text_calls: list[dict[str, object]] = []
        self.group_text_calls: list[dict[str, object]] = []
        self.c2c_input_notify_calls: list[dict[str, object]] = []
        self.c2c_stream_calls: list[dict[str, object]] = []
        self.c2c_image_calls: list[dict[str, object]] = []
        self.group_image_calls: list[dict[str, object]] = []
        self.c2c_file_calls: list[dict[str, object]] = []
        self.group_file_calls: list[dict[str, object]] = []
        self.closed = False

    async def get_access_token(self) -> str:
        return self.access_token

    async def get_gateway_url(self) -> str:
        return self.gateway_url

    async def send_c2c_message(self, openid: str, content: str, *, msg_id: str | None, msg_seq: int) -> dict[str, str]:
        self.c2c_text_calls.append(
            {"openid": openid, "content": content, "msg_id": msg_id, "msg_seq": msg_seq}
        )
        return {"id": "m-1", "timestamp": "1"}

    async def send_group_message(
        self, group_openid: str, content: str, *, msg_id: str | None, msg_seq: int
    ) -> dict[str, str]:
        self.group_text_calls.append(
            {
                "group_openid": group_openid,
                "content": content,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        return {"id": "m-1", "timestamp": "1"}

    async def send_c2c_input_notify(
        self,
        openid: str,
        *,
        msg_id: str | None,
        msg_seq: int,
        input_second: int,
    ) -> dict[str, str]:
        self.c2c_input_notify_calls.append(
            {
                "openid": openid,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
                "input_second": input_second,
            }
        )
        return {"id": "typing-1"}

    async def send_c2c_stream_message(
        self,
        openid: str,
        *,
        event_id: str,
        msg_id: str,
        msg_seq: int,
        index: int,
        content_raw: str,
        input_state: int,
        stream_msg_id: str | None = None,
    ) -> dict[str, str]:
        response = {"id": stream_msg_id or "stream-1", "timestamp": "1"}
        self.c2c_stream_calls.append(
            {
                "openid": openid,
                "event_id": event_id,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
                "index": index,
                "content_raw": content_raw,
                "input_state": input_state,
                "stream_msg_id": stream_msg_id,
            }
        )
        return response

    async def send_c2c_image_message(
        self,
        openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, str]:
        self.c2c_image_calls.append(
            {
                "openid": openid,
                "filename": filename,
                "content_base64": content_base64,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        return {"id": "img-1", "timestamp": "1"}

    async def send_group_image_message(
        self,
        group_openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, str]:
        self.group_image_calls.append(
            {
                "group_openid": group_openid,
                "filename": filename,
                "content_base64": content_base64,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        return {"id": "img-1", "timestamp": "1"}

    async def send_c2c_file_message(
        self,
        openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, str]:
        self.c2c_file_calls.append(
            {
                "openid": openid,
                "filename": filename,
                "content_base64": content_base64,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        return {"id": "file-1", "timestamp": "1"}

    async def send_group_file_message(
        self,
        group_openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, str]:
        self.group_file_calls.append(
            {
                "group_openid": group_openid,
                "filename": filename,
                "content_base64": content_base64,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        return {"id": "file-1", "timestamp": "1"}

    async def close(self) -> None:
        self.closed = True


class FakeSTTClient:
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript
        self.calls: list[dict[str, str]] = []

    async def transcribe_from_url(self, url: str, *, filename: str | None = None) -> str:
        self.calls.append({"url": url, "filename": filename or ""})
        return self.transcript


class FakeGatewayWS:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self._events = list(events)
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send(self, data: str) -> None:
        import json

        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        import json

        if not self._events:
            raise RuntimeError("gateway drained")
        return json.dumps(self._events.pop(0))

    async def close(self) -> None:
        self.closed = True


def _make_adapter(
    *,
    handler=None,
    stream_handler=None,
    stt_client=None,
    typing_delay_seconds: float = 0.5,
    typing_interval_seconds: float = 50.0,
    accounts: list[dict[str, object]] | None = None,
    gateway_events: list[dict[str, object]] | None = None,
) -> tuple[QQAdapter, dict[str, FakeQQClient]]:
    clients: dict[str, FakeQQClient] = {}
    gateway_ws = FakeGatewayWS(gateway_events or [])

    def client_factory(account):
        client = FakeQQClient(account_key=account.key)
        clients[account.key] = client
        return client

    async def gateway_connect(url: str):
        assert url == "wss://gateway.example.com"
        return gateway_ws

    adapter = QQAdapter(
        accounts=accounts
        or [
            {
                "key": "default",
                "app_id": "app-1",
                "app_secret": "secret-1",
                "dm_enabled": True,
                "group_enabled": True,
                "typing_enabled": True,
                "streaming_enabled": True,
            }
        ],
        message_handler=handler or AsyncMock(return_value="ok"),
        stream_handler=stream_handler,
        client_factory=client_factory,
        stt_client=stt_client,
        typing_delay_seconds=typing_delay_seconds,
        typing_interval_seconds=typing_interval_seconds,
        gateway_connect=gateway_connect,
    )
    adapter._test_gateway_ws = gateway_ws  # type: ignore[attr-defined]
    return adapter, clients


@pytest.mark.asyncio
async def test_qq_c2c_message_uses_stable_chat_id_and_streams() -> None:
    async def stream_handler(chat_id: str, text: str, chat_type: str):
        assert chat_id == "qq:acct1:c2c:user-openid-1"
        assert text == "你好"
        assert chat_type == "private"
        yield "第一段"
        yield "第二段"

    adapter, clients = _make_adapter(
        stream_handler=stream_handler,
        accounts=[
            {
                "key": "acct1",
                "app_id": "app-1",
                "app_secret": "secret-1",
                "dm_enabled": True,
                "group_enabled": False,
                "typing_enabled": True,
                "streaming_enabled": True,
            }
        ],
        typing_delay_seconds=0,
    )

    payload = {
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "msg-1",
            "content": "你好",
            "author": {"user_openid": "user-openid-1"},
        },
    }

    result = await adapter.handle_event(payload, account_key="acct1")

    assert result == {"msg": "ok"}
    client = clients["acct1"]
    assert client.c2c_text_calls == []
    assert [c["content_raw"] for c in client.c2c_stream_calls] == ["第一段", "第一段第二段", "第一段第二段"]
    assert [c["input_state"] for c in client.c2c_stream_calls] == [1, 1, 10]
    assert len(client.c2c_input_notify_calls) >= 1


@pytest.mark.asyncio
async def test_qq_group_message_uses_stable_chat_id_and_plain_reply() -> None:
    handler = AsyncMock(return_value="群回复")
    adapter, clients = _make_adapter(
        handler=handler,
        accounts=[
            {
                "key": "acct1",
                "app_id": "app-1",
                "app_secret": "secret-1",
                "dm_enabled": False,
                "group_enabled": True,
                "require_mention": True,
            }
        ],
    )
    payload = {
        "op": 0,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "id": "group-msg-1",
            "group_openid": "group-openid-1",
            "content": "帮我查下大盘",
            "mentions": [{"is_you": True}],
            "author": {"member_openid": "member-1"},
        },
    }

    result = await adapter.handle_event(payload, account_key="acct1")

    assert result == {"msg": "ok"}
    handler.assert_called_once_with("qq:acct1:group:group-openid-1", "帮我查下大盘", "group")
    client = clients["acct1"]
    assert client.c2c_input_notify_calls == []
    assert client.c2c_stream_calls == []
    assert client.group_text_calls[0]["group_openid"] == "group-openid-1"
    assert client.group_text_calls[0]["content"] == "群回复"


@pytest.mark.asyncio
async def test_qq_group_message_without_mention_is_ignored() -> None:
    handler = AsyncMock()
    adapter, _ = _make_adapter(handler=handler)
    payload = {
        "op": 0,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "id": "group-msg-1",
            "group_openid": "group-openid-1",
            "content": "机器人在吗",
            "author": {"member_openid": "member-1"},
            "mentions": [],
        },
    }

    result = await adapter.handle_event(payload, account_key="default")

    assert result == {"msg": "ok"}
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_qq_voice_attachment_prefers_asr_text() -> None:
    handler = AsyncMock(return_value="ok")
    adapter, _ = _make_adapter(handler=handler)
    payload = {
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "voice-msg-1",
            "content": "",
            "author": {"user_openid": "user-openid-1"},
            "attachments": [
                {
                    "content_type": "voice",
                    "url": "https://example.com/voice.silk",
                    "asr_refer_text": "你好，这是语音转写",
                }
            ],
        },
    }

    await adapter.handle_event(payload, account_key="default")

    handler.assert_called_once_with(
        "qq:default:c2c:user-openid-1",
        "你好，这是语音转写",
        "private",
    )


@pytest.mark.asyncio
async def test_qq_voice_attachment_uses_stt_when_asr_missing() -> None:
    handler = AsyncMock(return_value="ok")
    stt_client = FakeSTTClient("这是 STT 结果")
    adapter, _ = _make_adapter(handler=handler, stt_client=stt_client)
    payload = {
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "voice-msg-1",
            "content": "",
            "author": {"user_openid": "user-openid-1"},
            "attachments": [
                {
                    "content_type": "audio/wav",
                    "url": "https://example.com/voice.silk",
                    "voice_wav_url": "https://example.com/voice.wav",
                }
            ],
        },
    }

    await adapter.handle_event(payload, account_key="default")

    assert stt_client.calls == [{"url": "https://example.com/voice.wav", "filename": "voice-msg-1.wav"}]
    handler.assert_called_once_with(
        "qq:default:c2c:user-openid-1",
        "这是 STT 结果",
        "private",
    )


@pytest.mark.asyncio
async def test_qq_send_image_and_file_response_for_c2c_and_group(tmp_path: Path) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"png-bytes")
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"pdf-bytes")

    adapter, clients = _make_adapter()

    await adapter.handle_event(
        {
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": "msg-1",
                "content": "hi",
                "author": {"user_openid": "user-openid-1"},
            },
        },
        account_key="default",
    )
    await adapter.handle_event(
        {
            "op": 0,
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {
                "id": "msg-2",
                "group_openid": "group-openid-1",
                "content": "hi",
                "mentions": [{"is_you": True}],
                "author": {"member_openid": "member-1"},
            },
        },
        account_key="default",
    )

    await adapter.send_image_response("qq:default:c2c:user-openid-1", str(image_path))
    await adapter.send_file_response("qq:default:group:group-openid-1", str(file_path))

    client = clients["default"]
    assert client.c2c_image_calls[0]["filename"] == "sample.png"
    assert client.c2c_image_calls[0]["content_base64"] == base64.b64encode(b"png-bytes").decode()
    assert client.group_file_calls[0]["filename"] == "report.pdf"
    assert client.group_file_calls[0]["content_base64"] == base64.b64encode(b"pdf-bytes").decode()


@pytest.mark.asyncio
async def test_qq_gateway_start_sends_identify_and_dispatches_private_message() -> None:
    handler = AsyncMock(return_value="gateway reply")
    adapter, clients = _make_adapter(
        handler=handler,
        gateway_events=[
            {"op": 10, "d": {"heartbeat_interval": 30_000}},
            {
                "op": 0,
                "t": "READY",
                "s": 1,
                "d": {"session_id": "session-1"},
            },
            {
                "op": 0,
                "t": "C2C_MESSAGE_CREATE",
                "s": 2,
                "d": {
                    "id": "msg-1",
                    "content": "你好",
                    "author": {"user_openid": "user-openid-1"},
                },
            },
        ],
    )

    await adapter.start()
    await asyncio.sleep(0.05)
    await adapter.stop()

    handler.assert_called_once_with("qq:default:c2c:user-openid-1", "你好", "private")
    sent = adapter._test_gateway_ws.sent  # type: ignore[attr-defined]
    assert sent[0]["op"] == 2
    assert sent[0]["d"]["intents"] > 0
    assert clients["default"].c2c_text_calls[0]["content"] == "gateway reply"


@pytest.mark.asyncio
async def test_qq_gateway_resume_uses_cached_session() -> None:
    adapter, _ = _make_adapter(
        gateway_events=[
            {"op": 10, "d": {"heartbeat_interval": 30_000}},
        ],
    )
    adapter._gateway_sessions["default"].session_id = "cached-session"
    adapter._gateway_sessions["default"].last_seq = 42

    await adapter.start()
    await asyncio.sleep(0.05)
    await adapter.stop()

    sent = adapter._test_gateway_ws.sent  # type: ignore[attr-defined]
    assert sent[0]["op"] == 6
    assert sent[0]["d"]["session_id"] == "cached-session"
    assert sent[0]["d"]["seq"] == 42
