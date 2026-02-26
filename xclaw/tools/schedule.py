"""Tools: schedule_task, list_scheduled_tasks, cancel_scheduled_task."""

from __future__ import annotations

from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class ScheduleTaskTool(Tool):
    """Create a new scheduled task."""

    @property
    def name(self) -> str:
        return "schedule_task"

    @property
    def description(self) -> str:
        return "创建定时任务，支持 cron 表达式（周期任务）或一次性任务。任务触发时会以指定 prompt 调用 Agent。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "任务描述",
                },
                "prompt": {
                    "type": "string",
                    "description": "任务触发时发送给 Agent 的指令",
                },
                "cron_expression": {
                    "type": "string",
                    "description": "cron 表达式（如 '0 15 * * 1-5' 代表每个工作日 15:00）。留空则为一次性任务。",
                },
                "run_once_at": {
                    "type": "string",
                    "description": "一次性任务的执行时间，格式 YYYY-MM-DD HH:MM（cron_expression 为空时使用）",
                },
            },
            "required": ["description", "prompt"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.db is None:
            return ToolResult(content="数据库未初始化", is_error=True)
        description = params.get("description", "").strip()
        prompt = params.get("prompt", "").strip()
        cron = params.get("cron_expression", "").strip() or None
        run_once_at = params.get("run_once_at", "").strip() or None

        if not description or not prompt:
            return ToolResult(content="description 和 prompt 不能为空", is_error=True)

        task_id = await context.db.add_scheduled_task(
            context.chat_id,
            description=description,
            prompt=prompt,
            cron_expression=cron,
            next_run_at=run_once_at,
        )
        task_type = f"cron: {cron}" if cron else f"一次性: {run_once_at or '立即'}"
        return ToolResult(content=f"定时任务已创建（id={task_id}）\n类型: {task_type}\n描述: {description}")


class ListScheduledTasksTool(Tool):
    """List all active scheduled tasks for this chat."""

    @property
    def name(self) -> str:
        return "list_scheduled_tasks"

    @property
    def description(self) -> str:
        return "查看当前对话的所有活跃定时任务列表。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.db is None:
            return ToolResult(content="数据库未初始化", is_error=True)
        tasks = await context.db.get_active_tasks()
        # Filter to this chat
        my_tasks = [t for t in tasks if t["chat_id"] == context.chat_id]
        if not my_tasks:
            return ToolResult(content="当前没有活跃的定时任务")
        lines = ["=== 定时任务列表 ==="]
        for t in my_tasks:
            cron = t.get("cron_expression") or "一次性"
            lines.append(
                f"[{t['id']}] {t['description']}  ({cron})  下次: {t.get('next_run_at', 'N/A')}"
            )
        return ToolResult(content="\n".join(lines))


class CancelScheduledTaskTool(Tool):
    """Cancel an active scheduled task."""

    @property
    def name(self) -> str:
        return "cancel_scheduled_task"

    @property
    def description(self) -> str:
        return "取消一个定时任务（通过任务 ID）。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "要取消的任务 ID（从 list_scheduled_tasks 获取）",
                },
            },
            "required": ["task_id"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.db is None:
            return ToolResult(content="数据库未初始化", is_error=True)
        task_id = int(params.get("task_id", 0))
        if not task_id:
            return ToolResult(content="必须提供 task_id", is_error=True)
        await context.db.update_task_status(task_id, "cancelled")
        return ToolResult(content=f"任务 {task_id} 已取消")
