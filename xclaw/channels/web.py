"""FastAPI web channel with REST API + SSE streaming and rate limiting."""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Coroutine

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
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


# ── Request / Response models (module-level for annotation resolution) ────────

class ChatRequest(BaseModel):
    chat_id: str = "web_default"
    message: str


class ChatResponse(BaseModel):
    chat_id: str
    reply: str


class WxMpLoginRequest(BaseModel):
    code: str


class WxMpLoginResponse(BaseModel):
    chat_id: str  # "wechat_mp_<openid>"


def create_web_app(
    message_handler: Callable[[str, str], Coroutine[Any, Any, str]],
    stream_handler: Callable[[str, str], AsyncIterator[str]] | None = None,
    auth_token: str = "",
    rate_limit: int = 20,
    db: Any = None,
    settings: Any = None,
) -> FastAPI:
    """Create the FastAPI application with all XClaw web routes.

    Args:
        message_handler: async (chat_id, text) → reply_text
        stream_handler:  async (chat_id, text) → AsyncIterator[str chunk]
        auth_token:      Bearer token for simple auth (empty = disabled)
        rate_limit:      requests per minute per IP
        db:              Optional Database instance (for /api/sessions)
        settings:        Optional Settings instance (for /api/config)
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

    # ── Sessions API ──────────────────────────────────────────────────────────

    @app.get("/api/sessions", dependencies=[Depends(verify_token)])
    async def list_sessions() -> JSONResponse:
        """List recent chats/sessions from the database."""
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        async with db.conn.execute(
            "SELECT id, channel, external_chat_id, chat_type, title, created_at "
            "FROM chats ORDER BY id DESC LIMIT 50"
        ) as cur:
            rows = await cur.fetchall()
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/sessions/{chat_id}/messages", dependencies=[Depends(verify_token)])
    async def get_session_messages(chat_id: int, limit: int = 50) -> JSONResponse:
        """Return recent messages for a given internal chat_id."""
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        msgs = await db.get_recent_messages(chat_id, limit=limit)
        return JSONResponse(msgs)

    # ── Config API ────────────────────────────────────────────────────────────

    @app.get("/api/config", dependencies=[Depends(verify_token)])
    async def get_config() -> JSONResponse:
        """Return a sanitised view of the current settings (no secrets)."""
        if settings is None:
            raise HTTPException(status_code=503, detail="Settings not available")
        safe = {
            "llm_provider": settings.llm_provider,
            "model": settings.model,
            "max_tokens": settings.max_tokens,
            "web_enabled": settings.web_enabled,
            "web_host": settings.web_host,
            "web_port": settings.web_port,
            "feishu_enabled": settings.feishu_enabled,
            "wecom_enabled": settings.wecom_enabled,
            "dingtalk_enabled": settings.dingtalk_enabled,
            "wechat_mp_enabled": getattr(settings, "wechat_mp_enabled", False),
            "data_dir": settings.data_dir,
            "timezone": settings.timezone,
            "stock_market_default": settings.stock_market_default,
            "bash_enabled": settings.bash_enabled,
            "rate_limit_per_minute": settings.rate_limit_per_minute,
        }
        return JSONResponse(safe)

    # ── WeChat Official Account (公众号) webhook ───────────────────────────────
    _wechat_mp_adapter: Any = None

    def set_wechat_mp_adapter(adapter: Any) -> None:
        nonlocal _wechat_mp_adapter
        _wechat_mp_adapter = adapter

    app.state.set_wechat_mp_adapter = set_wechat_mp_adapter

    @app.get("/webhook/wechat_mp")
    async def wechat_mp_verify(
        signature: str = "",
        timestamp: str = "",
        nonce: str = "",
        echostr: str = "",
    ) -> PlainTextResponse:
        """WeChat server URL verification (GET request with echostr challenge)."""
        if _wechat_mp_adapter is None:
            raise HTTPException(status_code=503, detail="WeChatMP adapter not configured")
        if not _wechat_mp_adapter.verify_signature(signature, timestamp, nonce):
            raise HTTPException(status_code=403, detail="Invalid signature")
        return PlainTextResponse(echostr)

    @app.post("/webhook/wechat_mp")
    async def wechat_mp_webhook(
        request: Request,
        signature: str = "",
        timestamp: str = "",
        nonce: str = "",
    ) -> PlainTextResponse:
        """Receive WeChat Official Account messages (XML body)."""
        if _wechat_mp_adapter is None:
            raise HTTPException(status_code=503, detail="WeChatMP adapter not configured")
        body = await request.body()
        reply_xml = await _wechat_mp_adapter.handle_event(
            body.decode("utf-8"),
            signature=signature,
            timestamp=timestamp,
            nonce=nonce,
        )
        return PlainTextResponse(reply_xml, media_type="application/xml")

    # ── WeChat Mini Program login ──────────────────────────────────────────────

    @app.post("/api/wxmp/login", response_model=WxMpLoginResponse)
    async def wxmp_login(req: WxMpLoginRequest) -> WxMpLoginResponse:
        """Exchange a wx.login() code for an XClaw chat_id.

        The Mini Program calls wx.login() to obtain a temporary ``code``,
        then sends it here.  XClaw exchanges it for an OpenID via WeChat's
        code2session API and maps the OpenID to an internal chat session.
        """
        if _wechat_mp_adapter is None:
            raise HTTPException(status_code=503, detail="WeChatMP adapter not configured")
        if not req.code:
            raise HTTPException(status_code=400, detail="code is required")
        try:
            data = await _wechat_mp_adapter.code2session(req.code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error(f"wxmp_login error: {exc}")
            raise HTTPException(
                status_code=502,
                detail=f"WeChat API error ({type(exc).__name__})",
            )

        open_id = data.get("openid", "")
        if not open_id:
            raise HTTPException(status_code=502, detail="No openid returned by WeChat")

        # Use a namespaced chat_id so it's clearly a Mini Program session
        chat_id = f"wechat_mp_{open_id}"
        return WxMpLoginResponse(chat_id=chat_id)

    return app
