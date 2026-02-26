"""Tools: read_memory and write_memory (file-based AGENTS.md memory)."""

from __future__ import annotations

from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


class ReadMemoryTool(Tool):
    """Read the agent's AGENTS.md memory file for this chat."""

    @property
    def name(self) -> str:
        return "read_memory"

    @property
    def description(self) -> str:
        return "读取当前对话的记忆文件（AGENTS.md），用于了解已记录的用户偏好和信息。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.file_memory is None:
            return ToolResult(content="记忆系统未启用", is_error=True)
        content = context.file_memory.read(context.chat_id)
        return ToolResult(content=content if content else "（记忆文件为空）")


class WriteMemoryTool(Tool):
    """Overwrite the agent's AGENTS.md memory file for this chat."""

    @property
    def name(self) -> str:
        return "write_memory"

    @property
    def description(self) -> str:
        return "更新当前对话的记忆文件（AGENTS.md），将重要信息持久化以供未来参考。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要写入记忆文件的完整内容（Markdown 格式）",
                },
            },
            "required": ["content"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.file_memory is None:
            return ToolResult(content="记忆系统未启用", is_error=True)
        content = params.get("content", "")
        context.file_memory.write(context.chat_id, content)
        return ToolResult(content="记忆文件已更新")


class StructuredMemoryReadTool(Tool):
    """Query the structured memory (SQLite memories table)."""

    @property
    def name(self) -> str:
        return "structured_memory_read"

    @property
    def description(self) -> str:
        return "读取当前对话的结构化记忆列表（分类存储的事实）。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.structured_memory is None:
            return ToolResult(content="结构化记忆系统未启用", is_error=True)
        memories = await context.structured_memory.get_all(context.chat_id)
        if not memories:
            return ToolResult(content="（无结构化记忆）")
        lines = [f"[{m.get('category', '未分类')}] {m['content']}" for m in memories]
        return ToolResult(content="\n".join(lines))


class StructuredMemoryUpdateTool(Tool):
    """Add a new fact to the structured memory."""

    @property
    def name(self) -> str:
        return "structured_memory_update"

    @property
    def description(self) -> str:
        return "向结构化记忆中添加一条新的事实记录（自动去重）。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要记录的事实内容",
                },
                "category": {
                    "type": "string",
                    "description": "分类标签（如 '偏好'、'持仓'、'目标'）",
                },
                "confidence": {
                    "type": "number",
                    "description": "置信度 0.0-1.0（默认 0.8）",
                    "default": 0.8,
                },
            },
            "required": ["content"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.structured_memory is None:
            return ToolResult(content="结构化记忆系统未启用", is_error=True)
        content = params.get("content", "").strip()
        if not content:
            return ToolResult(content="内容不能为空", is_error=True)
        memory_id = await context.structured_memory.add(
            context.chat_id,
            content,
            category=params.get("category"),
            confidence=float(params.get("confidence", 0.8)),
        )
        if memory_id is None:
            return ToolResult(content="相似记忆已存在，已跳过（自动去重）")
        return ToolResult(content=f"记忆已保存 (id={memory_id})")
