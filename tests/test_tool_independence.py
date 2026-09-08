"""Independence is reviewed opt-in metadata, never inferred from read-only."""

from xclaw.tools import Tool, ToolResult
from xclaw.tools.stock_correlation import StockCorrelationTool
from xclaw.tools.stock_history import StockHistoryTool
from xclaw.tools.stock_indicators import StockIndicatorsTool
from xclaw.tools.stock_quote import StockQuoteTool
from xclaw.tools.web_search import WebSearchTool


class ReadOnlyProbe(Tool):
    name = "probe"
    description = "read-only but not audited for parallel execution"
    parameters = {"type": "object"}
    read_only = True

    async def execute(self, params, context):
        return ToolResult("ok")


def test_read_only_does_not_imply_independent():
    assert Tool.independent is False
    assert ReadOnlyProbe().independent is False


def test_reviewed_stateless_tools_explicitly_opt_in():
    for tool_type in (StockQuoteTool, StockHistoryTool, StockIndicatorsTool, WebSearchTool):
        assert tool_type.__dict__["independent"] is True
        assert tool_type().read_only is True


def test_correlation_with_optional_file_export_is_not_independent():
    assert StockCorrelationTool().independent is False

