"""Tests for channel adapters: Feishu, WeCom, DingTalk, QQ, and Web."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from fastapi.testclient import TestClient

from xclaw.channels.feishu import FeishuAdapter
from xclaw.channels.wecom import WeComAdapter
from xclaw.channels.dingtalk import DingTalkAdapter
from xclaw.channels.qq import QQAdapter
from xclaw.channels.web import create_web_app
from xclaw.config import Settings


# ── Feishu adapter ────────────────────────────────────────────────────────────

def _make_feishu(handler=None) -> FeishuAdapter:
    return FeishuAdapter(
        app_id="app_id",
        app_secret="secret",
        verification_token="token123",
        message_handler=handler or AsyncMock(return_value="ok"),
    )


@pytest.mark.asyncio
async def test_feishu_url_verification():
    adapter = _make_feishu()
    result = await adapter.handle_event({"type": "url_verification", "challenge": "abc"})
    assert result == {"challenge": "abc"}


@pytest.mark.asyncio
async def test_feishu_receive_message():
    handler = AsyncMock(return_value="hello reply")
    adapter = _make_feishu(handler)

    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "chat_id": "oc_abc123",
                "content": json.dumps({"text": "你好"}),
            },
            "sender": {"sender_id": {"open_id": "ou_xxx"}},
        },
    }
    with patch.object(adapter, "send_response", new=AsyncMock()):
        result = await adapter.handle_event(payload)
    assert result == {"msg": "ok"}
    handler.assert_called_once_with("oc_abc123", "你好")


@pytest.mark.asyncio
async def test_feishu_receive_message_no_text():
    """Empty text should not invoke the handler."""
    handler = AsyncMock(return_value="reply")
    adapter = _make_feishu(handler)

    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "chat_id": "oc_abc",
                "content": json.dumps({"text": "  "}),
            },
            "sender": {},
        },
    }
    await adapter.handle_event(payload)
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_feishu_unknown_event_type():
    """Non-message events should return ok without calling handler."""
    handler = AsyncMock()
    adapter = _make_feishu(handler)
    result = await adapter.handle_event({"header": {"event_type": "other.event"}, "event": {}})
    assert result == {"msg": "ok"}
    handler.assert_not_called()


def test_feishu_verify_signature():
    adapter = _make_feishu()
    import hashlib
    timestamp, nonce, body = "ts", "nc", b'{"data": 1}'
    content = (timestamp + nonce + adapter.verification_token).encode() + body
    expected = hashlib.sha256(content).hexdigest()
    assert adapter.verify_signature(timestamp, nonce, body, expected) is True
    assert adapter.verify_signature(timestamp, nonce, body, "wrong") is False


@pytest.mark.asyncio
async def test_feishu_start_stop():
    adapter = _make_feishu()
    await adapter.start()  # Should not raise
    with patch.object(adapter._client, "aclose", new=AsyncMock()):
        await adapter.stop()


class _FakeOAuthManager:
    def __init__(self) -> None:
        self.start_login = AsyncMock(
            return_value={
                "login_id": "login-1",
                "authorize_url": "https://auth.openai.com/oauth/authorize?state=abc",
                "expires_at": "2026-04-16T12:00:00Z",
                "mode": "manual",
            }
        )
        self.get_login_status = AsyncMock(
            return_value={
                "login_id": "login-1",
                "status": "completed",
                "authenticated": True,
                "email": "alice@example.com",
            }
        )
        self.complete_login = AsyncMock(
            return_value={
                "authenticated": True,
                "provider": "openai-codex",
                "email": "alice@example.com",
                "display_name": "Alice",
            }
        )
        self.get_session_payload = AsyncMock(
            return_value={
                "authenticated": True,
                "provider": "openai-codex",
                "email": "alice@example.com",
                "display_name": "Alice",
            }
        )
        self.logout = AsyncMock(return_value=None)


def test_admin_page_mentions_openai_codex_oauth_controls():
    app = create_web_app(
        message_handler=AsyncMock(return_value="ok"),
        settings=Settings(llm_provider="openai-codex"),
    )
    client = TestClient(app)

    resp = client.get("/admin")

    assert resp.status_code == 200
    assert "ChatGPT / Codex 登录" in resp.text
    assert "/api/llm/oauth/openai-codex/start" in resp.text


def test_openai_codex_oauth_web_api_flow():
    oauth_manager = _FakeOAuthManager()
    app = create_web_app(
        message_handler=AsyncMock(return_value="ok"),
        settings=Settings(llm_provider="openai-codex"),
        oauth_manager=oauth_manager,
    )
    client = TestClient(app)

    start = client.post("/api/llm/oauth/openai-codex/start")
    status = client.get("/api/llm/oauth/openai-codex/status/login-1")
    complete = client.post(
        "/api/llm/oauth/openai-codex/complete",
        json={"login_id": "login-1", "redirect_url_or_code": "http://localhost:1455/auth/callback?code=1&state=abc"},
    )
    session = client.get("/api/llm/provider/session")
    logout = client.post("/api/llm/oauth/openai-codex/logout")

    assert start.status_code == 200
    assert start.json()["login_id"] == "login-1"
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert complete.status_code == 200
    assert complete.json()["authenticated"] is True
    assert session.status_code == 200
    assert session.json()["provider"] == "openai-codex"
    assert logout.status_code == 200


# ── WeCom adapter ─────────────────────────────────────────────────────────────

def _make_wecom(handler=None) -> WeComAdapter:
    return WeComAdapter(
        corp_id="corpid",
        agent_id="1000",
        secret="secret",
        token="token",
        message_handler=handler or AsyncMock(return_value="ok"),
    )


@pytest.mark.asyncio
async def test_wecom_receive_text_message():
    handler = AsyncMock(return_value="reply")
    adapter = _make_wecom(handler)

    xml_body = (
        "<xml>"
        "<MsgType>text</MsgType>"
        "<FromUserName>user001</FromUserName>"
        "<Content>查询大盘</Content>"
        "</xml>"
    )
    with patch.object(adapter, "send_response", new=AsyncMock()):
        result = await adapter.handle_event(xml_body)
    assert result == "ok"
    handler.assert_called_once_with("user001", "查询大盘")


@pytest.mark.asyncio
async def test_wecom_non_text_message():
    """Non-text MsgType should not call handler."""
    handler = AsyncMock()
    adapter = _make_wecom(handler)
    xml_body = (
        "<xml><MsgType>image</MsgType><FromUserName>u</FromUserName></xml>"
    )
    await adapter.handle_event(xml_body)
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_wecom_invalid_xml():
    """Invalid XML should not raise, just log error."""
    adapter = _make_wecom()
    result = await adapter.handle_event("not xml at all")
    assert result == "ok"


def test_wecom_verify_signature():
    import hashlib

    adapter = _make_wecom()
    timestamp, nonce = "ts", "nc"
    items = sorted([adapter.token, timestamp, nonce, ""])
    expected = hashlib.sha1("".join(items).encode()).hexdigest()
    assert adapter.verify_signature(expected, timestamp, nonce, "") is True
    assert adapter.verify_signature("wrong", timestamp, nonce, "") is False


@pytest.mark.asyncio
async def test_wecom_start_stop():
    adapter = _make_wecom()
    await adapter.start()
    with patch.object(adapter._client, "aclose", new=AsyncMock()):
        await adapter.stop()


# ── DingTalk adapter ──────────────────────────────────────────────────────────

def _make_dingtalk(handler=None) -> DingTalkAdapter:
    return DingTalkAdapter(
        app_key="key",
        app_secret="secret",
        robot_code="robot",
        message_handler=handler or AsyncMock(return_value="ok"),
    )


@pytest.mark.asyncio
async def test_dingtalk_receive_text():
    handler = AsyncMock(return_value="answer")
    adapter = _make_dingtalk(handler)
    payload = {
        "msgtype": "text",
        "senderStaffId": "staff001",
        "text": {"content": "涨了吗"},
    }
    with patch.object(adapter, "send_response", new=AsyncMock()):
        result = await adapter.handle_event(payload)
    assert result == {"msg": "ok"}
    handler.assert_called_once_with("staff001", "涨了吗")


@pytest.mark.asyncio
async def test_dingtalk_receive_rich_text():
    handler = AsyncMock(return_value="reply")
    adapter = _make_dingtalk(handler)
    payload = {
        "msgtype": "richText",
        "senderStaffId": "s2",
        "content": {
            "richText": [
                {"type": "text", "text": "Hello"},
                {"type": "image", "downloadCode": "xxx"},
                {"type": "text", "text": " World"},
            ]
        },
    }
    with patch.object(adapter, "send_response", new=AsyncMock()):
        await adapter.handle_event(payload)
    handler.assert_called_once_with("s2", "Hello World")


@pytest.mark.asyncio
async def test_dingtalk_empty_text_not_dispatched():
    handler = AsyncMock()
    adapter = _make_dingtalk(handler)
    payload = {"msgtype": "text", "senderStaffId": "s3", "text": {"content": "   "}}
    await adapter.handle_event(payload)
    handler.assert_not_called()


def test_dingtalk_sign_request():
    import hashlib, hmac, time
    from base64 import b64encode

    adapter = _make_dingtalk()
    ts = "1700000000000"
    string_to_sign = f"{ts}\n{adapter.app_secret}"
    expected = b64encode(
        hmac.new(adapter.app_secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    ).decode()
    assert adapter.sign_request(ts) == expected


@pytest.mark.asyncio
async def test_dingtalk_start_stop():
    adapter = _make_dingtalk()
    await adapter.start()
    with patch.object(adapter._client, "aclose", new=AsyncMock()):
        await adapter.stop()


# ── QQ adapter ────────────────────────────────────────────────────────────────

def _make_qq(handler=None) -> QQAdapter:
    class _FakeClient:
        def __init__(self) -> None:
            self.closed = False

        async def get_access_token(self) -> str:
            return "token"

        async def get_gateway_url(self) -> str:
            return "wss://gateway.example.com"

        async def send_c2c_message(self, *args, **kwargs):
            return {"id": "1"}

        async def send_group_message(self, *args, **kwargs):
            return {"id": "1"}

        async def send_c2c_input_notify(self, *args, **kwargs):
            return {"id": "1"}

        async def send_c2c_stream_message(self, *args, **kwargs):
            return {"id": "stream-1"}

        async def send_c2c_image_message(self, *args, **kwargs):
            return {"id": "1"}

        async def send_group_image_message(self, *args, **kwargs):
            return {"id": "1"}

        async def send_c2c_file_message(self, *args, **kwargs):
            return {"id": "1"}

        async def send_group_file_message(self, *args, **kwargs):
            return {"id": "1"}

        async def close(self) -> None:
            self.closed = True

        async def aclose(self) -> None:
            self.closed = True

    class _FakeGateway:
        def __init__(self) -> None:
            self.sent = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

        async def recv(self) -> str:
            await asyncio.sleep(3600)
            return ""

        async def close(self) -> None:
            return None

    async def _gateway_connect(url: str):
        return _FakeGateway()

    return QQAdapter(
        app_id="app_id",
        app_secret="secret",
        message_handler=handler or AsyncMock(return_value="ok"),
        client_factory=lambda account: _FakeClient(),
        gateway_connect=_gateway_connect,
    )


@pytest.mark.asyncio
async def test_qq_receive_group_message():
    handler = AsyncMock(return_value="reply")
    adapter = _make_qq(handler)
    payload = {
        "op": 0,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "group_openid": "group123",
            "id": "msg456",
            "content": "你好机器人",
        },
    }
    with patch.object(adapter, "send_response", new=AsyncMock()):
        result = await adapter.handle_event(payload)
    assert result == {"msg": "ok"}
    handler.assert_called_once_with("qq:default:group:group123", "你好机器人", "group")


@pytest.mark.asyncio
async def test_qq_receive_group_message_no_msg_id():
    handler = AsyncMock(return_value="reply")
    adapter = _make_qq(handler)
    payload = {
        "op": 0,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "group_openid": "group123",
            "content": "测试消息",
        },
    }
    with patch.object(adapter, "send_response", new=AsyncMock()):
        result = await adapter.handle_event(payload)
    assert result == {"msg": "ok"}
    handler.assert_called_once_with("qq:default:group:group123", "测试消息", "group")


@pytest.mark.asyncio
async def test_qq_empty_text_not_dispatched():
    handler = AsyncMock()
    adapter = _make_qq(handler)
    payload = {
        "op": 0,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {"group_openid": "g1", "id": "m1", "content": "   "},
    }
    await adapter.handle_event(payload)
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_qq_url_verification():
    adapter = _make_qq()
    payload = {
        "op": 13,
        "d": {"plain_token": "abc123", "event_ts": "12345"},
    }
    result = await adapter.handle_event(payload)
    assert result["plain_token"] == "abc123"


@pytest.mark.asyncio
async def test_qq_unknown_event_type():
    handler = AsyncMock()
    adapter = _make_qq(handler)
    payload = {"op": 0, "t": "SOME_OTHER_EVENT", "d": {}}
    result = await adapter.handle_event(payload)
    assert result == {"msg": "ok"}
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_qq_start_stop():
    adapter = _make_qq()
    await adapter.start()
    await adapter.stop()


# ── Web channel (FastAPI) ─────────────────────────────────────────────────────

def _make_web_app(handler=None, auth_token="", db=None, settings=None):
    if handler is None:
        async def handler(chat_id: str, text: str) -> str:
            return f"echo: {text}"
    return create_web_app(
        message_handler=handler,
        auth_token=auth_token,
        db=db,
        settings=settings,
    )


def test_web_health():
    app = _make_web_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_web_root_chat_page():
    app = _make_web_app()
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "XClaw" in resp.text
    assert "/api/chat" in resp.text


def test_web_root_chat_page_mentions_auth_when_enabled():
    app = _make_web_app(auth_token="secret123")
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Bearer Token" in resp.text


def test_web_chat_endpoint():
    app = _make_web_app()
    client = TestClient(app)
    resp = client.post("/api/chat", json={"chat_id": "u1", "message": "hello"})
    assert resp.status_code == 200
    assert "echo" in resp.json()["reply"]


def test_web_chat_auth_required():
    app = _make_web_app(auth_token="secret123")
    client = TestClient(app)
    resp = client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 401


def test_web_chat_auth_valid():
    app = _make_web_app(auth_token="secret123")
    client = TestClient(app)
    resp = client.post(
        "/api/chat",
        json={"message": "hi"},
        headers={"Authorization": "Bearer secret123"},
    )
    assert resp.status_code == 200


def test_web_feishu_webhook_not_configured():
    app = _make_web_app()
    client = TestClient(app)
    resp = client.post("/webhook/feishu", json={"type": "url_verification", "challenge": "x"})
    assert resp.status_code == 503


def test_web_sessions_no_db():
    app = _make_web_app()
    client = TestClient(app)
    resp = client.get("/api/sessions")
    assert resp.status_code == 503


def test_web_sessions_with_db():
    """With a db, /api/sessions should return a list."""
    db = MagicMock()

    # Mock conn with an async context manager for execute
    mock_cursor = MagicMock()
    mock_cursor.fetchall = AsyncMock(return_value=[])
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    db.conn.execute = MagicMock(return_value=mock_cm)

    app = _make_web_app(db=db)
    client = TestClient(app)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_web_config_no_settings():
    app = _make_web_app()
    client = TestClient(app)
    resp = client.get("/api/config")
    assert resp.status_code == 503


def test_web_config_with_settings():
    settings = MagicMock()
    settings.llm_provider = "anthropic"
    settings.model = "claude-opus-4-5"
    settings.max_tokens = 4096
    settings.web_enabled = True
    settings.web_host = "127.0.0.1"
    settings.web_port = 8080
    settings.feishu_enabled = False
    settings.wecom_enabled = False
    settings.dingtalk_enabled = False
    settings.wechat_mp_enabled = False
    settings.qq_enabled = False
    settings.data_dir = "./xclaw.data"
    settings.timezone = "Asia/Shanghai"
    settings.stock_market_default = "CN"
    settings.bash_enabled = False
    settings.rate_limit_per_minute = 20
    settings.multi_user_mode = False
    settings.enabled_skills = ["all"]
    # Sensitive fields on the settings object that should NOT be exposed
    settings.api_key = "sk-super-secret-key"
    settings.feishu_app_secret = "feishu-secret"
    settings.wecom_secret = "wecom-secret"

    app = _make_web_app(settings=settings)
    client = TestClient(app)
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["llm_provider"] == "anthropic"
    assert data["wechat_mp_enabled"] is False
    # Secrets must be excluded
    assert "api_key" not in data
    assert "feishu_app_secret" not in data
    assert "wecom_secret" not in data
