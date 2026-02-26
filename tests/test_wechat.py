"""Tests for the WeChat Official Account / Mini Program adapter and endpoints."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from xclaw.channels.wechat_mp import WeChatMPAdapter, _build_passive_reply
from xclaw.channels.web import create_web_app


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_adapter(handler=None) -> WeChatMPAdapter:
    return WeChatMPAdapter(
        app_id="wx_app_id",
        app_secret="wx_app_secret",
        token="my_token",
        message_handler=handler or AsyncMock(return_value="AI 回复"),
    )


def _xml_text_msg(from_user: str = "openid_user", to_user: str = "gh_xxx",
                  content: str = "你好", msg_id: str = "1001") -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        "</xml>"
    )


def _xml_image_msg(from_user: str = "openid_user", to_user: str = "gh_xxx") -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[image]]></MsgType>"
        "<MsgId>9999</MsgId>"
        "</xml>"
    )


# ── Signature verification ────────────────────────────────────────────────────

def test_verify_signature_valid():
    adapter = _make_adapter()
    timestamp = "1700000000"
    nonce = "random123"
    items = sorted([adapter.token, timestamp, nonce])
    sig = hashlib.sha1("".join(items).encode()).hexdigest()
    assert adapter.verify_signature(sig, timestamp, nonce) is True


def test_verify_signature_invalid():
    adapter = _make_adapter()
    assert adapter.verify_signature("wrong_sig", "ts", "nc") is False


# ── Passive reply builder ─────────────────────────────────────────────────────

def test_build_passive_reply_xml():
    xml = _build_passive_reply("openid_user", "gh_xxx", "Hello")
    assert "<ToUserName>" in xml
    assert "Hello" in xml
    assert "text" in xml


# ── handle_event: text messages ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_text_message_returns_ack():
    """Incoming text should return an immediate passive-reply ACK."""
    handler = AsyncMock(return_value="AI 回复")
    adapter = _make_adapter(handler)

    xml = _xml_text_msg(content="查大盘", msg_id="101")
    reply = await adapter.handle_event(xml)

    assert "<MsgType><![CDATA[text]]>" in reply
    assert "处理中" in reply  # ACK message contains "处理中"


@pytest.mark.asyncio
async def test_handle_text_message_fires_handler():
    """After returning ACK, the adapter should schedule the handler call."""
    import asyncio

    handler = AsyncMock(return_value="行情摘要")
    adapter = _make_adapter(handler)
    # Patch send_response so the async task doesn't make real HTTP requests
    adapter.send_response = AsyncMock()

    xml = _xml_text_msg(content="查上证指数", msg_id="102")
    await adapter.handle_event(xml)

    # Allow the asyncio task to run
    await asyncio.sleep(0.05)
    handler.assert_called_once_with("openid_user", "查上证指数")


@pytest.mark.asyncio
async def test_handle_duplicate_message_id_ignored():
    """Duplicate MsgId should be silently dropped."""
    handler = AsyncMock(return_value="reply")
    adapter = _make_adapter(handler)
    adapter.send_response = AsyncMock()

    xml = _xml_text_msg(msg_id="dup_001")
    await adapter.handle_event(xml)
    reply2 = await adapter.handle_event(xml)  # same MsgId

    assert reply2 == ""  # second call returns empty


@pytest.mark.asyncio
async def test_handle_image_message_sends_hint():
    """Non-text messages should return ACK and schedule an unsupported hint."""
    import asyncio

    handler = AsyncMock(return_value="reply")
    adapter = _make_adapter(handler)
    adapter.send_response = AsyncMock()

    xml = _xml_image_msg()
    reply = await adapter.handle_event(xml)

    # Still returns passive reply
    assert "<xml>" in reply
    handler.assert_not_called()

    await asyncio.sleep(0.05)
    # send_response should have been called with unsupported hint
    adapter.send_response.assert_called_once()
    hint_text = adapter.send_response.call_args[0][1]
    assert "image" in hint_text or "不支持" in hint_text


@pytest.mark.asyncio
async def test_handle_invalid_xml_returns_empty():
    adapter = _make_adapter()
    reply = await adapter.handle_event("not xml at all")
    assert reply == ""


@pytest.mark.asyncio
async def test_handle_empty_content_not_dispatched():
    handler = AsyncMock(return_value="reply")
    adapter = _make_adapter(handler)

    xml = _xml_text_msg(content="   ", msg_id="200")
    await adapter.handle_event(xml)

    handler.assert_not_called()


@pytest.mark.asyncio
async def test_handle_signature_failure_returns_empty():
    adapter = _make_adapter()
    xml = _xml_text_msg(msg_id="300")
    reply = await adapter.handle_event(xml, signature="bad_sig", timestamp="ts", nonce="nc")
    assert reply == ""


# ── code2session ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_code2session_success():
    adapter = _make_adapter()
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "openid": "oABC123",
        "session_key": "sk_xxx",
        "errcode": 0,
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(adapter._client, "get", new=AsyncMock(return_value=mock_response)):
        data = await adapter.code2session("test_code")
    assert data["openid"] == "oABC123"


@pytest.mark.asyncio
async def test_code2session_error_raises():
    adapter = _make_adapter()
    mock_response = MagicMock()
    mock_response.json.return_value = {"errcode": 40029, "errmsg": "invalid code"}
    mock_response.raise_for_status = MagicMock()

    with patch.object(adapter._client, "get", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ValueError, match="40029"):
            await adapter.code2session("bad_code")


# ── send_response (Customer Service API) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_send_response_calls_api():
    adapter = _make_adapter()
    adapter._access_token = "mock_token"
    adapter._token_expires = time.time() + 3600

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}

    with patch.object(adapter._client, "post", new=AsyncMock(return_value=mock_resp)) as mock_post:
        await adapter.send_response("openid_user", "Hello!")
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert payload["touser"] == "openid_user"
        assert payload["text"]["content"] == "Hello!"


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_stop():
    adapter = _make_adapter()
    await adapter.start()  # Should not raise
    with patch.object(adapter._client, "aclose", new=AsyncMock()):
        await adapter.stop()


# ── Web routes ────────────────────────────────────────────────────────────────

def _make_web_app_with_wechat(adapter=None):
    async def handler(chat_id: str, text: str) -> str:
        return f"echo: {text}"

    app = create_web_app(message_handler=handler)
    if adapter is not None:
        app.state.set_wechat_mp_adapter(adapter)
    return app


def _valid_echostr_params(token: str = "my_token"):
    ts = "1700000000"
    nonce = "nonce_abc"
    items = sorted([token, ts, nonce])
    sig = hashlib.sha1("".join(items).encode()).hexdigest()
    return {"signature": sig, "timestamp": ts, "nonce": nonce, "echostr": "echo_value"}


def test_wechat_mp_verify_no_adapter():
    app = _make_web_app_with_wechat()
    client = TestClient(app)
    resp = client.get("/webhook/wechat_mp", params=_valid_echostr_params())
    assert resp.status_code == 503


def test_wechat_mp_verify_success():
    adapter = _make_adapter()
    app = _make_web_app_with_wechat(adapter)
    client = TestClient(app)
    params = _valid_echostr_params(token=adapter.token)
    resp = client.get("/webhook/wechat_mp", params=params)
    assert resp.status_code == 200
    assert resp.text == "echo_value"


def test_wechat_mp_verify_bad_signature():
    adapter = _make_adapter()
    app = _make_web_app_with_wechat(adapter)
    client = TestClient(app)
    params = {**_valid_echostr_params(), "signature": "wrong"}
    resp = client.get("/webhook/wechat_mp", params=params)
    assert resp.status_code == 403


def test_wechat_mp_webhook_post_no_adapter():
    app = _make_web_app_with_wechat()
    client = TestClient(app)
    resp = client.post("/webhook/wechat_mp", content=_xml_text_msg())
    assert resp.status_code == 503


def test_wechat_mp_webhook_post_returns_xml():
    adapter = _make_adapter()
    # Disable signature check for this test (no signature query params)
    adapter.token = ""
    app = _make_web_app_with_wechat(adapter)
    client = TestClient(app)
    xml_body = _xml_text_msg(content="你好", msg_id="888")
    resp = client.post(
        "/webhook/wechat_mp",
        content=xml_body,
        headers={"Content-Type": "application/xml"},
    )
    assert resp.status_code == 200
    assert "<xml>" in resp.text


def test_wxmp_login_no_adapter():
    app = _make_web_app_with_wechat()
    client = TestClient(app)
    resp = client.post("/api/wxmp/login", json={"code": "test_code"})
    assert resp.status_code == 503


def test_wxmp_login_empty_code():
    adapter = _make_adapter()
    app = _make_web_app_with_wechat(adapter)
    client = TestClient(app)
    resp = client.post("/api/wxmp/login", json={"code": ""})
    assert resp.status_code == 400


def test_wxmp_login_success():
    adapter = _make_adapter()
    adapter.code2session = AsyncMock(return_value={"openid": "oXYZ456", "session_key": "sk"})
    app = _make_web_app_with_wechat(adapter)
    client = TestClient(app)
    resp = client.post("/api/wxmp/login", json={"code": "valid_code"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["chat_id"] == "wechat_mp_oXYZ456"


def test_wxmp_login_wechat_error():
    adapter = _make_adapter()
    adapter.code2session = AsyncMock(side_effect=ValueError("errcode=40029"))
    app = _make_web_app_with_wechat(adapter)
    client = TestClient(app)
    resp = client.post("/api/wxmp/login", json={"code": "bad_code"})
    assert resp.status_code == 400


# ── Config API includes wechat_mp_enabled ─────────────────────────────────────

def test_config_api_includes_wechat_mp():
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

    async def handler(chat_id: str, text: str) -> str:
        return "ok"

    app = create_web_app(message_handler=handler, settings=settings)
    client = TestClient(app)
    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert "wechat_mp_enabled" in resp.json()
