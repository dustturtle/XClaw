"""WeChat (iLink QR login + private bot) channel adapter.

This adapter integrates the iLink WeChat bot flow used in HTClaw:
- QR login: fetch a bot QR code and poll its confirmation status
- Incoming messages: long polling via ``/ilink/bot/getupdates``
- Outgoing messages: send text via ``/ilink/bot/sendmessage``

Unlike HTClaw, replies are produced by XClaw's shared ``agent_loop`` through
the injected ``message_handler`` callback.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Coroutine, Protocol
from urllib.parse import quote

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from xclaw.channels import ChannelAdapter

BOT_TYPE = "3"
CHANNEL_VERSION = "0.1.0"
MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
MSG_ITEM_TEXT = 1
MSG_ITEM_IMAGE = 2
MSG_ITEM_VOICE = 3
MSG_ITEM_FILE = 4
MSG_ITEM_VIDEO = 5

BACKOFF_DELAYS_SECONDS = (1, 3, 10)
MAX_RECENT_MESSAGE_IDS = 200
UNSUPPORTED_PRIVATE_MESSAGE = "当前仅支持文本消息，请直接发送文字。"
HANDLER_FAILURE_MESSAGE = "我刚才处理这条消息时遇到了一点问题，请稍后再试。"
EMPTY_REPLY_MESSAGE = "我暂时没有生成合适的回复，请换一种说法再试一次。"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LoginAttemptNotFoundError(RuntimeError):
    """Raised when the requested QR login attempt is unknown."""


class IlinkClientError(RuntimeError):
    """Raised when iLink requests fail or return invalid data."""


class WechatLoginState(StrEnum):
    PENDING = "pending"
    SCANNED = "scanned"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    ERROR = "error"


class QRCodeResponse(BaseModel):
    qrcode: str
    qrcode_img_content: str


class QRStatusResponse(BaseModel):
    status: str
    bot_token: str = ""
    ilink_bot_id: str = ""
    baseurl: str = ""
    ilink_user_id: str = ""


class WechatAccount(BaseModel):
    bot_token: str
    ilink_bot_id: str
    ilink_user_id: str = ""
    base_url: str
    saved_at: str = Field(default_factory=lambda: utc_now().isoformat())


class WechatAccountSummary(BaseModel):
    ilink_bot_id: str
    ilink_user_id: str = ""
    base_url: str
    saved_at: str


class WechatSessionPayload(BaseModel):
    logged_in: bool
    account: WechatAccountSummary | None = None


class LoginAttempt(BaseModel):
    login_id: str
    qrcode: str
    qr_content: str
    state: WechatLoginState = WechatLoginState.PENDING
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    expires_at: str
    error: str | None = None

    def is_expired(self) -> bool:
        return datetime.fromisoformat(self.expires_at) <= utc_now()


class StartLoginResponse(BaseModel):
    login_id: str
    qr_svg: str
    expires_at: str
    poll_interval_ms: int


class LoginStatusPayload(BaseModel):
    state: WechatLoginState
    account: WechatAccountSummary | None = None
    error: str | None = None


class WechatBotStatusPayload(BaseModel):
    enabled: bool
    running: bool
    logged_in: bool
    last_poll_at: str | None = None
    last_error: str | None = None
    active_sessions: int = 0


class IlinkGetConfigResponse(BaseModel):
    ret: int = 0
    typing_ticket: str = ""


class WechatState(BaseModel):
    get_updates_buf: str = ""
    context_tokens: dict[str, str] = Field(default_factory=dict)
    recent_message_ids: list[str] = Field(default_factory=list)
    last_error: str | None = None
    last_poll_at: str | None = None
    typing_ticket: str = ""


class IlinkTextItem(BaseModel):
    text: str = ""


class IlinkMessageItem(BaseModel):
    type: int | None = None
    text_item: IlinkTextItem | None = None


class IlinkWireMessage(BaseModel):
    from_user_id: str = ""
    to_user_id: str = ""
    client_id: str = ""
    session_id: str = ""
    group_id: str = ""
    message_type: int | None = None
    message_state: int | None = None
    item_list: list[IlinkMessageItem] = Field(default_factory=list)
    context_token: str = ""
    create_time_ms: int | None = None
    message_id: str | int | None = None


class IlinkGetUpdatesResponse(BaseModel):
    ret: int = 0
    errcode: int | None = None
    errmsg: str = ""
    msgs: list[IlinkWireMessage] = Field(default_factory=list)
    get_updates_buf: str = ""
    longpolling_timeout_ms: int | None = None


class NormalizedWechatInbound(BaseModel):
    sender_id: str
    message_id: str
    text: str
    context_token: str = ""
    group_id: str | None = None
    message_type: int | None = None
    timestamp_ms: int | None = None
    is_text: bool = True


def summarize_account(account: WechatAccount | None) -> WechatAccountSummary | None:
    if account is None:
        return None
    return WechatAccountSummary(
        ilink_bot_id=account.ilink_bot_id,
        ilink_user_id=account.ilink_user_id,
        base_url=account.base_url,
        saved_at=account.saved_at,
    )


class WechatAccountStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> WechatAccount | None:
        with self._lock:
            if not self.path.exists():
                return None
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return WechatAccount.model_validate(data)

    def save(self, account: WechatAccount) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(account.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def clear(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()


class WechatStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> WechatState:
        with self._lock:
            if not self.path.exists():
                return WechatState()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return WechatState()
            return WechatState.model_validate(data)

    def save(self, state: WechatState) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(state.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def clear(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()


class LoginAttemptStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: LoginAttempt | None = None

    def create(self, qr: QRCodeResponse, ttl_seconds: int) -> LoginAttempt:
        expires_at = utc_now() + timedelta(seconds=ttl_seconds)
        attempt = LoginAttempt(
            login_id=str(uuid.uuid4()),
            qrcode=qr.qrcode,
            qr_content=qr.qrcode_img_content,
            expires_at=expires_at.isoformat(),
        )
        with self._lock:
            self._current = attempt
        return attempt

    def get(self, login_id: str) -> LoginAttempt | None:
        with self._lock:
            if self._current is None or self._current.login_id != login_id:
                return None
            return self._current.model_copy(deep=True)

    def update_state(
        self,
        login_id: str,
        state: WechatLoginState,
        error: str | None = None,
    ) -> LoginAttempt | None:
        with self._lock:
            if self._current is None or self._current.login_id != login_id:
                return None
            self._current.state = state
            self._current.error = error
            return self._current.model_copy(deep=True)

    def clear(self) -> None:
        with self._lock:
            self._current = None


def render_qr_svg(content: str) -> str:
    from qrcode import QRCode
    from qrcode.image.svg import SvgPathImage

    qr = QRCode(border=2)
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    return image.to_string(encoding="unicode")


class IlinkClient(Protocol):
    async def fetch_qrcode(self, base_url: str) -> QRCodeResponse: ...

    async def poll_qr_status(self, base_url: str, qrcode: str) -> QRStatusResponse: ...

    async def get_updates(
        self,
        base_url: str,
        token: str,
        get_updates_buf: str,
        *,
        timeout_ms: int,
    ) -> IlinkGetUpdatesResponse: ...

    async def send_text_message(
        self,
        base_url: str,
        token: str,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> dict[str, Any]: ...

    async def get_config(
        self, base_url: str, token: str,
    ) -> IlinkGetConfigResponse: ...

    async def send_typing(
        self,
        base_url: str,
        token: str,
        to_user_id: str,
        typing_ticket: str,
    ) -> None: ...

    async def close(self) -> None: ...


class HttpIlinkClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: bool = True,
        request_timeout_seconds: float = 10.0,
        poll_timeout_seconds: float = 35.0,
    ) -> None:
        self._request_timeout_seconds = request_timeout_seconds
        self._poll_timeout_seconds = poll_timeout_seconds
        self._client = httpx.AsyncClient(
            transport=transport,
            verify=verify,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_qrcode(self, base_url: str) -> QRCodeResponse:
        url = f"{_ensure_trailing_slash(base_url)}ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}"
        payload = await self._get_json(url, timeout_seconds=self._request_timeout_seconds)
        try:
            return QRCodeResponse.model_validate(payload)
        except Exception as exc:  # pragma: no cover
            raise IlinkClientError(f"Failed to parse QR code response: {exc}") from exc

    async def poll_qr_status(self, base_url: str, qrcode: str) -> QRStatusResponse:
        url = (
            f"{_ensure_trailing_slash(base_url)}ilink/bot/get_qrcode_status"
            f"?qrcode={quote(qrcode, safe='')}"
        )
        try:
            payload = await self._get_json(
                url,
                headers={"iLink-App-ClientVersion": "1"},
                timeout_seconds=self._poll_timeout_seconds,
            )
        except httpx.TimeoutException:
            return QRStatusResponse(status="wait")
        try:
            return QRStatusResponse.model_validate(payload)
        except Exception as exc:  # pragma: no cover
            raise IlinkClientError(f"Failed to parse QR status response: {exc}") from exc

    async def get_updates(
        self,
        base_url: str,
        token: str,
        get_updates_buf: str,
        *,
        timeout_ms: int,
    ) -> IlinkGetUpdatesResponse:
        url = f"{_ensure_trailing_slash(base_url)}ilink/bot/getupdates"
        body = {
            "get_updates_buf": get_updates_buf,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        try:
            payload = await self._post_json(
                url,
                body=body,
                token=token,
                timeout_seconds=timeout_ms / 1000,
            )
        except httpx.TimeoutException:
            return IlinkGetUpdatesResponse(
                ret=0,
                msgs=[],
                get_updates_buf=get_updates_buf,
            )

        try:
            response = IlinkGetUpdatesResponse.model_validate(payload)
        except Exception as exc:  # pragma: no cover
            raise IlinkClientError(f"Failed to parse getupdates response: {exc}") from exc

        if response.ret != 0:
            error_code = response.errcode if response.errcode is not None else response.ret
            raise IlinkClientError(
                f"getupdates failed: errcode={error_code} errmsg={response.errmsg or 'unknown'}"
            )
        return response

    async def send_text_message(
        self,
        base_url: str,
        token: str,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> dict[str, Any]:
        url = f"{_ensure_trailing_slash(base_url)}ilink/bot/sendmessage"
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": _generate_client_id(),
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": [
                    {
                        "type": MSG_ITEM_TEXT,
                        "text_item": {"text": text},
                    }
                ],
                "context_token": context_token,
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        payload = await self._post_json(
            url,
            body=body,
            token=token,
            timeout_seconds=self._request_timeout_seconds,
        )
        if isinstance(payload, dict) and payload.get("ret") not in (None, 0):
            raise IlinkClientError(
                "sendmessage failed: "
                f"errcode={payload.get('errcode', payload.get('ret'))} "
                f"errmsg={payload.get('errmsg', 'unknown')}"
            )
        return dict(payload) if isinstance(payload, dict) else {}

    async def get_config(
        self, base_url: str, token: str,
    ) -> IlinkGetConfigResponse:
        url = f"{_ensure_trailing_slash(base_url)}ilink/bot/getconfig"
        body = {"base_info": {"channel_version": CHANNEL_VERSION}}
        payload = await self._post_json(
            url, body=body, token=token,
            timeout_seconds=self._request_timeout_seconds,
        )
        try:
            return IlinkGetConfigResponse.model_validate(payload)
        except Exception as exc:  # pragma: no cover
            raise IlinkClientError(f"Failed to parse getconfig response: {exc}") from exc

    async def send_typing(
        self,
        base_url: str,
        token: str,
        to_user_id: str,
        typing_ticket: str,
    ) -> None:
        url = f"{_ensure_trailing_slash(base_url)}ilink/bot/sendtyping"
        body = {
            "to_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        await self._post_json(
            url, body=body, token=token,
            timeout_seconds=self._request_timeout_seconds,
        )

    async def _get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        try:
            response = await self._client.get(url, headers=headers, timeout=timeout_seconds)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise IlinkClientError(f"iLink request failed: {exc}") from exc

    async def _post_json(
        self,
        url: str,
        *,
        body: dict[str, Any],
        token: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        headers = _build_post_headers(token)
        try:
            response = await self._client.post(
                url,
                headers=headers,
                json=body,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise IlinkClientError(f"iLink request failed: {exc}") from exc


def normalize_polled_message(message: IlinkWireMessage) -> NormalizedWechatInbound | None:
    if message.message_type != MSG_TYPE_USER:
        return None

    sender_id = message.from_user_id.strip()
    if not sender_id:
        return None

    text, is_text = _extract_text_and_kind(message)
    if not text:
        return None

    group_id = message.group_id.strip() or None
    context_token = message.context_token.strip()
    return NormalizedWechatInbound(
        sender_id=sender_id,
        message_id=_build_message_id(message, sender_id, text),
        text=text,
        context_token=context_token,
        group_id=group_id,
        message_type=message.message_type,
        timestamp_ms=message.create_time_ms,
        is_text=is_text,
    )


def sanitize_reply_text(text: str, *, max_chars: int) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"```(?:[\s\S]*?)```", _strip_code_block, cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", cleaned)
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*>\s?", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"[*_~]+", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip() + "…"
    return cleaned or EMPTY_REPLY_MESSAGE


def _strip_code_block(match: re.Match[str]) -> str:
    block = match.group(0)
    block = re.sub(r"^```[^\n]*\n?", "", block)
    block = re.sub(r"\n?```$", "", block)
    return block.strip()


def _extract_text_and_kind(message: IlinkWireMessage) -> tuple[str, bool]:
    if not message.item_list:
        return "", False

    for item in message.item_list:
        if item.type == MSG_ITEM_TEXT and item.text_item and item.text_item.text.strip():
            return item.text_item.text.strip(), True
        if item.type == MSG_ITEM_IMAGE:
            return "[图片消息]", False
        if item.type == MSG_ITEM_VOICE:
            return "[语音消息]", False
        if item.type == MSG_ITEM_FILE:
            return "[文件消息]", False
        if item.type == MSG_ITEM_VIDEO:
            return "[视频消息]", False
    return "[暂不支持的消息类型]", False


def _build_post_headers(token: str) -> dict[str, str]:
    wechat_uin = secrets.randbits(32)
    encoded_uin = base64.b64encode(str(wechat_uin).encode("utf-8")).decode("utf-8")
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": encoded_uin,
        "Authorization": f"Bearer {token.strip()}",
    }


def _build_message_id(message: IlinkWireMessage, sender_id: str, text: str) -> str:
    if message.message_id not in (None, ""):
        return str(message.message_id)

    fingerprint = "|".join(
        [
            sender_id,
            str(message.create_time_ms or 0),
            message.context_token.strip(),
            text,
        ]
    )
    return hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()


def _generate_client_id() -> str:
    return f"xclaw:{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def _ensure_trailing_slash(base_url: str) -> str:
    return base_url if base_url.endswith("/") else f"{base_url}/"


class WeChatAdapter(ChannelAdapter):
    """WeChat adapter backed by the iLink QR login + long-polling APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        account_path: Path,
        state_path: Path,
        qr_total_timeout_seconds: int = 480,
        qr_poll_interval_seconds: int = 1,
        poll_timeout_ms: int = 25_000,
        max_reply_chars: int = 1_500,
        qr_poll_timeout_seconds: float = 35.0,
        ilink_client: IlinkClient | None = None,
        account_store: WechatAccountStore | None = None,
        state_store: WechatStateStore | None = None,
        attempt_store: LoginAttemptStore | None = None,
        message_handler: Callable[[str, str], Coroutine[Any, Any, str]] | None = None,
    ) -> None:
        self.base_url = base_url
        self.qr_total_timeout_seconds = qr_total_timeout_seconds
        self.qr_poll_interval_seconds = qr_poll_interval_seconds
        self.poll_timeout_ms = poll_timeout_ms
        self.max_reply_chars = max_reply_chars
        self._message_handler = message_handler
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._account_store = account_store or WechatAccountStore(account_path)
        self._state_store = state_store or WechatStateStore(state_path)
        self._attempt_store = attempt_store or LoginAttemptStore()
        self._ilink_client = ilink_client or HttpIlinkClient(
            poll_timeout_seconds=qr_poll_timeout_seconds,
        )

    def set_message_handler(
        self,
        handler: Callable[[str, str], Coroutine[Any, Any, str]],
    ) -> None:
        self._message_handler = handler

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def is_logged_in(self) -> bool:
        return self._account_store.load() is not None

    def get_session_payload(self) -> WechatSessionPayload:
        return WechatSessionPayload(
            logged_in=self.is_logged_in(),
            account=summarize_account(self._account_store.load()),
        )

    def get_public_status(self) -> WechatBotStatusPayload:
        state = self._state_store.load()
        logged_in = self.is_logged_in()
        return WechatBotStatusPayload(
            enabled=True,
            running=self.is_running(),
            logged_in=logged_in,
            last_poll_at=state.last_poll_at,
            last_error=state.last_error,
            active_sessions=len(state.context_tokens) if logged_in else 0,
        )

    async def start_login(self) -> StartLoginResponse:
        if self.is_logged_in():
            raise RuntimeError("Already logged in.")
        qr = await self._ilink_client.fetch_qrcode(self.base_url)
        attempt = self._attempt_store.create(qr, ttl_seconds=self.qr_total_timeout_seconds)
        return StartLoginResponse(
            login_id=attempt.login_id,
            qr_svg=render_qr_svg(qr.qrcode_img_content),
            expires_at=attempt.expires_at,
            poll_interval_ms=self.qr_poll_interval_seconds * 1000,
        )

    async def poll_login_status(self, login_id: str) -> LoginStatusPayload:
        attempt = self._attempt_store.get(login_id)
        if attempt is None:
            raise LoginAttemptNotFoundError(f"Unknown login attempt: {login_id}")

        if attempt.is_expired():
            self._attempt_store.update_state(login_id, WechatLoginState.EXPIRED)
            return LoginStatusPayload(state=WechatLoginState.EXPIRED)

        if attempt.state in {
            WechatLoginState.CONFIRMED,
            WechatLoginState.EXPIRED,
            WechatLoginState.ERROR,
        }:
            return LoginStatusPayload(
                state=attempt.state,
                account=summarize_account(self._account_store.load()),
                error=attempt.error,
            )

        raw_status = await self._ilink_client.poll_qr_status(self.base_url, attempt.qrcode)
        payload = self._apply_remote_status(attempt, raw_status)
        if payload.state == WechatLoginState.CONFIRMED:
            await self.start()
        return payload

    async def logout(self) -> None:
        await self.stop(clear_state=True, clear_account=True)
        self._attempt_store.clear()

    async def _ensure_typing_ticket(self, account: WechatAccount, state: WechatState) -> None:
        """Fetch typing_ticket via getconfig if not yet cached."""
        if state.typing_ticket:
            return
        try:
            cfg = await self._ilink_client.get_config(account.base_url, account.bot_token)
            if cfg.typing_ticket:
                state.typing_ticket = cfg.typing_ticket
                self._state_store.save(state)
                logger.info("Fetched typing_ticket for single-tenant wechat bot")
        except Exception:  # noqa: BLE001
            logger.warning("Failed to fetch typing_ticket, will retry next poll")

    async def _send_typing_indicator(
        self,
        account: WechatAccount,
        to_user_id: str,
        typing_ticket: str,
    ) -> None:
        try:
            await self._ilink_client.send_typing(
                account.base_url,
                account.bot_token,
                to_user_id,
                typing_ticket,
            )
            logger.info("send_typing succeeded for single-tenant recipient %s", to_user_id)
        except Exception:  # noqa: BLE001
            logger.warning("send_typing failed, invalidating cached typing_ticket")
            latest_state = self._state_store.load()
            if latest_state.typing_ticket == typing_ticket:
                latest_state.typing_ticket = ""
                self._state_store.save(latest_state)

    async def poll_once(self) -> int:
        account = self._account_store.load()
        if account is None:
            return 0

        state = self._state_store.load()
        await self._ensure_typing_ticket(account, state)
        response = await self._ilink_client.get_updates(
            account.base_url,
            account.bot_token,
            state.get_updates_buf,
            timeout_ms=self.poll_timeout_ms,
        )
        state.last_poll_at = utc_now().isoformat()
        state.last_error = None
        if response.get_updates_buf:
            state.get_updates_buf = response.get_updates_buf

        processed = 0
        for raw_message in response.msgs:
            normalized = normalize_polled_message(raw_message)
            if normalized is None or normalized.group_id:
                continue
            handled = await self._handle_message(account, state, normalized)
            processed += 1 if handled else 0

        self._state_store.save(state)
        return processed

    async def start(self) -> None:
        async with self._lock:
            if self.is_running():
                return
            if not self.is_logged_in():
                return
            self._task = asyncio.create_task(self._run_forever(), name="xclaw-wechat")
            logger.info("WeChat adapter started in long-polling mode")

    async def send_response(self, chat_id: str, text: str) -> None:
        state = self._state_store.load()
        context_token = state.context_tokens.get(chat_id, "")
        if not context_token:
            raise RuntimeError(f"Missing context_token for chat_id={chat_id}")
        await self._deliver_text(chat_id, text, context_token)

    async def stop(
        self,
        *,
        clear_state: bool = False,
        clear_account: bool = False,
    ) -> None:
        async with self._lock:
            task = self._task
            self._task = None

        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if clear_state:
            self._state_store.clear()
        if clear_account:
            self._account_store.clear()

        if task is not None or clear_state or clear_account:
            logger.info("WeChat adapter stopped")

    async def _run_forever(self) -> None:
        backoff_index = 0
        while True:
            try:
                await self.poll_once()
                backoff_index = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("WeChat long polling failed")
                self._set_error(str(exc))
                await asyncio.sleep(
                    BACKOFF_DELAYS_SECONDS[min(backoff_index, len(BACKOFF_DELAYS_SECONDS) - 1)]
                )
                backoff_index += 1

    async def _handle_message(
        self,
        account: WechatAccount,
        state: WechatState,
        message: NormalizedWechatInbound,
    ) -> bool:
        if message.message_id in state.recent_message_ids:
            return False

        self._remember_message_id(state, message.message_id)
        if message.context_token:
            state.context_tokens[message.sender_id] = message.context_token

        reply_context_token = message.context_token or state.context_tokens.get(message.sender_id, "")
        if not reply_context_token:
            state.last_error = (
                f"Message from {message.sender_id} is missing a usable context_token."
            )
            return False

        # Send typing indicator (fire-and-forget)
        if state.typing_ticket:
            logger.info(
                "Scheduling send_typing for single-tenant inbound message from %s",
                message.sender_id,
            )
            asyncio.create_task(
                self._send_typing_indicator(
                    account,
                    message.sender_id,
                    state.typing_ticket,
                ),
                name="xclaw-wechat-send-typing",
            )

        if not message.is_text:
            try:
                await self._deliver_text_with_account(
                    account,
                    message.sender_id,
                    UNSUPPORTED_PRIVATE_MESSAGE,
                    reply_context_token,
                )
                state.last_error = None
                return True
            except Exception as exc:  # noqa: BLE001
                state.last_error = str(exc)
                return False

        if self._message_handler is None:
            state.last_error = "WeChat adapter message handler is not configured."
            return False

        try:
            reply_text = await self._message_handler(message.sender_id, message.text)
        except Exception as exc:  # noqa: BLE001
            state.last_error = str(exc)
            try:
                await self._deliver_text_with_account(
                    account,
                    message.sender_id,
                    HANDLER_FAILURE_MESSAGE,
                    reply_context_token,
                )
                return True
            except Exception as send_exc:  # noqa: BLE001
                state.last_error = f"{exc}; send failure: {send_exc}"
                return False

        try:
            await self._deliver_text_with_account(
                account,
                message.sender_id,
                reply_text,
                reply_context_token,
            )
        except Exception as exc:  # noqa: BLE001
            state.last_error = str(exc)
            return False

        state.last_error = None
        return True

    async def _deliver_text(self, chat_id: str, text: str, context_token: str) -> None:
        account = self._account_store.load()
        if account is None:
            raise RuntimeError("WeChat account is not linked.")
        await self._deliver_text_with_account(account, chat_id, text, context_token)

    async def _deliver_text_with_account(
        self,
        account: WechatAccount,
        chat_id: str,
        text: str,
        context_token: str,
    ) -> None:
        clean_text = sanitize_reply_text(text, max_chars=self.max_reply_chars)
        await self._ilink_client.send_text_message(
            account.base_url,
            account.bot_token,
            chat_id,
            clean_text,
            context_token,
        )

    def _remember_message_id(self, state: WechatState, message_id: str) -> None:
        state.recent_message_ids.append(message_id)
        if len(state.recent_message_ids) > MAX_RECENT_MESSAGE_IDS:
            state.recent_message_ids = state.recent_message_ids[-MAX_RECENT_MESSAGE_IDS:]

    def _set_error(self, error: str) -> None:
        state = self._state_store.load()
        state.last_error = error
        state.last_poll_at = utc_now().isoformat()
        self._state_store.save(state)

    def _apply_remote_status(
        self,
        attempt: LoginAttempt,
        raw_status: QRStatusResponse,
    ) -> LoginStatusPayload:
        match raw_status.status:
            case "wait":
                self._attempt_store.update_state(attempt.login_id, WechatLoginState.PENDING)
                return LoginStatusPayload(state=WechatLoginState.PENDING)
            case "scaned":
                self._attempt_store.update_state(attempt.login_id, WechatLoginState.SCANNED)
                return LoginStatusPayload(state=WechatLoginState.SCANNED)
            case "expired":
                self._attempt_store.update_state(attempt.login_id, WechatLoginState.EXPIRED)
                return LoginStatusPayload(state=WechatLoginState.EXPIRED)
            case "confirmed":
                try:
                    account = self._build_account(raw_status)
                except RuntimeError as exc:
                    self._attempt_store.update_state(
                        attempt.login_id,
                        WechatLoginState.ERROR,
                        error=str(exc),
                    )
                    return LoginStatusPayload(
                        state=WechatLoginState.ERROR,
                        error=str(exc),
                    )
                self._account_store.save(account)
                self._attempt_store.update_state(attempt.login_id, WechatLoginState.CONFIRMED)
                return LoginStatusPayload(
                    state=WechatLoginState.CONFIRMED,
                    account=summarize_account(account),
                )
            case _:
                message = f"Unexpected qrcode status: {raw_status.status}"
                self._attempt_store.update_state(
                    attempt.login_id,
                    WechatLoginState.ERROR,
                    error=message,
                )
                return LoginStatusPayload(
                    state=WechatLoginState.ERROR,
                    error=message,
                )

    def _build_account(self, raw_status: QRStatusResponse) -> WechatAccount:
        if not raw_status.bot_token.strip() or not raw_status.ilink_bot_id.strip():
            raise RuntimeError(
                "Login was confirmed, but iLink did not return a complete credential set."
            )
        base_url = raw_status.baseurl.strip() or self.base_url
        return WechatAccount(
            bot_token=raw_status.bot_token.strip(),
            ilink_bot_id=raw_status.ilink_bot_id.strip(),
            ilink_user_id=raw_status.ilink_user_id.strip(),
            base_url=base_url,
        )

    async def close(self) -> None:
        await self._ilink_client.close()


__all__ = [
    "EMPTY_REPLY_MESSAGE",
    "HANDLER_FAILURE_MESSAGE",
    "HttpIlinkClient",
    "IlinkClientError",
    "IlinkGetConfigResponse",
    "LoginAttemptNotFoundError",
    "LoginStatusPayload",
    "NormalizedWechatInbound",
    "QRCodeResponse",
    "QRStatusResponse",
    "StartLoginResponse",
    "UNSUPPORTED_PRIVATE_MESSAGE",
    "WeChatAdapter",
    "WechatBotStatusPayload",
    "WechatLoginState",
    "WechatSessionPayload",
    "normalize_polled_message",
    "sanitize_reply_text",
]
