"""Responses API transport, including explicit local Sol router configuration.

The router may forward to a remote model. No OAuth discovery or credential lookup.
"""
from __future__ import annotations

import json

from xclaw.llm import OpenAICodexProvider
from xclaw.llm_types import DoneEvent, TextDeltaEvent, Message, ToolDefinition, ToolUseBlock, ToolResultBlock


class ResponsesProtocolError(ValueError):
    """Local fail-closed history error; no request sent and no tool replayed."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"Invalid Responses tool history: {code}")


def validate_tool_history(messages):
    """Validate complete tool turns, without treating output content as instructions."""
    seen, pending, answered = set(), set(), set()
    for msg in messages:
        blocks = [] if isinstance(msg.content, str) else msg.content
        calls = [b for b in blocks if isinstance(b, ToolUseBlock)]
        outputs = [b for b in blocks if isinstance(b, ToolResultBlock)]
        if calls and msg.role != 'assistant':
            raise ResponsesProtocolError('call_role')
        if outputs and msg.role != 'user':
            raise ResponsesProtocolError('output_role')
        if pending and not outputs:
            raise ResponsesProtocolError('interrupted_call_batch')
        if calls:
            for call in calls:
                if not call.id.strip() or not call.name.strip():
                    raise ResponsesProtocolError('invalid_call')
                if call.id in seen:
                    raise ResponsesProtocolError('duplicate_call_id')
                seen.add(call.id)
                pending.add(call.id)
        for output in outputs:
            if output.tool_use_id in answered:
                raise ResponsesProtocolError('duplicate_output')
            if output.tool_use_id not in pending:
                raise ResponsesProtocolError('orphan_output')
            pending.remove(output.tool_use_id)
            answered.add(output.tool_use_id)
        if outputs and pending and any(not isinstance(b, ToolResultBlock) for b in blocks):
            raise ResponsesProtocolError('interrupted_call_batch')
    if pending:
        raise ResponsesProtocolError('unresolved_call')


def validate_responses_input(items):
    """Check wire call/output identity ordering; message roles checked before serialization."""
    seen, pending, answered = set(), set(), set()
    outputs_started = False
    for item in items:
        kind = item.get('type')
        if kind == 'function_call':
            call_id = item.get('call_id')
            if not isinstance(call_id, str) or not call_id.strip():
                raise ResponsesProtocolError('invalid_call_id')
            if call_id in seen:
                raise ResponsesProtocolError('duplicate_call_id')
            if pending and outputs_started:
                raise ResponsesProtocolError('interrupted_call_batch')
            seen.add(call_id)
            pending.add(call_id)
        elif kind == 'function_call_output':
            call_id = item.get('call_id')
            if call_id in answered:
                raise ResponsesProtocolError('duplicate_output')
            if call_id not in pending:
                raise ResponsesProtocolError('orphan_output')
            pending.remove(call_id)
            answered.add(call_id)
            outputs_started = bool(pending)
    if pending:
        raise ResponsesProtocolError('unresolved_call')


class ResponsesProvider(OpenAICodexProvider):
    def __init__(self, *, api_key: str, model: str, base_url: str, timeout: float = 120):
        super().__init__(credential_manager=None, model=model, base_url=base_url, timeout=timeout)
        self.api_key = api_key

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _build_payload(self, messages, tools, system, max_tokens):
        validate_tool_history(messages)
        instructions = [system] if system else []
        instructions.extend(m.text_content() for m in messages if m.role == "system")
        messages = [m for m in messages if m.role != "system"]
        payload = super()._build_payload(messages, tools, "\n\n".join(instructions) or None, max_tokens)
        validate_responses_input(payload["input"])
        payload["store"] = False
        for item in payload["input"]:
            if item.get("role") == "assistant":
                for part in item.get("content", []):
                    part["type"] = "output_text"
        return payload

    def _verify_model_identity(self, data):
        # Local routing adds codex/; upstream reports the unqualified name.
        identities = {self.model}
        if self.model.startswith("codex/"):
            identities.add(self.model[len("codex/"):])
        if (not isinstance(data, dict) or not isinstance(data.get("model"), str)
                or data["model"] not in identities):
            raise ResponsesProtocolError("response_model_mismatch")

    def _parse_response(self, data, *, prior_call_ids=()):
        if getattr(self, "require_model_identity", False):
            self._verify_model_identity(data)
        if data.get("error") or data.get("status") in {"failed", "cancelled"}:
            raise RuntimeError("Responses model request failed")
        # Never turn malformed tool arguments into a successful empty invocation.
        call_ids = set(prior_call_ids)
        for item in data.get("output", []):
            if item.get("type") == "function_call":
                if data.get("status") == "incomplete" or item.get("status") in {"in_progress", "incomplete"}:
                    raise ResponsesProtocolError("incomplete_response_call")
                if item.get("call_id") in call_ids:
                    raise ResponsesProtocolError("duplicate_response_call_id")
                call_ids.add(item.get("call_id"))
                args = json.loads(item.get("arguments", ""))
                if not isinstance(args, dict) or not item.get("call_id") or not item.get("name"):
                    raise ValueError("Invalid Responses tool call")
        return super()._parse_response(data)

    async def chat(self, messages: list[Message], tools: list[ToolDefinition] | None = None,
                   system: str | None = None, max_tokens: int = 4096):
        async for event in self.chat_stream(messages, tools, system, max_tokens):
            if isinstance(event, DoneEvent):
                return event.response
        raise RuntimeError("Responses stream ended without completion")

    async def chat_stream(self, messages, tools=None, system=None, max_tokens=4096):
        payload = self._build_payload(messages, tools, system, max_tokens)
        prior_call_ids = {i["call_id"] for i in payload["input"] if i.get("type") == "function_call"}
        payload["stream"] = True
        completed = False
        async with self._client.stream("POST", f"{self.base_url}/responses",
                                       headers=self._headers(), json=payload) as response:
            response.raise_for_status()
            if "application/json" in response.headers.get("content-type", ""):
                data = json.loads(await response.aread())
                parsed = self._parse_response(data, prior_call_ids=prior_call_ids)
                if parsed.text():
                    yield TextDeltaEvent(text=parsed.text())
                yield DoneEvent(response=parsed)
                return
            lines = []
            strict_identity = getattr(self, "require_model_identity", False)
            identity_verified = not strict_identity
            # No unverified delta buffer: if created/identity is absent, emit
            # only the terminal's verified complete text. Once suppressed, stay
            # in that mode to avoid displaying a tail with a missing prefix.
            suppressed_deltas = False
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    lines.append(line[5:].lstrip())
                elif not line and lines:
                    raw = "\n".join(lines)
                    lines = []
                    if raw == "[DONE]":
                        continue
                    data = json.loads(raw)
                    kind = data.get("type")
                    if kind == "response.created" and strict_identity:
                        created = data.get("response")
                        if not isinstance(created, dict):
                            raise ResponsesProtocolError("invalid_created_response")
                        if created.get("model") is not None:
                            self._verify_model_identity(created)
                            identity_verified = True
                    elif kind == "response.output_text.delta":
                        if identity_verified and not suppressed_deltas:
                            yield TextDeltaEvent(text=data.get("delta", ""))
                        else:
                            suppressed_deltas = True
                    elif kind in {"error", "response.failed"}:
                        raise RuntimeError("Responses stream failed")
                    elif kind in {"response.completed", "response.incomplete"}:
                        parsed = self._parse_response(data["response"], prior_call_ids=prior_call_ids)
                        if strict_identity and suppressed_deltas and parsed.text():
                            yield TextDeltaEvent(text=parsed.text())
                        completed = True
                        yield DoneEvent(response=parsed)
                        return
            if not completed:
                raise RuntimeError("Responses stream ended without completion")
