"""Bash execution tool (optional, disabled by default).

Only active when ``bash_enabled = true`` in config. Runs shell commands in a
restricted subprocess and returns stdout/stderr. Classified as HIGH risk.
"""

from __future__ import annotations

import asyncio
from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult


# Maximum output characters returned to the LLM
_MAX_OUTPUT = 4000
# Command execution timeout in seconds
_TIMEOUT = 30


class BashTool(Tool):
    """Execute a shell command and return the output.

    Disabled unless ``settings.bash_enabled`` is ``True``.
    Classified as HIGH risk – should only be enabled in trusted environments.
    """

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "在 shell 中执行命令并返回输出。"
            "仅在配置文件中显式启用（bash_enabled: true）时可用。"
            "请谨慎使用，避免破坏性操作。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"超时秒数（默认 {_TIMEOUT}，最大 120）",
                },
            },
            "required": ["command"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.HIGH

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        # Check if bash is enabled in settings
        if context.settings is None or not getattr(context.settings, "bash_enabled", False):
            return ToolResult(
                content="Bash 工具未启用。请在配置文件中设置 bash_enabled: true。",
                is_error=True,
            )

        command = params.get("command", "").strip()
        if not command:
            return ToolResult(content="command 不能为空", is_error=True)

        timeout = min(int(params.get("timeout", _TIMEOUT)), 120)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolResult(
                    content=f"命令超时（{timeout}s）：{command}",
                    is_error=True,
                )

            output = stdout.decode("utf-8", errors="replace")
            if len(output) > _MAX_OUTPUT:
                output = output[:_MAX_OUTPUT] + f"\n…（输出已截断，共 {len(output)} 字符）"

            exit_code = proc.returncode
            if exit_code != 0:
                return ToolResult(
                    content=f"退出码 {exit_code}:\n{output}",
                    is_error=True,
                )
            return ToolResult(content=output or "(无输出)")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"执行失败: {exc}", is_error=True)
