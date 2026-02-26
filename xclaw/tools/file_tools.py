"""Tools: read_file and write_file with path guard protection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult
from xclaw.tools.path_guard import assert_path_safe


class ReadFileTool(Tool):
    """Read the contents of a local file."""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "读取本地文件内容并返回文本。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（相对或绝对路径）",
                },
            },
            "required": ["path"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        path_str = params.get("path", "").strip()
        if not path_str:
            return ToolResult(content="路径不能为空", is_error=True)
        try:
            assert_path_safe(path_str)
        except PermissionError as exc:
            return ToolResult(content=str(exc), is_error=True)
        try:
            content = Path(path_str).read_text(encoding="utf-8")
            return ToolResult(content=content)
        except FileNotFoundError:
            return ToolResult(content=f"文件不存在: {path_str}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"读取文件失败: {exc}", is_error=True)


class WriteFileTool(Tool):
    """Write text content to a local file."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "将文本内容写入本地文件（会覆盖已有内容）。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文本内容",
                },
            },
            "required": ["path", "content"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        path_str = params.get("path", "").strip()
        content = params.get("content", "")
        if not path_str:
            return ToolResult(content="路径不能为空", is_error=True)
        try:
            assert_path_safe(path_str)
        except PermissionError as exc:
            return ToolResult(content=str(exc), is_error=True)
        try:
            p = Path(path_str)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return ToolResult(content=f"成功写入 {path_str}（{len(content)} 字节）")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"写入文件失败: {exc}", is_error=True)
