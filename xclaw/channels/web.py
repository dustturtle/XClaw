"""FastAPI web channel with REST API + SSE streaming and rate limiting."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import html
import hashlib
import json
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from loguru import logger
from pydantic import BaseModel, Field, model_validator
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
from xclaw.oauth import OAuthLoginNotFoundError
from xclaw.attachments import ChatBodyLimitMiddleware, ImageAttachment, MAX_IMAGES
from xclaw.responses import DirectReply, RejectedReply
from xclaw.receipts import ReceiptStore, ReceiptConflict, REQUEST_ID_PATTERN

# Rate limiting (slowapi)
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    _HAS_SLOWAPI = True
except ImportError:
    _HAS_SLOWAPI = False


# ── Request / Response models (module-level for annotation resolution) ────────

class JournalAcknowledgeRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=128)
    execution_id: str = Field(min_length=1, max_length=128)
    confirmed_reviewed: Literal[True]
    execution_stopped: Literal[True]
    note: str = Field(min_length=1, max_length=2000)


class ChatRequest(BaseModel):
    model: str | None = Field(default=None, min_length=1, max_length=200)
    request_id: str | None = Field(default=None, pattern=REQUEST_ID_PATTERN)
    chat_id: str = "web_default"
    message: str = ""
    attachments: list[ImageAttachment] = Field(default_factory=list, max_length=MAX_IMAGES)
    execution_mode: Literal["standard", "analysis", "prepare"] | None = None

    @model_validator(mode="after")
    def require_content(self):
        if not self.message.strip() and not self.attachments:
            raise ValueError("请输入消息或选择图片")
        return self


class ChatResponse(BaseModel):
    chat_id: str
    reply: str
    delivery: Literal["direct"] | None = None
    status: Literal["failed"] | None = None


_RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"


class ChatCancelRequest(BaseModel):
    chat_id: str = "web_default"
    run_id: str = Field(min_length=1, max_length=128, pattern=_RUN_ID_PATTERN)


class ChatProgressMessage(BaseModel):
    message_id: int | None = None
    presentation_id: str | None = None
    role: Literal["user", "assistant"]
    content: str
    run_id: str | None = Field(default=None, pattern=_RUN_ID_PATTERN)


class ChatProgressSnapshot(BaseModel):
    run_id: str | None = Field(default=None, pattern=_RUN_ID_PATTERN)
    status: Literal["idle", "queued", "running", "waiting_input", "completed", "failed", "cancelled"]
    stage: str
    message: str
    elapsed_seconds: float | None = Field(ge=0, allow_inf_nan=False)
    wall_time_ms: int = Field(ge=0)
    sequence: int = Field(ge=0)
    reply: str | None = None
    message_id: int | None = None
    presentation_id: str | None = None
    error_code: str | None = None
    messages: list[ChatProgressMessage] | None = None


def _public_chat_progress(snapshot: dict, *, include_history: bool = False) -> dict:
    """Only expose the runtime's public projection, never event/trace metadata."""
    payload = {key: value for key, value in snapshot.items() if key != "messages"}
    if include_history and "messages" in snapshot:
        payload["messages"] = [
            item for item in snapshot["messages"] or []
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
        ]
    return ChatProgressSnapshot.model_validate(payload).model_dump(exclude_none=True)


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


class OAuthCompleteRequest(BaseModel):
    login_id: str
    redirect_url_or_code: str


def _build_chat_page_html(
    *,
    auth_enabled: bool,
    model_name: str,
    provider_name: str,
    supports_images: bool = False,
    supports_progress: bool = False,
    supports_receipts: bool = False,
    supports_openui: bool = False,
    supports_prepare: bool = False,
) -> str:
    """Return a lightweight browser chat UI for local validation."""
    auth_flag = json.dumps(auth_enabled)
    safe_model = html.escape(model_name or "unknown")
    safe_provider = html.escape(provider_name or "unknown")
    openui_assets = (
        '<link rel="stylesheet" href="/chat-assets/openui.css" />'
        '<script src="/chat-assets/openui.js"></script>'
        '<script src="/chat-assets/openui-host.js"></script>'
    ) if supports_openui else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>XClaw Chat</title>
  <link rel="stylesheet" href="/chat-assets/chat.css?v=2" />
  <script src="/chat-assets/markdown-it.min.js"></script>
  <script src="/chat-assets/markdown.js?v=2"></script>
  {openui_assets}
</head>
<body>
  <div class="shell">
    <main class="main">
      <header class="toolbar">
        <div class="toolbar-title">
          <strong>XClaw <span class="brand-caption">对话</span></strong>
          <label class="sr-only" for="model-select">本次对话模型</label>
          <select class="model-select" id="model-select" aria-label="本次对话模型" title="仅用于本会话的新请求，不修改服务默认模型" disabled>
            <option value="">加载模型…</option>
          </select>
        </div>
        <div class="toolbar-actions">
          <div class="status" id="status-pill">准备就绪</div>
          <details class="settings" id="chat-settings">
            <summary>设置</summary>
            <div class="settings-panel">
              <div class="settings-heading">会话设置</div>
              <div class="field">
                <label for="chat-id">会话 ID</label>
                <input id="chat-id" placeholder="比如 demo 或 my-session" />
              </div>
              <div class="field">
                <label for="auth-token">Bearer Token（可选）</label>
                <input id="auth-token" type="password" autocomplete="off" placeholder="服务启用鉴权时在这里填" />
              </div>
              <div class="field" {('' if supports_progress or supports_prepare else 'hidden')}>
                <label for="execution-mode">执行模式</label>
                <select id="execution-mode">
                  <option value="standard" {("selected" if supports_prepare and not supports_progress else "")}>普通 · 3 分钟</option>
                  <option value="analysis" {("" if supports_prepare and not supports_progress else "selected")}>分析 · 10 分钟</option>
                  {('<option value="prepare">预览操作，确认后执行 · 3 分钟</option>' if supports_prepare else '')}
                </select>
              </div>
              <p class="settings-note" id="composer-note">{("当前服务启用了鉴权，请先填写 Token。" if auth_enabled else "当前服务未启用鉴权，可以直接开始聊天。")}</p>
              <div class="settings-meta">{safe_provider} / <span class="model-name">{safe_model}</span></div>
              <button class="ghost" id="clear-btn" type="button">清空显示</button>
              <button class="ghost" id="release-receipt-btn" type="button" hidden>结束本次提交核对</button>
              <p class="settings-note">只清空页面，不会取消任务或重置上下文。重置上下文请发送 /reset。</p>
              <a href="/docs" target="_blank" rel="noopener noreferrer">API 文档</a>
            </div>
          </details>
        </div>
      </header>
      <section class="messages" id="messages" tabindex="0" aria-label="对话消息" aria-live="polite"></section>
      <details id="write-recovery" hidden>
        <summary id="write-recovery-summary">有写操作需要人工核对</summary>
        <p>这些操作可能已经生效，后续写入已暂停。请先只读查询对应业务记录，停止原执行，再逐条确认。本次确认不会重试，也不表示原操作成功。</p>
        <button id="refresh-write-recovery" type="button">刷新待核对记录</button>
        <div id="write-recovery-records"></div>
      </details>
      <section class="composer">
        <button id="follow-tail" type="button" hidden>↓ 返回最新消息</button>
        <div class="composer-inner">
          <div class="run-controls" id="run-controls" {('' if supports_progress else 'hidden')}>
            <details class="run-progress">
              <summary><strong id="run-stage">尚未开始任务</strong><span id="run-time">已耗时 0 秒 / 预算 10 分钟</span></summary>
              <div class="progress-detail" id="run-message" role="status" aria-live="polite">显示实际执行进度，不是逐字输出。</div>
            </details>
            <button class="ghost compact" id="cancel-run-btn" type="button" {('' if supports_progress else 'hidden')} disabled>取消任务</button>
            <button class="ghost compact" id="resume-progress-btn" type="button" hidden>重新连接</button>
          </div>
          <div class="composer-box">
            <label class="sr-only" for="message-input">消息</label>
            <textarea id="message-input" rows="2" placeholder="继续提问，或粘贴需要分析的内容…"></textarea>
            <div class="attachment-preview" id="attachment-preview"></div>
            <div class="composer-actions">
              <div class="input-tools">
                <label class="attach-button" {('' if supports_images else 'hidden')} title="PNG / JPEG / WebP，最多 4 张，每张 5 MB">
                  <span>添加图片</span>
                  <input type="file" id="image-input" aria-label="添加图片" accept="image/png,image/jpeg,image/webp" multiple />
                </label>
                <button class="ghost compact" id="clear-images-btn" type="button" hidden>清除图片</button>
                <span class="keyboard-hint">Enter 发送 · Shift + Enter 换行</span>
              </div>
              <button class="primary" id="send-btn" type="button">发送</button>
            </div>
          </div>
        </div>
      </section>
    </main>
  </div>
  <script>
    const AUTH_REQUIRED = {auth_flag};
    const SUPPORTS_PROGRESS = {json.dumps(supports_progress)};
    const SUPPORTS_RECEIPTS = {json.dumps(supports_receipts)};
    const SUPPORTS_OPENUI = {json.dumps(supports_openui)};
    const SUPPORTS_PREPARE = {json.dumps(supports_prepare)};
    const DEFAULT_EXECUTION_MODE = SUPPORTS_PREPARE && !SUPPORTS_PROGRESS ? "standard" : "analysis";
    const POLL_INTERVAL_MS = 1500;
    const POLL_TIMEOUT_MS = 8000;
    const MAX_POLLS = 600;
    const MAX_POLL_FAILURES = 4;
    const MISSING_SUBMISSION_CHECKS = 4;
    const TERMINAL = new Set(["completed", "failed", "cancelled"]);
    const ACTIVE = new Set(["queued", "running", "waiting_input"]);
    const STATUS_LABELS = {{
      idle: "准备就绪", queued: "排队中", running: "执行中", waiting_input: "等待补充信息",
      completed: "任务已完成", failed: "任务失败", cancelled: "任务已取消"
    }};
    const STORAGE_KEYS = {{chatId: "xclaw.chat_id", token: "xclaw.auth_token"}};
    const messagesEl = document.getElementById("messages");

    let followingTail = true, tailFrame = 0, lastTop = 0;
    const tailButton = document.getElementById("follow-tail");
    tailButton.addEventListener("click", () => followMessages(true));
    function followMessages(force = false) {{
      if (force) {{ followingTail = true; tailButton.hidden = true; }}
      if (!followingTail || tailFrame) return;
      tailFrame = requestAnimationFrame(() => {{
        tailFrame = 0;
        if (!followingTail) return;
        const bottom = Math.max(0, messagesEl.scrollHeight - messagesEl.clientHeight);
        if (Math.abs(messagesEl.scrollTop - bottom) > 1) messagesEl.scrollTop = bottom;
        lastTop = messagesEl.scrollTop;
      }});
    }}
    let direction = 0, intentUntil = 0;
    function scrollIntent(delta, target) {{
      if (!delta || messagesEl.scrollHeight <= messagesEl.clientHeight) return;
      for (let el = target; el && el !== messagesEl; el = el.parentElement) {{
        if (el.matches?.('input, textarea, select, [contenteditable=true]')) return;
        if (el.scrollHeight > el.clientHeight && /auto|scroll/.test(getComputedStyle(el).overflowY)
            && (delta < 0 ? el.scrollTop > 0 : el.scrollTop + el.clientHeight < el.scrollHeight - 1)) return;
      }}
      direction = Math.sign(delta); intentUntil = performance.now() + 1200;
      if (direction < 0) {{ followingTail = false; tailButton.hidden = false; }}
      else if (messagesEl.scrollHeight - messagesEl.clientHeight - messagesEl.scrollTop <= 4) followMessages(true);
    }}
    messagesEl.addEventListener('wheel', e => {{
      if (!e.shiftKey && !e.ctrlKey && Math.abs(e.deltaY) > Math.abs(e.deltaX)) scrollIntent(e.deltaY, e.target);
    }}, {{passive:true}});
    let touchY = 0, touchX = 0;
    messagesEl.addEventListener('touchstart', e => {{
      touchY = e.touches[0].clientY; touchX = e.touches[0].clientX;
    }}, {{passive:true}});
    messagesEl.addEventListener('touchmove', e => {{
      const t = e.touches[0], dy = touchY - t.clientY;
      if (e.touches.length === 1 && Math.abs(dy) > Math.abs(touchX - t.clientX)) scrollIntent(dy,e.target);
      touchY = t.clientY; touchX = t.clientX;
    }}, {{passive:true}});
    messagesEl.addEventListener('keydown', e => {{
      if (e.defaultPrevented || e.altKey || e.metaKey || e.ctrlKey || e.target.closest?.(
        'input,textarea,select,button,summary,[contenteditable]:not([contenteditable=false]),[role=slider],[role=menu],[role=menubar],[role=menuitem],[role=button],[role=listbox],[role=tablist],[role=spinbutton],[role=combobox],[role=tree],[role=grid]'
      )) return;
      if (['ArrowUp','PageUp','Home'].includes(e.key) || (e.key === ' ' && e.shiftKey)) scrollIntent(-1,e.target);
      if (['ArrowDown','PageDown','End'].includes(e.key) || (e.key === ' ' && !e.shiftKey)) scrollIntent(1,e.target);
    }});
    let draggingBar = false;
    messagesEl.addEventListener('pointerdown', e => {{
      if (e.pointerType === 'mouse' && e.target === messagesEl && e.clientX >= messagesEl.getBoundingClientRect().right - Math.max(14,messagesEl.offsetWidth - messagesEl.clientWidth)) {{
        draggingBar = true; followingTail = false; tailButton.hidden = false;
      }}
    }});
    window.addEventListener('pointerup', () => {{
      if (draggingBar) {{ draggingBar = false; scrollIntent(1); }}
    }});
    messagesEl.addEventListener('scroll', () => {{
      const top = messagesEl.scrollTop, gap = messagesEl.scrollHeight - messagesEl.clientHeight - top;
      if (!draggingBar && direction > 0 && performance.now() <= intentUntil && top > lastTop && gap <= 4) followMessages(true);
      lastTop = top;
    }}, {{passive:true}});
    const tailSizes = new ResizeObserver(() => followMessages());
    tailSizes.observe(messagesEl);
    new MutationObserver(records => {{
      for (const record of records) {{
        for (const el of record.removedNodes) if (el.nodeType === 1) tailSizes.unobserve(el);
        for (const el of record.addedNodes) if (el.nodeType === 1) tailSizes.observe(el);
      }}
      followMessages();
    }}).observe(messagesEl, {{childList:true}});

    const chatIdInput = document.getElementById("chat-id");
    const tokenInput = document.getElementById("auth-token");
    const messageInput = document.getElementById("message-input");
    const sendBtn = document.getElementById("send-btn");
    const statusPill = document.getElementById("status-pill");
    const imageInput = document.getElementById("image-input");
    const clearImagesBtn = document.getElementById("clear-images-btn");
    const previewEl = document.getElementById("attachment-preview");
    const modeInput = document.getElementById("execution-mode");
    const modelInput = document.getElementById("model-select");
    const cancelBtn = document.getElementById("cancel-run-btn");
    const resumeBtn = document.getElementById("resume-progress-btn");
    const stageEl = document.getElementById("run-stage");
    const progressMessageEl = document.getElementById("run-message");
    const timeEl = document.getElementById("run-time");
    const drafts = new Map();
    let active = null;
    let selectedFiles = [];
    let previewUrls = [];

    function stored(storage, key, value) {{
      try {{
        if (value === undefined) return storage.getItem(key);
        storage.setItem(key, value);
      }} catch (_error) {{ /* Storage may be disabled or full; keep the in-memory draft. */ }}
      return null;
    }}

    function current(ctx) {{
      return active === ctx && chatIdInput.value.trim() === ctx.chatId && tokenInput.value.trim() === ctx.token;
    }}

    function headers(ctx) {{
      return ctx.token ? {{"Authorization": "Bearer " + ctx.token}} : {{}};
    }}

    function setStatus(text, failed = false) {{
      statusPill.textContent = text;
      statusPill.setAttribute("style", failed ? "color:#9f331c;background:#fff0e9;border-color:#e5b19f" : "");
    }}

    function renderFiles(files) {{
      previewUrls.forEach(url => URL.revokeObjectURL(url));
      previewUrls = [];
      selectedFiles = [...files];
      previewEl.replaceChildren();
      imageInput.value = "";
      files.forEach(file => {{
        const preview = document.createElement("img");
        const url = URL.createObjectURL(file);
        previewUrls.push(url);
        preview.src = url;
        preview.alt = file.name;
        previewEl.appendChild(preview);
      }});
      clearImagesBtn.hidden = !files.length;
    }}

    function saveDraft(ctx = active) {{
      if (!ctx?.draftReady) return;
      const p = ctx.pending;
      const receipt = p?.requestId && !p.receiptFinished ? {{
        requestId: p.requestId, fingerprint: p.fingerprint, mode: p.mode, model: p.model, draftText: p.draftText
      }} : null;
      const draft = {{text: messageInput.value, files: [...selectedFiles], mode: modeInput.value, model: modelInput.value,
        receipt, requestFiles: p?.files || []}};
      drafts.set(ctx.key, draft);
      if (ctx.draftKey) {{
        stored(sessionStorage, ctx.draftKey, JSON.stringify({{text: draft.text, mode: draft.mode, model: draft.model, receipt}}));
        if (SUPPORTS_RECEIPTS) stored(localStorage, ctx.draftKey + ".receipt",
          JSON.stringify(receipt ? {{requestId: receipt.requestId, fingerprint: receipt.fingerprint, mode: receipt.mode, model: receipt.model}} : null));
      }}
    }}

    function readImage(file) {{
      return new Promise((resolve, reject) => {{
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("image_read_failed"));
        reader.onload = () => resolve({{name: file.name, media_type: file.type, data: reader.result.split(",")[1]}});
        reader.readAsDataURL(file);
      }});
    }}

    function syncPresentationStatus(ctx) {{
      const view = ctx.presentation;
      if (!view || !current(ctx) || ctx.pending !== view.pending || !view.status) return;
      const labels = {{generating:"正在整理图表",completed:"响应完成",failed:"文字已完成，图表未完成"}};
      setStatus(labels[view.status], view.status === "failed");
      stageEl.textContent = view.status === "generating" ? "整理图表" : view.status === "failed" ? "图表未完成" : "已完成";
    }}

    function appendMessage(role, text, attachments = [], history = false, allowUI = true, identity = {{}}) {{
      if (role === "user" && active) active.presentation = null;
      const item = document.createElement("article");
      item.className = "message " + role;
      const verifiedModel = history ? identity.model : identity.response_model;
      if (role === "assistant" && typeof verifiedModel === "string" && verifiedModel) item.setAttribute("data-model", verifiedModel);
      if (role === "assistant" && window.renderChatMarkdown) {{
        const content = document.createElement("div");
        content.className = "markdown";
        content.innerHTML = window.renderChatMarkdown(text);
        item.appendChild(content);
      }} else if (role === "user" && text.length > 1200) {{
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = text.slice(0, 80).replace(/\\s+/g, " ") + "… · 展开原始消息（" + text.length + " 字）";
        const content = document.createElement("div");
        content.className = "user-content";
        content.textContent = text;
        details.appendChild(summary);
        details.appendChild(content);
        item.appendChild(details);
      }} else item.textContent = text;
      attachments.forEach(attachment => {{
        const img = document.createElement("img");
        img.src = "data:" + attachment.media_type + ";base64," + attachment.data;
        img.alt = attachment.name;
        item.appendChild(img);
      }});
      messagesEl.appendChild(item);
      if (role === "assistant" && SUPPORTS_OPENUI && active && allowUI) {{
        const ctx = active;
        const view = !history ? {{pending:ctx.pending,status:null}} : null;
        if (view) ctx.presentation = view;
        void window.XClawOpenUIHost?.attach(item, text, {{chatId:ctx.chatId, history,
          getToken:() => ctx.token, isCurrent:() => current(ctx),messageId:identity.message_id,presentationId:identity.presentation_id,
          onPresentationStatus:status => {{
            if (!view || !current(ctx) || ctx.presentation !== view || ctx.pending !== view.pending
                || !["generating","completed","failed"].includes(status)) return;
            view.status = status;
            syncPresentationStatus(ctx);
          }},
          onContinue:message => {{
            if (!current(ctx) || sendBtn.disabled || messageInput.value.trim()) return;
            messageInput.value = message;
            void sendMessage();
          }}
        }});
      }}
      followMessages(role === "user" && !history);
    }}

    function controls(ctx) {{
      if (!current(ctx)) return;
      const status = ctx.snapshot?.status;
      const busy = ctx.syncing || ctx.cancelling || (ctx.pending && !ctx.pending.settled) || ["queued", "running"].includes(status)
        || (ACTIVE.has(status) && ["reconnecting", "cancelling"].includes(ctx.snapshot?.stage));
      if (!SUPPORTS_PROGRESS) document.getElementById("run-controls").hidden = !busy && !(ctx.pending?.requestId && !ctx.pending.receiptFinished);
      sendBtn.disabled = Boolean(busy || !ctx.modelsReady || !ctx.modelOptions?.includes(modelInput.value));
      sendBtn.textContent = busy ? (ctx.observationPaused ? "待确认" : "处理中") : status === "waiting_input" ? "继续" : "发送";
      imageInput.disabled = Boolean(busy);
      clearImagesBtn.disabled = Boolean(busy);
      modeInput.disabled = Boolean(busy || status === "waiting_input");
      modelInput.disabled = Boolean(busy || !ctx.modelsReady || status === "waiting_input"
        || (ctx.pending?.requestId && !ctx.pending.receiptFinished));
      cancelBtn.disabled = ctx.cancelling || ctx.snapshot?.stage === "cancelling" || !ctx.snapshot?.run_id || !ACTIVE.has(status);
      document.getElementById("release-receipt-btn").hidden = !(ctx.pending?.requestId
        && ctx.pending.settled && !ctx.pending.receiptFinished);
    }}

    function stopObservation(ctx) {{
      clearLiveText(ctx);
      clearTimeout(ctx.timer);
      ctx.epoch++;
      ctx.controller?.abort();
      ctx.controller = null;
    }}

    function pauseObservation(ctx, message) {{
      stopObservation(ctx);
      ctx.observationPaused = true;
      resumeBtn.hidden = false;
      setStatus("进度更新已暂停");
      stageEl.textContent = "状态待确认";
      progressMessageEl.textContent = message;
      controls(ctx);
    }}


    function recoverySummary(records) {{
      return records.length && records.every(row => row.tool_name === "strategy_scan")
        ? "上次策略分析需要核对" : "有写操作需要人工核对";
    }}

    async function loadWriteRecovery(ctx) {{
      if (!current(ctx) || !ctx.chatId || (AUTH_REQUIRED && !ctx.token)) return;
      const panel = document.getElementById("write-recovery");
      const list = document.getElementById("write-recovery-records");
      try {{
        const response = await fetch("/api/chat/write-recovery?" + new URLSearchParams({{chat_id: ctx.chatId}}), {{headers: headers(ctx)}});
        if (!response.ok) throw new Error("待核对记录暂时无法读取，请稍后刷新。");
        const data = await response.json();
        if (!current(ctx)) return;
        list.replaceChildren();
        panel.hidden = !(data.records || []).length;
        document.getElementById("write-recovery-summary").textContent = recoverySummary(data.records || []);
        for (const row of data.records || []) {{
          const section = document.createElement("section");
          const info = document.createElement("div");
          let args = {{}};
          try {{ args = JSON.parse(row.arguments_json); }} catch (_error) {{ /* Raw parameters remain available below. */ }}
          const isStrategy = row.tool_name === "strategy_scan";
          const title = document.createElement("strong");
          const symbol = typeof args?.symbol === "string" ? " · " + args.symbol : "";
          title.textContent = (isStrategy ? "策略扫描（本地分析记录）" : row.tool_name) + symbol + " · 待核对";
          info.appendChild(title);
          const explanation = document.createElement("p");
          explanation.textContent = isStrategy
            ? "这是策略分析，不是买入、卖出或下单。扫描会保存本地分析记录；本次是否已经保存尚未确认。请只读查看该标的的策略扫描记录，不要直接重复执行。"
            : "该操作的实际结果尚未确认，可能已经生效。请只读核对对应业务记录，不要直接重复执行。";
          info.appendChild(explanation);
          const time = document.createElement("p"); time.textContent = "开始时间：" + row.created_at + " UTC"; info.appendChild(time);
          const details = document.createElement("details");
          const summary = document.createElement("summary"); summary.textContent = "查看操作参数"; details.appendChild(summary);
          const raw = document.createElement("pre"); raw.textContent = row.arguments_json; details.appendChild(raw);
          info.appendChild(details); section.appendChild(info);
          const checks = ["我已只读核对实际业务结果", "我确认原执行已停止"] .map(text => {{
            const label = document.createElement("label"), input = document.createElement("input");
            input.type = "checkbox"; label.appendChild(input); label.appendChild(document.createTextNode(text)); section.appendChild(label); return input;
          }});
          const note = document.createElement("textarea"); note.placeholder = "核对说明（例如实际查到的记录、是否已生效）"; note.maxLength = 2000; section.appendChild(note);
          const button = document.createElement("button"); button.textContent = "记录核对并解除屏障（不重试）"; button.disabled = true;
          const status = document.createElement("p"); status.setAttribute("role", "status");
          const validate = () => {{ button.disabled = !checks.every(c => c.checked) || !note.value.trim(); }};
          checks.forEach(c => c.addEventListener("change", validate)); note.addEventListener("input", validate);
          button.addEventListener("click", async () => {{
            if (!current(ctx) || button.disabled) return;
            button.disabled = true;
            try {{
              const result = await fetch("/api/chat/write-recovery/acknowledge", {{method: "POST", headers: {{...headers(ctx), "Content-Type": "application/json"}}, body: JSON.stringify({{chat_id: ctx.chatId, execution_id: row.id, confirmed_reviewed: checks[0].checked, execution_stopped: checks[1].checked, note: note.value.trim()}})}});
              const payload = await result.json();
              if (!current(ctx)) return;
              if (!result.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "确认未完成，请核对后重试。");
              appendMessage("system", payload.message);
              await loadWriteRecovery(ctx);
            }} catch (error) {{ if (current(ctx)) {{ status.textContent = error.message; validate(); }} }}
          }});
          section.appendChild(button); section.appendChild(status); list.appendChild(section);
        }}
      }} catch (error) {{
        if (current(ctx) && !panel.hidden) {{ list.textContent = error.message; }}
      }}
    }}
    document.getElementById("refresh-write-recovery").addEventListener("click", () => {{ if (active) loadWriteRecovery(active); }});
    function replyKey(snapshot) {{
      return snapshot.run_id + ":" + (snapshot.status === "waiting_input" ? "input:" + snapshot.sequence : "final");
    }}

    function showReply(ctx, snapshot, reply) {{
      if (!reply || !snapshot.run_id) return;
      const key = replyKey(snapshot);
      if (ctx.delivered.has(key)) return;
      ctx.delivered.add(key);
      loadWriteRecovery(ctx);
      if (!finishInline(ctx, reply, snapshot.status, snapshot.response_model)) appendMessage("assistant", reply, [], false, !["failed", "cancelled"].includes(snapshot.status), snapshot);
    }}

    function clearAcceptedDraft(ctx, pending) {{
      if (messageInput.value === pending.draftText) messageInput.value = "";
      if (selectedFiles.length === pending.files.length && selectedFiles.every((file, index) => file === pending.files[index])) {{
        renderFiles([]);
      }}
      saveDraft(ctx);
    }}

    function failSubmission(ctx, pending, message) {{
      if (pending.settled) return;
      pending.settled = true;
      ctx.syncing = false;
      appendMessage("system", message);
      setStatus("发送失败");
      stageEl.textContent = "提交未完成";
      progressMessageEl.textContent = message;
      controls(ctx);
    }}

    function applySnapshot(ctx, snapshot, withHistory = false) {{
      if (!Object.hasOwn(STATUS_LABELS, snapshot.status)) throw new Error("invalid_snapshot");
      const pending = ctx.pending;
      // The first poll after POST may still describe the previous Run. Never finish the new submission with it.
      if (pending && !pending.runId && !pending.settled) {{
        const changedRun = snapshot.run_id && snapshot.run_id !== pending.baseRunId;
        const continuedRun = snapshot.run_id && pending.continuing && snapshot.run_id === pending.baseRunId
          && snapshot.sequence > pending.baseSequence;
        if (changedRun || continuedRun) pending.runId = snapshot.run_id;
        else {{
          const stablePreviousState = !["queued", "running"].includes(snapshot.status)
            && !["reconnecting", "cancelling"].includes(snapshot.stage);
          // A lost POST is not proof of rejection. Give acceptance recovery time,
          // but do not poll an unchanged previous Run forever or replay the POST.
          if (pending.transportError && stablePreviousState) {{
            pending.missingChecks = (pending.missingChecks || 0) + 1;
            if (pending.missingChecks >= MISSING_SUBMISSION_CHECKS) {{
              failSubmission(ctx, pending, "连接已恢复，但未确认本次提交对应的新任务。问题和图片已保留；请核对状态后手动重试，也可重新连接再次查询。");
              setStatus("提交结果未确认");
              stageEl.textContent = "未确认新任务";
              resumeBtn.hidden = false;
              return false;
            }}
            setStatus("正在核对提交结果");
            stageEl.textContent = "确认中";
            progressMessageEl.textContent = "连接已恢复，正在确认本次提交是否被接收，不会自动重发。";
          }} else pending.missingChecks = 0;
          if (pending.httpError && !ACTIVE.has(snapshot.status)) {{
            failSubmission(ctx, pending, "消息处理失败，草稿和图片已保留，可手动重试。");
            return false;
          }}
          if (pending.httpDone && !["queued", "running"].includes(snapshot.status)
              && !["reconnecting", "cancelling"].includes(snapshot.stage)) {{
            failSubmission(ctx, pending, "请求已结束，但未查到新任务进展。草稿和图片已保留，请核对状态后手动重试。");
            return false;
          }}
          return true;
        }}
      }}
      if (pending && !pending.settled && pending.runId && pending.runId !== snapshot.run_id) return true;
      if (ctx.snapshot && ctx.snapshot.run_id === snapshot.run_id && snapshot.sequence < ctx.snapshot.sequence) return true;
      if (ctx.finishedRuns.has(snapshot.run_id) && !TERMINAL.has(snapshot.status)) return false;
      ctx.snapshot = snapshot;
      ctx.syncing = false;
      if (ACTIVE.has(snapshot.status) && [180000, 600000].includes(snapshot.wall_time_ms)) {{
        modeInput.value = pending?.mode === "prepare" ? "prepare" : snapshot.wall_time_ms === 180000 ? "standard" : "analysis";
      }}

      if (withHistory && !ctx.historyLoaded) {{
        ctx.historyLoaded = true;
        window.XClawOpenUIHost?.clear();
      messagesEl.replaceChildren();
        const history = (snapshot.messages || []).filter(item => ["user", "assistant"].includes(item.role) && typeof item.content === "string");
        ctx.historyFailed = history.at(-1)?.role === "assistant" && history.at(-1)?.status === "failed";
        history.forEach(item => {{ if (item.status === "failed") appendMessage(item.role, item.content, [], true, false); else appendMessage(item.role, item.content, [], true, true, item); }});
        followMessages(true);
        const last = history.at(-1);
        if (last?.role === "assistant" && (!last.run_id || last.run_id === snapshot.run_id)
            && snapshot.reply && last.content === snapshot.reply) {{
          ctx.delivered.add(replyKey(snapshot));
        }}
      }}

      setStatus(snapshot.status === "idle" && ctx.historyFailed ? "上次任务未完成" : STATUS_LABELS[snapshot.status], snapshot.status === "failed" || (snapshot.status === "idle" && ctx.historyFailed));
      stageEl.textContent = ({{idle:"准备就绪",queued:"已接收",model:"分析中",tool:"查询中",finalizing:"整理结论",waiting_input:"待补充",completed:"已完成",failed:"未完成",cancelled:"已取消",cancelling:"取消中",reconnecting:"正在重新连接"}})[snapshot.stage] || STATUS_LABELS[snapshot.status];
      progressMessageEl.textContent = snapshot.message || STATUS_LABELS[snapshot.status];
      const budget = snapshot.wall_time_ms || (["standard","prepare"].includes(pending?.mode || modeInput.value) ? 180000 : 600000);
      timeEl.textContent = (snapshot.elapsed_seconds == null ? "耗时未记录" : "已耗时 " + Math.floor(snapshot.elapsed_seconds) + " 秒") + " / 预算 " + (budget / 60000) + " 分钟";

      const ended = TERMINAL.has(snapshot.status) || (snapshot.status === "waiting_input"
        && !["reconnecting", "cancelling"].includes(snapshot.stage));
      if (ended) {{
        const matching = pending && pending.runId === snapshot.run_id;
        if (matching && !pending.settled) {{
          pending.settled = true;
          if (["completed", "waiting_input"].includes(snapshot.status)) clearAcceptedDraft(ctx, pending);
        }}
        showReply(ctx, snapshot, snapshot.reply);
        if (TERMINAL.has(snapshot.status)) ctx.finishedRuns.add(snapshot.run_id);
      }}
      controls(ctx);
      syncPresentationStatus(ctx);
      // Terminal state and persisted reply can become visible on adjacent reads.
      if (snapshot.status === "completed" && !snapshot.reply) {{
        ctx.replyAttempts = (ctx.replyAttempts || 0) + 1;
        if (ctx.replyAttempts < 8) return true;
        progressMessageEl.textContent = "任务已完成，终答尚未同步。请重新连接，不要重新发送。";
        resumeBtn.hidden = false;
      }} else ctx.replyAttempts = 0;
      return !ended && snapshot.status !== "idle";
    }}

    function clearStreamDiagnostic(ctx) {{
      ctx.streamDiagnostic?.panel.remove();
      ctx.streamDiagnostic = null;
    }}
    function recordStreamDiagnostic(ctx, kind, event = {{}}) {{
      if (!current(ctx)) return;
      if (new URLSearchParams(window.location?.search || "").get("stream_diagnostics") !== "1") return;
      const requestId = ctx.pending?.requestId;
      if (!ctx.streamDiagnostic || ctx.streamDiagnostic.requestId !== requestId) {{
        clearStreamDiagnostic(ctx);
        const panel=document.createElement("details"), summary=document.createElement("summary"), rows=document.createElement("pre");
        summary.textContent="流式观察记录（本地指标，非精确屏幕绘制时间）";
        panel.appendChild(summary);panel.appendChild(rows);messagesEl.insertAdjacentElement("afterend",panel);
        ctx.streamDiagnostic={{requestId,panel,rows,start:performance.now(),lines:[]}};
      }}
      const diagnostic=ctx.streamDiagnostic, bubble=ctx.liveBubble;
      const metadata={{seq:event.seq ?? "-",round:event.round ?? "-",chars:typeof event.text === "string" ? event.text.length : 0}};
      requestAnimationFrame(() => requestAnimationFrame(() => {{
        if (!current(ctx) || ctx.streamDiagnostic !== diagnostic) return;
        const box=bubble?.isConnected ? bubble.getBoundingClientRect() : null, viewport=messagesEl.getBoundingClientRect();
        const intersects=Boolean(box && box.bottom>Math.max(0,viewport.top) && box.top<Math.min(window.innerHeight,viewport.bottom)
          && box.right>Math.max(0,viewport.left) && box.left<Math.min(window.innerWidth,viewport.right));
        const elapsed=Math.round(performance.now()-diagnostic.start);
        diagnostic.lines.push(`${{elapsed}}ms ${{kind}} seq=${{metadata.seq}} round=${{metadata.round}} chars=${{metadata.chars}} visibility=${{document.visibilityState}} intersects=${{intersects}}`);
        if(diagnostic.lines.length>300)diagnostic.lines.shift();
        diagnostic.rows.textContent=diagnostic.lines.join("\\n");
      }}));
    }}

    function onStreamDiagnosticEvent(event) {{
      if (new URLSearchParams(window.location?.search || "").get("stream_diagnostics") !== "1" || !active) return;
      const phase=event.detail?.phase, tool=event.detail?.tool;
      if (phase === "card-data-visible") {{
        const {{cardType,documentVisible,inViewport}} = event.detail;
        if (typeof cardType !== "string" || !/^[a-zA-Z][a-zA-Z0-9_]{{0,63}}$/.test(cardType)
            || typeof documentVisible !== "boolean" || typeof inViewport !== "boolean") return;
        recordStreamDiagnostic(active,phase+" card="+cardType+" documentVisible="+documentVisible+" inViewport="+inViewport);
        return;
      }}
      if (!["query-start","query-end","provider-ready"].includes(phase)) return;
      if (tool !== undefined && (typeof tool !== "string" || !/^[a-zA-Z][a-zA-Z0-9_]{{0,63}}$/.test(tool))) return;
      // Only whitelisted metadata is copied; never serialize arbitrary detail.
      recordStreamDiagnostic(active,phase+(tool ? " tool="+tool : ""));
    }}
    window.addEventListener("xclaw-stream-diagnostic",onStreamDiagnosticEvent);

    function finishInline(ctx, reply, status, responseModel) {{
      if (!ctx.inlinePresentation || !ctx.liveBubble) return false;
      if (ctx.inlineFinishing) return true;
      ctx.inlineFinishing = true;
      const bubble=ctx.liveBubble, requestId=ctx.liveRequestId;
      if (status === "completed" && typeof responseModel === "string" && responseModel) bubble.setAttribute("data-model", responseModel);
      void (async()=>{{
        try {{
          if (status !== "completed") throw new Error("unfinished");
          let presentation=ctx.inlinePresentation;
          const digest=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(reply));
          const replyHash=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,"0")).join("");
          if(presentation.reply_hash!==undefined&&presentation.reply_hash!==replyHash)throw new Error("reply_hash_mismatch");
          presentation={{...presentation,reply_hash:replyHash}};
          if (!presentation.validated || presentation.provisional) {{
            const response=await fetch('/api/openui/presentation',{{method:'POST',headers:headers(ctx),body:JSON.stringify({{chat_id:ctx.chatId,reply_hash:replyHash,...(presentation.presentation_id?{{presentation_id:presentation.presentation_id}}:{{}})}})}});
            if(!response.ok)throw new Error("presentation_unavailable");
            const saved=await response.json(), source=JSON.parse(saved.source);
            if(source.version!=="xclaw-narrative-1"||!Array.isArray(source.blocks))throw new Error("invalid_presentation");
            if(presentation.presentation_id && (saved.presentation_id!==presentation.presentation_id||source.presentation_id!==presentation.presentation_id))throw new Error("presentation_identity_mismatch");
            presentation={{...presentation,blocks:source.blocks,provisional:false,validated:true,reply_hash:replyHash,message_id:saved.message_id}};
          }}
          if(!current(ctx)||ctx.liveRequestId!==requestId)return;
          if(presentation.blocks.filter(b=>b.type==='text').map(b=>b.source).join("\\n\\n")!==reply)throw new Error("changed_prose");
          if(!window.XClawOpenUIHost.updateLive(bubble,presentation,{{chatId:ctx.chatId,runId:requestId,getToken:()=>ctx.token,isCurrent:()=>current(ctx)}}))throw new Error("invalid_presentation");
          bubble.classList.remove('streaming');ctx.liveLabel?.remove();ctx.liveNote?.remove();
          recordStreamDiagnostic(ctx,"inline-committed");
        }} catch (_) {{
          if(current(ctx)&&ctx.liveRequestId===requestId){{
            window.XClawOpenUIHost?.failLive?.(bubble);
            if(ctx.liveLabel)ctx.liveLabel.textContent="本次生成未完成";
            if(status==='completed'){{
              window.XClawOpenUIHost?.disposeLive?.(bubble);
              bubble.classList.remove('streaming');bubble.replaceChildren();const text=document.createElement('div');text.className='markdown';text.innerHTML=window.renderChatMarkdown(reply);bubble.appendChild(text);
              const note=document.createElement('p');note.textContent='图表未通过最终校验，已保留完整文字答复。';bubble.appendChild(note);
            }}
          }}
        }} finally {{
          if(ctx.liveBubble===bubble){{ctx.liveController?.abort();ctx.liveController=null;ctx.liveBubble=null;ctx.inlinePresentation=null;ctx.inlineFinishing=false;}}
        }}
      }})();
      return true;
    }}
    function clearLiveText(ctx) {{
      ctx.liveController?.abort(); ctx.liveController = null;
      if(ctx.liveBubble)window.XClawOpenUIHost?.disposeLive?.(ctx.liveBubble);
      ctx.liveBubble?.remove(); ctx.liveBubble = null;ctx.inlinePresentation=null;ctx.inlineFinishing=false;
    }}
    async function observeLiveText(ctx) {{
      if (ctx.liveController || !ctx.pending || ctx.pending.settled) return;
      const requestId = ctx.pending.requestId;
      if (ctx.liveRequestId !== requestId) {{ ctx.liveRequestId = requestId; ctx.liveSeq = -1; }}
      const controller = new AbortController(); ctx.liveController = controller;
      try {{
        const query = new URLSearchParams({{chat_id:ctx.chatId}});
        const response = await fetch('/api/chat/receipts/'+requestId+'/events?'+query, {{headers:headers(ctx),signal:controller.signal,cache:'no-store'}});
        if (!response.ok || !response.body) return;
        const reader=response.body.getReader(), decoder=new TextDecoder(); let buffer='';
        while (true) {{
          const part=await reader.read(); if(part.done) break;
          buffer+=decoder.decode(part.value,{{stream:true}}).replace(/\\r/g,'');
          let end;
          while ((end=buffer.indexOf('\\n\\n'))>=0) {{
            const frame=buffer.slice(0,end);buffer=buffer.slice(end+2);
            const line=frame.split('\\n').find(x=>x.startsWith('data:'));
            if(!line)continue;
            const event=JSON.parse(line.slice(5));
            if(!current(ctx)||ctx.liveController!==controller||ctx.pending?.requestId!==requestId||ctx.pending.settled) return;
            if(!Number.isInteger(event.seq)||event.seq<ctx.liveSeq)continue;
            ctx.liveSeq=event.seq;
            if(event.presentation && SUPPORTS_OPENUI) {{
              ctx.inlinePresentation={{...event.presentation,...(event.reply_hash?{{reply_hash:event.reply_hash}}:{{}})}};
            }}
            if(event.draft_failed && ctx.liveBubble) {{
              const shown=(ctx.inlinePresentation?.blocks || []).filter(b=>b.type==='text').map(b=>b.source).join("\\n\\n");
              const accepted=window.XClawOpenUIHost?.failLive?.(ctx.liveBubble,event.text);
              if(!accepted && typeof event.text==='string' && event.text.startsWith(shown)){{
                window.XClawOpenUIHost?.disposeLive?.(ctx.liveBubble);
                ctx.liveContent.hidden=false;
                ctx.liveContent.innerHTML=window.renderChatMarkdown(event.text);
              }}
              ctx.liveLabel.textContent="本次生成未完成，以下为已收到的文字";
              if(event.terminal)return;
              continue;
            }}
            if(!event.text && ctx.inlinePresentation) {{if(event.terminal)return;continue;}}
            if(!event.text) {{
              ctx.liveBubble?.remove();ctx.liveBubble=null;ctx.liveLabel=null;
              if(event.terminal)return;
              continue;
            }}
            if(!ctx.liveBubble){{
              ctx.liveBubble=document.createElement('article');ctx.liveBubble.className='message assistant streaming';
              const label=document.createElement('div');label.textContent='正在生成…';ctx.liveLabel=label;
              ctx.liveContent=document.createElement('div');ctx.liveContent.className='markdown';
              ctx.liveNote=document.createElement('div');
              ctx.liveBubble.appendChild(label);ctx.liveBubble.appendChild(ctx.liveContent);ctx.liveBubble.appendChild(ctx.liveNote);
              messagesEl.appendChild(ctx.liveBubble);
            }}
            // Use the same html:false parser as final replies; never attach OpenUI
            // or synthesize closing Markdown delimiters for partial provider text.
            if(event.presentation && window.XClawOpenUIHost?.updateLive) {{
              ctx.liveContent.hidden=true;
              const accepted=window.XClawOpenUIHost.updateLive(ctx.liveBubble,ctx.inlinePresentation,{{chatId:ctx.chatId,runId:requestId,getToken:()=>ctx.token,isCurrent:()=>current(ctx)}});
              if(!accepted)window.XClawOpenUIHost.failLive(ctx.liveBubble);
            }} else if(!ctx.inlinePresentation && window.renderChatMarkdown)ctx.liveContent.innerHTML=window.renderChatMarkdown(event.text);
            else ctx.liveContent.textContent=event.text;
            ctx.liveNote.textContent=event.truncated?'实时预览已达上限，完整答复稍后显示':'';
            recordStreamDiagnostic(ctx,"snapshot",event);
            followMessages();
            if(event.terminal) {{
              // The final snapshot may coalesce the last provider deltas. Render
              // its draft first; only the durable receipt can declare success.
              ctx.liveLabel.textContent='正在确认结果…';
              return;
            }}
          }}
        }}
        if(ctx.inlinePresentation&&ctx.liveBubble&&!ctx.inlineFinishing){{window.XClawOpenUIHost?.failLive?.(ctx.liveBubble);ctx.liveLabel.textContent="连接已中断，等待最终回执";}}
      }} catch (_) {{ if(ctx.inlinePresentation&&ctx.liveBubble){{window.XClawOpenUIHost?.failLive?.(ctx.liveBubble);ctx.liveLabel.textContent="连接已中断，等待最终回执";}} /* Receipt polling remains authoritative; never re-POST. */ }}
      finally {{if(ctx.liveController===controller)ctx.liveController=null;}}
    }}

    function applyReceipt(ctx, result) {{
      if (!ctx.pending || result.request_id !== ctx.pending.requestId) throw new Error("receipt_mismatch");
      if (["completed", "failed", "interrupted"].includes(result.status)) {{ if(!ctx.inlinePresentation)clearLiveText(ctx); loadWriteRecovery(ctx); }}
      else if (result.status === "processing") observeLiveText(ctx);
      const pending = ctx.pending;
      if (!pending || result.request_id !== pending.requestId) throw new Error("receipt_mismatch");
      ctx.syncing = false;
      if (result.status === "not_found") {{
        if (pending.httpInFlight) return true;
        pending.settled = true;
        setStatus("未找到提交回执");
        stageEl.textContent = "尚未确认接收";
        progressMessageEl.textContent = "服务器未找到本次回执。草稿已保留；手动重试相同内容会复用原提交 ID，不会另建重复请求。";
        resumeBtn.hidden = false;
      }} else if (result.status === "interrupted") {{
        pending.settled = true;
        setStatus("提交中断待确认");
        stageEl.textContent = "执行结果待确认";
        progressMessageEl.textContent = result.message;
        resumeBtn.hidden = false;
        if (!pending.warned) {{ appendMessage("system", result.message); pending.warned = true; }}
      }} else if (["processing", "completed", "failed"].includes(result.status)) {{
        const done = result.status !== "processing";
        if (result.progress && (!TERMINAL.has(result.progress.status) || done)) {{
          pending.runId = result.progress.run_id;
          applySnapshot(ctx, result.progress);
        }}
        if (done) {{
          pending.settled = true;
          pending.receiptFinished = true;
          if (!result.progress && !pending.replyRendered) {{
            const last = messagesEl.lastElementChild;
            if (!(pending.restored && last?.className === "message assistant"
                && last.textContent.trim() === result.reply?.trim())) {{
              if(!finishInline(ctx,result.reply || "",result.status,result.response_model)) appendMessage("assistant", result.reply || "请求已结束，但没有可展示的回复。", [], false, result.status === "completed", result);
            }}
            pending.replyRendered = true;

          }}
          if (!pending.finalDiagnosticRecorded) {{ recordStreamDiagnostic(ctx,"final-replaced");pending.finalDiagnosticRecorded=true; }}
          if (result.status === "completed") clearAcceptedDraft(ctx, pending);
          else saveDraft(ctx);
          if (!result.progress) {{
            setStatus(result.status === "completed" ? "响应完成" : "任务未完成");
            stageEl.textContent = result.status === "completed" ? "已处理" : "未完成";
            timeEl.textContent = "";
            progressMessageEl.textContent = result.status === "completed" ? "已按提交回执确认结果。" : "本次请求未完成，草稿已保留；不会自动重发已执行的工具。";
          }}
        }} else {{
          pending.settled = false;
          if (!result.progress || TERMINAL.has(result.progress.status)) {{
            setStatus(result.message || "请求已接收");
            stageEl.textContent = result.message || "确认执行结果";
            timeEl.textContent = Number.isFinite(result.elapsed_seconds) ? "已耗时 " + Math.floor(result.elapsed_seconds) + " 秒" : "";
            progressMessageEl.textContent = result.message || "正在等待原任务的持久化回复，不会重新执行。";
          }}
        }}
        syncPresentationStatus(ctx);
        controls(ctx);
        return !done;
      }} else throw new Error("invalid_receipt");
      saveDraft(ctx);
      controls(ctx);
      return false;
    }}

    async function pollProgress(ctx, epoch) {{
      if (!current(ctx) || epoch !== ctx.epoch) return;
      if (++ctx.attempts > MAX_POLLS) {{
        pauseObservation(ctx, "本次进度查询已达上限。任务未被取消，点击重新连接可继续查看；不会重新发送消息。");
        return;
      }}
      const controller = new AbortController();
      ctx.controller = controller;
      const timeout = setTimeout(() => controller.abort(), POLL_TIMEOUT_MS);
      let again = false;
      try {{
        const restoreReceiptHistory = Boolean(SUPPORTS_PROGRESS && !ctx.historyLoaded && ctx.pending?.requestId);
        const withHistory = !ctx.historyLoaded && (!ctx.pending || restoreReceiptHistory);
        const byReceipt = ctx.pending?.requestId && !ctx.pending.receiptFinished && !restoreReceiptHistory;
        const query = new URLSearchParams({{chat_id: ctx.chatId, include_history: String(withHistory)}});
        const path = byReceipt ? "/api/chat/receipts/" + ctx.pending.requestId : "/api/chat/progress";
        const response = await fetch(path + "?" + query, {{
          headers: headers(ctx), cache: "no-store", signal: controller.signal
        }});
        if (!response.ok && !(byReceipt && response.status === 404)) {{
          const error = new Error("progress_unavailable");
          error.status = response.status;
          throw error;
        }}
        const snapshot = await response.json();
        if (!current(ctx) || epoch !== ctx.epoch) return;
        ctx.failures = 0;
        ctx.observationPaused = false;
        resumeBtn.hidden = true;
        if (byReceipt) again = applyReceipt(ctx, snapshot);
        else if (restoreReceiptHistory) {{
          const pending = ctx.pending;
          ctx.pending = null;
          try {{ applySnapshot(ctx, snapshot, true); }} finally {{ ctx.pending = pending; }}
          controls(ctx);
          again = true;
        }} else again = applySnapshot(ctx, snapshot, withHistory);
      }} catch (error) {{
        if (!current(ctx) || epoch !== ctx.epoch) return;
        if (ctx.pending) ctx.pending.missingChecks = 0;
        const denied = [401, 403, 501].includes(error.status);
        if (denied || ++ctx.failures >= MAX_POLL_FAILURES) {{
          pauseObservation(ctx, denied ? "无法读取进度，请检查 Token 和后端能力后重新连接。" : "无法连接聊天服务，当前提交和任务状态尚未确认。请重新连接核对，不要重复发送。");
          return;
        }}
        setStatus("正在恢复进度连接");
        progressMessageEl.textContent = "仅重试进度查询，不会重新发送消息。";
        again = true;
      }} finally {{
        clearTimeout(timeout);
        if (ctx.controller === controller) ctx.controller = null;
      }}
      if (again && current(ctx) && epoch === ctx.epoch) {{
        ctx.timer = setTimeout(() => pollProgress(ctx, epoch), POLL_INTERVAL_MS);
      }}
    }}

    function observe(ctx) {{
      if (!(SUPPORTS_PROGRESS || SUPPORTS_RECEIPTS) || !current(ctx)) return;
      stopObservation(ctx);
      ctx.observationPaused = false;
      ctx.attempts = 0;
      ctx.failures = 0;
      ctx.replyAttempts = 0;
      resumeBtn.hidden = true;
      void pollProgress(ctx, ctx.epoch);
    }}

    async function loadModels(ctx, preferred) {{
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {{
        const response = await fetch("/api/chat/models", {{headers: headers(ctx), signal: controller.signal, cache: "no-store"}});
        if (!response.ok) throw new Error("models_unavailable");
        const data = await response.json();
        if (!current(ctx)) return;
        if (!Array.isArray(data.model_options) || !data.model_options.length
            || data.model_options.some(id => typeof id !== "string" || !id)
            || !data.model_options.includes(data.default_model)) throw new Error("invalid_models");
        ctx.modelOptions = [...new Set(data.model_options)];
        modelInput.replaceChildren();
        for (const id of ctx.modelOptions) {{
          const option = document.createElement("option");
          option.value = id;
          option.textContent = (id.startsWith("codex/") ? id.slice(6) : id) + (id === data.default_model ? " · 默认" : "");
          modelInput.appendChild(option);
        }}
        // Preserve a removed selection visibly, never silently substitute another model.
        if (preferred && !ctx.modelOptions.includes(preferred)) {{
          const unavailable = document.createElement("option");
          unavailable.value = preferred;
          unavailable.textContent = preferred + " · 不可用，请重新选择";
          modelInput.appendChild(unavailable);
        }}
        modelInput.value = preferred || data.default_model;
        ctx.modelsReady = true;
      }} catch (_error) {{
        if (!current(ctx)) return;
        ctx.modelsReady = false;
        modelInput.replaceChildren();
        const option = document.createElement("option");
        option.textContent = "模型列表不可用，请检查 Token 或刷新";
        option.value = "";
        modelInput.appendChild(option);
      }} finally {{ clearTimeout(timeout); if (current(ctx)) controls(ctx); }}
    }}

    async function openChat() {{
      if (active) {{
        saveDraft(active);
        clearStreamDiagnostic(active);
        stopObservation(active);
        active.cancelController?.abort();
      }}
      const chatId = chatIdInput.value.trim();
      const token = tokenInput.value.trim();
      const ctx = {{
        chatId, token, key: JSON.stringify([chatId, token]), epoch: 0, snapshot: null, pending: null,
        delivered: new Set(), finishedRuns: new Set(), historyLoaded: false, syncing: true, cancelling: false
      }};
      active = ctx;
      document.getElementById("write-recovery").hidden = true;
      loadWriteRecovery(ctx);
      controls(ctx);
      window.XClawOpenUIHost?.clear();
      direction = 0; intentUntil = 0; draggingBar = false; lastTop = 0;
      followMessages(true);
      messagesEl.replaceChildren();
      renderFiles([]);
      messageInput.value = "";
      messageInput.disabled = true;
      modeInput.value = DEFAULT_EXECUTION_MODE;
      modelInput.value = "";
      stageEl.textContent = "尚未开始任务";
      progressMessageEl.textContent = "正在查询会话，不会重发消息。";
      timeEl.textContent = SUPPORTS_PROGRESS ? "已耗时 0 秒 / 预算 10 分钟" : "";
      resumeBtn.hidden = true;
      stored(localStorage, STORAGE_KEYS.chatId, chatId);
      stored(localStorage, STORAGE_KEYS.token, token);
      // Persist text only, scoped to chat AND credentials; never put a token or image in a storage key.
      try {{
        const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(ctx.key));
        ctx.draftKey = "xclaw.draft." + Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
      }} catch (_error) {{ /* Non-secure origins still retain drafts in memory. */ }}
      if (!current(ctx)) return;
      let saved = drafts.get(ctx.key);
      if (!saved && ctx.draftKey) {{
        try {{ saved = JSON.parse(stored(sessionStorage, ctx.draftKey) || "null"); }} catch (_error) {{}}
      }}
      messageInput.value = saved?.text || "";
      messageInput.disabled = false;
      ctx.draftReady = true;
      modeInput.value = (["standard", "analysis"].includes(saved?.mode) || SUPPORTS_PREPARE && saved?.mode === "prepare") ? saved.mode : DEFAULT_EXECUTION_MODE;
      renderFiles(saved?.files || []);
      let receipt = saved?.receipt;
      if (!receipt && SUPPORTS_RECEIPTS && ctx.draftKey) {{
        try {{ receipt = JSON.parse(stored(localStorage, ctx.draftKey + ".receipt") || "null"); }} catch (_error) {{}}
      }}
      if (SUPPORTS_RECEIPTS && /^[a-f0-9]{{32}}$/.test(receipt?.requestId || "")) {{
        ctx.pending = {{...receipt, files: saved?.requestFiles || [], restored: true, settled: false}};
      }}
      await loadModels(ctx, ctx.pending?.model || saved?.model);
      if (!current(ctx)) return;
      if (!SUPPORTS_PROGRESS && SUPPORTS_RECEIPTS && chatId && (!AUTH_REQUIRED || token)) {{
        const historyController = new AbortController();
        const historyTimeout = setTimeout(() => historyController.abort(), 10000);
        try {{
          setStatus("正在恢复会话历史");
          const response = await fetch("/api/chat/history?" + new URLSearchParams({{chat_id: chatId}}),
            {{headers: token ? {{Authorization: "Bearer " + token}} : {{}}, signal: historyController.signal}});
          if (!response.ok) throw new Error("history_unavailable");
          const result = await response.json();
          if (!current(ctx)) return;
          const history = (result.messages || []).filter(item => ["user", "assistant"].includes(item.role) && typeof item.content === "string");
          ctx.historyFailed = history.at(-1)?.role === "assistant" && history.at(-1)?.status === "failed";
        history.forEach(item => {{ if (item.status === "failed") appendMessage(item.role, item.content, [], true, false); else appendMessage(item.role, item.content, [], true, true, item); }});
          ctx.historyLoaded = true;
        }} catch (_error) {{
          if (!current(ctx)) return;
          appendMessage("system", "历史消息暂时无法恢复，请重新连接；不会自动重发任何问题。");
        }} finally {{ clearTimeout(historyTimeout); }}
      }}
      if (!current(ctx)) return;
      ctx.syncing = SUPPORTS_PROGRESS || Boolean(ctx.pending);
      controls(ctx);
      if ((SUPPORTS_PROGRESS || ctx.pending) && chatId && (!AUTH_REQUIRED || token)) {{
        setStatus("正在恢复会话");
        observe(ctx);
      }} else {{
        ctx.syncing = false;
        controls(ctx);
        setStatus(AUTH_REQUIRED && !token ? "需要 Bearer Token" : ctx.historyFailed ? "上次任务未完成" : "准备就绪", Boolean(ctx.historyFailed));
        if (!messagesEl.children.length) appendMessage("system", SUPPORTS_PROGRESS ? "填好会话 ID 和 Token 后重新连接。" : "页面已就绪，可以开始聊天。");
      }}
    }}

    async function sendMessage() {{
      const ctx = active;
      if (!ctx || !current(ctx) || sendBtn.disabled) return;
      const message = messageInput.value.trim() || (selectedFiles.length ? "请描述图片内容。" : "");
      if (!message) {{ setStatus("先输入消息或选择图片"); return; }}
      if (!ctx.chatId) {{ setStatus("先填会话 ID"); return; }}
      if (AUTH_REQUIRED && !ctx.token) {{ setStatus("需要 Bearer Token"); return; }}
      ctx.historyFailed = false;
      const prior = ctx.pending;
      const pending = {{
        message, draftText: messageInput.value, files: [...selectedFiles], mode: modeInput.value, model: modelInput.value,
        baseRunId: ctx.snapshot?.run_id, baseSequence: ctx.snapshot?.sequence || 0,
        continuing: ctx.snapshot?.status === "waiting_input", runId: null, settled: false
      }};
      // Legacy receipts were fingerprinted before model selection existed.
      // Omit model only when retrying that same unresolved ID; the full original
      // payload fingerprint below still rejects altered content, images or mode.
      if (prior?.requestId && !prior.receiptFinished && !prior.model) pending.model = undefined;
      ctx.pending = pending;
      saveDraft(ctx);
      controls(ctx);
      setStatus("正在提交");
      stageEl.textContent = "提交中";
      progressMessageEl.textContent = "正在等待服务器确认本次提交。";
      timeEl.textContent = "本轮耗时待确认";
      let posted = false;
      try {{
        const attachments = await Promise.all(pending.files.map(readImage));
        if (!current(ctx) || ctx.pending !== pending) return;
        const payload = {{chat_id: ctx.chatId, message, attachments, model: pending.model}};
        if (SUPPORTS_PROGRESS || SUPPORTS_PREPARE) payload.execution_mode = pending.mode;
        if (SUPPORTS_RECEIPTS) {{
          const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(payload)));
          if (!current(ctx) || ctx.pending !== pending) return;
          pending.fingerprint = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
          if (prior?.requestId && !prior.receiptFinished && prior.fingerprint !== pending.fingerprint) {{
            ctx.pending = prior;
            setStatus("请先核对原提交");
            progressMessageEl.textContent = "仍有未确认的提交。重试须保持原文字、模型、模式和图片；如要开始新请求，请先在设置中结束本次提交核对。";
            saveDraft(ctx);
            controls(ctx);
            return;
          }}
          pending.requestId = prior?.requestId && !prior.receiptFinished && prior.fingerprint === pending.fingerprint
            ? prior.requestId : crypto.randomUUID().replaceAll("-", "");
          payload.request_id = pending.requestId;
          saveDraft(ctx);
          pending.httpInFlight = true;
        }}
        appendMessage("user", message, attachments);
        // POST is issued exactly once. A lost response is recovered only via GET.
        const request = fetch("/api/chat", {{
          method: "POST", headers: {{...headers(ctx), "Content-Type": "application/json"}}, body: JSON.stringify(payload)
        }});
        posted = true;
        observe(ctx);
        const response = await request;
        if (!current(ctx) || ctx.pending !== pending) return;
        pending.httpInFlight = false;
        if (!response.ok) {{
          if (pending.settled) return;
          pending.httpError = true;
          let rejection = "";
          try {{ const problem = await response.json(); if (["model_unavailable", "model_not_allowed"].includes(problem.detail?.code)) rejection = "所选模型不可用，未自动切换模型"; }} catch (_error) {{}}
          if (!current(ctx) || ctx.pending !== pending) return;
          if (!SUPPORTS_PROGRESS || (response.status < 500 && !pending.runId)) {{
            stopObservation(ctx);
            failSubmission(ctx, pending, response.status === 401 ? "鉴权失败，请检查 Token。草稿和图片已保留。" : (rejection ? "发送失败：" + rejection + "。草稿和图片已保留。" : "发送失败，请检查消息或图片后手动重试。草稿和图片已保留。"));
          }} else {{
            progressMessageEl.textContent = "消息处理失败，正在核对任务状态。草稿和图片已保留。";
            observe(ctx);
          }}
          return;
        }}
        const data = await response.json();
        if (!current(ctx) || ctx.pending !== pending) return;
        pending.httpDone = true;
        pending.httpReply = data.reply || "";
        if (pending.requestId) {{
          if (data.request_id !== pending.requestId || data.status !== "accepted") throw new Error("invalid_receipt_ack");
          observe(ctx);
        }} else if (data.status === "failed") {{
          stopObservation(ctx);
          pending.settled = true;
          appendMessage("assistant", pending.httpReply, [], false, false);
          saveDraft(ctx);
          setStatus("任务未完成");
          stageEl.textContent = "未完成";
          progressMessageEl.textContent = "本次请求未完成，草稿已保留；不会自动重发已执行的工具。";
          controls(ctx);
        }} else if (SUPPORTS_PROGRESS && data.delivery === "direct" && typeof data.reply === "string" && data.reply.trim()) {{
          // Local operations complete without a Run. Invalidate pre-operation polls,
          // then refresh the baseline before accepting another submission.
          stopObservation(ctx);
          pending.settled = true;
          ctx.snapshot = null;
          ctx.syncing = true;
          appendMessage("assistant", pending.httpReply);
          clearAcceptedDraft(ctx, pending);
          setStatus("响应完成");
          stageEl.textContent = "已处理";
          progressMessageEl.textContent = pending.httpReply;
          controls(ctx);
          observe(ctx);
        }} else if (!SUPPORTS_PROGRESS) {{
          pending.settled = true;
          appendMessage("assistant", pending.httpReply || "(空回复)");
          clearAcceptedDraft(ctx, pending);
          setStatus("响应完成");
          controls(ctx);
        }} else if (pending.runId && ctx.snapshot?.run_id === pending.runId) {{
          applySnapshot(ctx, ctx.snapshot);
        }}
      }} catch (_error) {{
        pending.httpInFlight = false;
        if (!current(ctx) || ctx.pending !== pending || pending.settled) return;
        if (posted && (SUPPORTS_PROGRESS || SUPPORTS_RECEIPTS)) {{
          pending.transportError = true;
          setStatus("发送结果待确认");
          progressMessageEl.textContent = "连接中断，提交结果尚未确认。仅查询原任务，不会自动重发；草稿和图片已保留。";
          // The existing observer remains bounded, even after the message connection fails.
          resumeBtn.hidden = false;
        }} else {{
          failSubmission(ctx, pending, posted ? "连接失败，草稿和图片已保留，请手动重试。" : "图片读取失败，草稿和图片已保留，请手动重试。");
        }}
      }}
    }}

    async function cancelRun() {{
      const ctx = active;
      if (!ctx || !current(ctx) || cancelBtn.disabled) return;
      const runId = ctx.snapshot.run_id;
      ctx.cancelling = true;
      stopObservation(ctx);
      controls(ctx);
      const controller = new AbortController();
      ctx.cancelController = controller;
      const timeout = setTimeout(() => controller.abort(), POLL_TIMEOUT_MS);
      progressMessageEl.textContent = "正在请求取消，等待后端确认。";
      try {{
        const response = await fetch("/api/chat/cancel", {{
          method: "POST", headers: {{...headers(ctx), "Content-Type": "application/json"}},
          body: JSON.stringify({{chat_id: ctx.chatId, run_id: runId}}), signal: controller.signal
        }});
        if (!response.ok) throw new Error("cancel_unconfirmed");
        const snapshot = await response.json();
        if (!current(ctx) || ctx.snapshot?.run_id !== runId) return;
        if (snapshot.run_id === runId) applySnapshot(ctx, snapshot);
        if (!TERMINAL.has(ctx.snapshot.status)) observe(ctx);
        else stopObservation(ctx);
      }} catch (_error) {{
        if (!current(ctx)) return;
        progressMessageEl.textContent = "取消未确认。请重新连接核对任务状态，也可以手动重试取消。";
        observe(ctx);
      }} finally {{
        clearTimeout(timeout);
        ctx.cancelling = false;
        controls(ctx);
      }}
    }}

    imageInput.addEventListener("change", () => {{
      const files = Array.from(imageInput.files);
      if (files.length > 4 || files.some(file => file.size > 5 * 1024 * 1024 || !["image/png", "image/jpeg", "image/webp"].includes(file.type))) {{
        imageInput.value = "";
        setStatus("请选择最多 4 张 PNG / JPEG / WebP 图片，每张不超过 5 MB；原有图片已保留");
        return;
      }}
      renderFiles(files);
      saveDraft();
    }});
    clearImagesBtn.addEventListener("click", () => {{ renderFiles([]); saveDraft(); }});
    messageInput.addEventListener("input", () => saveDraft());
    modelInput.addEventListener("change", () => {{ saveDraft(); if (active) controls(active); }});
    modeInput.addEventListener("change", () => {{
      saveDraft();
      timeEl.textContent = "已耗时 0 秒 / 预算 " + (["standard","prepare"].includes(modeInput.value) ? 3 : 10) + " 分钟";
    }});
    sendBtn.addEventListener("click", sendMessage);
    cancelBtn.addEventListener("click", cancelRun);
    resumeBtn.addEventListener("click", () => observe(active));
    document.getElementById("clear-btn").addEventListener("click", () => {{
      window.XClawOpenUIHost?.clear();
      messagesEl.replaceChildren();
      appendMessage("system", "消息区已清空，不会取消任务或删除服务端历史。");
    }});
    document.getElementById("release-receipt-btn").addEventListener("click", () => {{
      if (!active?.pending?.settled || !window.confirm("这不会取消已经执行的操作。确认已核对原提交，允许下一次发送使用新的提交 ID？")) return;
      active.pending = null;
      saveDraft(active);
      controls(active);
      setStatus("已结束提交核对");
    }});
    chatIdInput.addEventListener("change", openChat);
    tokenInput.addEventListener("change", openChat);
    messageInput.addEventListener("keydown", event => {{
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {{
        event.preventDefault();
        void sendMessage();
      }}
    }});
    window.addEventListener("online", () => {{ if (SUPPORTS_PROGRESS && active) observe(active); }});
    window.addEventListener("pagehide", () => {{ saveDraft(); if (active) {{clearStreamDiagnostic(active);stopObservation(active);}} }});
    window.addEventListener("keydown", event => {{
      if (event.key === "Escape") document.getElementById("chat-settings").open = false;
    }});
    window.addEventListener("click", event => {{
      const settings = document.getElementById("chat-settings");
      if (settings.open && !settings.contains(event.target)) settings.open = false;
    }});
    chatIdInput.value = stored(localStorage, STORAGE_KEYS.chatId) || "web-" + Math.random().toString(36).slice(2, 10);
    tokenInput.value = stored(localStorage, STORAGE_KEYS.token) || "";
    void openChat();
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
    textarea, input { width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid #decfb7; margin-top: 8px; font: inherit; }
    button { border: 0; border-radius: 999px; background: #bf5a36; color: #fff; padding: 10px 16px; cursor: pointer; margin-top: 8px; margin-right: 8px; }
    button.secondary { background: #6e5a42; }
    .status { margin-top: 10px; color: #6e5a42; white-space: pre-wrap; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; }
    .link { display: inline-block; margin-top: 8px; }
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
      <div class="card">
        <strong>ChatGPT / Codex 登录</strong>
        <p><code>/api/llm/oauth/openai-codex/start</code></p>
        <div class="actions">
          <button id="oauth-start">开始登录</button>
          <button id="oauth-refresh" class="secondary">刷新状态</button>
          <button id="oauth-logout" class="secondary">退出登录</button>
        </div>
        <a id="oauth-link" class="link" href="#" target="_blank" rel="noreferrer" hidden>打开授权页</a>
        <label for="oauth-manual">手动粘贴 redirect URL 或授权码</label>
        <textarea id="oauth-manual" rows="4" placeholder="http://localhost:1455/auth/callback?code=...&state=..."></textarea>
        <button id="oauth-complete">提交回调</button>
        <div id="oauth-status" class="status">正在加载 OAuth 状态…</div>
      </div>
    </div>
  </div>
  <script>
    let currentLoginId = "";
    const statusEl = document.getElementById("oauth-status");
    const linkEl = document.getElementById("oauth-link");
    const inputEl = document.getElementById("oauth-manual");

    function setStatus(text) {
      statusEl.textContent = text;
    }

    function renderSession(payload) {
      if (!payload || !payload.provider) {
        setStatus("当前 provider 未启用 OAuth。");
        return;
      }
      if (!payload.authenticated) {
        setStatus(`Provider: ${payload.provider}\\n状态: 未登录`);
        return;
      }
      setStatus(
        [
          `Provider: ${payload.provider}`,
          `状态: 已登录`,
          `账号: ${payload.email || "-"}`,
          `显示名: ${payload.display_name || "-"}`,
          `到期时间: ${payload.expires_at || "-"}`,
        ].join("\\n")
      );
    }

    async function loadSession() {
      const response = await fetch("/api/llm/provider/session");
      const data = await response.json();
      renderSession(data);
    }

    async function startLogin() {
      const response = await fetch("/api/llm/oauth/openai-codex/start", { method: "POST" });
      const data = await response.json();
      currentLoginId = data.login_id || "";
      if (data.authorize_url) {
        linkEl.href = data.authorize_url;
        linkEl.hidden = false;
      }
      setStatus(
        [
          `登录会话: ${data.login_id || "-"}`,
          `模式: ${data.mode || "-"}`,
          `到期时间: ${data.expires_at || "-"}`,
          "请打开授权页完成登录；如果没有自动完成，请粘贴回调 URL。"
        ].join("\\n")
      );
    }

    async function refreshLogin() {
      if (currentLoginId) {
        const response = await fetch(`/api/llm/oauth/openai-codex/status/${currentLoginId}`);
        if (response.ok) {
          const data = await response.json();
          if (data.status === "completed") {
            await loadSession();
            return;
          }
          setStatus(
            [
              `登录会话: ${data.login_id || "-"}`,
              `状态: ${data.status || "-"}`,
              `模式: ${data.mode || "-"}`,
              `错误: ${data.error || "-"}`,
            ].join("\\n")
          );
          return;
        }
      }
      await loadSession();
    }

    async function completeLogin() {
      if (!currentLoginId) {
        setStatus("请先点击“开始登录”。");
        return;
      }
      const response = await fetch("/api/llm/oauth/openai-codex/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          login_id: currentLoginId,
          redirect_url_or_code: inputEl.value.trim(),
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setStatus(data.detail || "登录失败");
        return;
      }
      renderSession(data);
    }

    async function logout() {
      const response = await fetch("/api/llm/oauth/openai-codex/logout", { method: "POST" });
      if (!response.ok) {
        setStatus("退出登录失败。");
        return;
      }
      currentLoginId = "";
      linkEl.hidden = true;
      inputEl.value = "";
      await loadSession();
    }

    document.getElementById("oauth-start").addEventListener("click", startLogin);
    document.getElementById("oauth-refresh").addEventListener("click", refreshLogin);
    document.getElementById("oauth-complete").addEventListener("click", completeLogin);
    document.getElementById("oauth-logout").addEventListener("click", logout);
    loadSession();
  </script>
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
    message_handler: Callable[..., Coroutine[Any, Any, str]],
    stream_handler: Callable[..., AsyncIterator[str]] | None = None,
    auth_token: str = "",
    rate_limit: int = 20,
    db: Any = None,
    settings: Any = None,
    multi_user_mode: bool = False,
    tool_registry: Any = None,
    oauth_manager: Any = None,
    supports_images: bool = False,
    progress_handler: Callable[..., Coroutine[Any, Any, dict]] | None = None,
    cancel_handler: Callable[..., Coroutine[Any, Any, dict]] | None = None,
    startup_handler: Callable[[], Coroutine[Any, Any, None]] | None = None,
    readiness_handler: Callable[[], Coroutine[Any, Any, None]] | None = None,
    openui_service: Any = None,
    receipt_progress_handler: Callable[..., Coroutine[Any, Any, dict | None]] | None = None,
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
        progress_handler: async (effective_chat_id, include_history=False) -> public snapshot
        cancel_handler:  async (effective_chat_id, run_id) -> public snapshot
    """
    @asynccontextmanager
    async def lifespan(app):
        if startup_handler is not None:
            await startup_handler()
        try:
            yield
        finally:
            tasks = list(chat_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="XClaw", version="0.1.0", docs_url="/docs", lifespan=lifespan)
    app.add_middleware(ChatBodyLimitMiddleware)
    # Keep detached work alive even when the request task/browser goes away.
    chat_tasks: set[asyncio.Task] = set()
    app.state.chat_tasks = chat_tasks
    receipts = ReceiptStore(db) if isinstance(getattr(db, "_path", None), Path) else None
    app.state.receipts = receipts

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        if request.url.path not in {"/api/chat", "/api/chat/stream", "/api/chat/progress", "/api/chat/cancel"}:
            return await request_validation_exception_handler(request, error)
        # Validation errors must not echo an uploaded private image as base64.
        return JSONResponse(status_code=422, content={"detail": [
            {key: value for key, value in item.items() if key in {"loc", "msg", "type"}}
            for item in error.errors()
        ]})

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

    supports_prepare = bool(openui_service is not None and getattr(settings, "agent_backend", None) == "native" and getattr(settings, "openui_inline_stream", False))

    if openui_service is not None:
        from xclaw.openui import create_openui_router
        app.include_router(create_openui_router(openui_service, verify_token, _effective_chat_id))

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/chat-assets/{name}", include_in_schema=False)
    async def chat_asset(name: str) -> FileResponse:
        if name not in {"chat.css", "markdown-it.min.js", "markdown.js", "openui.js", "openui.css", "openui-host.js"}:
            raise HTTPException(status_code=404)
        return FileResponse(
            Path(__file__).with_name("static") / name,
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/", response_class=HTMLResponse)
    async def chat_page() -> HTMLResponse:
        model_name = getattr(settings, "model", "unknown") if settings else "unknown"
        provider_name = getattr(settings, "llm_provider", "unknown") if settings else "unknown"
        return HTMLResponse(
            _build_chat_page_html(
                auth_enabled=bool(auth_token),
                model_name=model_name,
                provider_name=provider_name,
                supports_images=supports_images,
                supports_progress=progress_handler is not None,
                supports_receipts=receipts is not None,
                supports_openui=openui_service is not None,
                supports_prepare=supports_prepare,
            )
        )

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page() -> HTMLResponse:
        return HTMLResponse(_build_admin_page_html())

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "xclaw"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        try:
            if db is not None:
                await db.conn.execute("SELECT 1")
            if readiness_handler:
                async with asyncio.timeout(3):
                    await readiness_handler()
            return JSONResponse({"status": "ready"})
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    from xclaw.model_routing import configured_models, resolve_request_model

    @app.get("/api/chat/models", dependencies=[Depends(verify_token)])
    async def chat_models():
        models = configured_models(settings)
        return JSONResponse({"model_options": models, "default_model": models[0] if models else None},
                            headers={"Cache-Control": "no-store"})

    def selected_model(req):
        if req.model is None and not configured_models(settings):
            return None
        try:
            return resolve_request_model(settings, req.model)
        except ValueError:
            raise HTTPException(400, detail={"code": "model_not_allowed"}) from None

    async def invoke_message(effective_id, message, options):
        baseline = None
        if isinstance(getattr(db, "_path", None), Path):
            async with db.conn.execute("SELECT COALESCE(MAX(m.id),0) FROM messages m JOIN chats c ON c.id=m.chat_id WHERE c.channel='web' AND c.external_chat_id=?", (effective_id,)) as cursor:
                baseline = (await cursor.fetchone())[0]
        reply = await message_handler(effective_id, message, **options)
        if isinstance(reply, RejectedReply) and baseline is not None:
            # Persist trusted outcome against the actual message row, not failure-word heuristics.
            # Dedicated connection does not commit unrelated application transactions.
            import aiosqlite
            try:
                async with aiosqlite.connect(str(db._path)) as conn:
                    await conn.execute("CREATE TABLE IF NOT EXISTS web_message_outcomes (message_id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
                    message_id = getattr(reply, "message_id", None)
                    internal_chat_id = getattr(reply, "chat_id", None)
                    candidates = []
                    if message_id is not None or internal_chat_id is not None:
                        # Server-issued identity is authoritative. Invalid metadata MUST NOT
                        # fall back to matching an unrelated row with the same failure text.
                        if type(message_id) is int and type(internal_chat_id) is int:
                            async with conn.execute(
                                "SELECT m.id FROM messages m JOIN chats c ON c.id=m.chat_id "
                                "WHERE m.id=? AND m.chat_id=? AND c.channel='web' "
                                "AND c.external_chat_id=? AND m.role='assistant' AND m.content=?",
                                (message_id, internal_chat_id, effective_id, str(reply)),
                            ) as cursor:
                                candidates = await cursor.fetchall()
                    else:
                        # Compatibility for external handlers without message metadata only.
                        # Unique post-call matching is conservative, not concurrent-safe identity.
                        async with conn.execute(
                            "SELECT m.id FROM messages m JOIN chats c ON c.id=m.chat_id "
                            "WHERE c.channel='web' AND c.external_chat_id=? AND m.role='assistant' AND m.content=? AND m.id>?",
                            (effective_id, str(reply), baseline),
                        ) as cursor:
                            candidates = await cursor.fetchall()
                    if len(candidates) == 1:
                        await conn.execute("INSERT OR REPLACE INTO web_message_outcomes VALUES(?,'failed')", (candidates[0][0],))
                    await conn.commit()
            except Exception:
                logger.warning("Failed reply history metadata could not be persisted")
        return reply

    @app.post("/api/chat", response_model=ChatResponse, response_model_exclude_none=True, dependencies=[Depends(verify_token)])
    async def chat(req: ChatRequest, authorization: str = Header(default="")) -> ChatResponse:
        """Non-streaming chat endpoint."""
        if req.execution_mode == "prepare" and not supports_prepare:
            raise HTTPException(status_code=400, detail="当前实例不支持预览操作模式")
        effective_id = _effective_chat_id(req.chat_id, authorization)
        if req.attachments and not supports_images:
            raise HTTPException(status_code=400, detail="当前执行后端未启用图片输入")
        options = {"attachments": req.attachments} if req.attachments else {}
        chosen = selected_model(req)
        if req.model is not None or getattr(settings, "web_model_options", None):
            options["model"] = chosen
        if req.execution_mode is not None:
            options["execution_mode"] = req.execution_mode

        if req.request_id is not None:
            if receipts is None:
                raise HTTPException(status_code=501, detail="当前服务未启用持久化回执")
            scope = receipts.scope(effective_id, _extract_token(authorization))
            try:
                receipt, created = await receipts.admit(
                    scope, req.request_id, {**req.model_dump(exclude={"request_id", "model"}),
                                            **({"model": chosen} if chosen else {})},
                    allow_legacy=req.model is None,
                )
            except ReceiptConflict:
                raise HTTPException(status_code=409, detail="同一提交 ID 不能用于不同内容") from None
            if created:
                task = asyncio.create_task(receipts.execute(
                    receipt, lambda: invoke_message(effective_id, req.message, options),
                ))
                chat_tasks.add(task)
                task.add_done_callback(chat_tasks.discard)
            return JSONResponse(
                {"request_id": req.request_id, "status": "accepted"},
                status_code=202, headers={"Cache-Control": "no-store"},
            )

        async def execute_message() -> ChatResponse:
            try:
                reply = await invoke_message(effective_id, req.message, options)
                return ChatResponse(chat_id=effective_id, reply=reply,
                                    delivery="direct" if isinstance(reply, DirectReply) else None,
                                    status="failed" if isinstance(reply, RejectedReply) else None)
            except Exception:
                logger.error("Chat endpoint failed; private exception details withheld")
                raise HTTPException(status_code=500, detail="消息处理失败，请查看任务状态后再重试") from None

        def release_task(task: asyncio.Task) -> None:
            chat_tasks.discard(task)
            if not task.cancelled():
                task.exception()  # Retrieve failures even if the request no longer awaits them.

        task = asyncio.create_task(execute_message())
        chat_tasks.add(task)
        task.add_done_callback(release_task)
        return await asyncio.shield(task)

    @app.get("/api/chat/receipts/{request_id}/events", dependencies=[Depends(verify_token)])
    async def receipt_events(request_id: str, chat_id: str = "web_default",
                             authorization: str = Header(default="")):
        import re
        from sse_starlette.sse import EventSourceResponse
        if receipts is None: raise HTTPException(501, "Text stream unavailable")
        if not re.fullmatch(REQUEST_ID_PATTERN, request_id): raise HTTPException(400, "Invalid request ID")
        effective_id = _effective_chat_id(chat_id, authorization)
        receipt = await receipts.lookup(receipts.scope(effective_id, _extract_token(authorization)), request_id)
        if receipt is None: raise HTTPException(404, "Receipt not found")
        async def observe():
            # Disconnect ends this observer only, never the receipt's background task.
            previous = -1
            for _ in range(1200):
                snapshot = receipts.text_streams.snapshot(receipt['id'])
                if snapshot['seq'] != previous:
                    yield {'event':'snapshot','id':str(snapshot['seq']),'data':json.dumps(snapshot,ensure_ascii=False)}
                    previous=snapshot['seq']
                if snapshot['terminal'] or receipt['id'] not in receipts.active: return
                await asyncio.sleep(.1)
        return EventSourceResponse(observe(), headers={"Cache-Control":"no-store"})

    @app.get("/api/chat/receipts/{request_id}", dependencies=[Depends(verify_token)])
    async def chat_receipt(request_id: str, chat_id: str = "web_default",
                           authorization: str = Header(default="")) -> JSONResponse:
        import re

        if receipts is None:
            raise HTTPException(status_code=501, detail="当前服务未启用持久化回执")
        if not re.fullmatch(REQUEST_ID_PATTERN, request_id):
            raise HTTPException(status_code=400, detail="无效提交 ID")
        effective_id = _effective_chat_id(chat_id, authorization)
        receipt = await receipts.lookup(
            receipts.scope(effective_id, _extract_token(authorization)), request_id,
        )
        if receipt is None:
            return JSONResponse({"request_id": request_id, "status": "not_found"},
                                status_code=404, headers={"Cache-Control": "no-store"})
        try:
            result = await receipts.project(
                receipt, (lambda: progress_handler(effective_id, include_history=False))
                if progress_handler else None,
            )
            if result.get("status") == "processing" and receipt_progress_handler:
                native_status = await receipt_progress_handler(receipt["id"])
                if native_status:
                    result["message"] = str(native_status["message"])[:200]
                    result["elapsed_seconds"] = max(0, float(native_status["elapsed_seconds"]))
            if result.get("progress"):
                result["progress"] = _public_chat_progress(result["progress"])
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except Exception:
            logger.error("Chat receipt query failed; private exception details withheld")
            raise HTTPException(status_code=503, detail="回执暂时无法查询，请重新连接，不要换 ID 重发") from None

    async def journal_chat(chat_id, authorization):
        if db is None:
            raise HTTPException(status_code=501, detail="当前服务未启用写操作核对")
        if not chat_id or len(chat_id) > 128:
            raise HTTPException(status_code=422, detail="无效会话 ID")
        async with db.conn.execute(
            "SELECT id FROM chats WHERE channel='web' AND external_chat_id=?",
            (_effective_chat_id(chat_id, authorization),),
        ) as cursor:
            row = await cursor.fetchone()
        return row["id"] if row else None

    @app.get("/api/chat/write-recovery", dependencies=[Depends(verify_token)])
    async def write_recovery(chat_id: str = "web_default", authorization: str = Header(default="")):
        from xclaw.tool_execution_journal import inspect_journal
        cid = await journal_chat(chat_id, authorization)
        rows = await inspect_journal(db, cid) if cid is not None else []
        public = [{key: row[key] for key in ("id", "tool_name", "arguments_json", "state", "created_at", "updated_at")}
                  for row in rows if row["state"] in {"started", "unknown"}]
        return JSONResponse({"records": public}, headers={"Cache-Control": "no-store"})

    @app.post("/api/chat/write-recovery/acknowledge", dependencies=[Depends(verify_token)])
    async def acknowledge_write(req: JournalAcknowledgeRequest, authorization: str = Header(default="")):
        from xclaw.tool_execution_journal import acknowledge_unknown
        cid = await journal_chat(req.chat_id, authorization)
        if cid is None:
            raise HTTPException(status_code=404, detail="未找到该会话待核对记录")
        try:
            await acknowledge_unknown(db, cid, req.execution_id,
                confirmed_reviewed=req.confirmed_reviewed, execution_stopped=req.execution_stopped, note=req.note)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return JSONResponse({"status": "acknowledged", "message": "已记录人工核对并解除本条写入屏障；未重试，也不表示原操作成功。"},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/chat/history", dependencies=[Depends(verify_token)])
    async def chat_history(chat_id: str = "web_default", authorization: str = Header(default="")) -> JSONResponse:
        """Restore public chat messages using the same external/auth scope as sending."""
        if db is None:
            raise HTTPException(status_code=501, detail="当前服务未启用会话历史")
        if not chat_id or len(chat_id) > 128:
            raise HTTPException(status_code=422, detail="无效会话 ID")
        effective_id = _effective_chat_id(chat_id, authorization)
        async with db.conn.execute(
            "SELECT id FROM chats WHERE channel=? AND external_chat_id=?", ("web", effective_id),
        ) as cursor:
            row = await cursor.fetchone()
        public = []
        if row:
            async with db.conn.execute("SELECT id,role,content,response_model FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 100", (row["id"],)) as cursor:
                messages = list(reversed(await cursor.fetchall()))
            # Older databases may not yet have any structured failed outcome.
            async with db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='web_message_outcomes'") as cursor:
                has_outcomes = await cursor.fetchone()
            failed_ids = set()
            if has_outcomes:
                async with db.conn.execute("SELECT o.message_id FROM web_message_outcomes o JOIN messages m ON m.id=o.message_id WHERE m.chat_id=? AND o.status='failed'", (row["id"],)) as cursor:
                    failed_ids = {item[0] for item in await cursor.fetchall()}
            from xclaw.ui_presentations_v2 import presentation_metadata_for_messages
            async with db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ui_presentations_v2'") as cursor:
                has_v2 = await cursor.fetchone()
            metadata = await presentation_metadata_for_messages(db, row["id"], [item["id"] for item in messages]) if has_v2 else {}
            for item in messages:
                if item["role"] in {"user", "assistant"} and isinstance(item["content"], str):
                    entry = {"role": item["role"], "content": item["content"], **metadata.get(item["id"], {})}
                    if item["response_model"]:
                        entry["model"] = item["response_model"]
                    if item["id"] in failed_ids:
                        entry["status"] = "failed"
                    public.append(entry)
        return JSONResponse({"messages": public}, headers={"Cache-Control": "no-store"})

    @app.get("/api/chat/progress", dependencies=[Depends(verify_token)])
    async def chat_progress(
        chat_id: str = "web_default", include_history: bool = False,
        authorization: str = Header(default=""),
    ) -> JSONResponse:
        if progress_handler is None:
            raise HTTPException(status_code=501, detail="当前执行后端不支持任务进度")
        try:
            snapshot = await progress_handler(
                _effective_chat_id(chat_id, authorization), include_history=include_history,
            )
            public = _public_chat_progress(snapshot, include_history=include_history)
            return JSONResponse(public, headers={"Cache-Control": "no-store"})
        except Exception:
            logger.error("Chat progress failed; private exception details withheld")
            raise HTTPException(status_code=500, detail="任务进度暂时不可用，请重新连接，不要重复发送") from None

    @app.post("/api/chat/cancel", dependencies=[Depends(verify_token)])
    async def chat_cancel(req: ChatCancelRequest, authorization: str = Header(default="")) -> JSONResponse:
        if cancel_handler is None:
            raise HTTPException(status_code=501, detail="当前执行后端不支持取消任务")
        try:
            snapshot = await cancel_handler(_effective_chat_id(req.chat_id, authorization), req.run_id)
            return JSONResponse(_public_chat_progress(snapshot), headers={"Cache-Control": "no-store"})
        except Exception:
            logger.error("Chat cancellation failed; private exception details withheld")
            raise HTTPException(status_code=500, detail="取消未确认，请查看任务状态后再重试") from None

    @app.post("/api/chat/stream", dependencies=[Depends(verify_token)])
    async def chat_stream(
        req: ChatRequest, authorization: str = Header(default="")
    ) -> EventSourceResponse:
        """SSE streaming chat endpoint."""
        if req.request_id is not None:
            raise HTTPException(status_code=400, detail="带提交 ID 的请求请使用 /api/chat 和回执查询接口")
        if req.execution_mode == "prepare":
            raise HTTPException(status_code=400, detail="预览操作请使用 /api/chat 和回执查询接口")
        if stream_handler is None:
            raise HTTPException(status_code=501, detail="Streaming not enabled")
        effective_id = _effective_chat_id(req.chat_id, authorization)
        if req.attachments and not supports_images:
            raise HTTPException(status_code=400, detail="当前执行后端未启用图片输入")

        chosen = selected_model(req)
        async def event_generator():
            try:
                options = {"attachments": req.attachments} if req.attachments else {}
                if req.model is not None or getattr(settings, "web_model_options", None):
                    options["model"] = chosen
                if req.execution_mode is not None:
                    options["execution_mode"] = req.execution_mode
                async for chunk in stream_handler(effective_id, req.message, **options):
                    if isinstance(chunk, RejectedReply):
                        yield {"event": "error", "data": json.dumps({"status": "failed", "reply": str(chunk)}, ensure_ascii=False)}
                        return
                    yield {"data": chunk}
                yield {"data": "[DONE]"}
            except Exception:
                logger.error("Chat stream failed; private exception details withheld")
                yield {"data": "[ERROR] 消息处理失败，请稍后重试"}

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
    _oauth_manager: Any = oauth_manager

    def set_dingtalk_adapter(adapter: Any) -> None:
        nonlocal _dingtalk_adapter
        _dingtalk_adapter = adapter

    app.state.set_dingtalk_adapter = set_dingtalk_adapter

    def set_llm_oauth_manager(manager: Any) -> None:
        nonlocal _oauth_manager
        _oauth_manager = manager

    app.state.set_llm_oauth_manager = set_llm_oauth_manager

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
        strategy_bias_threshold = getattr(settings, "strategy_bias_threshold", 5.0)
        if not isinstance(strategy_bias_threshold, (int, float)):
            strategy_bias_threshold = 5.0
        strategy_report_max_symbols = getattr(settings, "strategy_report_max_symbols", 10)
        if not isinstance(strategy_report_max_symbols, int):
            strategy_report_max_symbols = 10
        safe = {
            "supports_images": supports_images,
            "supports_progress": progress_handler is not None,
            "agent_backend": "powercore" if getattr(settings, "agent_backend", "native") == "powercore" else "native",
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

    # ── Provider OAuth APIs ─────────────────────────────────────────────────

    @app.post("/api/llm/oauth/openai-codex/start", dependencies=[Depends(verify_token)])
    async def start_openai_codex_oauth() -> JSONResponse:
        if _oauth_manager is None:
            raise HTTPException(status_code=503, detail="OpenAI Codex OAuth is not configured")
        try:
            payload = await _oauth_manager.start_login()
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return JSONResponse(payload)

    @app.get("/api/llm/oauth/openai-codex/status/{login_id}", dependencies=[Depends(verify_token)])
    async def openai_codex_oauth_status(login_id: str) -> JSONResponse:
        if _oauth_manager is None:
            raise HTTPException(status_code=503, detail="OpenAI Codex OAuth is not configured")
        try:
            payload = await _oauth_manager.get_login_status(login_id)
        except OAuthLoginNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(payload)

    @app.post("/api/llm/oauth/openai-codex/complete", dependencies=[Depends(verify_token)])
    async def complete_openai_codex_oauth(payload: OAuthCompleteRequest) -> JSONResponse:
        if _oauth_manager is None:
            raise HTTPException(status_code=503, detail="OpenAI Codex OAuth is not configured")
        try:
            result = await _oauth_manager.complete_login(payload.login_id, payload.redirect_url_or_code)
        except OAuthLoginNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.get("/api/llm/provider/session", dependencies=[Depends(verify_token)])
    async def get_llm_provider_session() -> JSONResponse:
        if _oauth_manager is None:
            provider_name = getattr(settings, "llm_provider", "unknown") if settings else "unknown"
            return JSONResponse(
                {
                    "provider": provider_name,
                    "authenticated": False,
                    "email": None,
                    "display_name": None,
                    "account_id": None,
                    "expires_at": None,
                }
            )
        payload = await _oauth_manager.get_session_payload()
        return JSONResponse(payload)

    @app.post("/api/llm/oauth/openai-codex/logout", dependencies=[Depends(verify_token)])
    async def logout_openai_codex_oauth() -> JSONResponse:
        if _oauth_manager is None:
            raise HTTPException(status_code=503, detail="OpenAI Codex OAuth is not configured")
        await _oauth_manager.logout()
        return JSONResponse({"ok": True})

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
