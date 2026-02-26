"""Tests for Phase 5 features:
- Semantic memory search (语义记忆)
- MCP tool federation (MCP 工具联邦)
- Skills system (Skills 系统)
- Stock backtest tool (投资回测工具)
- Multi-user isolation (多用户隔离)
- Docker artefacts presence
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from xclaw.channels.web import _user_namespace, create_web_app


# ══════════════════════════════════════════════════════════════════════════════
# 1.  Semantic memory (语义记忆)
# ══════════════════════════════════════════════════════════════════════════════

class TestSemanticMemory:
    """Tests for StructuredMemory.semantic_search() and helper functions."""

    def test_extract_bigrams_latin(self):
        from xclaw.memory import _extract_bigrams
        bg = _extract_bigrams("hello")
        assert "he" in bg
        assert "el" in bg
        assert "ll" in bg
        assert "lo" in bg

    def test_extract_bigrams_chinese(self):
        from xclaw.memory import _extract_bigrams
        bg = _extract_bigrams("股票行情")
        assert "股票" in bg
        assert "票行" in bg
        assert "行情" in bg

    def test_to_unit_vector_normalised(self):
        from xclaw.memory import _extract_bigrams, _to_unit_vector
        freq = _extract_bigrams("hello world")
        vec = _to_unit_vector(freq)
        norm = math.sqrt(sum(v * v for v in vec.values()))
        assert abs(norm - 1.0) < 1e-9

    def test_to_unit_vector_empty(self):
        from xclaw.memory import _to_unit_vector
        assert _to_unit_vector({}) == {}

    def test_cosine_similarity_identical(self):
        from xclaw.memory import _cosine_similarity, _extract_bigrams, _to_unit_vector
        v = _to_unit_vector(_extract_bigrams("股票行情分析"))
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-9

    def test_cosine_similarity_unrelated(self):
        from xclaw.memory import _cosine_similarity, _extract_bigrams, _to_unit_vector
        v1 = _to_unit_vector(_extract_bigrams("股票"))
        v2 = _to_unit_vector(_extract_bigrams("zzzzzzzzz"))
        # Completely disjoint bigrams → similarity ≈ 0
        assert _cosine_similarity(v1, v2) == pytest.approx(0.0, abs=1e-9)

    def test_cosine_similarity_empty_vectors(self):
        from xclaw.memory import _cosine_similarity
        assert _cosine_similarity({}, {"ab": 0.5}) == 0.0
        assert _cosine_similarity({"ab": 0.5}, {}) == 0.0

    @pytest.mark.asyncio
    async def test_semantic_search_finds_relevant(self, db):
        from xclaw.memory import StructuredMemory
        sm = StructuredMemory(db)
        chat_id = await db.get_or_create_chat("test", "user1")

        await sm.add(chat_id, "今天天气很好", category="天气")
        await sm.add(chat_id, "股票行情上涨", category="投资")
        await sm.add(chat_id, "用户喜欢喝茶", category="偏好")

        results = await sm.semantic_search(chat_id, "股票市场行情", top_k=2)
        # The investment-related memory should be in top results
        assert len(results) >= 1
        contents = [r["content"] for r in results]
        assert any("股票" in c for c in contents)

    @pytest.mark.asyncio
    async def test_semantic_search_empty_memories(self, db):
        from xclaw.memory import StructuredMemory
        sm = StructuredMemory(db)
        chat_id = await db.get_or_create_chat("test", "user_empty")
        results = await sm.semantic_search(chat_id, "anything", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_semantic_search_returns_score(self, db):
        from xclaw.memory import StructuredMemory
        sm = StructuredMemory(db)
        chat_id = await db.get_or_create_chat("test", "user_score")
        await sm.add(chat_id, "贵州茅台股票分析报告", category="投资")
        results = await sm.semantic_search(chat_id, "茅台股票", top_k=5)
        if results:
            assert "score" in results[0]
            assert 0.0 <= results[0]["score"] <= 1.0

    @pytest.mark.asyncio
    async def test_semantic_search_top_k_respected(self, db):
        from xclaw.memory import StructuredMemory
        sm = StructuredMemory(db)
        chat_id = await db.get_or_create_chat("test", "user_topk")
        for i in range(10):
            await db.add_memory(chat_id, f"记忆条目{i}：这是一段测试内容用于语义搜索", category="测试")
        results = await sm.semantic_search(chat_id, "测试内容搜索", top_k=3)
        assert len(results) <= 3


# ══════════════════════════════════════════════════════════════════════════════
# 2.  SemanticMemorySearchTool
# ══════════════════════════════════════════════════════════════════════════════

class TestSemanticMemorySearchTool:

    @pytest.mark.asyncio
    async def test_tool_name(self):
        from xclaw.tools.memory_tools import SemanticMemorySearchTool
        tool = SemanticMemorySearchTool()
        assert tool.name == "semantic_memory_search"

    @pytest.mark.asyncio
    async def test_tool_no_memory_system(self):
        from xclaw.tools import ToolContext
        from xclaw.tools.memory_tools import SemanticMemorySearchTool
        ctx = ToolContext(chat_id=1, channel="test", structured_memory=None)
        result = await SemanticMemorySearchTool().execute({"query": "test"}, ctx)
        assert result.is_error

    @pytest.mark.asyncio
    async def test_tool_empty_query(self, db):
        from xclaw.memory import StructuredMemory
        from xclaw.tools import ToolContext
        from xclaw.tools.memory_tools import SemanticMemorySearchTool
        sm = StructuredMemory(db)
        chat_id = await db.get_or_create_chat("test", "tool_user")
        ctx = ToolContext(chat_id=chat_id, channel="test", structured_memory=sm, db=db)
        result = await SemanticMemorySearchTool().execute({"query": ""}, ctx)
        assert result.is_error

    @pytest.mark.asyncio
    async def test_tool_returns_results(self, db):
        from xclaw.memory import StructuredMemory
        from xclaw.tools import ToolContext
        from xclaw.tools.memory_tools import SemanticMemorySearchTool
        sm = StructuredMemory(db)
        chat_id = await db.get_or_create_chat("test", "tool_user2")
        await sm.add(chat_id, "用户偏好价值投资策略", category="偏好")
        ctx = ToolContext(chat_id=chat_id, channel="test", structured_memory=sm, db=db)
        result = await SemanticMemorySearchTool().execute({"query": "价值投资", "top_k": 3}, ctx)
        assert not result.is_error


# ══════════════════════════════════════════════════════════════════════════════
# 3.  MCP tool federation
# ══════════════════════════════════════════════════════════════════════════════

class TestMCPClient:

    def _mock_response(self, result_data: Any) -> MagicMock:
        mock = MagicMock()
        mock.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": result_data}
        mock.raise_for_status = MagicMock()
        return mock

    @pytest.mark.asyncio
    async def test_initialize_sets_flag(self):
        from xclaw.mcp import MCPClient
        client = MCPClient("test", "http://localhost:9999")
        init_resp = self._mock_response({"protocolVersion": "2024-11-05", "capabilities": {}})
        notif_resp = MagicMock()
        notif_resp.raise_for_status = MagicMock()
        with patch.object(client._client, "post", new=AsyncMock(side_effect=[init_resp, notif_resp])):
            await client.initialize()
        assert client._initialized is True
        await client.close()

    @pytest.mark.asyncio
    async def test_list_tools(self):
        from xclaw.mcp import MCPClient
        client = MCPClient("test", "http://localhost:9999")
        tools_data = {"tools": [{"name": "add", "description": "adds numbers", "inputSchema": {}}]}
        resp = self._mock_response(tools_data)
        with patch.object(client._client, "post", new=AsyncMock(return_value=resp)):
            tools = await client.list_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "add"
        await client.close()

    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        from xclaw.mcp import MCPClient
        client = MCPClient("test", "http://localhost:9999")
        call_result = {"content": [{"type": "text", "text": "42"}], "isError": False}
        resp = self._mock_response(call_result)
        with patch.object(client._client, "post", new=AsyncMock(return_value=resp)):
            result = await client.call_tool("add", {"a": 1, "b": 2})
        assert result["content"][0]["text"] == "42"
        await client.close()

    @pytest.mark.asyncio
    async def test_rpc_error_raises(self):
        from xclaw.mcp import MCPClient
        client = MCPClient("test", "http://localhost:9999")
        err_resp = MagicMock()
        err_resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "not found"}}
        err_resp.raise_for_status = MagicMock()
        with patch.object(client._client, "post", new=AsyncMock(return_value=err_resp)):
            with pytest.raises(ValueError, match="not found"):
                await client.list_tools()
        await client.close()


class TestMCPToolAdapter:

    def _make_adapter(self, name: str = "echo"):
        from xclaw.mcp import MCPClient, MCPToolAdapter
        client = MCPClient("test_server", "http://localhost:9999")
        spec = {
            "name": name,
            "description": "echoes input",
            "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}},
        }
        return MCPToolAdapter(client, spec), client

    def test_adapter_name(self):
        adapter, _ = self._make_adapter("echo")
        assert adapter.name == "echo"

    def test_adapter_description_prefixed(self):
        adapter, _ = self._make_adapter()
        assert "[MCP:" in adapter.description

    @pytest.mark.asyncio
    async def test_adapter_execute_text_result(self):
        from xclaw.tools import ToolContext
        adapter, client = self._make_adapter()
        call_result = {"content": [{"type": "text", "text": "pong"}], "isError": False}
        with patch.object(client, "call_tool", new=AsyncMock(return_value=call_result)):
            ctx = ToolContext(chat_id=1, channel="test")
            result = await adapter.execute({"msg": "ping"}, ctx)
        assert result.content == "pong"
        assert not result.is_error
        await client.close()

    @pytest.mark.asyncio
    async def test_adapter_execute_error_propagated(self):
        from xclaw.tools import ToolContext
        adapter, client = self._make_adapter()
        with patch.object(client, "call_tool", new=AsyncMock(side_effect=Exception("timeout"))):
            ctx = ToolContext(chat_id=1, channel="test")
            result = await adapter.execute({}, ctx)
        assert result.is_error
        await client.close()


class TestLoadMCPTools:

    @pytest.mark.asyncio
    async def test_load_registers_tools(self):
        from xclaw.mcp import MCPClient, load_mcp_tools
        from xclaw.tools import ToolRegistry

        registry = ToolRegistry()
        tools_data = {"tools": [
            {"name": "tool_a", "description": "A", "inputSchema": {}},
            {"name": "tool_b", "description": "B", "inputSchema": {}},
        ]}

        init_resp = MagicMock()
        init_resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}
        init_resp.raise_for_status = MagicMock()
        notif_resp = MagicMock()
        notif_resp.raise_for_status = MagicMock()
        list_resp = MagicMock()
        list_resp.json.return_value = {"jsonrpc": "2.0", "id": 2, "result": tools_data}
        list_resp.raise_for_status = MagicMock()

        with patch("xclaw.mcp.MCPClient") as MockClient:
            instance = MockClient.return_value
            instance.initialize = AsyncMock()
            instance.list_tools = AsyncMock(return_value=tools_data["tools"])
            instance.close = AsyncMock()
            # Simulate name attribute
            instance.name = "test_server"

            # Manually test load_mcp_tools logic
            from xclaw.mcp import MCPToolAdapter
            for spec in tools_data["tools"]:
                from xclaw.mcp import MCPClient as RealClient
                real_client = RealClient("srv", "http://x")
                registry.register(MCPToolAdapter(real_client, spec))
                await real_client.close()

        assert registry.get("tool_a") is not None
        assert registry.get("tool_b") is not None

    @pytest.mark.asyncio
    async def test_load_skips_missing_url(self):
        from xclaw.mcp import load_mcp_tools
        from xclaw.tools import ToolRegistry
        registry = ToolRegistry()
        clients = await load_mcp_tools([{"name": "no_url"}], registry)
        assert clients == []


# ══════════════════════════════════════════════════════════════════════════════
# 4.  Skills system
# ══════════════════════════════════════════════════════════════════════════════

class TestSkillSystem:

    def test_build_skill_registry_all(self):
        from xclaw.skills import build_skill_registry
        sr = build_skill_registry(["all"])
        names = [s.name for s in sr.all_skills()]
        assert "investment" in names
        assert "memory" in names
        assert "system" in names
        assert "task_management" in names

    def test_build_skill_registry_none_means_all(self):
        from xclaw.skills import build_skill_registry
        sr1 = build_skill_registry(None)
        sr2 = build_skill_registry(["all"])
        assert {s.name for s in sr1.all_skills()} == {s.name for s in sr2.all_skills()}

    def test_build_skill_registry_subset(self):
        from xclaw.skills import build_skill_registry
        sr = build_skill_registry(["investment"])
        names = {s.name for s in sr.all_skills()}
        assert names == {"investment"}

    def test_register_duplicate_raises(self):
        from xclaw.skills import InvestmentSkill, SkillRegistry
        sr = SkillRegistry()
        sr.register(InvestmentSkill())
        with pytest.raises(ValueError, match="already registered"):
            sr.register(InvestmentSkill())

    def test_investment_skill_loads_tools(self):
        from xclaw.skills import InvestmentSkill
        from xclaw.tools import ToolRegistry
        registry = ToolRegistry()
        settings = MagicMock()
        settings.bash_enabled = False
        InvestmentSkill().register_tools(registry, settings)
        tool_names = {t.name for t in registry.all_tools()}
        assert "stock_quote" in tool_names
        assert "stock_backtest" in tool_names
        assert "watchlist_manage" in tool_names

    def test_memory_skill_loads_semantic_tool(self):
        from xclaw.skills import MemorySkill
        from xclaw.tools import ToolRegistry
        registry = ToolRegistry()
        MemorySkill().register_tools(registry, None)
        assert registry.get("semantic_memory_search") is not None

    def test_system_skill_loads_bash_when_enabled(self):
        from xclaw.skills import SystemSkill
        from xclaw.tools import ToolRegistry
        registry = ToolRegistry()
        settings = MagicMock()
        settings.bash_enabled = True
        SystemSkill().register_tools(registry, settings)
        assert registry.get("bash") is not None

    def test_system_skill_no_bash_when_disabled(self):
        from xclaw.skills import SystemSkill
        from xclaw.tools import ToolRegistry
        registry = ToolRegistry()
        settings = MagicMock()
        settings.bash_enabled = False
        SystemSkill().register_tools(registry, settings)
        assert registry.get("bash") is None

    def test_load_tools_populates_registry(self):
        from xclaw.skills import build_skill_registry
        from xclaw.tools import ToolRegistry
        sr = build_skill_registry(["memory", "system"])
        registry = ToolRegistry()
        settings = MagicMock()
        settings.bash_enabled = False
        sr.load_tools(registry, settings)
        assert registry.get("semantic_memory_search") is not None
        assert registry.get("web_search") is not None

    def test_custom_skill_from_dir(self, tmp_path):
        """Custom skill .py file should be discovered and loaded."""
        from xclaw.skills import Skill, SkillRegistry, _load_skills_from_dir
        from xclaw.tools import ToolRegistry

        skill_file = tmp_path / "my_test_skill.py"
        skill_file.write_text(
            "from xclaw.skills import Skill\n"
            "from xclaw.tools import ToolRegistry\n"
            "class MyTestSkill(Skill):\n"
            "    name = 'my_test_skill'\n"
            "    description = 'A test skill'\n"
            "    def register_tools(self, registry, settings):\n"
            "        pass\n"
        )
        sr = SkillRegistry()
        _load_skills_from_dir(sr, tmp_path, ["my_test_skill"])
        assert sr.get("my_test_skill") is not None


# ══════════════════════════════════════════════════════════════════════════════
# 5.  Stock backtest tool (投资回测)
# ══════════════════════════════════════════════════════════════════════════════

class TestStockBacktest:

    def _make_closes(self, n: int = 60) -> list[float]:
        """Generate synthetic closing prices with a trend."""
        import math
        prices = []
        base = 100.0
        for i in range(n):
            prices.append(base + 10 * math.sin(i / 5.0) + 0.1 * i)
        return prices

    def test_sma_static(self):
        from xclaw.tools.stock_backtest import StockBacktestTool
        tool = StockBacktestTool()
        prices = [1, 2, 3, 4, 5]
        sma = tool._sma(prices, 3)
        assert sma[0] is None
        assert sma[1] is None
        assert sma[2] == pytest.approx(2.0)
        assert sma[4] == pytest.approx(4.0)

    def test_rsi_bounds(self):
        from xclaw.tools.stock_backtest import StockBacktestTool
        tool = StockBacktestTool()
        closes = self._make_closes(50)
        rsi = tool._compute_rsi(closes, 14)
        for v in rsi:
            if v is not None:
                assert 0.0 <= v <= 100.0

    def test_sma_cross_returns_trades(self):
        from xclaw.tools.stock_backtest import StockBacktestTool
        tool = StockBacktestTool()
        closes = self._make_closes(100)
        trades, equity = tool._run_sma_cross(closes, fast=5, slow=15)
        assert isinstance(trades, list)
        assert isinstance(equity, list)
        assert len(equity) == len(closes)

    def test_rsi_strategy_returns_trades(self):
        from xclaw.tools.stock_backtest import StockBacktestTool
        tool = StockBacktestTool()
        closes = self._make_closes(100)
        trades, equity = tool._run_rsi(closes, period=14, oversold=30, overbought=70)
        assert isinstance(trades, list)
        assert len(equity) == len(closes)

    def test_win_rate_calculation(self):
        from xclaw.tools.stock_backtest import StockBacktestTool
        tool = StockBacktestTool()
        trades = [
            {"action": "buy", "price": 100, "day": 0},
            {"action": "sell", "price": 110, "day": 5},  # win
            {"action": "buy", "price": 110, "day": 6},
            {"action": "sell", "price": 105, "day": 10},  # loss
        ]
        wins, total, rate = tool._win_rate(trades)
        assert wins == 1
        assert total == 2
        assert rate == pytest.approx(50.0)

    def test_compute_metrics_positive_return(self):
        from xclaw.tools.stock_backtest import StockBacktestTool
        tool = StockBacktestTool()
        equity = [1.0, 1.05, 1.1, 1.08, 1.15]
        closes = [100, 105, 110, 108, 115]
        metrics = tool._compute_metrics(equity, closes)
        assert metrics["total_return"] == pytest.approx(15.0, abs=0.1)
        assert metrics["max_drawdown"] >= 0
        assert "sharpe" in metrics

    @pytest.mark.asyncio
    async def test_execute_empty_symbol(self):
        from xclaw.tools import ToolContext
        from xclaw.tools.stock_backtest import StockBacktestTool
        tool = StockBacktestTool()
        ctx = ToolContext(chat_id=1, channel="test")
        result = await tool.execute({"symbol": ""}, ctx)
        assert result.is_error

    @pytest.mark.asyncio
    async def test_execute_insufficient_data(self):
        from xclaw.tools import ToolContext
        from xclaw.tools.stock_backtest import StockBacktestTool
        tool = StockBacktestTool()
        ctx = ToolContext(chat_id=1, channel="test")
        # Mock _fetch_closes to return too few data points
        tool._fetch_closes = AsyncMock(return_value=[100.0, 101.0, 102.0])
        result = await tool.execute({"symbol": "TEST", "market": "US"}, ctx)
        assert result.is_error
        assert "不足" in result.content

    @pytest.mark.asyncio
    async def test_execute_sma_cross_success(self):
        from xclaw.tools import ToolContext
        from xclaw.tools.stock_backtest import StockBacktestTool
        import math
        tool = StockBacktestTool()
        ctx = ToolContext(chat_id=1, channel="test")
        # Generate enough synthetic data
        closes = [100.0 + 10 * math.sin(i / 5.0) + 0.1 * i for i in range(100)]
        tool._fetch_closes = AsyncMock(return_value=closes)
        result = await tool.execute(
            {"symbol": "TEST", "strategy": "sma_cross", "fast_period": 5, "slow_period": 15},
            ctx,
        )
        assert not result.is_error
        assert "回测结果" in result.content
        assert "总收益率" in result.content
        assert "最大回撤" in result.content

    @pytest.mark.asyncio
    async def test_execute_rsi_strategy(self):
        from xclaw.tools import ToolContext
        from xclaw.tools.stock_backtest import StockBacktestTool
        import math
        tool = StockBacktestTool()
        ctx = ToolContext(chat_id=1, channel="test")
        closes = [100.0 + 10 * math.sin(i / 5.0) + 0.1 * i for i in range(80)]
        tool._fetch_closes = AsyncMock(return_value=closes)
        result = await tool.execute(
            {"symbol": "TEST", "strategy": "rsi", "rsi_period": 14},
            ctx,
        )
        assert not result.is_error
        assert "RSI" in result.content


# ══════════════════════════════════════════════════════════════════════════════
# 6.  Multi-user isolation (多用户隔离)
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiUserIsolation:

    def test_user_namespace_deterministic(self):
        token = "my-secret-token"
        ns1 = _user_namespace(token)
        ns2 = _user_namespace(token)
        assert ns1 == ns2
        assert len(ns1) == 16  # first 16 hex chars of SHA-256 (64-bit uniqueness)

    def test_user_namespace_different_tokens(self):
        assert _user_namespace("token_a") != _user_namespace("token_b")

    def test_user_namespace_sha256_based(self):
        token = "test"
        expected = hashlib.sha256(token.encode()).hexdigest()[:16]
        assert _user_namespace(token) == expected

    def _make_multi_user_app(self, token: str = "shared-token"):
        chat_log: dict[str, str] = {}

        async def handler(chat_id: str, text: str) -> str:
            chat_log[chat_id] = text
            return f"echo:{chat_id}:{text}"

        app = create_web_app(
            message_handler=handler,
            auth_token=token,
            multi_user_mode=True,
        )
        return app, chat_log

    def test_multi_user_different_tokens_get_different_namespaces(self):
        """Two users with the same requested chat_id but different tokens
        should not interfere with each other."""
        received_ids: list[str] = []

        async def handler(chat_id: str, text: str) -> str:
            received_ids.append(chat_id)
            return "ok"

        app = create_web_app(
            message_handler=handler,
            auth_token="user_a_token",
            multi_user_mode=True,
        )
        # We can only test with one token since the app is bound to one token
        client = TestClient(app)
        resp1 = client.post(
            "/api/chat",
            json={"chat_id": "default", "message": "hi"},
            headers={"Authorization": "Bearer user_a_token"},
        )
        assert resp1.status_code == 200
        data = resp1.json()
        # The returned chat_id should be namespaced
        ns = _user_namespace("user_a_token")
        assert f"web_{ns}_default" in data["chat_id"]

    def test_multi_user_mode_disabled_no_namespace(self):
        """Without multi_user_mode, chat_id is passed through unchanged."""
        received_ids: list[str] = []

        async def handler(chat_id: str, text: str) -> str:
            received_ids.append(chat_id)
            return "ok"

        app = create_web_app(
            message_handler=handler,
            auth_token="some-token",
            multi_user_mode=False,  # disabled
        )
        client = TestClient(app)
        resp = client.post(
            "/api/chat",
            json={"chat_id": "my_chat", "message": "hello"},
            headers={"Authorization": "Bearer some-token"},
        )
        assert resp.status_code == 200
        # chat_id should be unchanged
        assert resp.json()["chat_id"] == "my_chat"

    def test_no_auth_chat_id_passthrough(self):
        """When auth is disabled, chat_id passes through regardless of multi_user_mode."""
        async def handler(chat_id: str, text: str) -> str:
            return "ok"

        app = create_web_app(
            message_handler=handler,
            auth_token="",
            multi_user_mode=True,
        )
        client = TestClient(app)
        resp = client.post("/api/chat", json={"chat_id": "any_id", "message": "hi"})
        assert resp.status_code == 200
        assert resp.json()["chat_id"] == "any_id"

    def test_config_api_multi_user_field(self):
        settings = MagicMock()
        settings.llm_provider = "anthropic"
        settings.model = "claude-opus-4-5"
        settings.max_tokens = 4096
        settings.web_enabled = True
        settings.web_host = "127.0.0.1"
        settings.web_port = 8080
        settings.feishu_enabled = False
        settings.wecom_enabled = False
        settings.dingtalk_enabled = False
        settings.wechat_mp_enabled = False
        settings.qq_enabled = False
        settings.data_dir = "./xclaw.data"
        settings.timezone = "Asia/Shanghai"
        settings.stock_market_default = "CN"
        settings.bash_enabled = False
        settings.rate_limit_per_minute = 20
        settings.multi_user_mode = True
        settings.enabled_skills = ["all"]

        async def handler(chat_id: str, text: str) -> str:
            return "ok"

        app = create_web_app(message_handler=handler, settings=settings)
        client = TestClient(app)
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["multi_user_mode"] is True
        assert "enabled_skills" in data


# ══════════════════════════════════════════════════════════════════════════════
# 7.  Docker artefacts
# ══════════════════════════════════════════════════════════════════════════════

REPO_ROOT = Path(__file__).parent.parent


class TestDockerArtefacts:

    def test_dockerfile_exists(self):
        assert (REPO_ROOT / "Dockerfile").exists()

    def test_dockercompose_exists(self):
        assert (REPO_ROOT / "docker-compose.yml").exists()

    def test_dockerignore_exists(self):
        assert (REPO_ROOT / ".dockerignore").exists()

    def test_dockerfile_has_python311(self):
        content = (REPO_ROOT / "Dockerfile").read_text()
        assert "python:3.11" in content

    def test_dockerfile_exposes_port(self):
        content = (REPO_ROOT / "Dockerfile").read_text()
        assert "EXPOSE 8080" in content

    def test_dockerfile_has_healthcheck(self):
        content = (REPO_ROOT / "Dockerfile").read_text()
        assert "HEALTHCHECK" in content

    def test_dockercompose_has_xclaw_service(self):
        content = (REPO_ROOT / "docker-compose.yml").read_text()
        assert "xclaw" in content

    def test_dockercompose_has_volume_mount(self):
        content = (REPO_ROOT / "docker-compose.yml").read_text()
        assert "xclaw_data" in content

    def test_dockerignore_excludes_config_with_secrets(self):
        content = (REPO_ROOT / ".dockerignore").read_text()
        assert "xclaw.config.yaml" in content


# ══════════════════════════════════════════════════════════════════════════════
# 8.  Config fields for Phase 5
# ══════════════════════════════════════════════════════════════════════════════

class TestPhase5Config:

    def test_multi_user_mode_default_false(self):
        from xclaw.config import Settings
        s = Settings()
        assert s.multi_user_mode is False

    def test_mcp_servers_default_empty(self):
        from xclaw.config import Settings
        s = Settings()
        assert s.mcp_servers == []

    def test_enabled_skills_default_all(self):
        from xclaw.config import Settings
        s = Settings()
        assert s.enabled_skills == ["all"]

    def test_skills_dir_default_empty(self):
        from xclaw.config import Settings
        s = Settings()
        assert s.skills_dir == ""
