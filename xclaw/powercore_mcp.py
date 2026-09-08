"""Authenticated MCP adapter for PowerCore's stateless HTTP transport."""

from __future__ import annotations

import asyncio
import hmac
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from xclaw.mcp_server import _jsonrpc_error, _jsonrpc_result
from xclaw.powercore import MCP_PROTOCOL_VERSION, allowed_tools


def create_powercore_mcp_router(registry: Any, backend: Any, context_factory: Any) -> APIRouter:
    token = backend.settings.powercore_mcp_token
    if not token:
        raise ValueError("powercore_mcp_token is required for the PowerCore tool endpoint")
    router = APIRouter()

    @router.post("")
    async def mcp(request: Request) -> dict:
        expected = ("Bearer " + token).encode()
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected):
            raise HTTPException(status_code=401, detail="Invalid MCP credentials")
        raw = await request.body()
        if len(raw) > 262144:
            raise HTTPException(status_code=413, detail="MCP request too large")
        try:
            body = json.loads(raw)
        except ValueError:
            return _jsonrpc_error(None, -32700, "Invalid JSON")
        if not isinstance(body, dict):
            return _jsonrpc_error(None, -32600, "Expected a JSON-RPC object")
        req_id = body.get("id")
        params = body.get("params", {})
        if not isinstance(params, dict):
            return _jsonrpc_error(req_id, -32602, "Expected object params")
        metadata = params.get("_meta", {})
        if not isinstance(metadata, dict) or metadata.get("io.modelcontextprotocol/protocolVersion") != MCP_PROTOCOL_VERSION:
            return _jsonrpc_error(req_id, -32602, "Unsupported MCP protocol version")
        method = body.get("method")
        if method == "server/discover":
            result = {
                "resultType": "complete", "supportedVersions": [MCP_PROTOCOL_VERSION],
                "capabilities": {"tools": {}}, "serverInfo": {"name": "xclaw", "version": "0.1.0"},
            }
        elif method == "tools/list":
            names = allowed_tools(registry, "mcp-controlled")
            result = {
                "resultType": "complete", "ttlMs": 30000, "cacheScope": "public",
                "tools": [registry.get(name).to_definition().to_mcp_tool() for name in names],
            }
        elif method == "tools/call":
            subject = metadata.get("io.powercore/requestContext", {})
            if not isinstance(subject, dict) or (
                subject.get("tenantId") != backend.settings.powercore_tenant_id
                or subject.get("userId") != backend.settings.powercore_user_id
            ):
                return _jsonrpc_error(req_id, -32001, "Unbound caller")
            run_id = subject.get("runId")
            if not isinstance(run_id, str):
                return _jsonrpc_error(req_id, -32001, "Missing run binding")
            # A worker can start before POST /runs has returned to XClaw.
            # Wait for the accepted run binding; never infer a chat from args.
            binding = None
            for _ in range(40):
                binding = await backend.db.get_powercore_run(run_id)
                if binding and binding["status"] != "waiting_input":
                    break
                await asyncio.sleep(0.05)
            if not binding or binding["status"] not in {"queued", "running"} or binding.get("cancel_requested"):
                return _jsonrpc_error(req_id, -32001, "Run is not active or not bound")
            name = params.get("name")
            arguments = params.get("arguments", {})
            if name not in json.loads(binding["allowed_tools_json"]):
                return _jsonrpc_error(req_id, -32001, "Tool is not allowed for this run")
            if not isinstance(arguments, dict):
                return _jsonrpc_error(req_id, -32602, "Expected object arguments")
            context = await context_factory(binding["chat_id"])
            tool_result = await registry.execute(name, arguments, context)
            result = {
                "resultType": "complete", "isError": tool_result.is_error,
                "content": [{"type": "text", "text": tool_result.content}],
            }
        else:
            return _jsonrpc_error(req_id, -32601, "Unknown method")
        return _jsonrpc_result(req_id, result)

    return router
