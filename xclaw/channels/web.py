"""FastAPI web channel with REST API + SSE streaming and rate limiting."""

from __future__ import annotations

import html
import hashlib
import json
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from loguru import logger
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from xclaw.channels.wechat import IlinkClientError
from xclaw.channels.wechat_multi_tenant import (
    CreateInviteLinkRequest,
    CreateInviteLinkResponse,
    CreateTenantRequest,
    CreateTenantResponse,
    InviteLinkUnavailableError,
    InviteSessionNotFoundError,
    InviteSessionStartResponse,
    InviteSessionStatusPayload,
    TenantMemberPayload,
    TenantSummaryPayload,
    build_invite_page,
)
from xclaw.investment.report_service import InvestmentReportService
from xclaw.investment.report_export_service import ReportExportService

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


class InvestmentReportRunRequest(BaseModel):
    chat_id: int
    market: str = "CN"
    symbols: list[str] | None = None


class InvestmentWatchlistRequest(BaseModel):
    chat_id: int
    symbol: str
    market: str = "CN"
    name: str = ""
    notes: str = ""


class InvestmentTaskRequest(BaseModel):
    chat_id: int
    description: str
    cron_expression: str


class InvestmentDeliverRequest(BaseModel):
    chat_id: str
    channel: str = "wechat"
    mode: str = "image+pdf"


def _build_chat_page_html(
    *,
    auth_enabled: bool,
    model_name: str,
    provider_name: str,
) -> str:
    """Return a lightweight browser chat UI for local validation."""
    auth_flag = json.dumps(auth_enabled)
    safe_model = html.escape(model_name or "unknown")
    safe_provider = html.escape(provider_name or "unknown")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>XClaw Chat</title>
  <style>
    :root {{
      --bg: #f6efe4;
      --panel: rgba(255, 252, 247, 0.88);
      --panel-strong: #fffaf2;
      --line: rgba(76, 53, 29, 0.12);
      --text: #2e2418;
      --muted: #7a6853;
      --brand: #bf5a36;
      --brand-deep: #7b2f16;
      --user: #2f5f4f;
      --user-soft: #d8ece2;
      --bot-soft: #f7e3ce;
      --shadow: 0 20px 50px rgba(85, 56, 22, 0.12);
      --radius: 22px;
    }}
    * {{
      box-sizing: border-box;
    }}
    html, body {{
      margin: 0;
      min-height: 100%;
      font-family: "Avenir Next", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(255, 209, 158, 0.65), transparent 28%),
        radial-gradient(circle at top right, rgba(190, 225, 204, 0.7), transparent 30%),
        linear-gradient(180deg, #f8f2e9 0%, #f4eadb 100%);
    }}
    body {{
      display: grid;
      place-items: center;
      padding: 24px;
    }}
    .shell {{
      width: min(1120px, 100%);
      min-height: min(860px, calc(100vh - 48px));
      display: grid;
      grid-template-columns: 320px 1fr;
      border: 1px solid rgba(95, 65, 33, 0.08);
      border-radius: 30px;
      overflow: hidden;
      box-shadow: var(--shadow);
      background: rgba(255, 250, 243, 0.76);
      backdrop-filter: blur(20px);
    }}
    .sidebar {{
      padding: 28px 24px;
      background:
        linear-gradient(180deg, rgba(92, 45, 26, 0.96), rgba(59, 29, 17, 0.96)),
        linear-gradient(180deg, transparent, transparent);
      color: #fff8ef;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }}
    .brand {{
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .badge {{
      width: fit-content;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.12);
      border: 1px solid rgba(255, 255, 255, 0.15);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0;
      font-size: 34px;
      line-height: 1;
      font-weight: 700;
    }}
    .subtitle {{
      margin: 0;
      color: rgba(255, 248, 239, 0.78);
      line-height: 1.6;
      font-size: 15px;
    }}
    .meta, .tips {{
      padding: 16px;
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.12);
    }}
    .meta strong, .tips strong {{
      display: block;
      margin-bottom: 8px;
      font-size: 13px;
      color: rgba(255, 248, 239, 0.88);
    }}
    .meta div, .tips div {{
      color: rgba(255, 248, 239, 0.74);
      font-size: 14px;
      line-height: 1.6;
    }}
    .sidebar a {{
      color: #ffd8b6;
    }}
    .main {{
      display: grid;
      grid-template-rows: auto 1fr auto;
      min-height: 0;
      background:
        linear-gradient(180deg, rgba(255, 252, 247, 0.9), rgba(255, 249, 241, 0.96));
    }}
    .toolbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 20px 24px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 250, 244, 0.74);
      backdrop-filter: blur(18px);
    }}
    .toolbar-title {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .toolbar-title strong {{
      font-size: 18px;
    }}
    .toolbar-title span {{
      color: var(--muted);
      font-size: 13px;
    }}
    .toolbar-actions {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .status {{
      padding: 8px 12px;
      border-radius: 999px;
      background: #f0e6d8;
      color: var(--brand-deep);
      font-size: 12px;
      border: 1px solid rgba(123, 47, 22, 0.1);
    }}
    button, input, textarea {{
      font: inherit;
    }}
    .ghost, .primary {{
      appearance: none;
      border: none;
      border-radius: 14px;
      padding: 11px 15px;
      cursor: pointer;
      transition: transform 120ms ease, opacity 120ms ease, background 120ms ease;
    }}
    .ghost {{
      background: #efe2d1;
      color: var(--brand-deep);
    }}
    .primary {{
      background: linear-gradient(135deg, var(--brand), #d07a49);
      color: white;
      font-weight: 600;
      min-width: 112px;
    }}
    button:hover {{
      transform: translateY(-1px);
    }}
    button:disabled {{
      opacity: 0.55;
      cursor: wait;
      transform: none;
    }}
    .messages {{
      padding: 24px;
      overflow: auto;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }}
    .message {{
      max-width: min(760px, 88%);
      border-radius: 22px;
      padding: 16px 18px;
      line-height: 1.65;
      white-space: pre-wrap;
      word-break: break-word;
      box-shadow: 0 8px 22px rgba(92, 62, 23, 0.07);
      animation: rise 160ms ease-out;
    }}
    .message.system {{
      max-width: 100%;
      background: rgba(247, 232, 214, 0.9);
      color: #73553b;
      border: 1px solid rgba(126, 90, 52, 0.12);
    }}
    .message.assistant {{
      align-self: flex-start;
      background: var(--bot-soft);
      border-top-left-radius: 8px;
    }}
    .message.user {{
      align-self: flex-end;
      background: var(--user-soft);
      color: #18392d;
      border-top-right-radius: 8px;
    }}
    .composer {{
      border-top: 1px solid var(--line);
      padding: 18px 20px 20px;
      background: rgba(255, 250, 244, 0.9);
    }}
    .advanced {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .field {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .field label {{
      font-size: 12px;
      color: var(--muted);
    }}
    .field input, textarea {{
      width: 100%;
      border-radius: 16px;
      border: 1px solid rgba(107, 79, 47, 0.14);
      background: rgba(255, 255, 255, 0.92);
      color: var(--text);
      padding: 14px 16px;
      outline: none;
      transition: border-color 120ms ease, box-shadow 120ms ease;
    }}
    .field input:focus, textarea:focus {{
      border-color: rgba(191, 90, 54, 0.42);
      box-shadow: 0 0 0 4px rgba(191, 90, 54, 0.12);
    }}
    textarea {{
      min-height: 108px;
      resize: vertical;
    }}
    .composer-row {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: end;
    }}
    .composer-note {{
      margin-top: 10px;
      font-size: 12px;
      color: var(--muted);
    }}
    @keyframes rise {{
      from {{
        transform: translateY(8px);
        opacity: 0;
      }}
      to {{
        transform: translateY(0);
        opacity: 1;
      }}
    }}
    @media (max-width: 920px) {{
      body {{
        padding: 12px;
      }}
      .shell {{
        grid-template-columns: 1fr;
        min-height: calc(100vh - 24px);
      }}
      .sidebar {{
        padding: 20px;
      }}
      .advanced, .composer-row {{
        grid-template-columns: 1fr;
      }}
      .message {{
        max-width: 100%;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="badge">Local Chat</div>
        <h1>XClaw</h1>
        <p class="subtitle">不用 Swagger，不用命令行。直接在浏览器里验证你这台机器上的 Agent 是否真的能对话。</p>
      </div>
      <div class="meta">
        <strong>当前模型</strong>
        <div>{safe_provider} / {safe_model}</div>
      </div>
      <div class="tips">
        <strong>怎么用</strong>
        <div>左下角输入消息直接发送。会话 ID 会自动记住，刷新页面也能继续同一个对话。</div>
      </div>
      <div class="tips">
        <strong>附加入口</strong>
        <div><a href="/docs" target="_blank" rel="noreferrer">Swagger 文档</a> 仍然保留，适合调接口时对照看。</div>
      </div>
    </aside>
    <main class="main">
      <header class="toolbar">
        <div class="toolbar-title">
          <strong>浏览器聊天页</strong>
          <span>最小可用界面，默认调用 <code>/api/chat</code></span>
        </div>
        <div class="toolbar-actions">
          <div class="status" id="status-pill">准备就绪</div>
          <button class="ghost" id="clear-btn" type="button">清空消息</button>
        </div>
      </header>
      <section class="messages" id="messages" aria-live="polite"></section>
      <section class="composer">
        <div class="advanced">
          <div class="field">
            <label for="chat-id">会话 ID</label>
            <input id="chat-id" placeholder="比如 demo 或 my-session" />
          </div>
          <div class="field">
            <label for="auth-token">Bearer Token（可选）</label>
            <input id="auth-token" placeholder="服务启用鉴权时在这里填" />
          </div>
        </div>
        <div class="composer-row">
          <div class="field">
            <label for="message-input">消息</label>
            <textarea id="message-input" placeholder="输入一条消息，按 Enter 发送，Shift+Enter 换行"></textarea>
          </div>
          <button class="primary" id="send-btn" type="button">发送</button>
        </div>
        <div class="composer-note" id="composer-note">
          {("当前服务启用了鉴权，页面已为你保留 Token 输入框。" if auth_enabled else "当前服务未启用鉴权，可以直接开始聊天。")}
        </div>
      </section>
    </main>
  </div>
  <script>
    const AUTH_REQUIRED = {auth_flag};
    const STORAGE_KEYS = {{
      chatId: "xclaw.chat_id",
      token: "xclaw.auth_token"
    }};

    const messagesEl = document.getElementById("messages");
    const chatIdInput = document.getElementById("chat-id");
    const tokenInput = document.getElementById("auth-token");
    const messageInput = document.getElementById("message-input");
    const sendBtn = document.getElementById("send-btn");
    const clearBtn = document.getElementById("clear-btn");
    const statusPill = document.getElementById("status-pill");

    function makeDefaultChatId() {{
      return "web-" + Math.random().toString(36).slice(2, 10);
    }}

    function setStatus(text) {{
      statusPill.textContent = text;
    }}

    function appendMessage(role, text) {{
      const item = document.createElement("article");
      item.className = "message " + role;
      item.textContent = text;
      messagesEl.appendChild(item);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }}

    function loadPreferences() {{
      chatIdInput.value = localStorage.getItem(STORAGE_KEYS.chatId) || makeDefaultChatId();
      tokenInput.value = localStorage.getItem(STORAGE_KEYS.token) || "";
    }}

    function persistPreferences() {{
      localStorage.setItem(STORAGE_KEYS.chatId, chatIdInput.value.trim() || makeDefaultChatId());
      localStorage.setItem(STORAGE_KEYS.token, tokenInput.value.trim());
    }}

    async function sendMessage() {{
      const message = messageInput.value.trim();
      const chatId = chatIdInput.value.trim();
      const token = tokenInput.value.trim();

      if (!message) {{
        setStatus("先输入消息");
        messageInput.focus();
        return;
      }}
      if (!chatId) {{
        setStatus("先填会话 ID");
        chatIdInput.focus();
        return;
      }}
      if (AUTH_REQUIRED && !token) {{
        setStatus("需要 Bearer Token");
        tokenInput.focus();
        return;
      }}

      persistPreferences();
      appendMessage("user", message);
      messageInput.value = "";
      sendBtn.disabled = true;
      setStatus("正在请求模型...");

      const headers = {{
        "Content-Type": "application/json"
      }};
      if (token) {{
        headers["Authorization"] = `Bearer ${{token}}`;
      }}

      try {{
        const response = await fetch("/api/chat", {{
          method: "POST",
          headers,
          body: JSON.stringify({{ chat_id: chatId, message }})
        }});

        if (!response.ok) {{
          let detail = `HTTP ${{response.status}}`;
          try {{
            const data = await response.json();
            detail = data.detail || detail;
          }} catch (_err) {{
          }}
          throw new Error(detail);
        }}

        const data = await response.json();
        appendMessage("assistant", data.reply || "(空回复)");
        setStatus("响应完成");
      }} catch (error) {{
        appendMessage("system", `请求失败：${{error.message}}`);
        setStatus("请求失败");
      }} finally {{
        sendBtn.disabled = false;
        messageInput.focus();
      }}
    }}

    sendBtn.addEventListener("click", sendMessage);
    clearBtn.addEventListener("click", () => {{
      messagesEl.innerHTML = "";
      appendMessage("system", "消息区已清空。你可以继续使用相同的会话 ID，也可以改成新的。");
      setStatus("已清空");
    }});
    chatIdInput.addEventListener("change", persistPreferences);
    tokenInput.addEventListener("change", persistPreferences);
    messageInput.addEventListener("keydown", (event) => {{
      if (event.key === "Enter" && !event.shiftKey) {{
        event.preventDefault();
        sendMessage();
      }}
    }});

    loadPreferences();
    appendMessage(
      "system",
      AUTH_REQUIRED
        ? "页面已就绪。这个服务启用了鉴权，如果你还没填 Bearer Token，请先填右下方输入框。"
        : "页面已就绪。直接发一条消息试试，比如“你好，介绍一下你自己”。"
    );
    setStatus("页面已连接");
  </script>
</body>
</html>
"""


def _build_admin_page_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>XClaw Investment Admin</title>
  <style>
    body { font-family: "Avenir Next", "PingFang SC", sans-serif; background: #f7f3ec; color: #2d2418; margin: 0; padding: 24px; }
    .shell { max-width: 1100px; margin: 0 auto; background: #fffaf3; border: 1px solid #e7dccd; border-radius: 20px; padding: 24px; box-shadow: 0 18px 40px rgba(84, 56, 20, 0.08); }
    h1 { margin-top: 0; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
    .card { background: #fff; border: 1px solid #eadfce; border-radius: 16px; padding: 16px; }
    code { background: #f5eee3; padding: 2px 6px; border-radius: 8px; }
  </style>
</head>
<body>
  <div class="shell">
    <h1>投资日报后台</h1>
    <p>统一查看日报历史、手动运行、自选股管理和日报任务。</p>
    <div class="grid">
      <div class="card"><strong>报告历史</strong><p><code>/api/investment/reports</code></p></div>
      <div class="card"><strong>手动运行</strong><p><code>/api/investment/reports/run</code></p></div>
      <div class="card"><strong>自选股管理</strong><p><code>/api/investment/watchlist</code></p></div>
      <div class="card"><strong>日报任务</strong><p><code>/api/investment/tasks</code></p></div>
    </div>
  </div>
</body>
</html>
"""


def _user_namespace(token: str) -> str:
    """Derive a short, stable user-namespace string from a bearer token.

    The namespace is the first 16 hex characters of SHA-256(token),
    giving 64 bits of uniqueness which avoids birthday-paradox collisions
    up to ~billions of tokens.
    It is used to prefix ``chat_id`` values in multi-user mode so that
    each unique bearer token maps to its own isolated session space.
    """
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def create_web_app(
    message_handler: Callable[[str, str], Coroutine[Any, Any, str]],
    stream_handler: Callable[[str, str], AsyncIterator[str]] | None = None,
    auth_token: str = "",
    rate_limit: int = 20,
    db: Any = None,
    settings: Any = None,
    multi_user_mode: bool = False,
    tool_registry: Any = None,
) -> FastAPI:
    """Create the FastAPI application with all XClaw web routes.

    Args:
        message_handler: async (chat_id, text) → reply_text
        stream_handler:  async (chat_id, text) → AsyncIterator[str chunk]
        auth_token:      Bearer token for simple auth (empty = disabled)
        rate_limit:      requests per minute per IP
        db:              Optional Database instance (for /api/sessions)
        settings:        Optional Settings instance (for /api/config)
        multi_user_mode: When True, each unique bearer token gets its own
                         isolated session namespace, preventing cross-user
                         data access on the web channel.
        tool_registry:   Optional ToolRegistry instance (for MCP server mode)
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
    def _extract_token(authorization: str) -> str:
        """Extract the bearer token from an Authorization header value."""
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return ""
        return token

    def verify_token(authorization: str = Header(default="")) -> None:
        if not auth_token:
            return
        token = _extract_token(authorization)
        if token != auth_token:
            raise HTTPException(status_code=401, detail="Invalid or missing token")

    def _effective_chat_id(req_chat_id: str, authorization: str = "") -> str:
        """Return the effective chat_id, namespaced by user in multi-user mode."""
        if not multi_user_mode or not auth_token:
            return req_chat_id
        token = _extract_token(authorization)
        if not token:
            return req_chat_id
        ns = _user_namespace(token)
        return f"web_{ns}_{req_chat_id}"

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def chat_page() -> HTMLResponse:
        model_name = getattr(settings, "model", "unknown") if settings else "unknown"
        provider_name = getattr(settings, "llm_provider", "unknown") if settings else "unknown"
        return HTMLResponse(
            _build_chat_page_html(
                auth_enabled=bool(auth_token),
                model_name=model_name,
                provider_name=provider_name,
            )
        )

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page() -> HTMLResponse:
        return HTMLResponse(_build_admin_page_html())

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "xclaw"}

    @app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(verify_token)])
    async def chat(req: ChatRequest, authorization: str = Header(default="")) -> ChatResponse:
        """Non-streaming chat endpoint."""
        effective_id = _effective_chat_id(req.chat_id, authorization)
        try:
            reply = await message_handler(effective_id, req.message)
            return ChatResponse(chat_id=effective_id, reply=reply)
        except Exception as exc:
            logger.error(f"Chat endpoint error: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/chat/stream", dependencies=[Depends(verify_token)])
    async def chat_stream(
        req: ChatRequest, authorization: str = Header(default="")
    ) -> EventSourceResponse:
        """SSE streaming chat endpoint."""
        if stream_handler is None:
            raise HTTPException(status_code=501, detail="Streaming not enabled")
        effective_id = _effective_chat_id(req.chat_id, authorization)

        async def event_generator():
            try:
                async for chunk in stream_handler(effective_id, req.message):
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

    # ── QQ webhook ────────────────────────────────────────────────────────────
    _qq_adapter: Any = None

    def set_qq_adapter(adapter: Any) -> None:
        nonlocal _qq_adapter
        _qq_adapter = adapter

    app.state.set_qq_adapter = set_qq_adapter

    @app.post("/webhook/qq")
    async def qq_webhook(request: Request) -> JSONResponse:
        if _qq_adapter is None:
            raise HTTPException(status_code=503, detail="QQ adapter not configured")
        payload = await request.json()
        result = await _qq_adapter.handle_event(payload)
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
        strategy_bias_threshold = getattr(settings, "strategy_bias_threshold", 5.0)
        if not isinstance(strategy_bias_threshold, (int, float)):
            strategy_bias_threshold = 5.0
        strategy_report_max_symbols = getattr(settings, "strategy_report_max_symbols", 10)
        if not isinstance(strategy_report_max_symbols, int):
            strategy_report_max_symbols = 10
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
            "wechat_enabled": getattr(settings, "wechat_enabled", False) is True,
            "wechat_mp_enabled": getattr(settings, "wechat_mp_enabled", False) is True,
            "qq_enabled": getattr(settings, "qq_enabled", False) is True,
            "data_dir": settings.data_dir,
            "timezone": settings.timezone,
            "stock_market_default": settings.stock_market_default,
            "strategy_bias_threshold": strategy_bias_threshold,
            "strategy_report_max_symbols": strategy_report_max_symbols,
            "bash_enabled": settings.bash_enabled,
            "rate_limit_per_minute": settings.rate_limit_per_minute,
            "multi_user_mode": getattr(settings, "multi_user_mode", False),
            "enabled_skills": getattr(settings, "enabled_skills", ["all"]),
            "mcp_server_enabled": getattr(settings, "mcp_server_enabled", False) is True,
        }
        return JSONResponse(safe)

    # ── Investment APIs ─────────────────────────────────────────────────────

    @app.get("/api/investment/reports", dependencies=[Depends(verify_token)])
    async def list_investment_reports(chat_id: int, limit: int = 20) -> JSONResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        rows = await db.list_investment_reports(chat_id, limit=min(limit, 50))
        return JSONResponse(rows)

    @app.get("/api/investment/reports/{report_id}", dependencies=[Depends(verify_token)])
    async def get_investment_report(report_id: int) -> JSONResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        async with db.conn.execute(
            "SELECT * FROM investment_reports WHERE id = ?",
            (report_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Report not found")
        return JSONResponse(dict(row))

    @app.post("/api/investment/reports/run", dependencies=[Depends(verify_token)])
    async def run_investment_report(payload: InvestmentReportRunRequest) -> JSONResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        service = InvestmentReportService(db=db, settings=settings)
        report = await service.generate_report(
            chat_id=payload.chat_id,
            trigger_source="web_admin",
            symbols=payload.symbols,
            market=payload.market,
        )
        return JSONResponse(report)

    @app.post("/api/investment/reports/{report_id}/exports/regenerate", dependencies=[Depends(verify_token)])
    async def regenerate_investment_exports(report_id: int) -> JSONResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        service = ReportExportService(db=db, settings=settings)
        assets = await service.generate_assets(report_id)
        return JSONResponse(assets)

    @app.get("/api/investment/reports/{report_id}/pdf", dependencies=[Depends(verify_token)])
    async def download_investment_pdf(report_id: int) -> FileResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        exports = await db.list_report_exports(report_id)
        pdf = next((item for item in exports if item["asset_type"] == "pdf"), None)
        if pdf is None:
            raise HTTPException(status_code=404, detail="PDF export not found")
        return FileResponse(pdf["file_path"], media_type="application/pdf", filename="report.pdf")

    @app.post("/api/investment/reports/{report_id}/deliver", dependencies=[Depends(verify_token)])
    async def deliver_investment_report(report_id: int, payload: InvestmentDeliverRequest) -> JSONResponse:
        if payload.channel != "wechat":
            raise HTTPException(status_code=400, detail="Only wechat delivery is supported in v1")
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")

        service = ReportExportService(db=db, settings=settings)
        assets = await service.generate_assets(report_id)

        if payload.chat_id.startswith("tenant:"):
            if _wechat_multi_tenant_service is None:
                raise HTTPException(status_code=503, detail="WeChat multi-tenant service not configured")
            media_adapter = _wechat_multi_tenant_service
        else:
            if _wechat_adapter is None:
                raise HTTPException(status_code=503, detail="WeChat adapter not configured")
            media_adapter = _wechat_adapter

        try:
            if "image" in payload.mode:
                for image in assets.get("images", []):
                    await media_adapter.send_image_response(payload.chat_id, Path(image["file_path"]))
            if "pdf" in payload.mode:
                pdf = assets.get("pdf")
                if pdf:
                    await media_adapter.send_file_response(payload.chat_id, Path(pdf["file_path"]))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return JSONResponse({"ok": True, "delivered_mode": payload.mode})

    @app.get("/api/investment/strategy-runs", dependencies=[Depends(verify_token)])
    async def list_strategy_runs(chat_id: int, limit: int = 20) -> JSONResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        rows = await db.list_strategy_runs(chat_id, limit=min(limit, 50))
        return JSONResponse(rows)

    @app.get("/api/investment/watchlist", dependencies=[Depends(verify_token)])
    async def get_investment_watchlist(chat_id: int) -> JSONResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        rows = await db.get_watchlist(chat_id)
        return JSONResponse(rows)

    @app.post("/api/investment/watchlist", dependencies=[Depends(verify_token)])
    async def add_investment_watchlist(payload: InvestmentWatchlistRequest) -> JSONResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        await db.add_to_watchlist(
            payload.chat_id,
            payload.symbol.upper(),
            payload.market.upper(),
            name=payload.name or None,
            notes=payload.notes or None,
        )
        return JSONResponse({"ok": True})

    @app.delete("/api/investment/watchlist/{symbol}", dependencies=[Depends(verify_token)])
    async def delete_investment_watchlist(chat_id: int, symbol: str, market: str = "CN") -> JSONResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        removed = await db.remove_from_watchlist(chat_id, symbol.upper(), market.upper())
        return JSONResponse({"ok": removed})

    @app.get("/api/investment/tasks", dependencies=[Depends(verify_token)])
    async def list_investment_tasks(chat_id: int) -> JSONResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        tasks = await db.get_active_tasks()
        rows = [task for task in tasks if int(task["chat_id"]) == chat_id]
        return JSONResponse(rows)

    @app.post("/api/investment/tasks", dependencies=[Depends(verify_token)])
    async def create_investment_task(payload: InvestmentTaskRequest) -> JSONResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        prompt = (
            "生成今日自选股日报，输出市场概览、关键策略卡、风险提示和免责声明。"
        )
        task_id = await db.add_scheduled_task(
            payload.chat_id,
            description=payload.description,
            prompt=prompt,
            cron_expression=payload.cron_expression,
        )
        task = await db.get_scheduled_task(task_id)
        return JSONResponse(task or {"id": task_id})

    @app.delete("/api/investment/tasks/{task_id}", dependencies=[Depends(verify_token)])
    async def delete_investment_task(task_id: int) -> JSONResponse:
        if db is None:
            raise HTTPException(status_code=503, detail="Database not available")
        await db.update_task_status(task_id, "cancelled")
        return JSONResponse({"ok": True})

    # ── WeChat Official Account (公众号) webhook ───────────────────────────────
    _wechat_adapter: Any = None

    def set_wechat_adapter(adapter: Any) -> None:
        nonlocal _wechat_adapter
        _wechat_adapter = adapter

    app.state.set_wechat_adapter = set_wechat_adapter

    @app.post("/api/auth/wechat/start", dependencies=[Depends(verify_token)])
    async def wechat_start_login() -> Any:
        if _wechat_adapter is None:
            raise HTTPException(status_code=503, detail="WeChat adapter not configured")
        try:
            return await _wechat_adapter.start_login()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            logger.error(f"WeChat login start error: {exc}")
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/api/auth/wechat/status/{login_id}", dependencies=[Depends(verify_token)])
    async def wechat_login_status(login_id: str) -> Any:
        if _wechat_adapter is None:
            raise HTTPException(status_code=503, detail="WeChat adapter not configured")
        try:
            return await _wechat_adapter.poll_login_status(login_id)
        except Exception as exc:
            from xclaw.channels.wechat import LoginAttemptNotFoundError

            if isinstance(exc, LoginAttemptNotFoundError):
                raise HTTPException(status_code=404, detail=str(exc))
            logger.error(f"WeChat login status error: {exc}")
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/api/auth/wechat/session", dependencies=[Depends(verify_token)])
    async def wechat_session() -> Any:
        if _wechat_adapter is None:
            raise HTTPException(status_code=503, detail="WeChat adapter not configured")
        return _wechat_adapter.get_session_payload()

    @app.post("/api/auth/wechat/logout", dependencies=[Depends(verify_token)])
    async def wechat_logout() -> JSONResponse:
        if _wechat_adapter is None:
            raise HTTPException(status_code=503, detail="WeChat adapter not configured")
        await _wechat_adapter.logout()
        return JSONResponse({"ok": True})

    @app.get("/api/wechat/bot/status", dependencies=[Depends(verify_token)])
    async def wechat_bot_status() -> Any:
        if _wechat_adapter is None:
            raise HTTPException(status_code=503, detail="WeChat adapter not configured")
        return _wechat_adapter.get_public_status()

    # ── WeChat Multi-tenant invite flow ──────────────────────────────────────
    _wechat_multi_tenant_service: Any = None

    def set_wechat_multi_tenant_service(service: Any) -> None:
        nonlocal _wechat_multi_tenant_service
        _wechat_multi_tenant_service = service

    app.state.set_wechat_multi_tenant_service = set_wechat_multi_tenant_service

    @app.get("/invite/{public_token}", response_class=HTMLResponse)
    async def wechat_invite_page(public_token: str) -> HTMLResponse:
        if _wechat_multi_tenant_service is None:
            raise HTTPException(status_code=503, detail="WeChat multi-tenant service not configured")
        return HTMLResponse(
            build_invite_page(public_token),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.post("/api/invite/{public_token}/sessions", response_model=InviteSessionStartResponse)
    async def start_invite_session(public_token: str) -> InviteSessionStartResponse:
        if _wechat_multi_tenant_service is None:
            raise HTTPException(status_code=503, detail="WeChat multi-tenant service not configured")
        try:
            return await _wechat_multi_tenant_service.invites.start_session(public_token)
        except InviteLinkUnavailableError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/api/invite/sessions/{invite_session_id}/refresh",
        response_model=InviteSessionStartResponse,
    )
    async def refresh_invite_session(invite_session_id: str) -> InviteSessionStartResponse:
        if _wechat_multi_tenant_service is None:
            raise HTTPException(status_code=503, detail="WeChat multi-tenant service not configured")
        try:
            return await _wechat_multi_tenant_service.invites.refresh_session(invite_session_id)
        except InviteSessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get(
        "/api/invite/sessions/{invite_session_id}",
        response_model=InviteSessionStatusPayload,
    )
    async def invite_session_status(invite_session_id: str) -> InviteSessionStatusPayload:
        if _wechat_multi_tenant_service is None:
            raise HTTPException(status_code=503, detail="WeChat multi-tenant service not configured")
        try:
            return await _wechat_multi_tenant_service.invites.poll_session(invite_session_id)
        except InviteSessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (IlinkClientError, ValueError, KeyError) as exc:
            updated_row = await _wechat_multi_tenant_service.db.update_invite_session_state(
                invite_session_id,
                "error",
                error=str(exc),
            )
            if updated_row is None:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return InviteSessionStatusPayload.model_validate(updated_row)

    @app.post(
        "/api/admin/tenants",
        response_model=CreateTenantResponse,
        dependencies=[Depends(verify_token)],
    )
    async def create_tenant(payload: CreateTenantRequest) -> CreateTenantResponse:
        if _wechat_multi_tenant_service is None:
            raise HTTPException(status_code=503, detail="WeChat multi-tenant service not configured")
        row = await _wechat_multi_tenant_service.db.create_tenant(payload.name)
        return CreateTenantResponse.model_validate(row)

    @app.post(
        "/api/admin/tenants/{tenant_id}/invite-links",
        response_model=CreateInviteLinkResponse,
        dependencies=[Depends(verify_token)],
    )
    async def create_invite_link(
        tenant_id: str,
        payload: CreateInviteLinkRequest,
        request: Request,
    ) -> CreateInviteLinkResponse:
        if _wechat_multi_tenant_service is None:
            raise HTTPException(status_code=503, detail="WeChat multi-tenant service not configured")
        if await _wechat_multi_tenant_service.db.get_tenant(tenant_id) is None:
            raise HTTPException(status_code=404, detail="Unknown tenant.")
        row = await _wechat_multi_tenant_service.db.create_invite_link(
            tenant_id,
            max_uses=payload.max_uses,
            expires_at=payload.expires_at,
        )
        base = str(request.base_url).rstrip("/")
        return CreateInviteLinkResponse(
            link_id=row["link_id"],
            tenant_id=row["tenant_id"],
            public_token=row["public_token"],
            invite_url=f"{base}/invite/{row['public_token']}",
            status=row["status"],
            created_at=row["created_at"],
        )

    @app.get(
        "/api/admin/tenants/{tenant_id}",
        response_model=TenantSummaryPayload,
        dependencies=[Depends(verify_token)],
    )
    async def tenant_summary(tenant_id: str) -> TenantSummaryPayload:
        if _wechat_multi_tenant_service is None:
            raise HTTPException(status_code=503, detail="WeChat multi-tenant service not configured")
        summary = await _wechat_multi_tenant_service.db.get_tenant_summary(tenant_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="Unknown tenant.")
        return TenantSummaryPayload.model_validate(summary)

    @app.get(
        "/api/admin/tenants/{tenant_id}/members",
        response_model=list[TenantMemberPayload],
        dependencies=[Depends(verify_token)],
    )
    async def tenant_members(tenant_id: str) -> list[TenantMemberPayload]:
        if _wechat_multi_tenant_service is None:
            raise HTTPException(status_code=503, detail="WeChat multi-tenant service not configured")
        if await _wechat_multi_tenant_service.db.get_tenant(tenant_id) is None:
            raise HTTPException(status_code=404, detail="Unknown tenant.")
        rows = await _wechat_multi_tenant_service.db.list_tenant_members(tenant_id)
        return [TenantMemberPayload.model_validate(r) for r in rows]

    @app.post(
        "/api/admin/invite-links/{link_id}/disable",
        dependencies=[Depends(verify_token)],
    )
    async def disable_invite_link(link_id: str) -> JSONResponse:
        if _wechat_multi_tenant_service is None:
            raise HTTPException(status_code=503, detail="WeChat multi-tenant service not configured")
        await _wechat_multi_tenant_service.db.disable_invite_link(link_id)
        return JSONResponse({"ok": True})

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

    # ── MCP Server endpoint ──────────────────────────────────────────────────
    mcp_server_enabled = getattr(settings, "mcp_server_enabled", False) if settings else False
    if mcp_server_enabled and tool_registry is not None:
        from xclaw.mcp_server import create_mcp_server_router

        mcp_router = create_mcp_server_router(tool_registry)
        app.include_router(mcp_router, prefix="/mcp")
        logger.info("MCP server endpoint enabled at /mcp")

    return app
