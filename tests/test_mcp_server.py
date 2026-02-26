"""Tests for MCP server, OpenAI format conversion, and protocol compatibility."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from xclaw.llm_types import ToolDefinition
from xclaw.tools import RiskLevel, Tool, ToolContext, ToolRegistry, ToolResult


# ── Helpers ───────────────────────────────────────────────────────────────────

class DummyTool(Tool):
    """Simple tool for testing protocol export."""

    @property
    def name(self) -> str:
        return "dummy_echo"

    @property
    def description(self) -> str:
        return "Echo the input text"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo"}},
            "required": ["text"],
        }

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        return ToolResult(content=params.get("text", ""))


class HighRiskTool(Tool):
    """High-risk tool that should be excluded from MCP server."""

    @property
    def name(self) -> str:
        return "dangerous_op"

    @property
    def description(self) -> str:
        return "A dangerous operation"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.HIGH

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        return ToolResult(content="done")


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(DummyTool())
    reg.register(HighRiskTool())
    return reg


# ── ToolDefinition format conversion ─────────────────────────────────────────

class TestToolDefinitionFormats:

    def test_to_openai_function(self):
        td = ToolDefinition(
            name="my_tool",
            description="Does things",
            input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
        )
        result = td.to_openai_function()
        assert result["type"] == "function"
        assert result["function"]["name"] == "my_tool"
        assert result["function"]["description"] == "Does things"
        assert result["function"]["parameters"]["properties"]["x"]["type"] == "integer"

    def test_to_mcp_tool(self):
        td = ToolDefinition(
            name="my_tool",
            description="Does things",
            input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
        )
        result = td.to_mcp_tool()
        assert result["name"] == "my_tool"
        assert result["description"] == "Does things"
        assert result["inputSchema"]["properties"]["x"]["type"] == "integer"

    def test_from_openai_function_full(self):
        data = {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search the web",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
        td = ToolDefinition.from_openai_function(data)
        assert td.name == "search"
        assert td.description == "Search the web"
        assert "query" in td.input_schema["properties"]

    def test_from_openai_function_inner(self):
        data = {
            "name": "calc",
            "description": "Calculate",
            "parameters": {"type": "object", "properties": {}},
        }
        td = ToolDefinition.from_openai_function(data)
        assert td.name == "calc"
        assert td.description == "Calculate"

    def test_from_mcp_tool(self):
        data = {
            "name": "fetch_data",
            "description": "Fetch data from API",
            "inputSchema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        }
        td = ToolDefinition.from_mcp_tool(data)
        assert td.name == "fetch_data"
        assert td.description == "Fetch data from API"
        assert "url" in td.input_schema["properties"]

    def test_roundtrip_openai(self):
        original = ToolDefinition(
            name="roundtrip",
            description="Test roundtrip",
            input_schema={"type": "object", "properties": {"a": {"type": "string"}}},
        )
        exported = original.to_openai_function()
        restored = ToolDefinition.from_openai_function(exported)
        assert restored.name == original.name
        assert restored.description == original.description
        assert restored.input_schema == original.input_schema

    def test_roundtrip_mcp(self):
        original = ToolDefinition(
            name="roundtrip",
            description="Test roundtrip",
            input_schema={"type": "object", "properties": {"b": {"type": "number"}}},
        )
        exported = original.to_mcp_tool()
        restored = ToolDefinition.from_mcp_tool(exported)
        assert restored.name == original.name
        assert restored.description == original.description
        assert restored.input_schema == original.input_schema


# ── ToolRegistry format helpers ───────────────────────────────────────────────

class TestToolRegistryFormats:

    def test_get_openai_definitions(self):
        reg = _make_registry()
        defs = reg.get_openai_definitions()
        assert len(defs) == 2
        assert all(d["type"] == "function" for d in defs)
        names = {d["function"]["name"] for d in defs}
        assert "dummy_echo" in names

    def test_get_openai_definitions_exclude_high_risk(self):
        reg = _make_registry()
        defs = reg.get_openai_definitions(exclude_high_risk=True)
        names = {d["function"]["name"] for d in defs}
        assert "dummy_echo" in names
        assert "dangerous_op" not in names

    def test_get_mcp_definitions(self):
        reg = _make_registry()
        defs = reg.get_mcp_definitions()
        assert len(defs) == 2
        assert all("inputSchema" in d for d in defs)
        names = {d["name"] for d in defs}
        assert "dummy_echo" in names

    def test_get_mcp_definitions_exclude_high_risk(self):
        reg = _make_registry()
        defs = reg.get_mcp_definitions(exclude_high_risk=True)
        names = {d["name"] for d in defs}
        assert "dummy_echo" in names
        assert "dangerous_op" not in names


# ── MCP Server endpoint ──────────────────────────────────────────────────────

@pytest.fixture
def mcp_app():
    """Create a minimal FastAPI app with the MCP server router."""
    from fastapi import FastAPI
    from xclaw.mcp_server import create_mcp_server_router

    reg = _make_registry()
    app = FastAPI()
    router = create_mcp_server_router(reg)
    app.include_router(router, prefix="/mcp")
    return app


@pytest_asyncio.fixture
async def mcp_client(mcp_app):
    transport = ASGITransport(app=mcp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_mcp_initialize(mcp_client):
    resp = await mcp_client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test_client", "version": "0.1"},
        },
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 1
    result = data["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "xclaw"


@pytest.mark.asyncio
async def test_mcp_tools_list(mcp_client):
    resp = await mcp_client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
    })
    assert resp.status_code == 200
    data = resp.json()
    result = data["result"]
    tools = result["tools"]
    # High-risk tools should be excluded
    names = {t["name"] for t in tools}
    assert "dummy_echo" in names
    assert "dangerous_op" not in names
    # Validate MCP tool schema
    echo_tool = next(t for t in tools if t["name"] == "dummy_echo")
    assert "inputSchema" in echo_tool
    assert echo_tool["description"] == "Echo the input text"


@pytest.mark.asyncio
async def test_mcp_tools_call(mcp_client):
    resp = await mcp_client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "dummy_echo",
            "arguments": {"text": "hello from MCP"},
        },
    })
    assert resp.status_code == 200
    data = resp.json()
    result = data["result"]
    assert result["isError"] is False
    content = result["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "hello from MCP"


@pytest.mark.asyncio
async def test_mcp_tools_call_unknown(mcp_client):
    resp = await mcp_client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "nonexistent_tool",
            "arguments": {},
        },
    })
    assert resp.status_code == 200
    data = resp.json()
    result = data["result"]
    assert result["isError"] is True


@pytest.mark.asyncio
async def test_mcp_method_not_found(mcp_client):
    resp = await mcp_client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 5,
        "method": "unknown/method",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_mcp_notification(mcp_client):
    """Notifications (no id) should be acknowledged silently."""
    resp = await mcp_client.post("/mcp", json={
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_mcp_invalid_json(mcp_client):
    resp = await mcp_client.post(
        "/mcp",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_mcp_tools_call_missing_name(mcp_client):
    resp = await mcp_client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {"arguments": {}},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == -32602


# ── Web app integration ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_web_app_mcp_enabled():
    """When mcp_server_enabled is True and registry is provided, /mcp is mounted."""
    from types import SimpleNamespace
    from xclaw.channels.web import create_web_app

    async def noop_handler(cid, text):
        return "ok"

    settings = SimpleNamespace(mcp_server_enabled=True)
    reg = _make_registry()

    app = create_web_app(
        message_handler=noop_handler,
        settings=settings,
        tool_registry=reg,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "tools" in data["result"]


@pytest.mark.asyncio
async def test_web_app_mcp_disabled():
    """When mcp_server_enabled is False, /mcp should not be available."""
    from types import SimpleNamespace
    from xclaw.channels.web import create_web_app

    async def noop_handler(cid, text):
        return "ok"

    settings = SimpleNamespace(mcp_server_enabled=False)
    reg = _make_registry()

    app = create_web_app(
        message_handler=noop_handler,
        settings=settings,
        tool_registry=reg,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
        })
        # Should return 404/405 since the router isn't mounted
        assert resp.status_code in (404, 405, 422)


# ── Config ────────────────────────────────────────────────────────────────────

def test_config_mcp_server_default():
    """mcp_server_enabled should default to False."""
    from xclaw.config import Settings

    s = Settings(api_key="test")
    assert s.mcp_server_enabled is False
