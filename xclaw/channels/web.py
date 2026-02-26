"""FastAPI web channel with REST API + SSE streaming and rate limiting."""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Coroutine

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

# Rate limiting (slowapi)
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    _HAS_SLOWAPI = True
except ImportError:
    _HAS_SLOWAPI = False


def create_web_app(
    message_handler: Callable[[str, str], Coroutine[Any, Any, str]],
    stream_handler: Callable[[str, str], AsyncIterator[str]] | None = None,
    auth_token: str = "",
    rate_limit: int = 20,
) -> FastAPI:
    """Create the FastAPI application with all XClaw web routes.

    Args:
        message_handler: async (chat_id, text) → reply_text
        stream_handler:  async (chat_id, text) → AsyncIterator[str chunk]
        auth_token:      Bearer token for simple auth (empty = disabled)
        rate_limit:      requests per minute per IP
    """
    app = FastAPI(title="XClaw", version="0.1.0", docs_url="/docs")

    # CORS for local development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Rate limiting
    if _HAS_SLOWAPI:
        limiter = Limiter(key_func=get_remote_address, default_limits=[f"{rate_limit}/minute"])
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── Auth helper ───────────────────────────────────────────────────────────
    def verify_token(authorization: str = Header(default="")) -> None:
        if not auth_token:
            return
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or token != auth_token:
            raise HTTPException(status_code=401, detail="Invalid or missing token")

    # ── Request / Response models ─────────────────────────────────────────────
    class ChatRequest(BaseModel):
        chat_id: str = "web_default"
        message: str

    class ChatResponse(BaseModel):
        chat_id: str
        reply: str

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "xclaw"}

    @app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(verify_token)])
    async def chat(req: ChatRequest) -> ChatResponse:
        """Non-streaming chat endpoint."""
        try:
            reply = await message_handler(req.chat_id, req.message)
            return ChatResponse(chat_id=req.chat_id, reply=reply)
        except Exception as exc:
            logger.error(f"Chat endpoint error: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/chat/stream", dependencies=[Depends(verify_token)])
    async def chat_stream(req: ChatRequest) -> EventSourceResponse:
        """SSE streaming chat endpoint."""
        if stream_handler is None:
            raise HTTPException(status_code=501, detail="Streaming not enabled")

        async def event_generator():
            try:
                async for chunk in stream_handler(req.chat_id, req.message):
                    yield {"data": chunk}
                yield {"data": "[DONE]"}
            except Exception as exc:
                logger.error(f"Stream error: {exc}")
                yield {"data": f"[ERROR] {exc}"}

        return EventSourceResponse(event_generator())

    # ── Feishu webhook ────────────────────────────────────────────────────────
    _feishu_adapter: Any = None

    def set_feishu_adapter(adapter: Any) -> None:
        nonlocal _feishu_adapter
        _feishu_adapter = adapter

    app.state.set_feishu_adapter = set_feishu_adapter

    @app.post("/webhook/feishu")
    async def feishu_webhook(request: Request) -> JSONResponse:
        if _feishu_adapter is None:
            raise HTTPException(status_code=503, detail="Feishu adapter not configured")
        payload = await request.json()
        result = await _feishu_adapter.handle_event(payload)
        return JSONResponse(result)

    # ── WeCom webhook ─────────────────────────────────────────────────────────
    _wecom_adapter: Any = None

    def set_wecom_adapter(adapter: Any) -> None:
        nonlocal _wecom_adapter
        _wecom_adapter = adapter

    app.state.set_wecom_adapter = set_wecom_adapter

    @app.post("/webhook/wecom")
    async def wecom_webhook(request: Request) -> JSONResponse:
        if _wecom_adapter is None:
            raise HTTPException(status_code=503, detail="WeCom adapter not configured")
        body = await request.body()
        result = await _wecom_adapter.handle_event(body.decode())
        return JSONResponse({"result": result})

    # ── DingTalk webhook ──────────────────────────────────────────────────────
    _dingtalk_adapter: Any = None

    def set_dingtalk_adapter(adapter: Any) -> None:
        nonlocal _dingtalk_adapter
        _dingtalk_adapter = adapter

    app.state.set_dingtalk_adapter = set_dingtalk_adapter

    @app.post("/webhook/dingtalk")
    async def dingtalk_webhook(request: Request) -> JSONResponse:
        if _dingtalk_adapter is None:
            raise HTTPException(status_code=503, detail="DingTalk adapter not configured")
        payload = await request.json()
        result = await _dingtalk_adapter.handle_event(payload)
        return JSONResponse(result)

    return app
