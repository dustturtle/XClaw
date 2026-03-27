"""Tests for WeChat multi-tenant invite flow and polling manager."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from xclaw.channels.wechat import IlinkGetUpdatesResponse, IlinkWireMessage, QRCodeResponse, QRStatusResponse
from xclaw.channels.wechat_multi_tenant import WeChatMultiTenantService, build_member_chat_id
from xclaw.channels.web import create_web_app
from xclaw.db import Database


class FakeIlinkClient:
    def __init__(self) -> None:
        self.qr_index = 0
        self.status_by_qr: dict[str, QRStatusResponse] = {}
        self.updates_by_token: dict[str, list[IlinkGetUpdatesResponse]] = {}
        self.sent_messages: list[dict[str, str]] = []
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

    async def close(self) -> None:
        self.closed = True


def _make_service(
    db: Database,
    *,
    ilink_client: FakeIlinkClient,
    handler=None,
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
    assert ilink_client.sent_messages[0]["to_user_id"] == "alice@im.wechat"
    assert ilink_client.sent_messages[1]["to_user_id"] == "bob@im.wechat"

    await service.stop()
    assert ilink_client.closed is True


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
