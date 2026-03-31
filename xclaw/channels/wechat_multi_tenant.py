"""Multi-tenant invite and per-credential polling for iLink WeChat."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Coroutine

from loguru import logger
from pydantic import BaseModel

from xclaw.channels.wechat import (
    HANDLER_FAILURE_MESSAGE,
    UNSUPPORTED_PRIVATE_MESSAGE,
    HttpIlinkClient,
    IlinkClient,
    IlinkClientError,
    QRStatusResponse,
    normalize_polled_message,
    render_qr_svg,
    sanitize_reply_text,
)
from xclaw.db import Database

BACKOFF_DELAYS_SECONDS = (1, 3, 10)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_member_chat_id(tenant_id: str, member_id: str) -> str:
    return f"tenant:{tenant_id}:member:{member_id}"


def parse_member_chat_id(chat_id: str) -> tuple[str, str]:
    prefix = "tenant:"
    separator = ":member:"
    if not chat_id.startswith(prefix) or separator not in chat_id:
        raise ValueError(f"Invalid multi-tenant member chat_id: {chat_id}")
    tenant_id, member_id = chat_id[len(prefix):].split(separator, 1)
    if not tenant_id or not member_id:
        raise ValueError(f"Invalid multi-tenant member chat_id: {chat_id}")
    return tenant_id, member_id


class EntityStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    INACTIVE = "inactive"
    FAILED = "failed"


class InviteSessionState(StrEnum):
    PENDING = "pending"
    SCANNED = "scanned"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    ERROR = "error"


class InviteSessionNotFoundError(RuntimeError):
    """Raised when an invite session cannot be found."""


class InviteLinkUnavailableError(RuntimeError):
    """Raised when an invite link is disabled or invalid."""


class TenantRecord(BaseModel):
    tenant_id: str
    name: str
    status: EntityStatus
    created_at: str


class InviteLinkRecord(BaseModel):
    link_id: str
    tenant_id: str
    public_token: str
    status: EntityStatus
    max_uses: int | None = None
    expires_at: str | None = None
    created_at: str


class InviteSessionRecord(BaseModel):
    invite_session_id: str
    link_id: str
    tenant_id: str
    qrcode: str
    qr_content: str
    state: InviteSessionState
    expires_at: str
    bound_member_id: str | None = None
    superseded_by: str | None = None
    error: str | None = None
    created_at: str

    def is_expired(self) -> bool:
        return datetime.fromisoformat(self.expires_at) <= _utc_now()


class TenantMemberRecord(BaseModel):
    member_id: str
    tenant_id: str
    ilink_user_id: str
    status: EntityStatus
    created_at: str
    last_bound_at: str


class ChannelCredentialRecord(BaseModel):
    credential_id: str
    member_id: str
    tenant_id: str
    bot_token: str
    ilink_bot_id: str
    base_url: str
    get_updates_buf: str = ""
    status: EntityStatus
    bound_at: str


class MemberRuntimeStateRecord(BaseModel):
    member_id: str
    tenant_id: str
    context_token: str = ""
    last_poll_at: str | None = None
    last_error: str | None = None


class InviteSessionStartResponse(BaseModel):
    invite_session_id: str
    qr_svg: str
    expires_at: str
    refresh_after_ms: int
    poll_interval_ms: int


class InviteSessionStatusPayload(BaseModel):
    state: InviteSessionState
    tenant_id: str
    member_id: str | None = None
    error: str | None = None


class CreateTenantRequest(BaseModel):
    name: str


class CreateTenantResponse(BaseModel):
    tenant_id: str
    name: str
    status: EntityStatus
    created_at: str


class CreateInviteLinkRequest(BaseModel):
    max_uses: int | None = None
    expires_at: str | None = None


class CreateInviteLinkResponse(BaseModel):
    link_id: str
    tenant_id: str
    public_token: str
    invite_url: str
    status: EntityStatus
    created_at: str


class TenantSummaryPayload(BaseModel):
    tenant_id: str
    name: str
    status: EntityStatus
    invite_link_count: int
    member_count: int
    active_credential_count: int


class TenantMemberPayload(BaseModel):
    member_id: str
    tenant_id: str
    ilink_user_id: str
    status: EntityStatus
    current_ilink_bot_id: str | None = None
    credential_status: EntityStatus | None = None
    last_poll_at: str | None = None
    last_error: str | None = None
    created_at: str
    last_bound_at: str


def build_invite_page(public_token: str) -> str:
    safe_token = json.dumps(public_token)
    title = html.escape(public_token)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>XClaw 邀请 - {title}</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "PingFang SC", "Hiragino Sans GB", sans-serif;
      background: linear-gradient(180deg, #f7f3eb 0%, #efe5d6 100%);
      color: #2e2418;
      display: grid;
      place-items: center;
      padding: 24px;
    }}
    .card {{
      width: min(100%, 460px);
      background: rgba(255, 252, 246, 0.92);
      border: 1px solid rgba(69, 48, 22, 0.1);
      border-radius: 28px;
      padding: 28px;
      box-shadow: 0 20px 60px rgba(69, 48, 22, 0.08);
    }}
    .status {{
      margin-top: 16px;
      padding: 14px 16px;
      border-radius: 16px;
      background: #f3eadc;
      color: #6b553d;
      font-size: 14px;
    }}
    .qr-box {{
      margin-top: 18px;
      min-height: 252px;
      border-radius: 20px;
      background: #fff;
      display: grid;
      place-items: center;
      border: 1px dashed rgba(69, 48, 22, 0.18);
      overflow: hidden;
      padding: 16px;
    }}
    .qr-box svg {{
      width: min(100%, 240px);
      height: auto;
    }}
    button {{
      margin-top: 18px;
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      background: #a14e2d;
      color: #fff;
      cursor: pointer;
    }}
  </style>
</head>
<body>
  <main class="card">
    <h1>微信邀请绑定</h1>
    <p>用微信扫码确认后，XClaw 会把你绑定到对应租户。</p>
    <div id="status" class="status">正在生成二维码…</div>
    <div id="qr-box" class="qr-box">准备中…</div>
    <button id="refresh-btn" type="button">刷新二维码</button>
  </main>
  <script>
    const publicToken = {safe_token};
    let currentSessionId = "";
    let refreshTimer = null;
    let pollTimer = null;
    let isRefreshing = false;
    const statusEl = document.getElementById("status");
    const qrBoxEl = document.getElementById("qr-box");
    const refreshBtn = document.getElementById("refresh-btn");

    function setStatus(text) {{
      statusEl.textContent = text;
    }}

    function stopTimers() {{
      if (refreshTimer) {{
        clearTimeout(refreshTimer);
        refreshTimer = null;
      }}
      if (pollTimer) {{
        clearTimeout(pollTimer);
        pollTimer = null;
      }}
    }}

    async function startSession() {{
      stopTimers();
      isRefreshing = false;
      setStatus("正在生成二维码...");
      const response = await fetch(`/api/invite/${{publicToken}}/sessions`, {{
        method: "POST",
        headers: {{ "Accept": "application/json" }},
      }});
      if (!response.ok) {{
        const error = await response.json().catch(() => ({{ detail: "生成二维码失败" }}));
        throw new Error(error.detail || "生成二维码失败");
      }}
      const payload = await response.json();
      currentSessionId = payload.invite_session_id;
      qrBoxEl.innerHTML = payload.qr_svg;
      setStatus("请在 1 分钟内使用微信扫码并确认。");
      refreshTimer = setTimeout(refreshSession, payload.refresh_after_ms);
      pollStatus();
    }}

    async function refreshSession() {{
      if (isRefreshing) {{
        return;
      }}
      stopTimers();
      if (!currentSessionId) {{
        return startSession();
      }}
      isRefreshing = true;
      try {{
        const response = await fetch(`/api/invite/sessions/${{currentSessionId}}/refresh`, {{
          method: "POST",
          headers: {{ "Accept": "application/json" }},
        }});
        if (!response.ok) {{
          isRefreshing = false;
          return startSession();
        }}
        const payload = await response.json();
        currentSessionId = payload.invite_session_id;
        qrBoxEl.innerHTML = payload.qr_svg;
        setStatus("二维码已刷新，请重新扫码。");
        refreshTimer = setTimeout(refreshSession, payload.refresh_after_ms);
        pollStatus();
      }} finally {{
        isRefreshing = false;
      }}
    }}

    async function pollStatus() {{
      if (!currentSessionId) {{
        return;
      }}
      const response = await fetch(`/api/invite/sessions/${{currentSessionId}}`, {{
        headers: {{ "Accept": "application/json" }},
      }});
      if (!response.ok) {{
        setStatus("轮询扫码状态失败，请稍后刷新页面。");
        return;
      }}
      const payload = await response.json();
      if (payload.state === "pending") {{
        setStatus("等待扫码中...");
      }} else if (payload.state === "scanned") {{
        setStatus("已扫码，请在微信里确认。");
      }} else if (payload.state === "confirmed") {{
        stopTimers();
        setStatus("绑定成功，请直接到微信里和 XClaw 对话。");
        return;
      }} else if (payload.state === "expired" || payload.state === "superseded") {{
        return refreshSession();
      }} else {{
        setStatus(payload.error || "当前二维码不可用，正在尝试刷新...");
        return refreshSession();
      }}
      pollTimer = setTimeout(pollStatus, 1000);
    }}

    function showError(err) {{
      console.error(err);
      isRefreshing = false;
      setStatus(err.message || "生成二维码失败，请稍后再试。");
      if (!qrBoxEl.innerHTML || qrBoxEl.textContent === "准备中…") {{
        qrBoxEl.textContent = "暂时无法生成二维码";
      }}
    }}

    refreshBtn.addEventListener("click", () => {{
      refreshSession().catch(showError);
    }});

    startSession().catch(showError);
  </script>
</body>
</html>
"""


@dataclass(slots=True)
class InviteService:
    base_url: str
    db: Database
    ilink_client: IlinkClient
    qr_poll_interval_seconds: int
    invite_refresh_seconds: int
    invite_session_total_timeout_seconds: int
    on_credential_bound: Callable[[str], Coroutine[Any, Any, None]] | None = None

    async def start_session(self, public_token: str) -> InviteSessionStartResponse:
        link_row = await self.db.get_invite_link_by_token(public_token)
        if link_row is None:
            raise InviteLinkUnavailableError("Invite link is unavailable.")
        link = InviteLinkRecord.model_validate(link_row)
        if link.status != EntityStatus.ACTIVE:
            raise InviteLinkUnavailableError("Invite link is unavailable.")
        if link.expires_at is not None and datetime.fromisoformat(link.expires_at) <= _utc_now():
            raise InviteLinkUnavailableError("Invite link has expired.")

        qr = await self.ilink_client.fetch_qrcode(self.base_url)
        session_row = await self.db.create_invite_session(
            link_id=link.link_id,
            tenant_id=link.tenant_id,
            qrcode=qr.qrcode,
            qr_content=qr.qrcode_img_content,
            ttl_seconds=self.invite_session_total_timeout_seconds,
        )
        session = InviteSessionRecord.model_validate(session_row)
        return InviteSessionStartResponse(
            invite_session_id=session.invite_session_id,
            qr_svg=render_qr_svg(qr.qrcode_img_content),
            expires_at=session.expires_at,
            refresh_after_ms=self.invite_refresh_seconds * 1000,
            poll_interval_ms=self.qr_poll_interval_seconds * 1000,
        )

    async def refresh_session(self, invite_session_id: str) -> InviteSessionStartResponse:
        current_row = await self.db.get_invite_session(invite_session_id)
        if current_row is None:
            raise InviteSessionNotFoundError(f"Unknown invite session: {invite_session_id}")

        qr = await self.ilink_client.fetch_qrcode(self.base_url)
        refreshed_row = await self.db.refresh_invite_session(
            invite_session_id,
            qrcode=qr.qrcode,
            qr_content=qr.qrcode_img_content,
            ttl_seconds=self.invite_session_total_timeout_seconds,
        )
        if refreshed_row is None:
            raise InviteSessionNotFoundError(f"Unknown invite session: {invite_session_id}")

        refreshed = InviteSessionRecord.model_validate(refreshed_row)
        return InviteSessionStartResponse(
            invite_session_id=refreshed.invite_session_id,
            qr_svg=render_qr_svg(qr.qrcode_img_content),
            expires_at=refreshed.expires_at,
            refresh_after_ms=self.invite_refresh_seconds * 1000,
            poll_interval_ms=self.qr_poll_interval_seconds * 1000,
        )

    async def poll_session(self, invite_session_id: str) -> InviteSessionStatusPayload:
        session_row = await self.db.get_invite_session(invite_session_id)
        if session_row is None:
            raise InviteSessionNotFoundError(f"Unknown invite session: {invite_session_id}")
        session = InviteSessionRecord.model_validate(session_row)

        if session.state in {
            InviteSessionState.CONFIRMED,
            InviteSessionState.EXPIRED,
            InviteSessionState.SUPERSEDED,
            InviteSessionState.ERROR,
        }:
            return InviteSessionStatusPayload(
                state=session.state,
                tenant_id=session.tenant_id,
                member_id=session.bound_member_id,
                error=session.error,
            )

        if session.is_expired():
            updated_row = await self.db.update_invite_session_state(
                invite_session_id,
                InviteSessionState.EXPIRED.value,
            )
            updated = InviteSessionRecord.model_validate(updated_row)
            return InviteSessionStatusPayload(
                state=updated.state,
                tenant_id=updated.tenant_id,
                member_id=updated.bound_member_id,
                error=updated.error,
            )

        raw_status = await self.ilink_client.poll_qr_status(self.base_url, session.qrcode)
        match raw_status.status:
            case "wait":
                return InviteSessionStatusPayload(
                    state=InviteSessionState.PENDING,
                    tenant_id=session.tenant_id,
                )
            case "scaned":
                await self.db.update_invite_session_state(
                    invite_session_id,
                    InviteSessionState.SCANNED.value,
                )
                return InviteSessionStatusPayload(
                    state=InviteSessionState.SCANNED,
                    tenant_id=session.tenant_id,
                )
            case "expired":
                updated_row = await self.db.update_invite_session_state(
                    invite_session_id,
                    InviteSessionState.EXPIRED.value,
                )
                updated = InviteSessionRecord.model_validate(updated_row)
                return InviteSessionStatusPayload(
                    state=updated.state,
                    tenant_id=updated.tenant_id,
                    member_id=updated.bound_member_id,
                    error=updated.error,
                )
            case "confirmed":
                updated_row, member_row, credential_row = await self.db.bind_invite_session(
                    invite_session_id,
                    ilink_user_id=raw_status.ilink_user_id.strip(),
                    bot_token=raw_status.bot_token.strip(),
                    ilink_bot_id=raw_status.ilink_bot_id.strip(),
                    default_base_url=self.base_url,
                    base_url=raw_status.baseurl.strip(),
                )
                updated = InviteSessionRecord.model_validate(updated_row)
                member = TenantMemberRecord.model_validate(member_row)
                credential = ChannelCredentialRecord.model_validate(credential_row)
                if self.on_credential_bound is not None:
                    await self.on_credential_bound(credential.credential_id)
                return InviteSessionStatusPayload(
                    state=updated.state,
                    tenant_id=updated.tenant_id,
                    member_id=member.member_id,
                )
            case _:
                updated_row = await self.db.update_invite_session_state(
                    invite_session_id,
                    InviteSessionState.ERROR.value,
                    error=f"Unexpected qrcode status: {raw_status.status}",
                )
                updated = InviteSessionRecord.model_validate(updated_row)
                return InviteSessionStatusPayload(
                    state=updated.state,
                    tenant_id=updated.tenant_id,
                    member_id=updated.bound_member_id,
                    error=updated.error,
                )


class MultiTenantBotManager:
    def __init__(
        self,
        *,
        db: Database,
        ilink_client: IlinkClient,
        message_handler: Callable[[str, str], Coroutine[Any, Any, str]],
        poll_timeout_ms: int,
        max_reply_chars: int,
    ) -> None:
        self.db = db
        self.ilink_client = ilink_client
        self.message_handler = message_handler
        self.poll_timeout_ms = poll_timeout_ms
        self.max_reply_chars = max_reply_chars
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._typing_tickets: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            for credential_row in await self.db.list_active_credentials():
                credential = ChannelCredentialRecord.model_validate(credential_row)
                self._ensure_task_locked(credential.credential_id)

    async def stop(self) -> None:
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks = {}
            self._typing_tickets = {}
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _send_typing_indicator(
        self,
        credential: ChannelCredentialRecord,
        to_user_id: str,
        typing_ticket: str,
    ) -> None:
        try:
            await self.ilink_client.send_typing(
                credential.base_url,
                credential.bot_token,
                to_user_id,
                typing_ticket,
            )
            logger.info(
                "send_typing succeeded for credential %s recipient %s",
                credential.credential_id,
                to_user_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "send_typing failed for credential %s, invalidating cached typing_ticket",
                credential.credential_id,
            )
            if self._typing_tickets.get(credential.credential_id) == typing_ticket:
                self._typing_tickets.pop(credential.credential_id, None)

    async def ensure_credential_running(self, credential_id: str) -> None:
        async with self._lock:
            self._ensure_task_locked(credential_id)

    def is_running(self, credential_id: str) -> bool:
        task = self._tasks.get(credential_id)
        return task is not None and not task.done()

    async def poll_credential_once(self, credential_id: str) -> int:
        credential_row = await self.db.get_credential(credential_id)
        if credential_row is None:
            return 0
        credential = ChannelCredentialRecord.model_validate(credential_row)
        if credential.status != EntityStatus.ACTIVE:
            return 0

        member_row = await self.db.get_member(credential.member_id)
        if member_row is None:
            return 0
        member = TenantMemberRecord.model_validate(member_row)
        if member.status != EntityStatus.ACTIVE:
            return 0

        runtime = MemberRuntimeStateRecord.model_validate(
            await self.db.get_runtime_state(member.member_id, member.tenant_id)
        )
        # Lazily fetch typing_ticket if not cached
        if credential.credential_id not in self._typing_tickets:
            try:
                cfg = await self.ilink_client.get_config(
                    credential.base_url, credential.bot_token,
                )
                if cfg.typing_ticket:
                    self._typing_tickets[credential.credential_id] = cfg.typing_ticket
                    logger.info(
                        "Fetched typing_ticket for credential %s",
                        credential.credential_id,
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to fetch typing_ticket for credential %s",
                    credential.credential_id,
                )
        response = await self.ilink_client.get_updates(
            credential.base_url,
            credential.bot_token,
            credential.get_updates_buf,
            timeout_ms=self.poll_timeout_ms,
        )
        await self.db.update_runtime_state(
            member.member_id,
            tenant_id=member.tenant_id,
            last_poll_at=_utc_now().isoformat(),
            last_error=None,
        )
        if response.get_updates_buf:
            await self.db.update_credential_get_updates_buf(
                credential.credential_id,
                response.get_updates_buf,
            )

        processed = 0
        for raw_message in response.msgs:
            normalized = normalize_polled_message(raw_message)
            if normalized is None or normalized.group_id:
                continue
            if normalized.sender_id != member.ilink_user_id:
                continue
            handled = await self._handle_message(
                credential=credential,
                member=member,
                runtime=MemberRuntimeStateRecord.model_validate(
                    await self.db.get_runtime_state(member.member_id, member.tenant_id)
                ),
                message=normalized,
            )
            processed += 1 if handled else 0
        return processed

    async def _run_forever(self, credential_id: str) -> None:
        backoff_index = 0
        while True:
            try:
                await self.poll_credential_once(credential_id)
                backoff_index = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("WeChat multi-tenant polling failed")
                credential_row = await self.db.get_credential(credential_id)
                if credential_row is not None:
                    credential = ChannelCredentialRecord.model_validate(credential_row)
                    await self.db.update_runtime_state(
                        credential.member_id,
                        tenant_id=credential.tenant_id,
                        last_poll_at=_utc_now().isoformat(),
                        last_error=str(exc),
                    )
                await asyncio.sleep(
                    BACKOFF_DELAYS_SECONDS[min(backoff_index, len(BACKOFF_DELAYS_SECONDS) - 1)]
                )
                backoff_index += 1

    async def _handle_message(
        self,
        *,
        credential: ChannelCredentialRecord,
        member: TenantMemberRecord,
        runtime: MemberRuntimeStateRecord,
        message: Any,
    ) -> bool:
        if await self.db.has_seen_message(credential.credential_id, message.message_id):
            return False

        await self.db.remember_message(
            tenant_id=credential.tenant_id,
            credential_id=credential.credential_id,
            message_id=message.message_id,
        )

        if message.context_token:
            await self.db.update_runtime_state(
                member.member_id,
                tenant_id=member.tenant_id,
                context_token=message.context_token,
            )

        reply_context = message.context_token or runtime.context_token
        if not reply_context:
            await self.db.update_runtime_state(
                member.member_id,
                tenant_id=member.tenant_id,
                last_error="Missing context_token for inbound message.",
            )
            return False

        # Send typing indicator (fire-and-forget)
        typing_ticket = self._typing_tickets.get(credential.credential_id, "")
        if typing_ticket:
            logger.info(
                "Scheduling send_typing for credential %s inbound message from %s",
                credential.credential_id,
                message.sender_id,
            )
            asyncio.create_task(
                self._send_typing_indicator(
                    credential,
                    message.sender_id,
                    typing_ticket,
                ),
                name=f"xclaw-tenant-send-typing-{credential.credential_id}",
            )

        if not message.is_text:
            await self.ilink_client.send_text_message(
                credential.base_url,
                credential.bot_token,
                message.sender_id,
                UNSUPPORTED_PRIVATE_MESSAGE,
                reply_context,
            )
            return True

        try:
            chat_id = build_member_chat_id(member.tenant_id, member.member_id)
            reply = await self.message_handler(chat_id, message.text)
            reply = sanitize_reply_text(reply, max_chars=self.max_reply_chars)
        except Exception as exc:  # noqa: BLE001
            await self.db.update_runtime_state(
                member.member_id,
                tenant_id=member.tenant_id,
                last_error=str(exc),
            )
            await self.ilink_client.send_text_message(
                credential.base_url,
                credential.bot_token,
                message.sender_id,
                HANDLER_FAILURE_MESSAGE,
                reply_context,
            )
            return True

        await self.ilink_client.send_text_message(
            credential.base_url,
            credential.bot_token,
            message.sender_id,
            reply,
            reply_context,
        )
        await self.db.update_runtime_state(
            member.member_id,
            tenant_id=member.tenant_id,
            last_error=None,
        )
        return True

    def _ensure_task_locked(self, credential_id: str) -> None:
        if self.is_running(credential_id):
            return
        self._tasks[credential_id] = asyncio.create_task(
            self._run_forever(credential_id),
            name=f"xclaw-tenant-credential-{credential_id}",
        )

    async def send_response(self, member_chat_id: str, text: str) -> None:
        tenant_id, member_id = parse_member_chat_id(member_chat_id)
        member_row = await self.db.get_member(member_id)
        if member_row is None:
            raise RuntimeError(f"Unknown tenant member: {member_id}")
        member = TenantMemberRecord.model_validate(member_row)
        if member.tenant_id != tenant_id:
            raise RuntimeError(f"Tenant member mismatch for chat_id={member_chat_id}")

        credential_row = await self.db.get_active_credential_for_member(member_id)
        if credential_row is None:
            raise RuntimeError(f"No active credential for member_id={member_id}")
        credential = ChannelCredentialRecord.model_validate(credential_row)

        runtime = MemberRuntimeStateRecord.model_validate(
            await self.db.get_runtime_state(member_id, tenant_id)
        )
        context_token = runtime.context_token.strip()
        if not context_token:
            raise RuntimeError(f"Missing context_token for member_id={member_id}")

        clean_text = sanitize_reply_text(text, max_chars=self.max_reply_chars)
        await self.ilink_client.send_text_message(
            credential.base_url,
            credential.bot_token,
            member.ilink_user_id,
            clean_text,
            context_token,
        )
        await self.db.update_runtime_state(
            member_id,
            tenant_id=tenant_id,
            last_error=None,
        )


class WeChatMultiTenantService:
    """Aggregate service that exposes invite APIs and polling lifecycle."""

    def __init__(
        self,
        *,
        db: Database,
        base_url: str,
        qr_poll_interval_seconds: int,
        invite_refresh_seconds: int,
        invite_session_total_timeout_seconds: int,
        poll_timeout_ms: int,
        max_reply_chars: int,
        message_handler: Callable[[str, str], Coroutine[Any, Any, str]],
        ilink_client: IlinkClient | None = None,
    ) -> None:
        self.ilink_client = ilink_client or HttpIlinkClient()
        self.manager = MultiTenantBotManager(
            db=db,
            ilink_client=self.ilink_client,
            message_handler=message_handler,
            poll_timeout_ms=poll_timeout_ms,
            max_reply_chars=max_reply_chars,
        )
        self.invites = InviteService(
            base_url=base_url,
            db=db,
            ilink_client=self.ilink_client,
            qr_poll_interval_seconds=qr_poll_interval_seconds,
            invite_refresh_seconds=invite_refresh_seconds,
            invite_session_total_timeout_seconds=invite_session_total_timeout_seconds,
            on_credential_bound=self.manager.ensure_credential_running,
        )
        self.db = db

    async def start(self) -> None:
        await self.manager.start()

    async def stop(self) -> None:
        await self.manager.stop()
        await self.ilink_client.close()

    async def send_response(self, member_chat_id: str, text: str) -> None:
        await self.manager.send_response(member_chat_id, text)


__all__ = [
    "CreateInviteLinkRequest",
    "CreateInviteLinkResponse",
    "CreateTenantRequest",
    "CreateTenantResponse",
    "EntityStatus",
    "InviteLinkUnavailableError",
    "InviteSessionNotFoundError",
    "InviteSessionStartResponse",
    "InviteSessionState",
    "InviteSessionStatusPayload",
    "TenantMemberPayload",
    "TenantSummaryPayload",
    "WeChatMultiTenantService",
    "build_invite_page",
    "build_member_chat_id",
    "parse_member_chat_id",
]
