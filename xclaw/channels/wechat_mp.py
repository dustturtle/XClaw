"""WeChat Official Account (公众号) channel adapter.

WeChat Official Account uses an XML-based callback model:
- URL verification: GET /webhook/wechat_mp?signature=...&echostr=...
- Incoming messages: POST /webhook/wechat_mp (XML body, signature-verified)
- Outgoing messages:
    1. Passive reply  – immediate XML in the POST response body (≤ 5 s ACK)
    2. Customer Service API – asynchronous push after agent_loop completes

Mini Program login is handled via the /api/wxmp/login HTTP endpoint (see web.py).

Design: see docs/wechat-design.md
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Callable, Coroutine
from xml.etree import ElementTree

import httpx
from loguru import logger

from xclaw.channels import ChannelAdapter

# Passive-reply ACK sent while the agent is processing.
_ACK_TEXT = "消息已收到，AI 正在处理中，请稍候..."


def _build_passive_reply(to_user: str, from_user: str, content: str) -> str:
    """Build a WeChat passive-reply XML string."""
    ts = int(time.time())
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{ts}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        "</xml>"
    )


class WeChatMPAdapter(ChannelAdapter):
    """WeChat Official Account (微信公众号) channel adapter.

    Required configuration (服务号 / 认证订阅号):
    - app_id / app_secret – for Customer Service API access token
    - token               – server-configuration token for signature verification
    - encoding_aes_key    – optional, for AES-encrypted message mode

    See docs/wechat-design.md for full architecture details.
    """

    TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
    SEND_MSG_URL = "https://api.weixin.qq.com/cgi-bin/message/custom/send"
    CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        token: str,
        encoding_aes_key: str = "",
        message_handler: Callable[[str, str], Coroutine[Any, Any, str]] | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.token = token
        self.encoding_aes_key = encoding_aes_key
        self._message_handler = message_handler
        self._access_token: str = ""
        self._token_expires: float = 0.0
        # MsgId deduplication: stores recently-seen message IDs
        self._seen_msg_ids: set[str] = set()
        self._client = httpx.AsyncClient(timeout=15.0)

    def set_message_handler(
        self, handler: Callable[[str, str], Coroutine[Any, Any, str]]
    ) -> None:
        self._message_handler = handler

    # ── Signature verification ─────────────────────────────────────────────────

    def verify_signature(self, signature: str, timestamp: str, nonce: str) -> bool:
        """Verify WeChat server push signature.

        Algorithm: SHA1( sorted([token, timestamp, nonce]).join("") )
        """
        items = sorted([self.token, timestamp, nonce])
        expected = hashlib.sha1("".join(items).encode()).hexdigest()
        return expected == signature

    # ── Access token ──────────────────────────────────────────────────────────

    async def _refresh_access_token(self) -> None:
        resp = await self._client.get(
            self.TOKEN_URL,
            params={
                "grant_type": "client_credential",
                "appid": self.app_id,
                "secret": self.app_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data.get("access_token", "")
        self._token_expires = time.time() + data.get("expires_in", 7200) - 60

    async def _get_access_token(self) -> str:
        if not self._access_token or time.time() >= self._token_expires:
            await self._refresh_access_token()
        return self._access_token

    # ── Customer Service API (active / proactive push) ────────────────────────

    async def send_response(self, open_id: str, text: str) -> None:
        """Push a text reply to a user via the Customer Service API.

        This is an active push (异步客服消息), used after the agent finishes.
        Requires a verified Official Account with customer service permission.
        """
        token = await self._get_access_token()
        payload = {
            "touser": open_id,
            "msgtype": "text",
            "text": {"content": text},
        }
        resp = await self._client.post(
            self.SEND_MSG_URL,
            params={"access_token": token},
            json=payload,
        )
        data = resp.json()
        if data.get("errcode") not in (0, None):
            if data.get("errcode") == 42001:  # token expired
                await self._refresh_access_token()
                token = self._access_token
                resp = await self._client.post(
                    self.SEND_MSG_URL,
                    params={"access_token": token},
                    json=payload,
                )
                data = resp.json()
        if data.get("errcode") not in (0, None):
            logger.warning(f"WeChatMP send_response failed: {data}")

    # ── Mini Program: code2session ─────────────────────────────────────────────

    async def code2session(self, code: str) -> dict[str, Any]:
        """Exchange a wx.login() code for openid + session_key.

        Returns dict with keys: openid, session_key, (optionally) unionid, errmsg.
        Raises httpx.HTTPStatusError on HTTP failure.
        Raises ValueError if WeChat returns a non-zero errcode.
        """
        resp = await self._client.get(
            self.CODE2SESSION_URL,
            params={
                "appid": self.app_id,
                "secret": self.app_secret,
                "js_code": code,
                "grant_type": "authorization_code",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise ValueError(
                f"code2session failed: errcode={data.get('errcode')} "
                f"errmsg={data.get('errmsg')}"
            )
        return data

    # ── Incoming message handling ──────────────────────────────────────────────

    def _passive_ack_xml(self, to_user: str, from_user: str) -> str:
        """Return the immediate ACK XML body for the POST response."""
        return _build_passive_reply(to_user, from_user, _ACK_TEXT)

    async def handle_event(
        self,
        xml_body: str,
        signature: str = "",
        timestamp: str = "",
        nonce: str = "",
    ) -> str:
        """Parse incoming XML event and dispatch to the agent.

        Returns the passive-reply XML string (must be sent immediately as the
        HTTP response body).  The actual AI result is pushed asynchronously via
        the Customer Service API.

        Args:
            xml_body:  Raw XML string from WeChat.
            signature: ``signature`` query param (for verification).
            timestamp: ``timestamp`` query param.
            nonce:     ``nonce`` query param.
        """
        # Signature verification (skip if token not configured)
        if self.token and signature:
            if not self.verify_signature(signature, timestamp, nonce):
                logger.warning("WeChatMP: signature verification failed")
                return ""

        try:
            root = ElementTree.fromstring(xml_body)
        except ElementTree.ParseError as exc:
            logger.error(f"WeChatMP: XML parse error: {exc}")
            return ""

        to_user = root.findtext("ToUserName", "")
        from_user = root.findtext("FromUserName", "")
        msg_type = root.findtext("MsgType", "")
        msg_id = root.findtext("MsgId", "")

        # Deduplicate (WeChat retries on timeout)
        if msg_id:
            if msg_id in self._seen_msg_ids:
                logger.debug(f"WeChatMP: duplicate MsgId={msg_id}, ignored")
                return ""
            self._seen_msg_ids.add(msg_id)
            # Keep set bounded
            if len(self._seen_msg_ids) > 500:
                self._seen_msg_ids.clear()

        if msg_type == "text":
            content = root.findtext("Content", "").strip()
            if content and self._message_handler:
                # Fire-and-forget: push result via Customer Service API
                asyncio.create_task(
                    self._process_and_reply(from_user, content)
                )
            # Immediate ACK (passive reply)
            return self._passive_ack_xml(from_user, to_user)

        # Non-text messages: politely decline
        if msg_type and self._message_handler:
            asyncio.create_task(
                self._send_unsupported_hint(from_user, msg_type)
            )
        return self._passive_ack_xml(from_user, to_user)

    async def _process_and_reply(self, open_id: str, text: str) -> None:
        """Run agent_loop and push result via Customer Service API."""
        try:
            assert self._message_handler is not None
            reply = await self._message_handler(open_id, text)
            await self.send_response(open_id, reply)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"WeChatMP async reply error for {open_id}: {exc}")

    async def _send_unsupported_hint(self, open_id: str, msg_type: str) -> None:
        try:
            await self.send_response(open_id, f"暂不支持 {msg_type} 类型消息，请发送文字。")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"WeChatMP unsupported-hint error: {exc}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("WeChatMP adapter ready (webhook mode)")

    async def stop(self) -> None:
        await self._client.aclose()
