"""MCP (Model Context Protocol) tool federation.

Implements a lightweight JSON-RPC 2.0 client that connects to external
MCP servers (via HTTP) and exposes their tools through XClaw's ToolRegistry.

Protocol reference: https://modelcontextprotocol.io/specification

Design: see docs/wechat-design.md (architecture overview)

Quick start:
    # xclaw.config.yaml
    mcp_servers:
      - name: "my_tools"
        url: "http://localhost:3000"
        timeout: 10

    # runtime.py will call load_mcp_tools(settings, registry)
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from loguru import logger

from xclaw.tools import RiskLevel, Tool, ToolContext, ToolResult, ToolRegistry


class MCPClient:
    """Minimal MCP client speaking JSON-RPC 2.0 over HTTP.

    Supports:
    - ``initialize``    – capability negotiation
    - ``tools/list``    – discover available tools
    - ``tools/call``    – invoke a tool
    """

    JSONRPC_VERSION = "2.0"

    def __init__(self, name: str, url: str, timeout: float = 10.0) -> None:
        self.name = name
        self.url = url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)
        self._id: int = 0
        self._initialized = False

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _rpc(self, method: str, params: Any = None) -> Any:
        """Send a JSON-RPC 2.0 request and return the ``result`` field."""
        payload: dict[str, Any] = {
            "jsonrpc": self.JSONRPC_VERSION,
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        resp = await self._client.post(
            self.url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise ValueError(
                f"MCP server '{self.name}' returned error: {body['error']}"
            )
        return body.get("result")

    async def initialize(self) -> dict[str, Any]:
        """Perform the MCP initialize handshake."""
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "xclaw", "version": "0.1.0"},
            },
        )
        self._initialized = True
        # Send initialized notification (no response expected)
        try:
            await self._client.post(
                self.url,
                json={
                    "jsonrpc": self.JSONRPC_VERSION,
                    "method": "notifications/initialized",
                },
                headers={"Content-Type": "application/json"},
            )
        except Exception:  # noqa: BLE001
            pass
        return result or {}

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the list of tools provided by the MCP server."""
        result = await self._rpc("tools/list")
        if isinstance(result, dict):
            return result.get("tools", [])
        return result or []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool on the MCP server and return its content."""
        result = await self._rpc(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )
        return result

    async def close(self) -> None:
        await self._client.aclose()


class MCPToolAdapter(Tool):
    """Wraps a single MCP tool as an XClaw Tool.

    Forwards ``execute`` calls to the remote MCP server via ``tools/call``.
    """

    def __init__(
        self,
        client: MCPClient,
        tool_spec: dict[str, Any],
    ) -> None:
        self._client = client
        self._spec = tool_spec
        self._name = tool_spec["name"]
        self._description = tool_spec.get("description", f"MCP tool from {client.name}")
        # MCP uses inputSchema; fall back to empty object schema
        self._parameters: dict[str, Any] = tool_spec.get(
            "inputSchema",
            {"type": "object", "properties": {}},
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"[MCP:{self._client.name}] {self._description}"

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM  # external tools are medium-risk by default

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            result = await self._client.call_tool(self._name, params)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"MCPToolAdapter '{self._name}' error: {exc}")
            return ToolResult(content=f"MCP tool error: {exc}", is_error=True)

        # MCP result is {content: [{type: "text", text: "..."}], isError: bool}
        if isinstance(result, dict):
            is_error = result.get("isError", False)
            content_blocks = result.get("content", [])
            text_parts = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            text = "\n".join(text_parts) if text_parts else json.dumps(result)
            return ToolResult(content=text, is_error=is_error)

        return ToolResult(content=str(result))


async def load_mcp_tools(
    mcp_servers: list[dict[str, Any]],
    registry: ToolRegistry,
) -> list[MCPClient]:
    """Connect to all configured MCP servers and register their tools.

    Args:
        mcp_servers: List of server configs, each with keys:
                     ``name`` (str), ``url`` (str), ``timeout`` (float, optional)
        registry:    The ToolRegistry to register tools into.

    Returns:
        List of connected MCPClient instances (caller is responsible for closing).
    """
    clients: list[MCPClient] = []
    for cfg in mcp_servers:
        name = cfg.get("name", "mcp")
        url = cfg.get("url", "")
        timeout = float(cfg.get("timeout", 10.0))
        if not url:
            logger.warning(f"MCP server '{name}' has no URL, skipping")
            continue
        client = MCPClient(name=name, url=url, timeout=timeout)
        try:
            await client.initialize()
            tools = await client.list_tools()
            loaded = 0
            for tool_spec in tools:
                tool_name = tool_spec.get("name", "")
                if not tool_name:
                    continue
                adapter = MCPToolAdapter(client, tool_spec)
                try:
                    registry.register(adapter)
                    loaded += 1
                except ValueError:
                    logger.warning(
                        f"MCP tool '{tool_name}' from '{name}' conflicts with "
                        "an existing tool name, skipped"
                    )
            logger.info(f"MCP server '{name}': loaded {loaded} tool(s)")
            clients.append(client)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Failed to load MCP server '{name}' ({url}): {exc}")
            await client.close()
    return clients
