"""DingTalk (钉钉) channel adapter.

DingTalk enterprise internal robots use callback webhooks:
- Incoming messages arrive at POST /webhook/dingtalk
- Outgoing messages via DingTalk messaging API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from base64 import b64encode
from typing import Any, Callable, Coroutine

import httpx
from loguru import logger

from xclaw.channels import ChannelAdapter


class DingTalkAdapter(ChannelAdapter):
    """DingTalk (钉钉) enterprise internal robot adapter."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        robot_code: str,
        message_handler: Callable[[str, str], Coroutine[Any, Any, str]] | None = None,
    ) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.robot_code = robot_code
        self._message_handler = message_handler
        self._access_token: str = ""
        self._token_expires: float = 0
        self._client = httpx.AsyncClient(timeout=15.0)

    def set_message_handler(
        self, handler: Callable[[str, str], Coroutine[Any, Any, str]]
    ) -> None:
        self._message_handler = handler

    async def _refresh_token(self) -> None:
        """Obtain DingTalk access token."""
        resp = await self._client.post(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            json={"appKey": self.app_key, "appSecret": self.app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data.get("accessToken", "")
        self._token_expires = time.time() + data.get("expireIn", 7200) - 60

    async def _get_token(self) -> str:
        if not self._access_token or time.time() >= self._token_expires:
            await self._refresh_token()
        return self._access_token

    def sign_request(self, timestamp: str) -> str:
        """Generate the HMAC-SHA256 signature for DingTalk webhook verification."""
        string_to_sign = f"{timestamp}\n{self.app_secret}"
        signature = hmac.new(
            self.app_secret.encode(), string_to_sign.encode(), hashlib.sha256
        ).digest()
        return b64encode(signature).decode()

    async def send_response(self, chat_id: str, text: str) -> None:
        """Send a text message to a DingTalk user via robot."""
        token = await self._get_token()
        payload = {
            "robotCode": self.robot_code,
            "userIds": [chat_id],
            "msgKey": "sampleText",
            "msgParam": json.dumps({"content": text}),
        }
        resp = await self._client.post(
            "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
            json=payload,
            headers={"x-acs-dingtalk-access-token": token},
        )
        if resp.status_code != 200:
            logger.warning(f"DingTalk send failed: {resp.text}")

    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle incoming DingTalk callback event."""
        msg_type = payload.get("msgtype", "")
        sender_id = payload.get("senderStaffId", payload.get("senderId", "unknown"))
        text = ""
        if msg_type == "text":
            text = payload.get("text", {}).get("content", "").strip()
        elif msg_type == "richText":
            for block in payload.get("content", {}).get("richText", []):
                if block.get("type") == "text":
                    text += block.get("text", "")
            text = text.strip()

        if text and self._message_handler:
            try:
                reply = await self._message_handler(sender_id, text)
                await self.send_response(sender_id, reply)
            except Exception as exc:
                logger.error(f"DingTalk message handler error: {exc}")

        return {"msg": "ok"}

    async def start(self) -> None:
        logger.info("DingTalk adapter ready (webhook mode)")

    async def stop(self) -> None:
        await self._client.aclose()
