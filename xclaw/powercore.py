"""PowerCore Service API v1 client; XClaw keeps its channels and business tools."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
from loguru import logger

from xclaw.tools import RiskLevel, ToolRegistry
from xclaw.attachments import ImageAttachment

SERVICE_API_VERSION = 1
MCP_PROTOCOL_VERSION = "2026-07-28"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_DEFINITIVE_REJECTIONS = {400, 401, 403, 413, 422}
_BACKGROUND_POLL_SECONDS = 1.0
_BACKGROUND_RETRY_SECONDS = 5.0
_BACKGROUND_MAX_OBSERVERS = 4
_CANCEL_TIMEOUT_SECONDS = 10.0


def allowed_tools(registry: ToolRegistry, profile: str) -> list[str]:
    return [
        tool.name for tool in registry.all_tools()
        if tool.risk_level != RiskLevel.HIGH
        and (profile == "mcp-controlled" or tool.read_only)
    ]


def mcp_catalog(registry: ToolRegistry, settings: Any, callback_url: str) -> str:
    """Generate PowerCore's existing allowlist format without a TOML dependency."""
    lines = ["schema_version = 1"]
    for profile in ("mcp-readonly", "mcp-controlled"):
        names = allowed_tools(registry, profile)
        if not names:
            continue
        lines.extend([
            "", "[[servers]]", f'id = "xclaw-{profile}"',
            f"profile = {json.dumps(profile)}", 'transport = "streamable-http"',
            f"url = {json.dumps(callback_url)}",
            f"allowed_tenants = [{json.dumps(settings.powercore_tenant_id)}]",
            "request_timeout_ms = 120000", "max_response_bytes = 1048576",
            "catalog_max_ttl_ms = 30000", "[servers.headers]",
            'Authorization = "env:XCLAW_POWERCORE_MCP_AUTHORIZATION"',
        ])
        for name in names:
            read = registry.get(name).read_only
            value = "true" if read else "false"
            lines.extend([
                "[[servers.tools]]", f"name = {json.dumps(name)}",
                f'risk = "{"read" if read else "write"}"',
                f"read_only = {value}", f"idempotent = {value}",
                f"independent = {str(read and getattr(registry.get(name), 'independent', False)).lower()}",
                "timeout_ms = 120000",
            ])
    return "\n".join(lines) + "\n"


class PowerCoreError(RuntimeError):
    """A safe, user-facing execution or compatibility error."""


class SubmissionRejected(PowerCoreError):
    """The service definitively rejected a submission before accepting it."""


class RunFailed(PowerCoreError):
    """A failure confirmed by a terminal service event, not an observer error."""


class PowerCoreClient:
    def __init__(self, settings: Any, db: Any, *, transport: Any = None) -> None:
        self.settings = settings
        self.db = db
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._inflight: dict[int, asyncio.Task[str]] = {}
        self._closing = False
        self._retry_at: dict[int, float] = {}
        self._background: dict[str, asyncio.Task] = {}
        self._background_supervisor: asyncio.Task | None = None
        self._background_callers: set[asyncio.Task] = set()
        self._owned_background: set[str] = set()
        headers = (
            {"Authorization": f"Bearer {settings.powercore_api_token}"}
            if settings.powercore_api_token else {
                "X-PowerCore-Tenant-Id": settings.powercore_tenant_id,
                "X-PowerCore-User-Id": settings.powercore_user_id,
            }
        )
        self.http = httpx.AsyncClient(
            base_url=settings.powercore_base_url.rstrip("/") + "/",
            headers=headers,
            timeout=httpx.Timeout(10.0, read=max(settings.timeout, 240.0)),
            transport=transport,
            trust_env=False,
        )

    async def close(self) -> None:
        self._closing = True
        tasks = set(self._inflight.values()) | set(self._background.values()) | self._background_callers
        if self._background_supervisor:
            tasks.add(self._background_supervisor)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.http.aclose()

    def _start_observer(self, chat_id: int, coroutine: Any) -> asyncio.Task[str]:
        task = asyncio.create_task(coroutine)
        self._inflight[chat_id] = task

        def done(completed):
            if self._inflight.get(chat_id) is completed:
                self._inflight.pop(chat_id, None)
            if not completed.cancelled() and completed.exception() is not None:
                logger.warning("PowerCore observer stopped: {}", type(completed.exception()).__name__)

        task.add_done_callback(done)
        return task

    def _requested_budget(self, ctx: Any) -> int:
        seconds = self.settings.powercore_standard_timeout if getattr(ctx, "execution_mode", "analysis") == "standard" \
            else self.settings.powercore_run_timeout
        return int(seconds * 1000)

    @staticmethod
    def _snapshot(binding: dict[str, Any] | None) -> dict[str, Any]:
        if binding is None:
            return {"status": "idle", "stage": "idle", "message": "可以开始对话", "sequence": 0,
                    "elapsed_seconds": 0.0, "wall_time_ms": 0}
        progress = json.loads(binding.get("progress_json") or "{}")
        if binding["status"] in TERMINAL_STATUSES and progress.get("stage") != binding["status"]:
            progress.update(stage=binding["status"], message={"completed": "本轮已完成", "failed": "本轮未完成", "cancelled": "本轮已取消"}[binding["status"]])
        elif binding.get("cancel_requested") and binding["status"] not in TERMINAL_STATUSES:
            progress.update(stage="cancelling", message="取消请求待执行服务确认")
        started = datetime.fromisoformat(binding["created_at"]).replace(tzinfo=timezone.utc)
        elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        # Legacy terminal rows have no recorded duration; wall-clock age is not execution time.
        elapsed = progress.get("elapsed_seconds", None if binding["status"] in TERMINAL_STATUSES else elapsed)
        return {
            "run_id": binding["run_id"], "status": binding["status"],
            "stage": progress.get("stage", binding["status"]),
            "message": progress.get("message", "正在等待执行状态"),
            "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
            "wall_time_ms": binding.get("wall_time_ms", 180000), "sequence": binding["cursor"],
            "reply": binding.get("reply") if binding.get("reply_cursor") == binding["cursor"] else None,
            "error_code": progress.get("error_code"),
        }

    async def progress(self, ctx: Any, *, include_history: bool = False) -> dict[str, Any]:
        async with self._locks[ctx.chat_id]:
            pending = await self.db.get_pending_powercore_submission(ctx.chat_id)
            binding = await self.db.get_powercore_session(ctx.chat_id)
            active = self._inflight.get(ctx.chat_id)
            available = not (active and not active.done()) and not self._closing and time.monotonic() >= self._retry_at.get(ctx.chat_id, 0)
            if pending and available:
                self._start_observer(ctx.chat_id, self._recover_submission(ctx, pending))
            elif binding and available:
                if binding["status"] in {"queued", "running"} or binding.get("cancel_requested") and binding["status"] not in TERMINAL_STATUSES or (
                    binding["status"] in TERMINAL_STATUSES | {"waiting_input"} and binding.get("reply_cursor") != binding["cursor"]
                ):
                    self._start_observer(ctx.chat_id, self._observe_existing(ctx, binding))
            snapshot = self._snapshot(binding)
            if pending:
                snapshot = {"status": "queued", "stage": "reconnecting", "message": "正在确认先前提交，不会另建任务",
                            "sequence": pending["cursor"], "elapsed_seconds": 0.0,
                            "wall_time_ms": (binding or {}).get("wall_time_ms", 180000) if pending["kind"] == "input"
                            else json.loads(pending["request_json"])["wall_time_ms"]}
        if include_history:
            snapshot["messages"] = await self.db.get_recent_messages(ctx.chat_id)
        return snapshot

    async def cancel_run(self, chat_id: int, run_id: str) -> dict[str, Any]:
        async with self._locks[chat_id]:
            binding = await self.db.get_powercore_session(chat_id)
            if not binding or binding["run_id"] != run_id:
                raise PowerCoreError("任务不属于当前会话，或当前会话已进入下一轮。")
            if binding["status"] in TERMINAL_STATUSES:
                return self._snapshot(binding)
            await self.db.request_powercore_cancel(run_id)
            try:
                await self._cancel_remote(run_id)
            except (httpx.HTTPError, TimeoutError):
                raise PowerCoreError("暂时无法确认取消请求，请重新查看任务状态。") from None
            return self._snapshot(await self.db.get_powercore_run(run_id))

    async def _save_reply(self, ctx: Any, run_id: str | None, reply: str) -> None:
        binding = await self.db.get_powercore_run(run_id) if run_id else None
        if binding:
            if binding["status"] in TERMINAL_STATUSES | {"waiting_input"}:
                await self.db.save_powercore_reply(run_id, reply, persist_message=not ctx.skip_message_persistence)
        elif not ctx.skip_message_persistence:
            await self.db.save_message(ctx.chat_id, "assistant", reply)

    async def _observe_existing(self, ctx: Any, binding: dict[str, Any]) -> str:
        cursor = binding["cursor"]
        if binding["status"] in TERMINAL_STATUSES or binding["status"] == "waiting_input" and not binding.get("cancel_requested"):
            cursor = max(0, cursor - 1)
        try:
            if binding.get("cancel_requested") and binding["status"] not in TERMINAL_STATUSES:
                await self._cancel_remote(binding["run_id"])
            async with asyncio.timeout(binding.get("wall_time_ms", 180000) / 1000 + 30):
                reply = await self._wait(ctx, binding["run_id"], cursor)
        except RunFailed as exc:
            reply = str(exc)
        except (httpx.HTTPError, TimeoutError):
            if not ctx.skip_session_persistence:
                self._retry_at[ctx.chat_id] = time.monotonic() + 5
            await self.db.save_powercore_progress(binding["run_id"], {
                "stage": "reconnecting", "message": "观察连接中断，正在重新确认任务状态；不会重新执行请求",
            })
            raise
        await self._save_reply(ctx, binding["run_id"], reply)
        return reply

    async def _admit_submission(self, pending: dict) -> tuple[str, int]:
        if pending["kind"] == "input":
            return await self._admit_input(pending)
        request = json.loads(pending["request_json"])
        for attempt in range(2):
            try:
                accepted = await self._json("POST", pending["path"], json=request)
                break
            except httpx.TransportError:
                if attempt:
                    raise
            except httpx.HTTPStatusError as exc:
                await self._reject_if_definitive(pending, exc)
                raise
        run_id, session_id = accepted["run_id"], accepted["session_id"]
        if not re.fullmatch(r"run_[A-Za-z0-9_-]{1,124}", run_id) or not re.fullmatch(r"session_[A-Za-z0-9_-]{1,120}", session_id):
            raise PowerCoreError("PowerCore 返回了无效会话标识。")
        wall_time_ms = accepted.get("wall_time_ms")
        if type(wall_time_ms) is not int or not 0 < wall_time_ms <= request["wall_time_ms"]:
            await self._cancel_remote(run_id)
            await self.db.clear_powercore_submission(pending["submission_id"])
            raise PowerCoreError("执行服务未返回有效预算，请升级 PowerCore 后重试；已请求取消本轮。")
        await self.db.bind_powercore_run(
            pending["chat_id"], run_id, session_id, json.loads(pending["allowed_tools_json"]),
            conversational=bool(pending["conversational"]), wall_time_ms=wall_time_ms,
            persist_message=bool(pending["persist_message"]),
            submission_id=pending["submission_id"],
        )
        await self.db.clear_powercore_submission(pending["submission_id"])
        return run_id, wall_time_ms

    async def _reject_if_definitive(self, pending: dict, exc: httpx.HTTPStatusError) -> None:
        if exc.response.status_code in _DEFINITIVE_REJECTIONS:
            await self.db.clear_powercore_submission(pending["submission_id"])
            raise SubmissionRejected(
                f"执行服务拒绝了本次提交（HTTP {exc.response.status_code}），未接受本次输入；请调整后重试。"
            ) from None

    async def _admit_input(self, pending: dict) -> tuple[str, int]:
        run_id = pending["run_id"]
        request = json.loads(pending["request_json"])
        async with self._locks[pending["chat_id"]]:
            binding = await self.db.get_powercore_run(run_id)
            if binding is None:
                raise PowerCoreError("输入提交缺少运行绑定，尚未确认。")
            if not binding.get("cancel_requested"):
                # An input request ID may already have been consumed. Never retry
                # a POST before checking the authoritative run's current question.
                inspection = await self._json("GET", f"/runs/{run_id}")
                status = inspection.get("status")
                if inspection.get("run_id") != run_id or status not in TERMINAL_STATUSES | {"queued", "running", "waiting_input"}:
                    raise PowerCoreError("输入提交的运行状态尚未确认。")
                if status == "waiting_input":
                    question_id = inspection.get("waiting_for_input")
                    if not isinstance(question_id, str) or not question_id:
                        raise PowerCoreError("当前追问标识尚未确认，不会重新提交答案。")
                    if question_id == request["input_request_id"]:
                        try:
                            await self._json("POST", pending["path"], json=request)
                        except httpx.HTTPStatusError as exc:
                            await self._reject_if_definitive(pending, exc)
                            raise
            # Replay from the original cursor, not the inspection's latest one.
            # queued avoids replaying the old question when observing this run.
            if binding["cursor"] <= pending["cursor"]:
                await self.db.update_powercore_run(run_id, "queued", pending["cursor"])
            await self.db.clear_powercore_submission(pending["submission_id"])
            return run_id, binding["wall_time_ms"]

    async def _recover_submission(self, ctx: Any, pending: dict) -> str:
        try:
            run_id, _ = await self._admit_submission(pending)
            return await self._observe_existing(ctx, await self.db.get_powercore_run(run_id))
        except SubmissionRejected as exc:
            await self._save_reply(ctx, None, str(exc))
            return str(exc)
        except (httpx.HTTPError, PowerCoreError, TimeoutError, ValueError, KeyError):
            self._retry_at[ctx.chat_id] = time.monotonic() + 5
            return "先前提交尚未确认，请恢复观察，不要重复发送。"

    def _ensure_background_supervisor(self) -> None:
        if not self._closing and (self._background_supervisor is None or self._background_supervisor.done()):
            self._background_supervisor = asyncio.create_task(self._supervise_background())

    async def restore_background(self, context_factory: Any = None) -> None:
        # Recovery only needs the persisted owner and persistence flags, not a
        # new agent context or execution loop. Keep the runtime hook compatible.
        if self._background_supervisor is not None or self._closing:
            return
        await self.db.arm_powercore_background()
        self._ensure_background_supervisor()

    async def _supervise_background(self) -> None:
        while not self._closing:
            try:
                submissions, runs = await self.db.pending_powercore_background(
                    due_before=time.time(), limit=_BACKGROUND_MAX_OBSERVERS + len(self._background),
                )
                for is_submission, record in [(True, r) for r in submissions] + [(False, r) for r in runs]:
                    if len(self._background) >= _BACKGROUND_MAX_OBSERVERS:
                        break
                    identity = record["submission_id"] if is_submission else record["run_id"]
                    if identity in self._owned_background or record.get("submission_id") in self._owned_background:
                        continue
                    if identity in self._background:
                        continue
                    task = asyncio.create_task(self._recover_background(record, is_submission))
                    self._background[identity] = task
                    task.add_done_callback(self._background_done)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Background reconciliation paused: {}", type(exc).__name__)
            await asyncio.sleep(_BACKGROUND_POLL_SECONDS)

    def _background_done(self, task: asyncio.Task) -> None:
        for identity in [key for key, value in self._background.items() if value is task]:
            self._background.pop(identity, None)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Background observation stopped: {}", type(task.exception()).__name__)

    async def _recover_background(self, record: dict, is_submission: bool) -> None:
        identity = record["submission_id"] if is_submission else record["run_id"]
        ctx = SimpleNamespace(chat_id=record["chat_id"], skip_session_persistence=True,
                              skip_message_persistence=not bool(record["persist_message"]))
        try:
            if is_submission:
                run_id, _ = await self._admit_submission(record)
                self._background.pop(identity, None)
                self._background[run_id] = asyncio.current_task()
                identity, is_submission = run_id, False
                record = await self.db.get_powercore_run(run_id)
                await self.db.defer_powercore_observation(run_id, 0)
            if record["status"] == "waiting_input":
                await self.db.request_powercore_cancel(identity)
                record = await self.db.get_powercore_run(identity)
            await self._observe_existing(ctx, record)
            current = await self.db.get_powercore_run(identity)
            if current["status"] in TERMINAL_STATUSES and current["reply_cursor"] == current["cursor"]:
                return
        except SubmissionRejected as exc:
            await self._save_reply(ctx, None, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Background observation will retry: {}", type(exc).__name__)
        if not is_submission:
            await self.db.request_powercore_cancel(identity)
        delay = min(_BACKGROUND_RETRY_SECONDS * 2 ** min(record["observe_attempts"], 4), 60.0)
        await self.db.defer_powercore_observation(identity, time.time() + delay, submission=is_submission)

    async def _defer_background(self, run_id: str | None, submission: dict | None, reply: str) -> None:
        if run_id:
            binding = await self.db.get_powercore_run(run_id)
            if binding["status"] in TERMINAL_STATUSES:
                await self.db.save_powercore_reply(run_id, reply, persist_message=False)
                return
            await self.db.request_powercore_cancel(run_id)
            await self.db.defer_powercore_observation(run_id, 0)
            try:
                await self._cancel_remote(run_id)
            except (httpx.HTTPError, PowerCoreError, TimeoutError):
                pass
        elif submission:
            await self.db.defer_powercore_observation(submission["submission_id"], 0, submission=True)
        self._ensure_background_supervisor()

    @staticmethod
    def _version(data: dict) -> None:
        if data.get("schema_version") != SERVICE_API_VERSION:
            raise PowerCoreError("PowerCore API 版本不兼容，请检查服务版本。")

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict:
        response = await self.http.request(method, path, **kwargs)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise PowerCoreError("PowerCore 返回了无效响应。")
        self._version(data)
        return data

    async def _cancel_remote(self, run_id: str) -> None:
        async with asyncio.timeout(_CANCEL_TIMEOUT_SECONDS):
            await self._json("POST", f"/runs/{run_id}/cancel")

    async def check_ready(self) -> None:
        await self._json("GET", "/health/ready")

    async def reset(self, chat_id: int) -> None:
        if await self.db.get_pending_powercore_submission(chat_id):
            raise PowerCoreError("存在尚未确认的提交，请恢复观察后再重置，避免重复执行。")
        previous = await self.db.get_powercore_session(chat_id)
        if previous and previous["status"] not in TERMINAL_STATUSES:
            await self._cancel_remote(previous["run_id"])
            await self.db.update_powercore_run(previous["run_id"], "cancelled", previous["cursor"])
        await self.db.clear_powercore_session(chat_id)

    async def run(self, ctx: Any, text: str, max_iterations: int | None = None,
                  attachments: list[ImageAttachment] | None = None) -> str:
        from xclaw.responses import RejectedReply
        # A child has a separate PowerCore session and must not lock its parent.
        if ctx.skip_message_persistence or ctx.skip_session_persistence:
            caller = asyncio.current_task()
            self._background_callers.add(caller)
            try:
                return await self._run(ctx, text, max_iterations, attachments)
            finally:
                self._background_callers.discard(caller)
        async with self._locks[ctx.chat_id]:
            previous = self._inflight.get(ctx.chat_id)
            if previous and not previous.done():
                return RejectedReply("上一轮仍在执行，请查看进度或取消当前任务后再发送。")
            pending = await self.db.get_pending_powercore_submission(ctx.chat_id)
            if pending:
                self._start_observer(ctx.chat_id, self._recover_submission(ctx, pending))
                return RejectedReply("先前提交正在确认，请查看执行进度，不需要重复发送。")
            binding = await self.db.get_powercore_session(ctx.chat_id)
            if binding and binding.get("cancel_requested") and binding["status"] not in TERMINAL_STATUSES:
                return RejectedReply("当前任务正在取消，请等待确认后再发送。")
            task = self._start_observer(ctx.chat_id, self._run(ctx, text, max_iterations, attachments))
        if ctx.channel == "web":
            return await asyncio.shield(task)
        return await task

    async def _run(self, ctx: Any, text: str, max_iterations: int | None,
                   attachments: list[ImageAttachment] | None = None) -> str:
        run_id = None
        submission = None
        owned_submission_id = None
        observation_started = False
        if not ctx.skip_message_persistence:
            display_text = text + "".join(f"\n[图片附件：{item.name}]" for item in attachments or [])
            await self.db.save_message(ctx.chat_id, "user", display_text)
        try:
            previous = None if ctx.skip_session_persistence else await self.db.get_powercore_session(ctx.chat_id)
            if previous and previous["input_request_json"]:
                if attachments:
                    raise PowerCoreError("当前追问只接受文字回答；如需分析图片，请先发送 /reset 再上传。")
                pending = json.loads(previous["input_request_json"])
                run_id = previous["run_id"]
                schema = pending.get("response_schema", {})
                if schema.get("required") == ["answer"]:
                    value = {"answer": text}
                else:
                    try:
                        value = json.loads(text)
                    except ValueError:
                        raise PowerCoreError("请按上一条提示提供 JSON 答案，或发送 /reset 重置会话。") from None
                async with self._locks[ctx.chat_id]:
                    current = await self.db.get_powercore_run(run_id)
                    if current.get("cancel_requested"):
                        raise PowerCoreError("当前任务正在取消，请等待确认后再发送。")
                    submission = await self.db.save_powercore_submission(
                        ctx.chat_id, f"/runs/{run_id}/input",
                        {"input_request_id": pending["input_request_id"], "value": value},
                        json.loads(previous["allowed_tools_json"]), conversational=True,
                        persist_message=not ctx.skip_message_persistence,
                        kind="input", run_id=run_id, cursor=previous["cursor"],
                        submission_id=f"input:{run_id}:{pending['input_request_id']}",
                    )
                run_id, wall_time_ms = await self._admit_submission(submission)
                cursor = previous["cursor"]
            else:
                if previous and previous["status"] not in TERMINAL_STATUSES:
                    inspection = await self._json("GET", f"/runs/{previous['run_id']}")
                    if inspection["status"] not in TERMINAL_STATUSES:
                        raise PowerCoreError("上一轮仍在执行，请稍后重试，或发送 /reset 取消并重置。")
                    await self.db.update_powercore_run(
                        previous["run_id"], inspection["status"], previous["cursor"],
                    )
                from xclaw.agent_engine import _build_system_prompt

                memories = await ctx.structured_memory.get_all(ctx.chat_id) if ctx.structured_memory else []
                file_memory = ctx.file_memory.read(ctx.chat_id) if ctx.file_memory else ""
                context = _build_system_prompt(ctx, memories, file_memory)
                prompt = f"{context}\n\n## 本轮用户消息\n{text}"
                profile = self.settings.powercore_profile
                if all(tool.read_only for tool in ctx.tools.all_tools()):
                    profile = "mcp-readonly"
                path = f"/sessions/{previous['session_id']}/turns" if previous else "/runs"
                request = {
                    "prompt": prompt, "profile": profile,
                    "idempotency_key": f"xclaw-{uuid.uuid4().hex}",
                    "wall_time_ms": self._requested_budget(ctx),
                }
                if attachments:
                    request.pop("prompt")
                    request["input"] = [{"type": "text", "text": prompt}, *[item.to_powercore() for item in attachments]]
                request["max_turns"] = max_iterations if max_iterations is not None else self.settings.max_tool_iterations
                if ctx.skip_session_persistence:
                    owned_submission_id = request["idempotency_key"]
                    self._owned_background.add(owned_submission_id)
                submission = await self.db.save_powercore_submission(
                    ctx.chat_id, path, request, allowed_tools(ctx.tools, profile),
                    conversational=not ctx.skip_session_persistence, persist_message=not ctx.skip_message_persistence,
                )
                run_id, wall_time_ms = await self._admit_submission(submission)
                cursor = 0
            await self.db.save_powercore_progress(run_id, {"stage": "queued", "message": "请求已接收，等待执行"})
            observation_started = True
            async with asyncio.timeout(wall_time_ms / 1000 + 30):
                reply = await self._wait(ctx, run_id, cursor)
        except TimeoutError:
            if run_id:
                await self.db.save_powercore_progress(run_id, {"stage": "reconnecting", "message": "等待超时，正在重新确认任务状态"})
            reply = "观察连接超时，任务状态尚未确认；请查看执行进度，不需要重新发送。"
            if ctx.skip_session_persistence:
                await self._defer_background(run_id, submission, reply)
                raise PowerCoreError(reply) from None
        except asyncio.CancelledError:
            if run_id and not self._closing:
                await self.db.request_powercore_cancel(run_id)
                try:
                    await self._cancel_remote(run_id)
                except (httpx.HTTPError, PowerCoreError, TimeoutError):
                    logger.warning("PowerCore cancellation request failed for {}", run_id)
            if ctx.skip_session_persistence and not self._closing:
                await self._defer_background(run_id, submission, "本轮观察已取消，执行状态待确认。")
            raise
        except PowerCoreError as exc:
            if ctx.skip_session_persistence:
                await self._defer_background(run_id, submission, str(exc))
                raise
            reply = str(exc)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("PowerCore request failed: {} run={}", type(exc).__name__, run_id)
            reply = "AI 执行服务暂时不可用，请稍后重试。"
            if ctx.skip_session_persistence:
                await self._defer_background(run_id, submission, reply)
                raise PowerCoreError(reply) from None
        finally:
            if ctx.skip_session_persistence:
                if submission:
                    self._owned_background.discard(submission["submission_id"])
                self._owned_background.discard(owned_submission_id)
        await self._save_reply(ctx, run_id if observation_started else None, reply)
        if not observation_started:
            from xclaw.responses import RejectedReply

            return RejectedReply(reply)
        return reply

    async def _wait(self, ctx: Any, run_id: str, cursor: int) -> str:
        model = self.settings.model
        for attempt in range(3):
            try:
                async with self.http.stream(
                    "GET", f"/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)},
                ) as response:
                    response.raise_for_status()
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip(" "))
                            continue
                        if line or not data_lines:
                            continue
                        event = json.loads("\n".join(data_lines))
                        data_lines.clear()
                        self._version(event)
                        if event.get("run_id") != run_id:
                            raise PowerCoreError("PowerCore 返回了其他运行的事件，已停止处理。")
                        sequence = event.get("sequence")
                        if not isinstance(sequence, int):
                            raise PowerCoreError("PowerCore 事件序号无效。")
                        if sequence <= cursor:
                            continue
                        cursor = sequence
                        kind = event.get("type")
                        if kind == "run.started":
                            model = event.get("model", model)
                        elif kind == "usage":
                            usage = event.get("usage", {})
                            await self.db.record_usage(
                                ctx.chat_id, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                            )
                        elif kind in {"tool.started", "tool.completed"}:
                            logger.info("PowerCore run={} event={} tool={}", run_id, kind, event.get("name"))
                        status = {
                            "run.completed": "completed", "run.failed": "failed",
                            "run.cancelled": "cancelled", "input_required": "waiting_input",
                        }.get(kind, "running")
                        await self.db.update_powercore_run(
                            run_id, status, cursor, event if kind == "input_required" else None,
                        )
                        await self._project_progress(run_id, kind, event)
                        if kind == "run.completed":
                            reply = event.get("response", "")
                            if not isinstance(reply, str) or not reply.strip():
                                await self.db.update_powercore_run(run_id, "failed", cursor)
                                raise RunFailed("本轮模型没有返回正文，请重试。")
                            return reply
                        if kind == "input_required":
                            if ctx.skip_session_persistence:
                                await self.db.request_powercore_cancel(run_id)
                                raise PowerCoreError("后台任务缺少必要信息，取消请求待确认，请补充任务描述后重试。")
                            prompt = event.get("prompt", "")
                            if not isinstance(prompt, str) or not prompt.strip():
                                raise PowerCoreError("本轮缺少有效的补充信息提示，请发送 /reset 重置会话。")
                            return prompt
                        if kind == "run.failed":
                            code = event.get("error", {}).get("code", "unknown")
                            logger.warning("PowerCore run={} failed code={}", run_id, code)
                            if code == "provider_protocol":
                                raise RunFailed("模型返回了无效响应，本轮未能完成，请重试。")
                            if code == "deadline_exceeded":
                                raise RunFailed("本轮执行预算已耗尽（超时），未生成完整结论；可选择分析模式或缩小核验范围后重试。")
                            raise RunFailed("本轮任务未能完成，请稍后重试。")
                        if kind == "run.cancelled":
                            return "本轮任务已取消。"
            except httpx.TransportError:
                if attempt == 2:
                    raise
            if attempt < 2:
                await asyncio.sleep(0.1 * (attempt + 1))
        raise PowerCoreError("AI 响应连接中断，任务尚未确认完成，请稍后重试。")

    async def _project_progress(self, run_id: str, kind: str, event: dict[str, Any]) -> None:
        stages = {
            "run.started": ("model", "正在分析输入材料"),
            "tool.started": ("tool", "正在执行工具查询"),
            "tool.completed": ("model", "工具已返回，正在继续分析"),
            "input_required": ("waiting_input", "需要补充信息"),
            "run.completed": ("completed", "本轮已完成"),
            "run.failed": ("failed", "本轮未完成"),
            "run.cancelled": ("cancelled", "本轮已取消"),
            "run.finalizing": ("finalizing", "正在基于已有证据整理结论"),
        }
        if kind not in stages:
            return
        stage, message = stages[kind]
        progress = {"stage": stage, "message": message}
        if kind == "run.failed":
            code = event.get("error", {}).get("code", "execution_failed")
            safe_codes = {"deadline_exceeded", "provider_protocol", "model_turn_limit", "token_budget_exceeded", "cancelled"}
            progress["error_code"] = code if code in safe_codes else "execution_failed"
            if code == "deadline_exceeded":
                progress["message"] = "执行预算耗尽（超时），本轮未完成"
        if stage in TERMINAL_STATUSES | {"waiting_input"}:
            binding = await self.db.get_powercore_run(run_id)
            started = datetime.fromisoformat(binding["created_at"]).replace(tzinfo=timezone.utc)
            progress["elapsed_seconds"] = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        await self.db.save_powercore_progress(run_id, progress)
