"""Tests for WeChat multi-tenant invite flow and polling manager."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import xclaw.channels.wechat_multi_tenant as wechat_multi_tenant_module
from xclaw.channels.wechat import (
    IlinkGetConfigResponse,
    IlinkGetUpdatesResponse,
    IlinkClientError,
    IlinkWireMessage,
    QRCodeResponse,
    QRStatusResponse,
)
from xclaw.channels.wechat_multi_tenant import WeChatMultiTenantService, build_member_chat_id
from xclaw.channels.web import create_web_app
from xclaw.db import Database


class FakeIlinkClient:
    def __init__(self) -> None:
        self.qr_index = 0
        self.status_by_qr: dict[str, QRStatusResponse] = {}
        self.updates_by_token: dict[str, list[IlinkGetUpdatesResponse]] = {}
        self.sent_messages: list[dict[str, str]] = []
        self.sent_images: list[dict[str, str | None]] = []
        self.sent_files: list[dict[str, str | None]] = []
        self.config_calls: list[dict[str, str]] = []
        self.typing_calls: list[dict[str, str]] = []
        self.closed = False

    async def fetch_qrcode(self, base_url: str) -> QRCodeResponse:
        self.qr_index += 1
        qrcode = f"qr-{self.qr_index}"
        return QRCodeResponse(
            qrcode=qrcode,
            qrcode_img_content=f"https://example.com/{qrcode}.png",
        )

    async def poll_qr_status(self, base_url: str, qrcode: str) -> QRStatusResponse:
        return self.status_by_qr.get(qrcode, QRStatusResponse(status="wait"))

    async def get_updates(
        self,
        base_url: str,
        token: str,
        get_updates_buf: str,
        *,
        timeout_ms: int,
    ) -> IlinkGetUpdatesResponse:
        responses = self.updates_by_token.get(token, [])
        if responses:
            return responses.pop(0)
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
                "token": token,
                "to_user_id": to_user_id,
                "text": text,
                "context_token": context_token,
            }
        )
        return {"ret": 0}

    async def send_image_message(
        self,
        base_url: str,
        token: str,
        to_user_id: str,
        filename: str,
        content_base64: str,
        context_token: str | None = None,
    ) -> dict[str, int]:
        self.sent_images.append(
            {
                "token": token,
                "to_user_id": to_user_id,
                "filename": filename,
                "content_base64": content_base64,
                "context_token": context_token,
            }
        )
        return {"ret": 0}

    async def send_file_message(
        self,
        base_url: str,
        token: str,
        to_user_id: str,
        filename: str,
        content_base64: str,
        context_token: str | None = None,
    ) -> dict[str, int]:
        self.sent_files.append(
            {
                "token": token,
                "to_user_id": to_user_id,
                "filename": filename,
                "content_base64": content_base64,
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
                "token": token,
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
                "token": token,
                "to_user_id": to_user_id,
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "context_token": context_token or "",
                "status": "" if status is None else str(status),
            }
        )

    async def close(self) -> None:
        self.closed = True


def _make_service(
    db: Database,
    *,
    ilink_client: FakeIlinkClient,
    handler=None,
    debug_log_path: Path | None = None,
) -> WeChatMultiTenantService:
    return WeChatMultiTenantService(
        db=db,
        base_url="https://ilinkai.weixin.qq.com",
        qr_poll_interval_seconds=1,
        invite_refresh_seconds=45,
        invite_session_total_timeout_seconds=90,
        poll_timeout_ms=25_000,
        max_reply_chars=1500,
        message_handler=handler or AsyncMock(return_value="ok"),
        ilink_client=ilink_client,
        debug_log_path=debug_log_path,
    )


def _make_text_message(
    text: str,
    *,
    sender_id: str,
    context_token: str,
    message_id: str,
) -> IlinkWireMessage:
    return IlinkWireMessage.model_validate(
        {
            "from_user_id": sender_id,
            "message_type": 1,
            "context_token": context_token,
            "message_id": message_id,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
    )


def test_admin_and_invite_session_flow(db: Database) -> None:
    ilink_client = FakeIlinkClient()
    service = _make_service(db, ilink_client=ilink_client)

    async def handler(chat_id: str, text: str) -> str:
        return f"echo: {text}"

    app = create_web_app(message_handler=handler)
    app.state.set_wechat_multi_tenant_service(service)
    client = TestClient(app)

    tenant = client.post("/api/admin/tenants", json={"name": "Tenant A"})
    tenant_id = tenant.json()["tenant_id"]
    link = client.post(f"/api/admin/tenants/{tenant_id}/invite-links", json={})
    public_token = link.json()["public_token"]

    session1 = client.post(f"/api/invite/{public_token}/sessions")
    session2 = client.post(f"/api/invite/{public_token}/sessions")

    assert session1.status_code == 200
    assert session2.status_code == 200
    assert session1.json()["invite_session_id"] != session2.json()["invite_session_id"]
    assert link.json()["invite_url"].endswith(f"/invite/{public_token}")

    asyncio.run(service.stop())


def test_invite_session_refresh_and_confirm_binding(db: Database) -> None:
    ilink_client = FakeIlinkClient()
    service = _make_service(db, ilink_client=ilink_client)

    async def handler(chat_id: str, text: str) -> str:
        return f"echo: {text}"

    app = create_web_app(message_handler=handler)
    app.state.set_wechat_multi_tenant_service(service)
    client = TestClient(app)

    tenant = client.post("/api/admin/tenants", json={"name": "Tenant A"})
    tenant_id = tenant.json()["tenant_id"]
    link = client.post(f"/api/admin/tenants/{tenant_id}/invite-links", json={})
    public_token = link.json()["public_token"]

    session = client.post(f"/api/invite/{public_token}/sessions")
    first_session_id = session.json()["invite_session_id"]
    refreshed = client.post(f"/api/invite/sessions/{first_session_id}/refresh")
    second_session_id = refreshed.json()["invite_session_id"]
    old_status = client.get(f"/api/invite/sessions/{first_session_id}")

    assert refreshed.status_code == 200
    assert second_session_id != first_session_id
    assert old_status.json()["state"] == "superseded"

    second = asyncio.run(db.get_invite_session(second_session_id))
    assert second is not None
    ilink_client.status_by_qr[second["qrcode"]] = QRStatusResponse(
        status="confirmed",
        ilink_user_id="user-1",
        bot_token="token-1",
        ilink_bot_id="bot-1",
    )
    confirmed = client.get(f"/api/invite/sessions/{second_session_id}")
    members = client.get(f"/api/admin/tenants/{tenant_id}/members")

    assert confirmed.json()["state"] == "confirmed"
    assert len(members.json()) == 1
    assert members.json()[0]["ilink_user_id"] == "user-1"

    asyncio.run(service.stop())


@pytest.mark.asyncio
async def test_multitenant_manager_isolates_member_chat_ids(db: Database) -> None:
    ilink_client = FakeIlinkClient()
    handler = AsyncMock(side_effect=lambda chat_id, text: f"{chat_id} => {text}")
    service = _make_service(db, ilink_client=ilink_client, handler=handler)

    tenant = await db.create_tenant("Tenant A")
    link = await db.create_invite_link(tenant["tenant_id"])

    session1 = await db.create_invite_session(
        link_id=link["link_id"],
        tenant_id=tenant["tenant_id"],
        qrcode="qr-a",
        qr_content="https://example.com/a.png",
        ttl_seconds=90,
    )
    _, member1, credential1 = await db.bind_invite_session(
        session1["invite_session_id"],
        ilink_user_id="alice@im.wechat",
        bot_token="token-1",
        ilink_bot_id="bot-1",
        default_base_url="https://ilinkai.weixin.qq.com",
    )

    session2 = await db.create_invite_session(
        link_id=link["link_id"],
        tenant_id=tenant["tenant_id"],
        qrcode="qr-b",
        qr_content="https://example.com/b.png",
        ttl_seconds=90,
    )
    _, member2, credential2 = await db.bind_invite_session(
        session2["invite_session_id"],
        ilink_user_id="bob@im.wechat",
        bot_token="token-2",
        ilink_bot_id="bot-2",
        default_base_url="https://ilinkai.weixin.qq.com",
    )

    ilink_client.updates_by_token["token-1"] = [
        IlinkGetUpdatesResponse(
            ret=0,
            get_updates_buf="buf-1",
            msgs=[
                _make_text_message(
                    "你好",
                    sender_id="alice@im.wechat",
                    context_token="ctx-a",
                    message_id="m-a",
                )
            ],
        )
    ]
    ilink_client.updates_by_token["token-2"] = [
        IlinkGetUpdatesResponse(
            ret=0,
            get_updates_buf="buf-2",
            msgs=[
                _make_text_message(
                    "在吗",
                    sender_id="bob@im.wechat",
                    context_token="ctx-b",
                    message_id="m-b",
                )
            ],
        )
    ]

    processed1 = await service.manager.poll_credential_once(credential1["credential_id"])
    processed2 = await service.manager.poll_credential_once(credential2["credential_id"])

    assert processed1 == 1
    assert processed2 == 1
    assert handler.await_args_list[0].args[0] == build_member_chat_id(
        tenant["tenant_id"], member1["member_id"]
    )
    assert handler.await_args_list[1].args[0] == build_member_chat_id(
        tenant["tenant_id"], member2["member_id"]
    )
    assert len(ilink_client.sent_messages) == 2
    assert ilink_client.sent_messages[0]["to_user_id"] == "alice@im.wechat"
    assert ilink_client.sent_messages[1]["to_user_id"] == "bob@im.wechat"
    assert ilink_client.typing_calls == []

    await service.stop()
    assert ilink_client.closed is True


@pytest.mark.asyncio
async def test_multitenant_slow_reply_fetches_typing_and_clears(db: Database) -> None:
    ilink_client = FakeIlinkClient()
    async def slow_handler(chat_id: str, text: str) -> str:
        await asyncio.sleep(0.6)
        return "ok"

    service = _make_service(db, ilink_client=ilink_client, handler=slow_handler)

    tenant = await db.create_tenant("Tenant A")
    link = await db.create_invite_link(tenant["tenant_id"])
    session = await db.create_invite_session(
        link_id=link["link_id"],
        tenant_id=tenant["tenant_id"],
        qrcode="qr-a",
        qr_content="https://example.com/a.png",
        ttl_seconds=90,
    )
    _, member, credential = await db.bind_invite_session(
        session["invite_session_id"],
        ilink_user_id="alice@im.wechat",
        bot_token="token-1",
        ilink_bot_id="bot-1",
        default_base_url="https://ilinkai.weixin.qq.com",
    )

    await db.update_runtime_state(
        member["member_id"],
        tenant_id=tenant["tenant_id"],
        context_token="ctx-a",
    )

    ilink_client.updates_by_token["token-1"] = [
        IlinkGetUpdatesResponse(
            ret=0,
            get_updates_buf="buf-1",
            msgs=[
                _make_text_message(
                    "hello",
                    sender_id="alice@im.wechat",
                    context_token="ctx-a",
                    message_id="m-1",
                )
            ],
        ),
    ]

    processed = await service.manager.poll_credential_once(credential["credential_id"])
    assert processed == 1
    assert len(ilink_client.sent_messages) == 1
    assert ilink_client.sent_messages[0]["text"] == "ok"
    assert ilink_client.config_calls == [
        {
            "token": "token-1",
            "to_user_id": "alice@im.wechat",
            "ilink_user_id": "alice@im.wechat",
            "context_token": "ctx-a",
        }
    ]
    assert len(ilink_client.typing_calls) == 2
    assert ilink_client.typing_calls[0]["context_token"] == "ctx-a"
    assert ilink_client.typing_calls[0]["status"] == ""
    assert ilink_client.typing_calls[1]["status"] == "2"

    await service.stop()


@pytest.mark.asyncio
async def test_multitenant_poll_records_context_token_debug_log(db: Database, tmp_path: Path) -> None:
    ilink_client = FakeIlinkClient()
    service = _make_service(
        db,
        ilink_client=ilink_client,
        debug_log_path=tmp_path / "wechat_context_debug.jsonl",
    )

    tenant = await db.create_tenant("Tenant A")
    link = await db.create_invite_link(tenant["tenant_id"])
    session = await db.create_invite_session(
        link_id=link["link_id"],
        tenant_id=tenant["tenant_id"],
        qrcode="qr-a",
        qr_content="https://example.com/a.png",
        ttl_seconds=90,
    )
    _, member, credential = await db.bind_invite_session(
        session["invite_session_id"],
        ilink_user_id="alice@im.wechat",
        bot_token="token-1",
        ilink_bot_id="bot-1",
        default_base_url="https://ilinkai.weixin.qq.com",
    )
    await db.update_runtime_state(
        member["member_id"],
        tenant_id=tenant["tenant_id"],
        context_token="ctx-old",
    )
    ilink_client.updates_by_token["token-1"] = [
        IlinkGetUpdatesResponse(
            ret=0,
            get_updates_buf="buf-1",
            msgs=[
                _make_text_message(
                    "hello",
                    sender_id="alice@im.wechat",
                    context_token="ctx-new",
                    message_id="m-1",
                )
            ],
        )
    ]

    processed = await service.manager.poll_credential_once(credential["credential_id"])

    assert processed == 1
    lines = (tmp_path / "wechat_context_debug.jsonl").read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(lines[-1])
    assert payload["scope"] == "multi"
    assert payload["member_id"] == member["member_id"]
    assert payload["previous_context_token"] == "ctx-old"
    assert payload["context_token"] == "ctx-new"
    assert payload["changed"] is True

    await service.stop()


@pytest.mark.asyncio
async def test_multitenant_slow_send_keeps_typing_until_reply_sent(db: Database) -> None:
    ilink_client = FakeIlinkClient()
    handler = AsyncMock(return_value="ok")
    service = _make_service(db, ilink_client=ilink_client, handler=handler)

    tenant = await db.create_tenant("Tenant A")
    link = await db.create_invite_link(tenant["tenant_id"])
    session = await db.create_invite_session(
        link_id=link["link_id"],
        tenant_id=tenant["tenant_id"],
        qrcode="qr-a",
        qr_content="https://example.com/a.png",
        ttl_seconds=90,
    )
    _, member, credential = await db.bind_invite_session(
        session["invite_session_id"],
        ilink_user_id="alice@im.wechat",
        bot_token="token-1",
        ilink_bot_id="bot-1",
        default_base_url="https://ilinkai.weixin.qq.com",
    )

    await db.update_runtime_state(
        member["member_id"],
        tenant_id=tenant["tenant_id"],
        context_token="ctx-a",
    )

    original_send = ilink_client.send_text_message

    async def delayed_send(
        base_url: str,
        token: str,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> dict[str, int]:
        await asyncio.sleep(0.6)
        return await original_send(base_url, token, to_user_id, text, context_token)

    ilink_client.send_text_message = delayed_send  # type: ignore[method-assign]

    ilink_client.updates_by_token["token-1"] = [
        IlinkGetUpdatesResponse(
            ret=0,
            get_updates_buf="buf-1",
            msgs=[
                _make_text_message(
                    "hello",
                    sender_id="alice@im.wechat",
                    context_token="ctx-a",
                    message_id="m-1",
                )
            ],
        ),
    ]

    processed = await service.manager.poll_credential_once(credential["credential_id"])
    assert processed == 1
    assert len(ilink_client.sent_messages) == 1
    assert ilink_client.sent_messages[0]["text"] == "ok"
    assert len(ilink_client.config_calls) == 1
    assert len(ilink_client.typing_calls) == 2
    assert ilink_client.typing_calls[0]["status"] == ""
    assert ilink_client.typing_calls[1]["status"] == "2"

    await service.stop()


@pytest.mark.asyncio
async def test_multitenant_long_reply_refreshes_typing_until_completion(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wechat_multi_tenant_module, "TYPING_TRIGGER_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(wechat_multi_tenant_module, "TYPING_REFRESH_INTERVAL_SECONDS", 0.05)

    async def slow_handler(_chat_id: str, _text: str) -> str:
        await asyncio.sleep(0.18)
        return "ok"

    ilink_client = FakeIlinkClient()
    service = _make_service(db, ilink_client=ilink_client, handler=slow_handler)

    tenant = await db.create_tenant("Tenant A")
    link = await db.create_invite_link(tenant["tenant_id"])
    session = await db.create_invite_session(
        link_id=link["link_id"],
        tenant_id=tenant["tenant_id"],
        qrcode="qr-a",
        qr_content="https://example.com/a.png",
        ttl_seconds=90,
    )
    _, member, credential = await db.bind_invite_session(
        session["invite_session_id"],
        ilink_user_id="alice@im.wechat",
        bot_token="token-1",
        ilink_bot_id="bot-1",
        default_base_url="https://ilinkai.weixin.qq.com",
    )

    await db.update_runtime_state(
        member["member_id"],
        tenant_id=tenant["tenant_id"],
        context_token="ctx-a",
    )

    ilink_client.updates_by_token["token-1"] = [
        IlinkGetUpdatesResponse(
            ret=0,
            get_updates_buf="buf-1",
            msgs=[
                _make_text_message(
                    "hello",
                    sender_id="alice@im.wechat",
                    context_token="ctx-a",
                    message_id="m-refresh",
                )
            ],
        ),
    ]

    processed = await service.manager.poll_credential_once(credential["credential_id"])
    assert processed == 1
    assert len(ilink_client.sent_messages) == 1
    assert ilink_client.sent_messages[0]["text"] == "ok"
    assert len(ilink_client.config_calls) == 1
    assert len(ilink_client.typing_calls) >= 3
    assert ilink_client.typing_calls[0]["status"] == ""
    assert ilink_client.typing_calls[-1]["status"] == "2"
    assert any(call["status"] == "" for call in ilink_client.typing_calls[1:-1])

    await service.stop()


@pytest.mark.asyncio
async def test_multitenant_service_send_response_to_member_chat(db: Database) -> None:
    ilink_client = FakeIlinkClient()
    service = _make_service(db, ilink_client=ilink_client)

    tenant = await db.create_tenant("Tenant A")
    link = await db.create_invite_link(tenant["tenant_id"])
    session = await db.create_invite_session(
        link_id=link["link_id"],
        tenant_id=tenant["tenant_id"],
        qrcode="qr-send",
        qr_content="https://example.com/send.png",
        ttl_seconds=90,
    )
    _, member, _credential = await db.bind_invite_session(
        session["invite_session_id"],
        ilink_user_id="alice@im.wechat",
        bot_token="token-send",
        ilink_bot_id="bot-send",
        default_base_url="https://ilinkai.weixin.qq.com",
    )
    await db.update_runtime_state(
        member["member_id"],
        tenant_id=tenant["tenant_id"],
        context_token="ctx-send",
    )

    await service.send_response(build_member_chat_id(tenant["tenant_id"], member["member_id"]), "提醒内容")

    assert len(ilink_client.sent_messages) == 1
    assert ilink_client.sent_messages[0]["to_user_id"] == "alice@im.wechat"
    assert ilink_client.sent_messages[0]["text"] == "提醒内容"
    assert ilink_client.sent_messages[0]["context_token"] == "ctx-send"

    await service.stop()


@pytest.mark.asyncio
async def test_multitenant_service_send_image_and_file_to_member_chat(
    db: Database,
    tmp_path: Path,
) -> None:
    ilink_client = FakeIlinkClient()
    service = _make_service(db, ilink_client=ilink_client)

    tenant = await db.create_tenant("Tenant A")
    link = await db.create_invite_link(tenant["tenant_id"])
    session = await db.create_invite_session(
        link_id=link["link_id"],
        tenant_id=tenant["tenant_id"],
        qrcode="qr-send-media",
        qr_content="https://example.com/send-media.png",
        ttl_seconds=90,
    )
    _, member, _credential = await db.bind_invite_session(
        session["invite_session_id"],
        ilink_user_id="alice@im.wechat",
        bot_token="token-send",
        ilink_bot_id="bot-send",
        default_base_url="https://ilinkai.weixin.qq.com",
    )
    await db.update_runtime_state(
        member["member_id"],
        tenant_id=tenant["tenant_id"],
        context_token="ctx-send-media",
    )

    image_path = tmp_path / "report.png"
    file_path = tmp_path / "report.pdf"
    image_path.write_bytes(b"png-data")
    file_path.write_bytes(b"pdf-data")

    chat_id = build_member_chat_id(tenant["tenant_id"], member["member_id"])
    await service.send_image_response(chat_id, image_path)
    await service.send_file_response(chat_id, file_path)

    assert ilink_client.sent_images[0]["to_user_id"] == "alice@im.wechat"
    assert ilink_client.sent_images[0]["filename"] == "report.png"
    assert ilink_client.sent_images[0]["context_token"] is None
    assert ilink_client.sent_files[0]["to_user_id"] == "alice@im.wechat"
    assert ilink_client.sent_files[0]["filename"] == "report.pdf"
    assert ilink_client.sent_files[0]["context_token"] is None

    await service.stop()


@pytest.mark.asyncio
async def test_multitenant_service_send_response_retries_after_context_refresh(
    db: Database,
) -> None:
    ilink_client = FakeIlinkClient()
    service = _make_service(db, ilink_client=ilink_client)

    tenant = await db.create_tenant("Tenant A")
    link = await db.create_invite_link(tenant["tenant_id"])
    session = await db.create_invite_session(
        link_id=link["link_id"],
        tenant_id=tenant["tenant_id"],
        qrcode="qr-retry",
        qr_content="https://example.com/retry.png",
        ttl_seconds=90,
    )
    _, member, _credential = await db.bind_invite_session(
        session["invite_session_id"],
        ilink_user_id="alice@im.wechat",
        bot_token="token-send",
        ilink_bot_id="bot-send",
        default_base_url="https://ilinkai.weixin.qq.com",
    )
    await db.update_runtime_state(
        member["member_id"],
        tenant_id=tenant["tenant_id"],
        context_token="ctx-old",
    )

    original_send = ilink_client.send_text_message
    seen_context_tokens: list[str] = []

    async def flaky_send(
        base_url: str,
        token: str,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> dict[str, int]:
        seen_context_tokens.append(context_token)
        if context_token == "ctx-old":
            await db.update_runtime_state(
                member["member_id"],
                tenant_id=tenant["tenant_id"],
                context_token="ctx-new",
            )
            raise IlinkClientError("sendmessage failed: errcode=-14 errmsg=session timeout")
        return await original_send(base_url, token, to_user_id, text, context_token)

    ilink_client.send_text_message = flaky_send  # type: ignore[assignment]

    await service.send_response(build_member_chat_id(tenant["tenant_id"], member["member_id"]), "提醒内容")

    assert seen_context_tokens == ["ctx-old", "ctx-new"]
    assert ilink_client.sent_messages[0]["context_token"] == "ctx-new"

    await service.stop()
