"""WeCom (企业微信) channel adapter.

WeCom uses an XML-based callback model:
- Incoming messages arrive at GET/POST /webhook/wecom (signature-verified)
- Outgoing messages via the WeCom API using corpid + access token.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Coroutine
from xml.etree import ElementTree

import httpx
from loguru import logger

from xclaw.channels import ChannelAdapter


class WeComAdapter(ChannelAdapter):
    """WeCom (企业微信) enterprise app channel adapter."""

    SEND_MSG_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
    TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"

    def __init__(
        self,
        corp_id: str,
        agent_id: str,
        secret: str,
        token: str,
        encoding_aes_key: str = "",
        message_handler: Callable[[str, str], Coroutine[Any, Any, str]] | None = None,
    ) -> None:
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.secret = secret
        self.token = token
        self.encoding_aes_key = encoding_aes_key
        self._message_handler = message_handler
        self._access_token: str = ""
        self._client = httpx.AsyncClient(timeout=15.0)

    def set_message_handler(
        self, handler: Callable[[str, str], Coroutine[Any, Any, str]]
    ) -> None:
        self._message_handler = handler

    async def _refresh_token(self) -> None:
        resp = await self._client.get(
            self.TOKEN_URL,
            params={"corpid": self.corp_id, "corpsecret": self.secret},
        )
        resp.raise_for_status()
        self._access_token = resp.json().get("access_token", "")

    async def send_response(self, chat_id: str, text: str) -> None:
        """Send a text message to a WeCom user (open_userid)."""
        if not self._access_token:
            await self._refresh_token()
        payload = {
            "touser": chat_id,
            "msgtype": "text",
            "agentid": self.agent_id,
            "text": {"content": text},
        }
        resp = await self._client.post(
            self.SEND_MSG_URL,
            params={"access_token": self._access_token},
            json=payload,
        )
        data = resp.json()
        if data.get("errcode") == 42001:  # token expired
            await self._refresh_token()
            resp = await self._client.post(
                self.SEND_MSG_URL,
                params={"access_token": self._access_token},
                json=payload,
            )

    def verify_signature(self, msg_signature: str, timestamp: str, nonce: str, echostr: str = "") -> bool:
        """Verify WeCom request signature."""
        items = sorted([self.token, timestamp, nonce, echostr])
        digest = hashlib.sha1("".join(items).encode()).hexdigest()
        return digest == msg_signature

    async def handle_event(self, xml_body: str, user_id: str | None = None) -> str:
        """Parse XML event and dispatch to message handler."""
        try:
            root = ElementTree.fromstring(xml_body)
            msg_type = root.findtext("MsgType", "")
            from_user = root.findtext("FromUserName", user_id or "unknown")
            content = root.findtext("Content", "").strip()

            if msg_type == "text" and content and self._message_handler:
                reply = await self._message_handler(from_user, content)
                await self.send_response(from_user, reply)
        except Exception as exc:
            logger.error(f"WeCom event handling error: {exc}")
        return "ok"

    async def start(self) -> None:
        logger.info("WeCom adapter ready (webhook mode)")

    async def stop(self) -> None:
        await self._client.aclose()
