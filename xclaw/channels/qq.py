"""QQ Bot channel adapter with multi-account, C2C, group, media, and streaming.

This module implements a QQ Bot channel shaped for XClaw's existing runtime:
- Multi-account webhook routing
- Stable chat IDs for C2C and group chats
- C2C typing indicator with keep-alive
- Voice input via platform ASR text or optional STT fallback
- Outbound image/file sending for C2C and group scenes
- C2C streaming transport via QQ stream_messages API
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import re
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine, Literal, Protocol, Sequence

import httpx
import certifi
from loguru import logger
import websockets

from xclaw.channels import ChannelAdapter

_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
_API_BASE = "https://api.sgroup.qq.com"

_MSG_TYPE_TEXT = 0
_MSG_TYPE_MARKDOWN = 2
_MSG_TYPE_INPUT_NOTIFY = 6
_MSG_TYPE_MEDIA = 7

_STREAM_INPUT_MODE_REPLACE = "replace"
_STREAM_INPUT_STATE_GENERATING = 1
_STREAM_INPUT_STATE_DONE = 10
_STREAM_CONTENT_TYPE_MARKDOWN = "markdown"

_MEDIA_TYPE_IMAGE = 1
_MEDIA_TYPE_VIDEO = 2
_MEDIA_TYPE_VOICE = 3
_MEDIA_TYPE_FILE = 4

_VOICE_CONTENT_TYPES = {"voice"}
_TYPING_INPUT_SECONDS = 60
_DEFAULT_TYPING_DELAY_SECONDS = 0.5
_DEFAULT_TYPING_INTERVAL_SECONDS = 50.0

_UNSUPPORTED_INPUT_MESSAGE = "当前仅支持文本和可转写语音消息，请直接发送文字。"
_HANDLER_FAILURE_MESSAGE = "我刚才处理这条消息时遇到了一点问题，请稍后再试。"
_EMPTY_REPLY_MESSAGE = "我暂时没有生成合适的回复，请换一种说法再试一次。"

_GROUP_POLICY_OPEN = "open"
_GROUP_POLICY_ALLOWLIST = "allowlist"
_GROUP_POLICY_DISABLED = "disabled"

_FULL_INTENTS = (1 << 30) | (1 << 12) | (1 << 25) | (1 << 26)
_RECONNECT_DELAYS_SECONDS = (1, 2, 5, 10, 30, 60)

_QQ_CHAT_ID_RE = re.compile(r"^qq:(?P<account>[^:]+):(?P<scene>c2c|group):(?P<target>.+)$")
_QQ_MENTION_RE = re.compile(r"<@!?\w+>")


class QQClientProtocol(Protocol):
    async def get_access_token(self) -> str: ...
    async def get_gateway_url(self) -> str: ...

    async def send_c2c_message(
        self,
        openid: str,
        content: str,
        *,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]: ...

    async def send_group_message(
        self,
        group_openid: str,
        content: str,
        *,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]: ...

    async def send_c2c_input_notify(
        self,
        openid: str,
        *,
        msg_id: str | None,
        msg_seq: int,
        input_second: int,
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

    async def send_c2c_image_message(
        self,
        openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]: ...

    async def send_group_image_message(
        self,
        group_openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]: ...

    async def send_c2c_file_message(
        self,
        openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]: ...

    async def send_group_file_message(
        self,
        group_openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class QQSTTProtocol(Protocol):
    async def transcribe_from_url(
        self,
        url: str,
        *,
        filename: str | None = None,
    ) -> str: ...


class QQGatewayConnection(Protocol):
    async def send(self, data: str) -> None: ...
    async def recv(self) -> Any: ...
    async def close(self) -> None: ...


@dataclass(slots=True, frozen=True)
class QQAccount:
    key: str
    app_id: str
    app_secret: str
    dm_enabled: bool = True
    group_enabled: bool = True
    group_policy: str = _GROUP_POLICY_OPEN
    allowed_group_openids: tuple[str, ...] = ()
    require_mention: bool = True
    ignore_other_mentions: bool = True
    typing_enabled: bool = True
    streaming_enabled: bool = True
    markdown_enabled: bool = True
    stt_enabled: bool = True

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "QQAccount":
        allowed = data.get("allowed_group_openids") or []
        return cls(
            key=str(data.get("key") or "default"),
            app_id=str(data.get("app_id") or ""),
            app_secret=str(data.get("app_secret") or ""),
            dm_enabled=bool(data.get("dm_enabled", True)),
            group_enabled=bool(data.get("group_enabled", True)),
            group_policy=str(data.get("group_policy") or _GROUP_POLICY_OPEN),
            allowed_group_openids=tuple(str(item) for item in allowed),
            require_mention=bool(data.get("require_mention", True)),
            ignore_other_mentions=bool(data.get("ignore_other_mentions", True)),
            typing_enabled=bool(data.get("typing_enabled", True)),
            streaming_enabled=bool(data.get("streaming_enabled", True)),
            markdown_enabled=bool(data.get("markdown_enabled", True)),
            stt_enabled=bool(data.get("stt_enabled", True)),
        )


@dataclass(slots=True)
class QQReplyContext:
    account_key: str
    scene: Literal["c2c", "group"]
    target_id: str
    sender_id: str
    msg_id: str | None
    event_id: str | None


@dataclass(slots=True)
class QQGatewaySession:
    session_id: str | None = None
    last_seq: int | None = None


class QQApiClient:
    """Minimal QQ Open Platform API client for one bot account."""

    def __init__(
        self,
        account: QQAccount,
        *,
        timeout: float = 15.0,
    ) -> None:
        self.account = account
        self._access_token = ""
        self._token_expires_at = 0.0
        self._client = httpx.AsyncClient(timeout=timeout)

    async def _refresh_token(self) -> None:
        response = await self._client.post(
            _TOKEN_URL,
            json={"appId": self.account.app_id, "clientSecret": self.account.app_secret},
        )
        response.raise_for_status()
        data = response.json()
        self._access_token = str(data.get("access_token", ""))
        self._token_expires_at = time.time() + int(data.get("expires_in", 7200)) - 60

    async def _get_token(self) -> str:
        if not self._access_token or time.time() >= self._token_expires_at:
            await self._refresh_token()
        return self._access_token

    async def get_access_token(self) -> str:
        return await self._get_token()

    async def get_gateway_url(self) -> str:
        token = await self._get_token()
        response = await self._client.get(
            f"{_API_BASE}/gateway",
            headers={
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()
        url = str(payload.get("url", "")).strip()
        if not url:
            raise RuntimeError("QQ gateway response missing url")
        return url

    async def _request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await self._get_token()
        response = await self._client.request(
            method,
            f"{_API_BASE}{path}",
            json=body,
            headers={
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def _build_text_body(
        self,
        content: str,
        *,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]:
        body: dict[str, Any]
        if self.account.markdown_enabled:
            body = {"markdown": {"content": content}, "msg_type": _MSG_TYPE_MARKDOWN, "msg_seq": msg_seq}
        else:
            body = {"content": content, "msg_type": _MSG_TYPE_TEXT, "msg_seq": msg_seq}
        if msg_id:
            body["msg_id"] = msg_id
        return body

    async def send_c2c_message(
        self,
        openid: str,
        content: str,
        *,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]:
        return await self._request("POST", f"/v2/users/{openid}/messages", self._build_text_body(content, msg_id=msg_id, msg_seq=msg_seq))

    async def send_group_message(
        self,
        group_openid: str,
        content: str,
        *,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v2/groups/{group_openid}/messages",
            self._build_text_body(content, msg_id=msg_id, msg_seq=msg_seq),
        )

    async def send_c2c_input_notify(
        self,
        openid: str,
        *,
        msg_id: str | None,
        msg_seq: int,
        input_second: int,
    ) -> dict[str, Any]:
        body = {
            "msg_type": _MSG_TYPE_INPUT_NOTIFY,
            "input_notify": {"input_type": 1, "input_second": input_second},
            "msg_seq": msg_seq,
        }
        if msg_id:
            body["msg_id"] = msg_id
        return await self._request("POST", f"/v2/users/{openid}/messages", body)

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
    ) -> dict[str, Any]:
        body = {
            "input_mode": _STREAM_INPUT_MODE_REPLACE,
            "input_state": input_state,
            "content_type": _STREAM_CONTENT_TYPE_MARKDOWN,
            "content_raw": content_raw,
            "event_id": event_id,
            "msg_id": msg_id,
            "msg_seq": msg_seq,
            "index": index,
        }
        if stream_msg_id:
            body["stream_msg_id"] = stream_msg_id
        return await self._request("POST", f"/v2/users/{openid}/stream_messages", body)

    async def _upload_media(
        self,
        *,
        scene: Literal["c2c", "group"],
        target_id: str,
        file_type: int,
        filename: str,
        content_base64: str,
    ) -> str:
        if scene == "c2c":
            path = f"/v2/users/{target_id}/files"
        else:
            path = f"/v2/groups/{target_id}/files"
        body: dict[str, Any] = {"file_type": file_type, "file_data": content_base64, "srv_send_msg": False}
        if file_type == _MEDIA_TYPE_FILE:
            body["file_name"] = filename
        result = await self._request("POST", path, body)
        file_info = str(result.get("file_info", ""))
        if not file_info:
            raise RuntimeError("QQ media upload missing file_info")
        return file_info

    async def _send_media(
        self,
        *,
        scene: Literal["c2c", "group"],
        target_id: str,
        file_info: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]:
        if scene == "c2c":
            path = f"/v2/users/{target_id}/messages"
        else:
            path = f"/v2/groups/{target_id}/messages"
        body: dict[str, Any] = {"msg_type": _MSG_TYPE_MEDIA, "media": {"file_info": file_info}, "msg_seq": msg_seq}
        if msg_id:
            body["msg_id"] = msg_id
        return await self._request("POST", path, body)

    async def send_c2c_image_message(
        self,
        openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]:
        file_info = await self._upload_media(
            scene="c2c",
            target_id=openid,
            file_type=_MEDIA_TYPE_IMAGE,
            filename=filename,
            content_base64=content_base64,
        )
        return await self._send_media(scene="c2c", target_id=openid, file_info=file_info, msg_id=msg_id, msg_seq=msg_seq)

    async def send_group_image_message(
        self,
        group_openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]:
        file_info = await self._upload_media(
            scene="group",
            target_id=group_openid,
            file_type=_MEDIA_TYPE_IMAGE,
            filename=filename,
            content_base64=content_base64,
        )
        return await self._send_media(scene="group", target_id=group_openid, file_info=file_info, msg_id=msg_id, msg_seq=msg_seq)

    async def send_c2c_file_message(
        self,
        openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]:
        file_info = await self._upload_media(
            scene="c2c",
            target_id=openid,
            file_type=_MEDIA_TYPE_FILE,
            filename=filename,
            content_base64=content_base64,
        )
        return await self._send_media(scene="c2c", target_id=openid, file_info=file_info, msg_id=msg_id, msg_seq=msg_seq)

    async def send_group_file_message(
        self,
        group_openid: str,
        *,
        filename: str,
        content_base64: str,
        msg_id: str | None,
        msg_seq: int,
    ) -> dict[str, Any]:
        file_info = await self._upload_media(
            scene="group",
            target_id=group_openid,
            file_type=_MEDIA_TYPE_FILE,
            filename=filename,
            content_base64=content_base64,
        )
        return await self._send_media(scene="group", target_id=group_openid, file_info=file_info, msg_id=msg_id, msg_seq=msg_seq)

    async def close(self) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self.close()


class DefaultQQSTTClient:
    """Simple OpenAI-compatible STT client."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout)

    async def transcribe_from_url(
        self,
        url: str,
        *,
        filename: str | None = None,
    ) -> str:
        download = await self._client.get(url)
        download.raise_for_status()
        files = {
            "file": (filename or "audio.wav", download.content, download.headers.get("content-type", "audio/wav")),
        }
        data = {"model": self.model}
        response = await self._client.post(
            f"{self.base_url}/audio/transcriptions",
            data=data,
            files=files,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload.get("text", "")).strip()


@dataclass(slots=True)
class _QQStreamSession:
    client: QQClientProtocol
    openid: str
    reply_context: QQReplyContext
    next_seq: Callable[[str], int]
    content: str = ""
    index: int = 0
    stream_msg_id: str | None = None

    async def push(self, delta: str) -> None:
        if not delta:
            return
        self.content += delta
        await self._send(_STREAM_INPUT_STATE_GENERATING)

    async def finish(self) -> None:
        if not self.content:
            self.content = _EMPTY_REPLY_MESSAGE
        await self._send(_STREAM_INPUT_STATE_DONE)

    async def _send(self, input_state: int) -> None:
        seq_key = _sequence_key(self.reply_context)
        response = await self.client.send_c2c_stream_message(
            self.openid,
            event_id=self.reply_context.event_id or self.reply_context.msg_id or "",
            msg_id=self.reply_context.msg_id or "",
            msg_seq=self.next_seq(seq_key),
            index=self.index,
            content_raw=self.content,
            input_state=input_state,
            stream_msg_id=self.stream_msg_id,
        )
        if not self.stream_msg_id:
            self.stream_msg_id = str(response.get("id", "")) or None
        self.index += 1


def _compose_chat_id(account_key: str, scene: Literal["c2c", "group"], target_id: str) -> str:
    return f"qq:{account_key}:{scene}:{target_id}"


def _parse_chat_id(chat_id: str) -> tuple[str, Literal["c2c", "group"], str]:
    match = _QQ_CHAT_ID_RE.match(chat_id)
    if not match:
        raise ValueError(f"Unsupported QQ chat id: {chat_id}")
    scene = match.group("scene")
    if scene not in {"c2c", "group"}:
        raise ValueError(f"Unsupported QQ scene: {scene}")
    return match.group("account"), scene, match.group("target")


def _sequence_key(ctx: QQReplyContext) -> str:
    return f"{ctx.account_key}:{ctx.scene}:{ctx.target_id}:{ctx.msg_id or 'proactive'}"


def _normalize_reply_text(text: str) -> str:
    cleaned = text.strip()
    return cleaned if cleaned else _EMPTY_REPLY_MESSAGE


def _supports_three_args(func: Callable[..., Any] | None) -> bool:
    if func is None:
        return False
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return True
    params = list(sig.parameters.values())
    if any(p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD) for p in params):
        return True
    positional = [
        p
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 3


def _is_voice_attachment(attachment: dict[str, Any]) -> bool:
    content_type = str(attachment.get("content_type", "")).lower()
    return (
        content_type in _VOICE_CONTENT_TYPES
        or content_type.startswith("audio/")
        or "silk" in content_type
        or "amr" in content_type
        or str(attachment.get("voice_wav_url", "")).strip() != ""
    )


def _extract_asr_text(attachments: list[dict[str, Any]]) -> str:
    transcripts: list[str] = []
    for attachment in attachments:
        if _is_voice_attachment(attachment):
            text = str(attachment.get("asr_refer_text", "")).strip()
            if text:
                transcripts.append(text)
    return "\n".join(part for part in transcripts if part)


def _strip_group_mentions(content: str) -> str:
    return _QQ_MENTION_RE.sub("", content).strip()


class QQAdapter(ChannelAdapter):
    """QQ Bot adapter with multi-account support."""

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        message_handler: Callable[..., Coroutine[Any, Any, str]] | None = None,
        *,
        stream_handler: Callable[..., AsyncIterator[str]] | None = None,
        accounts: Sequence[QQAccount | dict[str, Any]] | None = None,
        client_factory: Callable[[QQAccount], QQClientProtocol] | None = None,
        stt_client: QQSTTProtocol | None = None,
        typing_delay_seconds: float = _DEFAULT_TYPING_DELAY_SECONDS,
        typing_interval_seconds: float = _DEFAULT_TYPING_INTERVAL_SECONDS,
        gateway_connect: Callable[[str], Coroutine[Any, Any, QQGatewayConnection]] | None = None,
    ) -> None:
        if accounts:
            parsed_accounts = [
                item if isinstance(item, QQAccount) else QQAccount.from_mapping(dict(item))
                for item in accounts
            ]
        elif app_id and app_secret:
            parsed_accounts = [QQAccount(key="default", app_id=app_id, app_secret=app_secret)]
        else:
            raise ValueError("QQAdapter requires either accounts or legacy app_id/app_secret")

        self._accounts = {account.key: account for account in parsed_accounts}
        self._message_handler = message_handler
        self._stream_handler = stream_handler
        self._message_handler_supports_chat_type = _supports_three_args(message_handler)
        self._stream_handler_supports_chat_type = _supports_three_args(stream_handler)
        self._client_factory = client_factory or (lambda account: QQApiClient(account))
        self._clients = {key: self._client_factory(account) for key, account in self._accounts.items()}
        self._client = next(iter(self._clients.values()))
        self._reply_contexts: dict[str, QQReplyContext] = {}
        self._sequence_counters: dict[str, int] = {}
        self._gateway_sessions: dict[str, QQGatewaySession] = {
            key: QQGatewaySession() for key in self._accounts
        }
        self._gateway_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_event = asyncio.Event()
        self._stt_client = stt_client
        self._typing_delay_seconds = typing_delay_seconds
        self._typing_interval_seconds = typing_interval_seconds
        self._gateway_connect = gateway_connect or self._default_gateway_connect

    def set_message_handler(
        self,
        handler: Callable[..., Coroutine[Any, Any, str]],
    ) -> None:
        self._message_handler = handler
        self._message_handler_supports_chat_type = _supports_three_args(handler)

    def set_stream_handler(self, handler: Callable[..., AsyncIterator[str]] | None) -> None:
        self._stream_handler = handler
        self._stream_handler_supports_chat_type = _supports_three_args(handler)

    async def _default_gateway_connect(self, url: str) -> QQGatewayConnection:
        ssl_context = None
        if url.startswith("wss://"):
            ssl_context = ssl.create_default_context(cafile=certifi.where())
        return await websockets.connect(url, proxy=None, ssl=ssl_context)

    async def _send_gateway_identify(self, ws: QQGatewayConnection, token: str) -> None:
        await ws.send(
            json.dumps(
                {
                    "op": 2,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": _FULL_INTENTS,
                        "shard": [0, 1],
                    },
                }
            )
        )

    async def _send_gateway_resume(
        self,
        ws: QQGatewayConnection,
        token: str,
        session: QQGatewaySession,
    ) -> None:
        await ws.send(
            json.dumps(
                {
                    "op": 6,
                    "d": {
                        "token": f"QQBot {token}",
                        "session_id": session.session_id,
                        "seq": session.last_seq,
                    },
                }
            )
        )

    async def _heartbeat_loop(
        self,
        ws: QQGatewayConnection,
        session: QQGatewaySession,
        interval_ms: int,
    ) -> None:
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(interval_ms / 1000)
                if self._stop_event.is_set():
                    break
                await ws.send(json.dumps({"op": 1, "d": session.last_seq}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"QQ gateway heartbeat loop stopped: {exc}")

    async def _dispatch_gateway_event(self, account: QQAccount, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("t", ""))
        data = dict(payload.get("d", {}))
        if event_type == "C2C_MESSAGE_CREATE":
            await self._handle_c2c_message(account, data)
        elif event_type == "GROUP_AT_MESSAGE_CREATE":
            await self._handle_group_message(account, data)

    async def _run_gateway_once(self, account: QQAccount) -> None:
        client = self._clients[account.key]
        gateway_url = await client.get_gateway_url()
        token = await client.get_access_token()
        session = self._gateway_sessions[account.key]
        ws = await self._gateway_connect(gateway_url)
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            while not self._stop_event.is_set():
                raw = await ws.recv()
                if raw is None:
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                payload = json.loads(raw)
                op = payload.get("op")
                if payload.get("s") is not None:
                    session.last_seq = int(payload["s"])

                if op == 10:
                    heartbeat_interval = int((payload.get("d") or {}).get("heartbeat_interval", 30000))
                    if session.session_id and session.last_seq is not None:
                        await self._send_gateway_resume(ws, token, session)
                    else:
                        await self._send_gateway_identify(ws, token)
                    if heartbeat_task:
                        heartbeat_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat_task
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(ws, session, heartbeat_interval)
                    )
                    continue

                if op == 0:
                    event_type = str(payload.get("t", ""))
                    if event_type == "READY":
                        session.session_id = str((payload.get("d") or {}).get("session_id", "")) or None
                    elif event_type == "RESUMED":
                        logger.info(f"QQ gateway resumed for account={account.key}")
                    else:
                        await self._dispatch_gateway_event(account, payload)
                    continue

                if op in {7, 9}:
                    if op == 9:
                        session.session_id = None
                        session.last_seq = None
                    raise RuntimeError(f"QQ gateway requested reconnect op={op}")
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            with contextlib.suppress(Exception):
                await ws.close()

    async def _gateway_runner(self, account: QQAccount) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._run_gateway_once(account)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                delay = _RECONNECT_DELAYS_SECONDS[min(attempt, len(_RECONNECT_DELAYS_SECONDS) - 1)]
                logger.warning(f"QQ gateway account={account.key} disconnected: {exc}; reconnect in {delay}s")
                attempt += 1
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except TimeoutError:
                    continue
            else:
                if not self._stop_event.is_set():
                    await asyncio.sleep(1)

    def _next_msg_seq(self, key: str) -> int:
        seq = self._sequence_counters.get(key, 0) + 1
        self._sequence_counters[key] = seq
        return seq

    def _resolve_account(self, account_key: str | None) -> QQAccount:
        if account_key:
            if account_key not in self._accounts:
                raise KeyError(account_key)
            return self._accounts[account_key]
        if len(self._accounts) == 1:
            return next(iter(self._accounts.values()))
        raise ValueError("QQ webhook requires account_key when multiple accounts are configured")

    async def _invoke_message_handler(self, chat_id: str, text: str, chat_type: str) -> str:
        if self._message_handler is None:
            return _HANDLER_FAILURE_MESSAGE
        if self._message_handler_supports_chat_type:
            return await self._message_handler(chat_id, text, chat_type)
        return await self._message_handler(chat_id, text)

    async def _invoke_stream_handler(self, chat_id: str, text: str, chat_type: str) -> AsyncIterator[str]:
        if self._stream_handler is None:
            raise RuntimeError("stream_handler is not configured")
        if self._stream_handler_supports_chat_type:
            async for chunk in self._stream_handler(chat_id, text, chat_type):
                yield chunk
        else:
            async for chunk in self._stream_handler(chat_id, text):
                yield chunk

    async def _send_typing_once(self, client: QQClientProtocol, ctx: QQReplyContext) -> None:
        if ctx.scene != "c2c" or not ctx.msg_id:
            return
        await client.send_c2c_input_notify(
            ctx.target_id,
            msg_id=ctx.msg_id,
            msg_seq=self._next_msg_seq(_sequence_key(ctx)),
            input_second=_TYPING_INPUT_SECONDS,
        )

    async def _typing_keepalive(
        self,
        client: QQClientProtocol,
        ctx: QQReplyContext,
        stop_event: asyncio.Event,
        *,
        initial_delay: float,
    ) -> None:
        if ctx.scene != "c2c" or not ctx.msg_id:
            return
        await asyncio.sleep(initial_delay)
        while not stop_event.is_set():
            try:
                await self._send_typing_once(client, ctx)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"QQ typing notify failed for {ctx.target_id}: {exc}")
                return

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._typing_interval_seconds)
            except TimeoutError:
                continue

    async def _extract_message_text(
        self,
        account: QQAccount,
        data: dict[str, Any],
    ) -> tuple[str | None, bool]:
        raw_content = str(data.get("content", "")).strip()
        attachments = list(data.get("attachments") or [])
        asr_text = _extract_asr_text(attachments)
        content = _strip_group_mentions(raw_content) if "group_openid" in data else raw_content
        if content and asr_text:
            return f"{content}\n{asr_text}", True
        if asr_text:
            return asr_text, True
        if content:
            return content, True

        if attachments and self._stt_client and account.stt_enabled:
            for attachment in attachments:
                if not _is_voice_attachment(attachment):
                    continue
                voice_url = str(attachment.get("voice_wav_url") or attachment.get("url") or "").strip()
                if not voice_url:
                    continue
                filename = f"{str(data.get('id') or 'voice')}.wav"
                try:
                    transcript = await self._stt_client.transcribe_from_url(voice_url, filename=filename)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"QQ STT failed for {voice_url}: {exc}")
                    continue
                if transcript.strip():
                    return transcript.strip(), True

        return None, False

    def _should_process_group(self, account: QQAccount, data: dict[str, Any]) -> bool:
        if not account.group_enabled:
            return False
        if account.group_policy == _GROUP_POLICY_DISABLED:
            return False
        group_openid = str(data.get("group_openid", "")).strip()
        if account.group_policy == _GROUP_POLICY_ALLOWLIST and group_openid not in account.allowed_group_openids:
            return False
        mentions_present = "mentions" in data
        mentions = list(data.get("mentions") or [])
        if not account.require_mention:
            return True
        if mentions_present and not mentions:
            return False
        if not mentions:
            return True
        if not any(bool(item.get("is_you")) for item in mentions):
            return False
        if account.ignore_other_mentions:
            for item in mentions:
                if item.get("scope") == "single" and not bool(item.get("is_you")):
                    return False
        return True

    async def _handle_c2c_message(self, account: QQAccount, data: dict[str, Any]) -> None:
        if not account.dm_enabled:
            return

        user_openid = str((data.get("author") or {}).get("user_openid", "")).strip()
        msg_id = str(data.get("id", "")).strip() or None
        if not user_openid:
            return

        text, supported = await self._extract_message_text(account, data)
        has_attachments = bool(data.get("attachments"))
        chat_id = _compose_chat_id(account.key, "c2c", user_openid)
        reply_ctx = QQReplyContext(
            account_key=account.key,
            scene="c2c",
            target_id=user_openid,
            sender_id=user_openid,
            msg_id=msg_id,
            event_id=msg_id,
        )
        self._reply_contexts[chat_id] = reply_ctx
        client = self._clients[account.key]

        if not supported or not text:
            if not has_attachments:
                return
            await self.send_response(chat_id, _UNSUPPORTED_INPUT_MESSAGE)
            return

        stop_event = asyncio.Event()
        typing_task: asyncio.Task[None] | None = None
        if account.typing_enabled:
            if self._typing_delay_seconds <= 0:
                with contextlib.suppress(Exception):
                    await self._send_typing_once(client, reply_ctx)
                typing_task = asyncio.create_task(
                    self._typing_keepalive(
                        client,
                        reply_ctx,
                        stop_event,
                        initial_delay=self._typing_interval_seconds,
                    )
                )
            else:
                typing_task = asyncio.create_task(
                    self._typing_keepalive(
                        client,
                        reply_ctx,
                        stop_event,
                        initial_delay=self._typing_delay_seconds,
                    )
                )

        try:
            if account.streaming_enabled and self._stream_handler:
                stream_session = _QQStreamSession(
                    client=client,
                    openid=user_openid,
                    reply_context=reply_ctx,
                    next_seq=self._next_msg_seq,
                )
                async for chunk in self._invoke_stream_handler(chat_id, text, "private"):
                    await stream_session.push(chunk)
                await stream_session.finish()
            else:
                reply = _normalize_reply_text(await self._invoke_message_handler(chat_id, text, "private"))
                await self.send_response(chat_id, reply)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"QQ C2C handler error: {exc}")
            await self.send_response(chat_id, _HANDLER_FAILURE_MESSAGE)
        finally:
            stop_event.set()
            if typing_task is not None:
                typing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await typing_task

    async def _handle_group_message(self, account: QQAccount, data: dict[str, Any]) -> None:
        if not self._should_process_group(account, data):
            return

        group_openid = str(data.get("group_openid", "")).strip()
        member_openid = str((data.get("author") or {}).get("member_openid", "")).strip()
        msg_id = str(data.get("id", "")).strip() or None
        if not group_openid:
            return

        text, supported = await self._extract_message_text(account, data)
        has_attachments = bool(data.get("attachments"))
        chat_id = _compose_chat_id(account.key, "group", group_openid)
        reply_ctx = QQReplyContext(
            account_key=account.key,
            scene="group",
            target_id=group_openid,
            sender_id=member_openid,
            msg_id=msg_id,
            event_id=msg_id,
        )
        self._reply_contexts[chat_id] = reply_ctx
        client = self._clients[account.key]

        if not supported or not text:
            if not has_attachments:
                return
            await self.send_response(chat_id, _UNSUPPORTED_INPUT_MESSAGE)
            return

        try:
            reply = _normalize_reply_text(await self._invoke_message_handler(chat_id, text, "group"))
            await self.send_response(chat_id, reply)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"QQ group handler error: {exc}")
            await self.send_response(chat_id, _HANDLER_FAILURE_MESSAGE)

    async def send_response(self, chat_id: str, text: str) -> None:
        account_key, scene, target_id = _parse_chat_id(chat_id)
        account = self._resolve_account(account_key)
        client = self._clients[account.key]
        reply_ctx = self._reply_contexts.get(chat_id)
        msg_id = reply_ctx.msg_id if reply_ctx else None
        seq_key = _sequence_key(reply_ctx) if reply_ctx else f"{account.key}:{scene}:{target_id}:proactive"
        normalized = _normalize_reply_text(text)
        if scene == "c2c":
            await client.send_c2c_message(target_id, normalized, msg_id=msg_id, msg_seq=self._next_msg_seq(seq_key))
        else:
            await client.send_group_message(target_id, normalized, msg_id=msg_id, msg_seq=self._next_msg_seq(seq_key))

    async def send_image_response(self, chat_id: str, image_path: str) -> None:
        account_key, scene, target_id = _parse_chat_id(chat_id)
        account = self._resolve_account(account_key)
        client = self._clients[account.key]
        reply_ctx = self._reply_contexts.get(chat_id)
        content_base64 = base64.b64encode(Path(image_path).read_bytes()).decode()
        filename = Path(image_path).name
        msg_id = reply_ctx.msg_id if reply_ctx else None
        seq_key = _sequence_key(reply_ctx) if reply_ctx else f"{account.key}:{scene}:{target_id}:proactive"
        if scene == "c2c":
            await client.send_c2c_image_message(
                target_id,
                filename=filename,
                content_base64=content_base64,
                msg_id=msg_id,
                msg_seq=self._next_msg_seq(seq_key),
            )
        else:
            await client.send_group_image_message(
                target_id,
                filename=filename,
                content_base64=content_base64,
                msg_id=msg_id,
                msg_seq=self._next_msg_seq(seq_key),
            )

    async def send_file_response(self, chat_id: str, file_path: str) -> None:
        account_key, scene, target_id = _parse_chat_id(chat_id)
        account = self._resolve_account(account_key)
        client = self._clients[account.key]
        reply_ctx = self._reply_contexts.get(chat_id)
        content_base64 = base64.b64encode(Path(file_path).read_bytes()).decode()
        filename = Path(file_path).name
        msg_id = reply_ctx.msg_id if reply_ctx else None
        seq_key = _sequence_key(reply_ctx) if reply_ctx else f"{account.key}:{scene}:{target_id}:proactive"
        if scene == "c2c":
            await client.send_c2c_file_message(
                target_id,
                filename=filename,
                content_base64=content_base64,
                msg_id=msg_id,
                msg_seq=self._next_msg_seq(seq_key),
            )
        else:
            await client.send_group_file_message(
                target_id,
                filename=filename,
                content_base64=content_base64,
                msg_id=msg_id,
                msg_seq=self._next_msg_seq(seq_key),
            )

    async def handle_event(self, payload: dict[str, Any], account_key: str | None = None) -> dict[str, Any]:
        op = payload.get("op")
        if op == 13:
            d = payload.get("d", {})
            return {"plain_token": d.get("plain_token", ""), "msg": "ok"}

        account = self._resolve_account(account_key)
        event_type = str(payload.get("t", ""))
        data = dict(payload.get("d", {}))

        if event_type == "C2C_MESSAGE_CREATE":
            await self._handle_c2c_message(account, data)
        elif event_type == "GROUP_AT_MESSAGE_CREATE":
            await self._handle_group_message(account, data)

        return {"msg": "ok"}

    async def start(self) -> None:
        self._stop_event = asyncio.Event()
        for key, task in list(self._gateway_tasks.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._gateway_tasks.pop(key, None)
        for account in self._accounts.values():
            self._gateway_tasks[account.key] = asyncio.create_task(self._gateway_runner(account))
        logger.info(f"QQ adapter gateway started with {len(self._accounts)} account(s)")

    async def stop(self) -> None:
        self._stop_event.set()
        for task in list(self._gateway_tasks.values()):
            task.cancel()
        for task in list(self._gateway_tasks.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._gateway_tasks.clear()
        for client in self._clients.values():
            await client.close()
