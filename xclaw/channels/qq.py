"""QQ group (QQ 群) channel adapter.

Uses the QQ Official Bot API (QQ 开放平台) for group message handling:
- Incoming messages arrive at POST /webhook/qq via event callbacks
- Outgoing messages via QQ Bot group messaging API.

Docs: https://bot.q.qq.com/wiki/develop/api-v2/
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Coroutine

import httpx
from loguru import logger

from xclaw.channels import ChannelAdapter

_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
_API_BASE = "https://api.sgroup.qq.com"


class QQAdapter(ChannelAdapter):
    """QQ group (QQ 群) bot adapter using the QQ Open Platform API."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        message_handler: Callable[[str, str], Coroutine[Any, Any, str]] | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._message_handler = message_handler
        self._access_token: str = ""
        self._token_expires: float = 0
        self._client = httpx.AsyncClient(timeout=15.0)

    def set_message_handler(
        self, handler: Callable[[str, str], Coroutine[Any, Any, str]]
    ) -> None:
        self._message_handler = handler

    async def _refresh_token(self) -> None:
        """Obtain QQ Bot access token via the Open Platform API."""
        resp = await self._client.post(
            _TOKEN_URL,
            json={"appId": self.app_id, "clientSecret": self.app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data.get("access_token", "")
        self._token_expires = time.time() + int(data.get("expires_in", 7200)) - 60

    async def _get_token(self) -> str:
        if not self._access_token or time.time() >= self._token_expires:
            await self._refresh_token()
        return self._access_token

    async def send_response(self, chat_id: str, text: str) -> None:
        """Send a text message to a QQ group.

        ``chat_id`` should be in the format ``group_openid:msg_id`` so that
        the reply can reference the original message (passive reply) or just
        ``group_openid`` for an active push.
        """
        token = await self._get_token()
        parts = chat_id.split(":", 1)
        group_openid = parts[0]
        msg_id = parts[1] if len(parts) > 1 else None

        payload: dict[str, Any] = {
            "content": text,
            "msg_type": 0,  # 0 = text
        }
        if msg_id:
            payload["msg_id"] = msg_id

        resp = await self._client.post(
            f"{_API_BASE}/v2/groups/{group_openid}/messages",
            json=payload,
            headers={
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            logger.warning(f"QQ send failed: {resp.text}")

    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle incoming QQ Bot callback event.

        The QQ Open Platform sends events via HTTP POST with the following
        structure::

            {
                "op": 0,               # opcode (0 = Dispatch)
                "t": "GROUP_AT_MESSAGE_CREATE",  # event type
                "d": { ... }           # event data
            }

        Validation callback (op 13) is handled as a URL verification step.
        """
        op = payload.get("op")

        # URL verification callback (op=13)
        if op == 13:
            d = payload.get("d", {})
            plain_token = d.get("plain_token", "")
            return {
                "plain_token": plain_token,
                "msg": "ok",
            }

        event_type = payload.get("t", "")
        data = payload.get("d", {})

        if event_type == "GROUP_AT_MESSAGE_CREATE":
            group_openid = data.get("group_openid", "")
            msg_id = data.get("id", "")
            content = data.get("content", "").strip()

            # Build a composite chat_id so send_response can reply properly
            chat_id = f"{group_openid}:{msg_id}" if msg_id else group_openid

            if content and self._message_handler:
                try:
                    reply = await self._message_handler(chat_id, content)
                    await self.send_response(chat_id, reply)
                except Exception as exc:
                    logger.error(f"QQ message handler error: {exc}")

        return {"msg": "ok"}

    async def start(self) -> None:
        logger.info("QQ adapter ready (webhook mode)")

    async def stop(self) -> None:
        await self._client.aclose()
