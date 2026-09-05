"""PowerCore Service API v1 client; XClaw keeps its channels and business tools."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import defaultdict
from typing import Any

import httpx
from loguru import logger

from xclaw.tools import RiskLevel, ToolRegistry
from xclaw.attachments import ImageAttachment

SERVICE_API_VERSION = 1
MCP_PROTOCOL_VERSION = "2026-07-28"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


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
                "independent = false", "timeout_ms = 120000",
            ])
    return "\n".join(lines) + "\n"


class PowerCoreError(RuntimeError):
    """A safe, user-facing execution or compatibility error."""


class PowerCoreClient:
    def __init__(self, settings: Any, db: Any, *, transport: Any = None) -> None:
        self.settings = settings
        self.db = db
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
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
        await self.http.aclose()

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

    async def check_ready(self) -> None:
        await self._json("GET", "/health/ready")

    async def reset(self, chat_id: int) -> None:
        previous = await self.db.get_powercore_session(chat_id)
        if previous and previous["status"] not in TERMINAL_STATUSES:
            await self._json("POST", f"/runs/{previous['run_id']}/cancel")
            await self.db.update_powercore_run(previous["run_id"], "cancelled", previous["cursor"])
        await self.db.clear_powercore_session(chat_id)

    async def run(self, ctx: Any, text: str, max_iterations: int | None = None,
                  attachments: list[ImageAttachment] | None = None) -> str:
        # A child has a separate PowerCore session and must not lock its parent.
        if ctx.skip_message_persistence:
            return await self._run(ctx, text, max_iterations, attachments)
        async with self._locks[ctx.chat_id]:
            return await self._run(ctx, text, max_iterations, attachments)

    async def _run(self, ctx: Any, text: str, max_iterations: int | None,
                   attachments: list[ImageAttachment] | None = None) -> str:
        run_id = None
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
                await self._json("POST", f"/runs/{run_id}/input", json={
                    "input_request_id": pending["input_request_id"], "value": value,
                })
                await self.db.update_powercore_run(run_id, "queued", previous["cursor"])
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
                }
                if attachments:
                    request.pop("prompt")
                    request["input"] = [{"type": "text", "text": prompt}, *[item.to_powercore() for item in attachments]]
                request["max_turns"] = max_iterations if max_iterations is not None else self.settings.max_tool_iterations
                for attempt in range(2):
                    try:
                        accepted = await self._json("POST", path, json=request)
                        break
                    except httpx.TransportError:
                        if attempt:
                            raise
                run_id = accepted["run_id"]
                session_id = accepted["session_id"]
                if not re.fullmatch(r"run_[A-Za-z0-9_-]{1,124}", run_id) or not re.fullmatch(r"session_[A-Za-z0-9_-]{1,120}", session_id):
                    raise PowerCoreError("PowerCore 返回了无效会话标识。")
                await self.db.bind_powercore_run(
                    ctx.chat_id, run_id, session_id, allowed_tools(ctx.tools, profile),
                    conversational=not ctx.skip_session_persistence,
                )
                cursor = 0
            async with asyncio.timeout(max(self.settings.timeout, 240.0) + 30):
                reply = await self._wait(ctx, run_id, cursor)
        except TimeoutError:
            if run_id:
                try:
                    await self._json("POST", f"/runs/{run_id}/cancel")
                except (httpx.HTTPError, PowerCoreError):
                    logger.warning("PowerCore timeout cancellation failed for {}", run_id)
            reply = "本轮任务执行超时，已请求取消，请稍后重试。"
            if ctx.skip_session_persistence:
                raise PowerCoreError(reply) from None
        except asyncio.CancelledError:
            if run_id:
                try:
                    await self._json("POST", f"/runs/{run_id}/cancel")
                except (httpx.HTTPError, PowerCoreError):
                    logger.warning("PowerCore cancellation request failed for {}", run_id)
            raise
        except PowerCoreError as exc:
            if ctx.skip_session_persistence:
                raise
            reply = str(exc)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("PowerCore request failed: {} run={}", type(exc).__name__, run_id)
            reply = "AI 执行服务暂时不可用，请稍后重试。"
            if ctx.skip_session_persistence:
                raise PowerCoreError(reply) from None
        if not ctx.skip_message_persistence:
            await self.db.save_message(ctx.chat_id, "assistant", reply)
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
                        if kind == "run.completed":
                            reply = event.get("response", "")
                            if not isinstance(reply, str) or not reply.strip():
                                await self.db.update_powercore_run(run_id, "failed", cursor)
                                raise PowerCoreError("本轮模型没有返回正文，请重试。")
                            return reply
                        if kind == "input_required":
                            if ctx.skip_session_persistence:
                                await self._json("POST", f"/runs/{run_id}/cancel")
                                await self.db.update_powercore_run(run_id, "cancelled", cursor)
                                raise PowerCoreError("后台任务缺少必要信息，已取消，请补充任务描述后重试。")
                            prompt = event.get("prompt", "")
                            if not isinstance(prompt, str) or not prompt.strip():
                                raise PowerCoreError("本轮缺少有效的补充信息提示，请发送 /reset 重置会话。")
                            return prompt
                        if kind == "run.failed":
                            code = event.get("error", {}).get("code", "unknown")
                            logger.warning("PowerCore run={} failed code={}", run_id, code)
                            if code == "provider_protocol":
                                raise PowerCoreError("模型返回了无效响应，本轮未能完成，请重试。")
                            raise PowerCoreError("本轮任务未能完成，请稍后重试。")
                        if kind == "run.cancelled":
                            return "本轮任务已取消。"
            except httpx.TransportError:
                if attempt == 2:
                    raise
            if attempt < 2:
                await asyncio.sleep(0.1 * (attempt + 1))
        raise PowerCoreError("AI 响应连接中断，任务尚未确认完成，请稍后重试。")
