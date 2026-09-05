"""Bounded image input shared by channels and execution backends."""

import base64
import binascii
import io
from typing import Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.responses import JSONResponse

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4
MAX_CHAT_BODY_BYTES = 28 * 1024 * 1024


class ImageAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(default="image", min_length=1, max_length=160)
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    data: str = Field(min_length=1, max_length=4 * ((MAX_IMAGE_BYTES + 2) // 3), repr=False)

    @model_validator(mode="after")
    def validate_image(self):
        try:
            raw = base64.b64decode(self.data, validate=True)
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError("图片不能超过 5 MB")
            with Image.open(io.BytesIO(raw)) as image:
                if Image.MIME.get(image.format) != self.media_type:
                    raise ValueError("图片内容与声明的类型不一致")
                if image.width * image.height > 25_000_000:
                    raise ValueError("图片不能超过 2500 万像素")
                image.verify()
        except (binascii.Error, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("图片编码或内容无效") from exc
        return self

    def to_powercore(self) -> dict:
        return {
            "type": "image", "alt_text": self.name,
            "source": {"source_type": "base64", "media_type": self.media_type, "data": self.data},
        }


class ChatBodyLimitMiddleware:
    """Limit actual received bytes before JSON parsing, including chunked uploads."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") not in {"/api/chat", "/api/chat/stream"}:
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_CHAT_BODY_BYTES:
                await JSONResponse({"detail": "聊天请求过大"}, status_code=413)(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if consumed:
                return await receive()
            consumed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)
