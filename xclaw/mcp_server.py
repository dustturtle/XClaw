"""MCP Server – expose XClaw tools via the Model Context Protocol.

Implements a JSON-RPC 2.0 endpoint that lets any MCP-compatible client
(Claude Desktop, other agents, etc.) discover and call XClaw's tools.

Protocol reference: https://modelcontextprotocol.io/specification

Quick start:
    # xclaw.config.yaml
    mcp_server_enabled: true   # expose /mcp endpoint

    # runtime.py will mount the MCP server router on the web app.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger

from xclaw.tools import ToolContext, ToolRegistry

PROTOCOL_VERSION = "2024-11-05"

_SERVER_INFO = {
    "name": "xclaw",
    "version": "0.1.0",
}


def _jsonrpc_error(
    req_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def create_mcp_server_router(
    registry: ToolRegistry,
    tool_context_factory: Any = None,
) -> APIRouter:
    """Create a FastAPI router implementing MCP server protocol.

    Args:
        registry:             The ToolRegistry containing all available tools.
        tool_context_factory: Optional callable ``() -> ToolContext`` that produces
                              a context for tool execution.  When ``None`` a
                              minimal default context is used.

    Returns:
        A FastAPI ``APIRouter`` to be mounted on the web application.
    """
    router = APIRouter()

    def _default_tool_context() -> ToolContext:
        return ToolContext(chat_id=0, channel="mcp")

    get_ctx = tool_context_factory or _default_tool_context

    # ── JSON-RPC 2.0 dispatcher ──────────────────────────────────────────────

    async def _handle_initialize(req_id: Any, _params: Any) -> JSONResponse:
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": _SERVER_INFO,
        }
        return JSONResponse(_jsonrpc_result(req_id, result))

    async def _handle_tools_list(req_id: Any, _params: Any) -> JSONResponse:
        tools = registry.get_mcp_definitions(exclude_high_risk=True)
        return JSONResponse(_jsonrpc_result(req_id, {"tools": tools}))

    async def _handle_tools_call(req_id: Any, params: Any) -> JSONResponse:
        if not isinstance(params, dict):
            return JSONResponse(
                _jsonrpc_error(req_id, -32602, "Invalid params: expected object")
            )
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        if not tool_name:
            return JSONResponse(
                _jsonrpc_error(req_id, -32602, "Missing required param: name")
            )

        ctx = get_ctx() if callable(get_ctx) else get_ctx
        result = await registry.execute(tool_name, arguments, ctx)

        content = [{"type": "text", "text": result.content}]
        return JSONResponse(
            _jsonrpc_result(
                req_id,
                {"content": content, "isError": result.is_error},
            )
        )

    _DISPATCH = {
        "initialize": _handle_initialize,
        "tools/list": _handle_tools_list,
        "tools/call": _handle_tools_call,
    }

    @router.post("")
    async def mcp_endpoint(request: Request) -> JSONResponse:
        """Single JSON-RPC 2.0 endpoint for the MCP protocol."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                _jsonrpc_error(None, -32700, "Parse error: invalid JSON")
            )

        # Notifications (no ``id``) – acknowledge silently
        req_id = body.get("id")
        method = body.get("method", "")

        if req_id is None:
            # JSON-RPC notification – no response expected
            return JSONResponse({"jsonrpc": "2.0"}, status_code=200)

        handler = _DISPATCH.get(method)
        if handler is None:
            return JSONResponse(
                _jsonrpc_error(req_id, -32601, f"Method not found: {method}")
            )

        params = body.get("params")
        return await handler(req_id, params)

    return router
