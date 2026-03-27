"""Sub-agent tool: spawns a nested Agent loop with a restricted tool set.

Allows the main agent to delegate focused sub-tasks (e.g. stock lookups,
web searches) to an inner agent that only has access to a safe subset of
tools. This prevents the sub-agent from accidentally modifying user data
(no write_file, no portfolio_manage with write actions, etc.).
"""

from __future__ import annotations

from typing import Any

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolRegistry, ToolResult

# Names of tools the sub-agent is allowed to use
_ALLOWED_SUB_AGENT_TOOLS: frozenset[str] = frozenset(
    {
        "web_search",
        "web_fetch",
        "stock_quote",
        "stock_gap_analysis",
        "stock_history",
        "stock_indicators",
        "stock_fundamentals",
        "stock_news",
        "market_overview",
        "read_file",
        "structured_memory_read",
    }
)


class SubAgentTool(Tool):
    """Delegate a sub-task to a nested agent with a restricted tool set.

    The parent agent provides a ``task`` prompt. The sub-agent runs a full
    agent loop using only the allowed (read-only) tools and returns its
    final text response.
    """

    def __init__(self, parent_registry: ToolRegistry) -> None:
        self._parent_registry = parent_registry

    @property
    def name(self) -> str:
        return "sub_agent"

    @property
    def description(self) -> str:
        return (
            "仅用于需要多步骤、多工具协作的复杂研究任务（如对比多只股票、综合多数据源交叉验证）。"
            "单一数据查询+分析（如单只股票/指数的K线缺口分析、技术指标计算、行情查询）"
            "请直接调用对应的数据工具后自行分析，不要委托给 sub_agent。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要委托给子 Agent 的任务描述",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "子 Agent 最大工具调用轮数（默认 5）",
                },
            },
            "required": ["task"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        task = params.get("task", "").strip()
        if not task:
            return ToolResult(content="task 不能为空", is_error=True)

        max_iter = min(int(params.get("max_iterations", 5)), 5)

        # Build a restricted registry from the parent's tools
        sub_registry = ToolRegistry()
        for tool in self._parent_registry.all_tools():
            if tool.name in _ALLOWED_SUB_AGENT_TOOLS:
                try:
                    sub_registry.register(tool)
                except ValueError:
                    pass  # already registered

        # Retrieve the LLM from context if available
        # The LLM reference is passed via a duck-typed attribute set by the runtime
        # when building the ToolContext.
        llm = getattr(context, "llm", None)
        if llm is None:
            return ToolResult(content="LLM 未配置，无法运行子 Agent", is_error=True)

        # Late import to avoid circular dependency
        from xclaw.agent_engine import AgentContext, agent_loop

        if context.db is None:
            return ToolResult(content="数据库未初始化，无法运行子 Agent", is_error=True)

        sub_ctx = AgentContext(
            chat_id=context.chat_id,
            channel=context.channel,
            db=context.db,
            llm=llm,
            tools=sub_registry,
            structured_memory=context.structured_memory,
            settings=context.settings,
            skip_session_persistence=True,
            skip_message_persistence=True,
        )

        try:
            result = await agent_loop(sub_ctx, task, max_iterations=max_iter)
            return ToolResult(content=result)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"子 Agent 执行失败: {exc}", is_error=True)
