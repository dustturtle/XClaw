"""Tests for the iLink-based WeChat adapter and web endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import xclaw.channels.wechat as wechat_module
from xclaw.channels.wechat import (
    EMPTY_REPLY_MESSAGE,
    HANDLER_FAILURE_MESSAGE,
    IlinkGetConfigResponse,
    IlinkGetUpdatesResponse,
    IlinkWireMessage,
    QRCodeResponse,
    QRStatusResponse,
    UNSUPPORTED_PRIVATE_MESSAGE,
    WeChatAdapter,
    WechatAccount,
    sanitize_reply_text,
)
from xclaw.channels.web import create_web_app


class FakeIlinkClient:
    def __init__(
        self,
        *,
        qr_response: QRCodeResponse | None = None,
        qr_statuses: list[QRStatusResponse] | None = None,
        updates: list[IlinkGetUpdatesResponse] | None = None,
    ) -> None:
        self.qr_response = qr_response or QRCodeResponse(
            qrcode="qr-123",
            qrcode_img_content="https://example.com/qr",
        )
        self.qr_statuses = qr_statuses or []
        self.updates = updates or []
        self.sent_messages: list[dict[str, str]] = []
        self.config_calls: list[dict[str, str]] = []
        self.typing_calls: list[dict[str, str]] = []
        self.closed = False

    async def fetch_qrcode(self, base_url: str) -> QRCodeResponse:
        return self.qr_response

    async def poll_qr_status(self, base_url: str, qrcode: str) -> QRStatusResponse:
        if self.qr_statuses:
            return self.qr_statuses.pop(0)
        return QRStatusResponse(status="wait")

    async def get_updates(
        self,
        base_url: str,
        token: str,
        get_updates_buf: str,
        *,
        timeout_ms: int,
    ) -> IlinkGetUpdatesResponse:
        if self.updates:
            return self.updates.pop(0)
        return IlinkGetUpdatesResponse(ret=0, msgs=[], get_updates_buf=get_updates_buf)

    async def send_text_message(
        self,
        base_url: str,
        token: str,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> dict[str, int]:
        self.sent_messages.append(
            {
                "to_user_id": to_user_id,
                "text": text,
                "context_token": context_token,
            }
        )
        return {"ret": 0}

    async def get_config(
        self,
        base_url: str,
        token: str,
        *,
        to_user_id: str,
        ilink_user_id: str,
        context_token: str,
    ) -> IlinkGetConfigResponse:
        self.config_calls.append(
            {
                "to_user_id": to_user_id,
                "ilink_user_id": ilink_user_id,
                "context_token": context_token,
            }
        )
        return IlinkGetConfigResponse(ret=0, typing_ticket="fake-ticket")

    async def send_typing(
        self,
        base_url: str,
        token: str,
        *,
        to_user_id: str,
        ilink_user_id: str,
        typing_ticket: str,
        context_token: str | None = None,
        status: int | None = None,
    ) -> None:
        self.typing_calls.append(
            {
                "to_user_id": to_user_id,
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "context_token": context_token or "",
                "status": "" if status is None else str(status),
            }
        )

    async def close(self) -> None:
        self.closed = True


def _make_adapter(
    tmp_path: Path,
    *,
    handler=None,
    qr_statuses: list[QRStatusResponse] | None = None,
    updates: list[IlinkGetUpdatesResponse] | None = None,
) -> tuple[WeChatAdapter, FakeIlinkClient]:
    client = FakeIlinkClient(qr_statuses=qr_statuses, updates=updates)
    adapter = WeChatAdapter(
        base_url="https://ilinkai.weixin.qq.com",
        account_path=tmp_path / "wechat_account.json",
        state_path=tmp_path / "wechat_state.json",
        ilink_client=client,
        message_handler=handler or AsyncMock(return_value="ok"),
    )
    return adapter, client


def _save_account(adapter: WeChatAdapter) -> None:
    adapter._account_store.save(
        WechatAccount(
            bot_token="token-1",
            ilink_bot_id="bot-1",
            ilink_user_id="user-1",
            base_url="https://ilinkai.weixin.qq.com",
        )
    )


def _make_text_message(
    text: str,
    *,
    sender_id: str = "alice@im.wechat",
    context_token: str = "ctx-1",
    create_time_ms: int = 1,
    group_id: str = "",
    message_id: str | int | None = None,
) -> IlinkWireMessage:
    payload: dict[str, object] = {
        "from_user_id": sender_id,
        "group_id": group_id,
        "message_type": 1,
        "context_token": context_token,
        "create_time_ms": create_time_ms,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if message_id is not None:
        payload["message_id"] = message_id
    return IlinkWireMessage.model_validate(payload)


def _make_image_message(
    *,
    sender_id: str = "alice@im.wechat",
    context_token: str = "ctx-1",
    create_time_ms: int = 1,
) -> IlinkWireMessage:
    return IlinkWireMessage.model_validate(
        {
            "from_user_id": sender_id,
            "message_type": 1,
            "context_token": context_token,
            "create_time_ms": create_time_ms,
            "item_list": [{"type": 2}],
        }
    )


def _make_voice_message(
    transcript: str,
    *,
    sender_id: str = "alice@im.wechat",
    context_token: str = "ctx-1",
    create_time_ms: int = 1,
    message_id: str | int | None = None,
) -> IlinkWireMessage:
    payload: dict[str, object] = {
        "from_user_id": sender_id,
        "message_type": 1,
        "context_token": context_token,
        "create_time_ms": create_time_ms,
        "item_list": [{"type": 3, "voice_item": {"text": transcript}}],
    }
    if message_id is not None:
        payload["message_id"] = message_id
    return IlinkWireMessage.model_validate(payload)


@pytest.mark.asyncio
async def test_wechat_poll_once_processes_private_text(tmp_path: Path) -> None:
    handler = AsyncMock(return_value="**你好**\n\n[链接](https://example.com)")
    adapter, client = _make_adapter(
        tmp_path,
        handler=handler,
        updates=[
            IlinkGetUpdatesResponse(
                ret=0,
                get_updates_buf="buf-2",
                msgs=[_make_text_message("你好", message_id="m-1")],
            )
        ],
    )
    _save_account(adapter)

    processed = await adapter.poll_once()
    state = adapter._state_store.load()

    assert processed == 1
    assert client.sent_messages[0]["to_user_id"] == "alice@im.wechat"
    assert client.sent_messages[0]["context_token"] == "ctx-1"
    assert "**你好**" in client.sent_messages[0]["text"]
    assert "[链接](https://example.com)" in client.sent_messages[0]["text"]
    assert client.typing_calls == []
    assert state.get_updates_buf == "buf-2"
    assert state.context_tokens["alice@im.wechat"] == "ctx-1"
    assert state.last_error is None

    await adapter.close()


@pytest.mark.asyncio
async def test_wechat_poll_once_ignores_duplicate_messages(tmp_path: Path) -> None:
    handler = AsyncMock(return_value="第一次")
    message = _make_text_message("你好", message_id="dup-1")
    adapter, client = _make_adapter(
        tmp_path,
        handler=handler,
        updates=[
            IlinkGetUpdatesResponse(ret=0, get_updates_buf="buf-1", msgs=[message]),
            IlinkGetUpdatesResponse(ret=0, get_updates_buf="buf-2", msgs=[message]),
        ],
    )
    _save_account(adapter)

    await adapter.poll_once()
    await adapter.poll_once()

    assert len(client.sent_messages) == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_wechat_poll_once_sends_notice_for_non_text(tmp_path: Path) -> None:
    handler = AsyncMock(return_value="不会被调用")
    adapter, client = _make_adapter(
        tmp_path,
        handler=handler,
        updates=[IlinkGetUpdatesResponse(ret=0, msgs=[_make_image_message()])],
    )
    _save_account(adapter)

    processed = await adapter.poll_once()

    assert processed == 1
    assert client.sent_messages[0]["text"] == UNSUPPORTED_PRIVATE_MESSAGE
    handler.assert_not_called()
    await adapter.close()


@pytest.mark.asyncio
async def test_wechat_poll_once_processes_voice_transcript_as_text(tmp_path: Path) -> None:
    handler = AsyncMock(return_value="收到你的语音转写")
    adapter, client = _make_adapter(
        tmp_path,
        handler=handler,
        updates=[
            IlinkGetUpdatesResponse(
                ret=0,
                msgs=[_make_voice_message("帮我看看今天大盘", message_id="voice-1")],
            )
        ],
    )
    _save_account(adapter)

    processed = await adapter.poll_once()

    assert processed == 1
    handler.assert_awaited_once_with("alice@im.wechat", "帮我看看今天大盘")
    assert client.sent_messages[0]["text"] == "收到你的语音转写"
    await adapter.close()


@pytest.mark.asyncio
async def test_wechat_poll_once_ignores_group_messages(tmp_path: Path) -> None:
    handler = AsyncMock(return_value="不会被调用")
    adapter, client = _make_adapter(
        tmp_path,
        handler=handler,
        updates=[
            IlinkGetUpdatesResponse(
                ret=0,
                msgs=[_make_text_message("群消息", group_id="group-1", message_id="group-1")],
            )
        ],
    )
    _save_account(adapter)

    processed = await adapter.poll_once()

    assert processed == 0
    assert client.sent_messages == []
    handler.assert_not_called()
    await adapter.close()


@pytest.mark.asyncio
async def test_wechat_poll_once_sends_failure_hint_when_handler_errors(tmp_path: Path) -> None:
    handler = AsyncMock(side_effect=RuntimeError("boom"))
    adapter, client = _make_adapter(
        tmp_path,
        handler=handler,
        updates=[
            IlinkGetUpdatesResponse(
                ret=0,
                msgs=[_make_text_message("你好", message_id="m-2")],
            )
        ],
    )
    _save_account(adapter)

    processed = await adapter.poll_once()

    assert processed == 1
    assert client.sent_messages[0]["text"] == HANDLER_FAILURE_MESSAGE
    await adapter.close()


@pytest.mark.asyncio
async def test_slow_send_keeps_typing_until_reply_sent(tmp_path: Path) -> None:
    handler = AsyncMock(return_value="ok")
    adapter, client = _make_adapter(
        tmp_path,
        handler=handler,
        updates=[
            IlinkGetUpdatesResponse(
                ret=0,
                msgs=[_make_text_message("你好", message_id="m-slow-send")],
            )
        ],
    )
    _save_account(adapter)

    original_send = client.send_text_message

    async def delayed_send(
        base_url: str,
        token: str,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> dict[str, int]:
        await asyncio.sleep(0.6)
        return await original_send(base_url, token, to_user_id, text, context_token)

    client.send_text_message = delayed_send  # type: ignore[method-assign]

    processed = await adapter.poll_once()

    assert processed == 1
    assert len(client.sent_messages) == 1
    assert client.sent_messages[0]["text"] == "ok"
    assert len(client.config_calls) == 1
    assert len(client.typing_calls) == 2
    assert client.typing_calls[0]["status"] == ""
    assert client.typing_calls[1]["status"] == "2"

    await adapter.close()


@pytest.mark.asyncio
async def test_long_reply_refreshes_typing_until_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wechat_module, "TYPING_TRIGGER_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(wechat_module, "TYPING_REFRESH_INTERVAL_SECONDS", 0.05)

    async def slow_handler(_chat_id: str, _text: str) -> str:
        await asyncio.sleep(0.18)
        return "ok"

    adapter, client = _make_adapter(
        tmp_path,
        handler=slow_handler,
        updates=[
            IlinkGetUpdatesResponse(
                ret=0,
                msgs=[_make_text_message("你好", message_id="m-refresh")],
            )
        ],
    )
    _save_account(adapter)

    processed = await adapter.poll_once()

    assert processed == 1
    assert len(client.sent_messages) == 1
    assert client.sent_messages[0]["text"] == "ok"
    assert len(client.config_calls) == 1
    assert len(client.typing_calls) >= 3
    assert client.typing_calls[0]["status"] == ""
    assert client.typing_calls[-1]["status"] == "2"
    assert any(call["status"] == "" for call in client.typing_calls[1:-1])

    await adapter.close()


def test_sanitize_reply_text_preserves_markdown() -> None:
    reply = sanitize_reply_text(
        "# 标题\n\n```python\nprint('hi')\n```\n[链接](https://example.com)\n**加粗**",
        max_chars=200,
    )

    assert "# 标题" in reply
    assert "```python" in reply
    assert "[链接](https://example.com)" in reply
    assert "**加粗**" in reply


def test_sanitize_reply_text_truncates_and_handles_empty_markdown() -> None:
    reply = sanitize_reply_text("A" * 50, max_chars=20)

    assert reply.endswith("…")
    assert sanitize_reply_text("```python\n```", max_chars=100) == EMPTY_REPLY_MESSAGE


def _make_web_app_with_wechat(adapter: WeChatAdapter | None = None, settings=None):
    async def handler(chat_id: str, text: str) -> str:
        return f"echo: {text}"

    app = create_web_app(message_handler=handler, settings=settings)
    if adapter is not None:
        app.state.set_wechat_adapter(adapter)
    return app


def test_wechat_login_routes_require_adapter() -> None:
    app = _make_web_app_with_wechat()
    client = TestClient(app)

    assert client.post("/api/auth/wechat/start").status_code == 503
    assert client.get("/api/auth/wechat/session").status_code == 503
    assert client.get("/api/wechat/bot/status").status_code == 503


def test_wechat_login_flow_and_logout(tmp_path: Path) -> None:
    adapter, client_stub = _make_adapter(
        tmp_path,
        qr_statuses=[
            QRStatusResponse(status="scaned"),
            QRStatusResponse(
                status="confirmed",
                bot_token="token-1",
                ilink_bot_id="bot-1",
                ilink_user_id="user-1",
            ),
        ],
    )
    adapter.start = AsyncMock()
    app = _make_web_app_with_wechat(adapter)
    client = TestClient(app)

    start = client.post("/api/auth/wechat/start")
    assert start.status_code == 200
    assert "<svg" in start.json()["qr_svg"]

    login_id = start.json()["login_id"]
    scanned = client.get(f"/api/auth/wechat/status/{login_id}")
    confirmed = client.get(f"/api/auth/wechat/status/{login_id}")
    session = client.get("/api/auth/wechat/session")
    bot_status = client.get("/api/wechat/bot/status")
    logout = client.post("/api/auth/wechat/logout")
    session_after_logout = client.get("/api/auth/wechat/session")

    assert scanned.json()["state"] == "scanned"
    assert confirmed.json()["state"] == "confirmed"
    assert confirmed.json()["account"]["ilink_bot_id"] == "bot-1"
    assert session.json()["logged_in"] is True
    assert bot_status.json()["logged_in"] is True
    assert logout.json() == {"ok": True}
    assert session_after_logout.json()["logged_in"] is False

    asyncio.run(adapter.close())
    assert client_stub.closed is True


def test_wechat_login_status_unknown_attempt(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    app = _make_web_app_with_wechat(adapter)
    client = TestClient(app)

    resp = client.get("/api/auth/wechat/status/missing")
    assert resp.status_code == 404

    asyncio.run(adapter.close())


def test_config_api_includes_wechat_flag() -> None:
    settings = SimpleNamespace(
        llm_provider="anthropic",
        model="claude-opus-4-5",
        max_tokens=4096,
        web_enabled=True,
        web_host="127.0.0.1",
        web_port=8080,
        feishu_enabled=False,
        wecom_enabled=False,
        dingtalk_enabled=False,
        wechat_enabled=False,
        wechat_mp_enabled=False,
        qq_enabled=False,
        data_dir="./xclaw.data",
        timezone="Asia/Shanghai",
        stock_market_default="CN",
        bash_enabled=False,
        rate_limit_per_minute=20,
        multi_user_mode=False,
        enabled_skills=["all"],
        mcp_server_enabled=False,
    )

    app = _make_web_app_with_wechat(settings=settings)
    client = TestClient(app)
    resp = client.get("/api/config")

    assert resp.status_code == 200
    assert "wechat_enabled" in resp.json()


@pytest.mark.asyncio
async def test_slow_reply_fetches_typing_and_clears(tmp_path: Path) -> None:
    updates = [
        IlinkGetUpdatesResponse(
            ret=0,
            msgs=[_make_text_message("hi")],
            get_updates_buf="buf-1",
        ),
    ]
    async def slow_handler(chat_id: str, text: str) -> str:
        await asyncio.sleep(0.6)
        return "done"

    adapter, client = _make_adapter(tmp_path, updates=updates, handler=slow_handler)
    _save_account(adapter)

    processed = await adapter.poll_once()
    assert processed == 1
    assert len(client.sent_messages) == 1
    assert client.sent_messages[0]["text"] == "done"
    assert client.sent_messages[0]["to_user_id"] == "alice@im.wechat"
    assert client.sent_messages[0]["context_token"] == "ctx-1"
    assert client.config_calls == [
        {
            "to_user_id": "alice@im.wechat",
            "ilink_user_id": "alice@im.wechat",
            "context_token": "ctx-1",
        }
    ]
    assert len(client.typing_calls) == 2
    assert client.typing_calls[0]["to_user_id"] == "alice@im.wechat"
    assert client.typing_calls[0]["context_token"] == "ctx-1"
    assert client.typing_calls[0]["status"] == ""
    assert client.typing_calls[1]["status"] == "2"
    await adapter.close()


@pytest.mark.asyncio
async def test_fast_reply_skips_typing(tmp_path: Path) -> None:
    updates = [
        IlinkGetUpdatesResponse(
            ret=0,
            msgs=[_make_text_message("hi")],
            get_updates_buf="buf-1",
        ),
    ]
    adapter, client = _make_adapter(tmp_path, updates=updates)
    _save_account(adapter)

    processed = await adapter.poll_once()
    assert processed == 1
    assert len(client.sent_messages) == 1
    assert client.config_calls == []
    assert client.typing_calls == []
    await adapter.close()


@pytest.mark.asyncio
async def test_typing_getconfig_failure_does_not_block_reply(tmp_path: Path) -> None:
    updates = [
        IlinkGetUpdatesResponse(
            ret=0,
            msgs=[_make_text_message("hi")],
            get_updates_buf="buf-1",
        ),
    ]

    async def slow_handler(chat_id: str, text: str) -> str:
        await asyncio.sleep(0.6)
        return "done"

    adapter, client = _make_adapter(tmp_path, updates=updates, handler=slow_handler)
    _save_account(adapter)

    async def broken_get_config(*args, **kwargs):
        raise RuntimeError("network error")

    client.get_config = broken_get_config  # type: ignore[assignment]

    processed = await adapter.poll_once()
    assert processed == 1
    assert len(client.sent_messages) == 1
    assert client.sent_messages[0]["text"] == "done"
    assert client.typing_calls == []
    await adapter.close()


@pytest.mark.asyncio
async def test_slow_failure_clears_typing_after_fallback(tmp_path: Path) -> None:
    updates = [
        IlinkGetUpdatesResponse(
            ret=0,
            msgs=[_make_text_message("hi")],
            get_updates_buf="buf-1",
        ),
    ]

    async def slow_broken_handler(chat_id: str, text: str) -> str:
        await asyncio.sleep(0.6)
        raise RuntimeError("boom")

    adapter, client = _make_adapter(tmp_path, updates=updates, handler=slow_broken_handler)
    _save_account(adapter)

    processed = await adapter.poll_once()
    assert processed == 1
    assert len(client.sent_messages) == 1
    assert client.sent_messages[0]["text"] == HANDLER_FAILURE_MESSAGE
    assert len(client.typing_calls) == 2
    assert client.typing_calls[1]["status"] == "2"
    await adapter.close()
