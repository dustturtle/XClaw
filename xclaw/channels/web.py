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
from xclaw.responses import DirectReply
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

class ChatRequest(BaseModel):
    request_id: str | None = Field(default=None, pattern=REQUEST_ID_PATTERN)
    chat_id: str = "web_default"
    message: str = ""
    attachments: list[ImageAttachment] = Field(default_factory=list, max_length=MAX_IMAGES)
    execution_mode: Literal["standard", "analysis"] | None = None

    @model_validator(mode="after")
    def require_content(self):
        if not self.message.strip() and not self.attachments:
            raise ValueError("请输入消息或选择图片")
        return self


class ChatResponse(BaseModel):
    chat_id: str
    reply: str
    delivery: Literal["direct"] | None = None


_RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"


class ChatCancelRequest(BaseModel):
    chat_id: str = "web_default"
    run_id: str = Field(min_length=1, max_length=128, pattern=_RUN_ID_PATTERN)


class ChatProgressMessage(BaseModel):
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
  <link rel="stylesheet" href="/chat-assets/chat.css?v=2" />
  <script src="/chat-assets/markdown-it.min.js"></script>
  <script src="/chat-assets/markdown.js?v=2"></script>
</head>
<body>
  <div class="shell">
    <main class="main">
      <header class="toolbar">
        <div class="toolbar-title">
          <strong>XClaw <span class="brand-caption">对话</span></strong>
          <span class="model-name">{safe_model}</span>
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
              <div class="field" {('' if supports_progress else 'hidden')}>
                <label for="execution-mode">执行模式</label>
                <select id="execution-mode">
                  <option value="standard">普通 · 3 分钟</option>
                  <option value="analysis" selected>分析 · 10 分钟</option>
                </select>
              </div>
              <p class="settings-note" id="composer-note">{("当前服务启用了鉴权，请先填写 Token。" if auth_enabled else "当前服务未启用鉴权，可以直接开始聊天。")}</p>
              <div class="settings-meta">{safe_provider} / {safe_model}</div>
              <button class="ghost" id="clear-btn" type="button">清空显示</button>
              <button class="ghost" id="release-receipt-btn" type="button" hidden>结束本次提交核对</button>
              <p class="settings-note">只清空页面，不会取消任务或重置上下文。重置上下文请发送 /reset。</p>
              <a href="/docs" target="_blank" rel="noopener noreferrer">API 文档</a>
            </div>
          </details>
        </div>
      </header>
      <section class="messages" id="messages" aria-label="对话消息" aria-live="polite"></section>
      <section class="composer">
        <div class="composer-inner">
          <div class="run-controls" id="run-controls" {('' if supports_progress else 'hidden')}>
            <details class="run-progress">
              <summary><strong id="run-stage">尚未开始任务</strong><span id="run-time">已耗时 0 秒 / 预算 10 分钟</span></summary>
              <div class="progress-detail" id="run-message" role="status" aria-live="polite">显示实际执行进度，不是逐字输出。</div>
            </details>
            <button class="ghost compact" id="cancel-run-btn" type="button" disabled>取消任务</button>
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
    const chatIdInput = document.getElementById("chat-id");
    const tokenInput = document.getElementById("auth-token");
    const messageInput = document.getElementById("message-input");
    const sendBtn = document.getElementById("send-btn");
    const statusPill = document.getElementById("status-pill");
    const imageInput = document.getElementById("image-input");
    const clearImagesBtn = document.getElementById("clear-images-btn");
    const previewEl = document.getElementById("attachment-preview");
    const modeInput = document.getElementById("execution-mode");
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

    function setStatus(text) {{ statusPill.textContent = text; }}

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
        requestId: p.requestId, fingerprint: p.fingerprint, mode: p.mode, draftText: p.draftText
      }} : null;
      const draft = {{text: messageInput.value, files: [...selectedFiles], mode: modeInput.value,
        receipt, requestFiles: p?.files || []}};
      drafts.set(ctx.key, draft);
      if (ctx.draftKey) {{
        stored(sessionStorage, ctx.draftKey, JSON.stringify({{text: draft.text, mode: draft.mode, receipt}}));
        if (SUPPORTS_RECEIPTS) stored(localStorage, ctx.draftKey + ".receipt",
          JSON.stringify(receipt ? {{requestId: receipt.requestId, fingerprint: receipt.fingerprint, mode: receipt.mode}} : null));
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

    function appendMessage(role, text, attachments = []) {{
      const followTail = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 100;
      const item = document.createElement("article");
      item.className = "message " + role;
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
      if (role === "user") messagesEl.scrollTop = messagesEl.scrollHeight;
      else if (followTail) messagesEl.scrollTop = Math.max(0, item.offsetTop - messagesEl.offsetTop - 24);
    }}

    function controls(ctx) {{
      if (!current(ctx)) return;
      const status = ctx.snapshot?.status;
      const busy = ctx.syncing || ctx.cancelling || (ctx.pending && !ctx.pending.settled) || ["queued", "running"].includes(status)
        || (ACTIVE.has(status) && ["reconnecting", "cancelling"].includes(ctx.snapshot?.stage));
      sendBtn.disabled = Boolean(busy);
      sendBtn.textContent = busy ? (ctx.observationPaused ? "待确认" : "处理中") : status === "waiting_input" ? "继续" : "发送";
      imageInput.disabled = Boolean(busy);
      clearImagesBtn.disabled = Boolean(busy);
      modeInput.disabled = Boolean(busy || status === "waiting_input");
      cancelBtn.disabled = ctx.cancelling || ctx.snapshot?.stage === "cancelling" || !ctx.snapshot?.run_id || !ACTIVE.has(status);
      document.getElementById("release-receipt-btn").hidden = !(ctx.pending?.requestId
        && ctx.pending.settled && !ctx.pending.receiptFinished);
    }}

    function stopObservation(ctx) {{
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

    function replyKey(snapshot) {{
      return snapshot.run_id + ":" + (snapshot.status === "waiting_input" ? "input:" + snapshot.sequence : "final");
    }}

    function showReply(ctx, snapshot, reply) {{
      if (!reply || !snapshot.run_id) return;
      const key = replyKey(snapshot);
      if (ctx.delivered.has(key)) return;
      ctx.delivered.add(key);
      appendMessage("assistant", reply);
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
        modeInput.value = snapshot.wall_time_ms === 180000 ? "standard" : "analysis";
      }}

      if (withHistory && !ctx.historyLoaded) {{
        ctx.historyLoaded = true;
        messagesEl.replaceChildren();
        const history = (snapshot.messages || []).filter(item => ["user", "assistant"].includes(item.role) && typeof item.content === "string");
        history.forEach(item => appendMessage(item.role, item.content));
        const lastItem = messagesEl.lastElementChild;
        if (lastItem) messagesEl.scrollTop = Math.max(0, lastItem.offsetTop - messagesEl.offsetTop - 24);
        const last = history.at(-1);
        if (last?.role === "assistant" && (!last.run_id || last.run_id === snapshot.run_id)
            && snapshot.reply && last.content === snapshot.reply) {{
          ctx.delivered.add(replyKey(snapshot));
        }}
      }}

      setStatus(STATUS_LABELS[snapshot.status]);
      stageEl.textContent = ({{idle:"准备就绪",queued:"已接收",model:"分析中",tool:"查询中",finalizing:"整理结论",waiting_input:"待补充",completed:"已完成",failed:"未完成",cancelled:"已取消",cancelling:"取消中",reconnecting:"正在重新连接"}})[snapshot.stage] || STATUS_LABELS[snapshot.status];
      progressMessageEl.textContent = snapshot.message || STATUS_LABELS[snapshot.status];
      const budget = snapshot.wall_time_ms || (pending?.mode === "standard" || modeInput.value === "standard" ? 180000 : 600000);
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
      // Terminal state and persisted reply can become visible on adjacent reads.
      if (snapshot.status === "completed" && !snapshot.reply) {{
        ctx.replyAttempts = (ctx.replyAttempts || 0) + 1;
        if (ctx.replyAttempts < 8) return true;
        progressMessageEl.textContent = "任务已完成，终答尚未同步。请重新连接，不要重新发送。";
        resumeBtn.hidden = false;
      }} else ctx.replyAttempts = 0;
      return !ended && snapshot.status !== "idle";
    }}

    function applyReceipt(ctx, result) {{
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
              appendMessage("assistant", result.reply || "请求已结束，但没有可展示的回复。");
            }}
            pending.replyRendered = true;
          }}
          if (result.status === "completed") clearAcceptedDraft(ctx, pending);
          else saveDraft(ctx);
          if (!result.progress) {{
            setStatus(result.status === "completed" ? "响应完成" : "任务未完成");
            stageEl.textContent = result.status === "completed" ? "已处理" : "未完成";
            timeEl.textContent = "";
            progressMessageEl.textContent = "已按提交回执确认结果。";
          }}
        }} else {{
          pending.settled = false;
          if (!result.progress || TERMINAL.has(result.progress.status)) {{
            setStatus("请求已接收");
            stageEl.textContent = "确认执行结果";
            progressMessageEl.textContent = result.message || "正在等待原任务的持久化回复，不会重新执行。";
          }}
        }}
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

    async function openChat() {{
      if (active) {{
        saveDraft(active);
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
      controls(ctx);
      messagesEl.replaceChildren();
      renderFiles([]);
      messageInput.value = "";
      messageInput.disabled = true;
      modeInput.value = "analysis";
      stageEl.textContent = "尚未开始任务";
      progressMessageEl.textContent = "正在查询会话，不会重发消息。";
      timeEl.textContent = "已耗时 0 秒 / 预算 10 分钟";
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
      modeInput.value = saved?.mode === "standard" ? "standard" : "analysis";
      renderFiles(saved?.files || []);
      let receipt = saved?.receipt;
      if (!receipt && SUPPORTS_RECEIPTS && ctx.draftKey) {{
        try {{ receipt = JSON.parse(stored(localStorage, ctx.draftKey + ".receipt") || "null"); }} catch (_error) {{}}
      }}
      if (SUPPORTS_RECEIPTS && /^[a-f0-9]{{32}}$/.test(receipt?.requestId || "")) {{
        ctx.pending = {{...receipt, files: saved?.requestFiles || [], restored: true, settled: false}};
      }}
      ctx.syncing = SUPPORTS_PROGRESS || Boolean(ctx.pending);
      controls(ctx);
      if ((SUPPORTS_PROGRESS || ctx.pending) && chatId && (!AUTH_REQUIRED || token)) {{
        setStatus("正在恢复会话");
        observe(ctx);
      }} else {{
        ctx.syncing = false;
        controls(ctx);
        setStatus(AUTH_REQUIRED && !token ? "需要 Bearer Token" : "准备就绪");
        appendMessage("system", SUPPORTS_PROGRESS ? "填好会话 ID 和 Token 后重新连接。" : "页面已就绪，可以开始聊天。");
      }}
    }}

    async function sendMessage() {{
      const ctx = active;
      if (!ctx || !current(ctx) || sendBtn.disabled) return;
      const message = messageInput.value.trim() || (selectedFiles.length ? "请描述图片内容。" : "");
      if (!message) {{ setStatus("先输入消息或选择图片"); return; }}
      if (!ctx.chatId) {{ setStatus("先填会话 ID"); return; }}
      if (AUTH_REQUIRED && !ctx.token) {{ setStatus("需要 Bearer Token"); return; }}
      const prior = ctx.pending;
      const pending = {{
        message, draftText: messageInput.value, files: [...selectedFiles], mode: modeInput.value,
        baseRunId: ctx.snapshot?.run_id, baseSequence: ctx.snapshot?.sequence || 0,
        continuing: ctx.snapshot?.status === "waiting_input", runId: null, settled: false
      }};
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
        const payload = {{chat_id: ctx.chatId, message, attachments}};
        if (SUPPORTS_PROGRESS) payload.execution_mode = pending.mode;
        if (SUPPORTS_RECEIPTS) {{
          const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(payload)));
          if (!current(ctx) || ctx.pending !== pending) return;
          pending.fingerprint = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
          if (prior?.requestId && !prior.receiptFinished && prior.fingerprint !== pending.fingerprint) {{
            ctx.pending = prior;
            setStatus("请先核对原提交");
            progressMessageEl.textContent = "仍有未确认的提交。重试须保持原文字、模式和图片；如要开始新请求，请先在设置中结束本次提交核对。";
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
          if (!SUPPORTS_PROGRESS || (response.status < 500 && !pending.runId)) {{
            stopObservation(ctx);
            failSubmission(ctx, pending, response.status === 401 ? "鉴权失败，请检查 Token。草稿和图片已保留。" : "发送失败，请检查消息或图片后手动重试。草稿和图片已保留。");
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
    modeInput.addEventListener("change", () => {{
      saveDraft();
      timeEl.textContent = "已耗时 0 秒 / 预算 " + (modeInput.value === "standard" ? 3 : 10) + " 分钟";
    }});
    sendBtn.addEventListener("click", sendMessage);
    cancelBtn.addEventListener("click", cancelRun);
    resumeBtn.addEventListener("click", () => observe(active));
    document.getElementById("clear-btn").addEventListener("click", () => {{
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
    window.addEventListener("pagehide", () => {{ saveDraft(); if (active) stopObservation(active); }});
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

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/chat-assets/{name}", include_in_schema=False)
    async def chat_asset(name: str) -> FileResponse:
        if name not in {"chat.css", "markdown-it.min.js", "markdown.js"}:
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

    @app.post("/api/chat", response_model=ChatResponse, response_model_exclude_none=True, dependencies=[Depends(verify_token)])
    async def chat(req: ChatRequest, authorization: str = Header(default="")) -> ChatResponse:
        """Non-streaming chat endpoint."""
        effective_id = _effective_chat_id(req.chat_id, authorization)
        if req.attachments and not supports_images:
            raise HTTPException(status_code=400, detail="当前执行后端未启用图片输入")
        options = {"attachments": req.attachments} if req.attachments else {}
        if req.execution_mode is not None:
            options["execution_mode"] = req.execution_mode

        if req.request_id is not None:
            if receipts is None:
                raise HTTPException(status_code=501, detail="当前服务未启用持久化回执")
            scope = receipts.scope(effective_id, _extract_token(authorization))
            try:
                receipt, created = await receipts.admit(
                    scope, req.request_id, req.model_dump(exclude={"request_id"}),
                )
            except ReceiptConflict:
                raise HTTPException(status_code=409, detail="同一提交 ID 不能用于不同内容") from None
            if created:
                task = asyncio.create_task(receipts.execute(
                    receipt, lambda: message_handler(effective_id, req.message, **options),
                ))
                chat_tasks.add(task)
                task.add_done_callback(chat_tasks.discard)
            return JSONResponse(
                {"request_id": req.request_id, "status": "accepted"},
                status_code=202, headers={"Cache-Control": "no-store"},
            )

        async def execute_message() -> ChatResponse:
            try:
                reply = await message_handler(effective_id, req.message, **options)
                return ChatResponse(chat_id=effective_id, reply=reply,
                                    delivery="direct" if isinstance(reply, DirectReply) else None)
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
            if result.get("progress"):
                result["progress"] = _public_chat_progress(result["progress"])
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except Exception:
            logger.error("Chat receipt query failed; private exception details withheld")
            raise HTTPException(status_code=503, detail="回执暂时无法查询，请重新连接，不要换 ID 重发") from None

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
        if stream_handler is None:
            raise HTTPException(status_code=501, detail="Streaming not enabled")
        effective_id = _effective_chat_id(req.chat_id, authorization)
        if req.attachments and not supports_images:
            raise HTTPException(status_code=400, detail="当前执行后端未启用图片输入")

        async def event_generator():
            try:
                options = {"attachments": req.attachments} if req.attachments else {}
                async for chunk in stream_handler(effective_id, req.message, **options):
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
