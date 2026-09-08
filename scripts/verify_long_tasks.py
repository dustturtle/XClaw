"""Replay the original long input through XClaw and inspect real terminal events.

Reads the source chat database read-only; uses new acceptance chats. Evidence is
written beneath the ignored data directory, never alongside tracked source.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import httpx


def verify_complete_response(terminal, reply):
    assert terminal.get("type") == "run.completed", "requires an actual completed terminal event"
    assert terminal.get("response") == reply, "public reply must match the terminal response"
    assert terminal.get("finish_reason") not in {"length", "max_tokens", "max_output_tokens"}, "truncated output is not a complete answer"
    assert len(reply.strip()) >= 200, "requires a useful review, not a generic acknowledgement"


async def verify(args):
    with sqlite3.connect(f"file:{args.source_db.resolve()}?mode=ro", uri=True) as database:
        row = database.execute("SELECT role,content FROM messages WHERE id=?", (args.message_id,)).fetchone()
    if not row or row[0] != "user":
        raise ValueError("The selected source message must be an existing user message")
    original = row[1]
    if len(original) < 7000:
        raise ValueError("The original long input must not be shortened for this acceptance")
    headers = {}
    if token := os.environ.get("XCLAW_WEB_AUTH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    core_headers = {
        "X-PowerCore-Tenant-Id": os.environ.get("XCLAW_POWERCORE_TENANT_ID", "tenant_xclaw"),
        "X-PowerCore-User-Id": os.environ.get("XCLAW_POWERCORE_USER_ID", "user_xclaw"),
    }
    if token := os.environ.get("XCLAW_POWERCORE_API_TOKEN"):
        core_headers = {"Authorization": f"Bearer {token}"}
    suffix = uuid.uuid4().hex[:10]
    evidence = {
        "started_at": datetime.now(timezone.utc).isoformat(), "status": "running", "cases": [],
        "source_message_id": args.message_id, "original_chars": len(original),
        "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
    }
    output = args.data_dir.resolve() / "powercore" / f"long-task-acceptance-{suffix}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(base_url=args.url, headers=headers, timeout=args.timeout, trust_env=False) as client, \
            httpx.AsyncClient(base_url=args.powercore_url, headers=core_headers, timeout=30, trust_env=False) as core:
        async def case(name, message, *, cancel=False):
            chat_id = f"long-{name}-{suffix}"
            record = {"name": name, "chat_id": chat_id, "progress": [], "input_chars": len(message)}
            evidence["cases"].append(record)
            started = time.monotonic()
            post = asyncio.create_task(client.post("/api/chat", json={
                "chat_id": chat_id, "message": message, "execution_mode": "analysis",
            }))
            last = None
            cancelled = False
            try:
                while time.monotonic() - started < args.timeout:
                    response = await client.get("/api/chat/progress", params={"chat_id": chat_id})
                    response.raise_for_status()
                    state = response.json()
                    signature = (state.get("run_id"), state["status"], state.get("stage"), state.get("sequence"), bool(state.get("reply")))
                    if signature != last:
                        record["progress"].append(state)
                        last = signature
                    if state.get("run_id") and cancel and not cancelled:
                        response = await client.post("/api/chat/cancel", json={"chat_id": chat_id, "run_id": state["run_id"]})
                        response.raise_for_status()
                        cancelled = True
                    if state["status"] in {"completed", "failed", "cancelled"} and state.get("reply"):
                        break
                    if post.done() and not state.get("run_id"):
                        response = await post
                        response.raise_for_status()
                        raise AssertionError(f"No admitted Run: {response.json().get('reply')}")
                    await asyncio.sleep(1)
                else:
                    raise TimeoutError("No terminal state within the acceptance observation window")
                response = await post
                response.raise_for_status()
                record.update({"final": state, "reply": response.json()["reply"], "elapsed_seconds": round(time.monotonic() - started, 2)})
                assert state["wall_time_ms"] == 600000, "the intended analysis budget was not admitted"
                assert state["status"] == ("cancelled" if cancel else "completed"), state
                assert state["reply"] == record["reply"]
                events = []
                async with core.stream("GET", f"/runs/{state['run_id']}/events") as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        item = json.loads(line[5:])
                        events.append(item)
                        if item["type"] in {"run.completed", "run.failed", "run.cancelled"}:
                            break
                record["events"] = events
                assert events[-1]["type"] == ("run.cancelled" if cancel else "run.completed")
                if not cancel:
                    verify_complete_response(events[-1], record["reply"])
                assert any(item.get("model") == args.expected_model for item in events), "wrong live model"
                restored = (await client.get("/api/chat/progress", params={"chat_id": chat_id, "include_history": "true"})).json()
                assert restored["run_id"] == state["run_id"]
                assert sum(item["role"] == "assistant" and item["content"] == record["reply"] for item in restored["messages"]) == 1
                record["result"] = "passed"
                print(f"PASS {name}: {state['run_id']} {state['status']} {record['elapsed_seconds']}s", flush=True)
                return record
            finally:
                if not post.done():
                    post.cancel()
                    await asyncio.gather(post, return_exceptions=True)

        try:
            config = (await client.get("/api/config")).json()
            assert config["model"] == args.expected_model and config["supports_progress"]
            if args.case in {"original", "all"}:
                await case("original", original)
            if args.case in {"tools", "all"}:
                record = await case("tools", original + "\n\n补充验收要求：请实际调用工具查询510500、518880、511010的最新行情、最近20个交易日日K线，并查询三者的相关性；将数据来源与缺失标明，再评审上述方案。只读，不建立持仓、记忆或定时任务。")
                tools = [item.get("name", "") for item in record["events"] if item["type"] == "tool.completed"]
                for required in ("stock_quote", "stock_history", "stock_correlation"):
                    assert any(name.endswith(required) for name in tools), f"missing actual {required}"
            if args.cancel:
                await case("cancel", original, cancel=True)
            evidence["status"] = "passed"
        except Exception as exc:
            evidence["status"] = "failed"
            evidence["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
            output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Evidence: {output}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--message-id", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--powercore-url", default="http://127.0.0.1:18080")
    parser.add_argument("--expected-model", default="glm-5-3-flash-260826")
    parser.add_argument("--case", choices=("original", "tools", "all"), default="all")
    parser.add_argument("--cancel", action="store_true")
    parser.add_argument("--timeout", type=float, default=750)
    asyncio.run(verify(parser.parse_args()))


if __name__ == "__main__":
    main()
