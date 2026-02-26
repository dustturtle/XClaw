"""Feishu (Lark) channel adapter.

Feishu uses a webhook callback model:
- Incoming events arrive at POST /webhook/feishu
- Outgoing messages are sent via the Feishu messaging API.

This adapter integrates with the FastAPI web app – it does NOT start
its own server, but registers routes on the shared app.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Callable, Coroutine

import httpx
from loguru import logger

from xclaw.channels import ChannelAdapter


class FeishuAdapter(ChannelAdapter):
    """Feishu (飞书) robot channel adapter.

    Requires an enterprise self-built app (企业自建应用) with:
    - app_id / app_secret  – for sending messages
    - verification_token   – for webhook event validation
    - encrypt_key          – optional, for payload decryption
    """

    SEND_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        verification_token: str,
        encrypt_key: str = "",
        message_handler: Callable[[str, str], Coroutine[Any, Any, str]] | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self._message_handler = message_handler
        self._tenant_token: str = ""
        self._client = httpx.AsyncClient(timeout=15.0)

    def set_message_handler(
        self, handler: Callable[[str, str], Coroutine[Any, Any, str]]
    ) -> None:
        """Set the async callback for (chat_id, user_text) → reply text."""
        self._message_handler = handler

    async def _refresh_token(self) -> None:
        resp = await self._client.post(
            self.TOKEN_URL,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        resp.raise_for_status()
        self._tenant_token = resp.json().get("tenant_access_token", "")

    async def send_response(self, chat_id: str, text: str) -> None:
        """Send a plain-text message to a Feishu chat."""
        if not self._tenant_token:
            await self._refresh_token()
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        resp = await self._client.post(
            self.SEND_MSG_URL,
            json=payload,
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {self._tenant_token}"},
        )
        if resp.status_code == 401:
            await self._refresh_token()
            resp = await self._client.post(
                self.SEND_MSG_URL,
                json=payload,
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {self._tenant_token}"},
            )
        resp.raise_for_status()

    def verify_signature(self, timestamp: str, nonce: str, body: bytes, signature: str) -> bool:
        """Verify the Feishu event signature."""
        content = (timestamp + nonce + self.verification_token).encode() + body
        digest = hashlib.sha256(content).hexdigest()
        return hmac.compare_digest(digest, signature)

    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Process an incoming Feishu event webhook payload.

        Returns the HTTP response body (dict) to send back.
        """
        # URL verification challenge
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}

        header = payload.get("header", {})
        event = payload.get("event", {})
        event_type = header.get("event_type", "")

        if event_type == "im.message.receive_v1":
            msg = event.get("message", {})
            sender = event.get("sender", {})
            chat_id = msg.get("chat_id", sender.get("sender_id", {}).get("open_id", "unknown"))
            raw_content = msg.get("content", "{}")
            try:
                content_obj = json.loads(raw_content)
                user_text = content_obj.get("text", "").strip()
            except Exception:
                user_text = raw_content

            if user_text and self._message_handler:
                try:
                    reply = await self._message_handler(chat_id, user_text)
                    await self.send_response(chat_id, reply)
                except Exception as exc:
                    logger.error(f"Feishu message handler error: {exc}")

        return {"msg": "ok"}

    async def start(self) -> None:
        """Feishu adapter is webhook-based; routes are registered by the web module."""
        logger.info("Feishu adapter ready (webhook mode)")

    async def stop(self) -> None:
        await self._client.aclose()
