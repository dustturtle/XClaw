"""Tool base class, ToolContext, ToolResult, RiskLevel, and ToolRegistry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from xclaw.llm_types import ToolDefinition


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolResult:
    """Return type from Tool.execute()."""

    def __init__(self, content: str, is_error: bool = False) -> None:
        self.content = content
        self.is_error = is_error

    def __repr__(self) -> str:
        return f"ToolResult(is_error={self.is_error}, content={self.content!r})"


class ToolContext:
    """Context passed to every tool execution."""

    def __init__(
        self,
        chat_id: int,
        channel: str,
        llm: Any = None,
        db: Any = None,
        settings: Any = None,
        file_memory: Any = None,
        structured_memory: Any = None,
    ) -> None:
        self.chat_id = chat_id
        self.channel = channel
        self.llm = llm
        self.db = db
        self.settings = settings
        self.file_memory = file_memory
        self.structured_memory = structured_memory


class Tool(ABC):
    """Abstract base class for all XClaw tools."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name used in LLM tool definitions."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human/LLM-readable description of what the tool does."""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema object describing the tool's input parameters."""
        ...

    @property
    def risk_level(self) -> RiskLevel:
        """Risk level of this tool. Defaults to LOW."""
        return RiskLevel.LOW

    @abstractmethod
    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool with the given parameters and context."""
        ...

    def to_definition(self) -> ToolDefinition:
        """Convert this tool to an LLM-compatible ToolDefinition."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.parameters,
        )


class ToolRegistry:
    """Central registry that manages all available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool. Raises ValueError on duplicate names."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def get_definitions(self, exclude_high_risk: bool = False) -> list[ToolDefinition]:
        """Return tool definitions for the LLM, optionally excluding high-risk tools."""
        return [
            t.to_definition()
            for t in self._tools.values()
            if not (exclude_high_risk and t.risk_level == RiskLevel.HIGH)
        ]

    def get_openai_definitions(self, exclude_high_risk: bool = False) -> list[dict]:
        """Return tool definitions in OpenAI function-calling format."""
        return [d.to_openai_function() for d in self.get_definitions(exclude_high_risk)]

    def get_mcp_definitions(self, exclude_high_risk: bool = False) -> list[dict]:
        """Return tool definitions in MCP ``tools/list`` format."""
        return [d.to_mcp_tool() for d in self.get_definitions(exclude_high_risk)]

    async def execute(
        self, name: str, params: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Execute a registered tool by name."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(content=f"Unknown tool: {name}", is_error=True)
        try:
            return await tool.execute(params, context)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Tool '{name}' raised an error: {exc}", is_error=True)
