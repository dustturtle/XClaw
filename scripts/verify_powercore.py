"""Live XClaw acceptance against a running local PowerCore demo stack.

Uses real model/data services, creates uniquely named test chats and paper records,
and reads SQLite plus public Run events back. No credentials or mocked tools.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--powercore-url", default="http://127.0.0.1:18080")
    args = parser.parse_args()
    database = args.data_dir.resolve() / "xclaw.db"
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    suffix = uuid.uuid4().hex[:10]
    first, second = f"pc-acceptance-{suffix}", f"pc-isolation-{suffix}"
    marker, scheduled_marker = f"中文验收偏好-{suffix}", f"后台验收完成-{suffix}"
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "chats": [first, second], "cases": []}
    headers = {}
    if token := os.environ.get("XCLAW_WEB_AUTH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    pc_headers = {
        "X-PowerCore-Tenant-Id": os.environ.get("XCLAW_POWERCORE_TENANT_ID", "tenant_xclaw"),
        "X-PowerCore-User-Id": os.environ.get("XCLAW_POWERCORE_USER_ID", "user_xclaw"),
    }
    if token := os.environ.get("XCLAW_POWERCORE_API_TOKEN"):
        pc_headers = {"Authorization": f"Bearer {token}"}
    client = httpx.Client(base_url=args.url, headers=headers, timeout=300, trust_env=False)
    core = httpx.Client(base_url=args.powercore_url, headers=pc_headers, timeout=30, trust_env=False)

    def rows(sql, *params):
        return [dict(row) for row in db.execute(sql, params).fetchall()]

    def owner(chat):
        return rows("SELECT id FROM chats WHERE channel='web' AND external_chat_id=?", chat)[0]["id"]

    def session(chat):
        return rows("SELECT r.* FROM powercore_sessions s JOIN powercore_runs r ON r.run_id=s.run_id WHERE s.chat_id=?", owner(chat))[0]

    def events(run_id):
        cursor = rows("SELECT cursor FROM powercore_runs WHERE run_id=?", run_id)[0]["cursor"]
        collected = []
        with core.stream("GET", f"/runs/{run_id}/events") as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data:"):
                    item = json.loads(line[5:])
                    collected.append(item)
                    if item["sequence"] >= cursor:
                        break
        return collected

    def chat(name, prompt, *, chat_id=first, tools=(), status="completed"):
        started = time.monotonic()
        response = client.post("/api/chat", json={"chat_id": chat_id, "message": prompt})
        response.raise_for_status()
        reply = response.json()["reply"]
        run = session(chat_id)
        run_events = events(run["run_id"])
        record = {"name": name, "prompt": prompt, "reply": reply, "run": run,
                  "events": run_events, "seconds": round(time.monotonic() - started, 2)}
        evidence["cases"].append(record)
        assert reply.strip(), f"{name}: empty response"
        assert run["status"] == status, f"{name}: unexpected status {run['status']}"
        for tool in tools:
            assert any(event.get("type") == "tool.completed" and event.get("name", "").endswith(tool)
                       and event.get("is_error") is False for event in run_events), f"{name}: no successful {tool}"
        print(f"PASS {name}: {run['run_id']} ({record['seconds']}s)", flush=True)
        return record

    try:
        config = client.get("/api/config")
        config.raise_for_status()
        assert config.json()["agent_backend"] == "powercore", "Not running PowerCore backend"
        quote = chat("quote", "请用行情工具查询贵州茅台 600519，只列代码、价格、报价时间和数据源。", tools=("stock_quote",))
        assert "600519" in quote["reply"]
        followup = chat("continuation", "刚才是哪只股票？请给出股票名称、代码、昨收和涨跌幅，不要重新查询。")
        assert "600519" in followup["reply"]
        assert followup["run"]["session_id"] == quote["run"]["session_id"]
        chat("stateful_tools", f"这是本地验收会话。把贵州茅台 600519 加入自选股；把“{marker}”写入结构化记忆；记录本地模拟持仓 600519，2 股，成本价1000元。不是实际交易，禁止下单。", tools=("watchlist_manage", "structured_memory_update", "portfolio_manage"))
        first_id = owner(first)
        assert rows("SELECT * FROM watchlist WHERE chat_id=? AND symbol='600519'", first_id)
        assert rows("SELECT * FROM memories WHERE chat_id=? AND content LIKE ?", first_id, f"%{marker}%")
        assert rows("SELECT * FROM portfolio WHERE chat_id=? AND symbol='600519' AND shares=2 AND avg_cost=1000", first_id)
        isolated = chat("isolation", "请分别调用工具读取当前会话的结构化记忆、自选股和持仓。只读，不创建记录。", chat_id=second, tools=("structured_memory_read", "watchlist_manage", "portfolio_manage"))
        for table in ("memories", "watchlist", "portfolio"):
            assert not rows(f"SELECT * FROM {table} WHERE chat_id=?", owner(second)), f"isolation: {table}"
        assert marker not in isolated["reply"]
        due = (datetime.now().astimezone() + timedelta(seconds=90)).isoformat(timespec="seconds")
        chat("schedule_create", f"创建一次性本地验收任务，description 为 {scheduled_marker}，run_once_at 精确使用 {due}。任务prompt为：调用结构化记忆工具保存“{scheduled_marker}”，然后回复保存结果。不要立刻执行任务，也不创建重复任务。", tools=("schedule_task",))
        task = rows("SELECT * FROM scheduled_tasks WHERE chat_id=? AND description=?", first_id, scheduled_marker)[0]
        question = chat("durable_question", "这轮做交互验收：请调用 ask_user 工具问我喜欢什么颜色，等待我回答后才输出“已收到颜色：”加上我的答案。不要猜测或提前完成。", status="waiting_input")
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            task = rows("SELECT * FROM scheduled_tasks WHERE id=?", task["id"])[0]
            if task["status"] == "completed":
                break
            if task["status"] not in {"active", "running"}:
                raise AssertionError(f"scheduled task: {task['status']}")
            time.sleep(2)
        assert task["status"] == "completed", "scheduled task did not finish"
        assert rows("SELECT * FROM memories WHERE chat_id=? AND content LIKE ?", first_id, f"%{scheduled_marker}%")
        assert session(first)["run_id"] == question["run"]["run_id"]
        assert session(first)["status"] == "waiting_input"
        scheduled_runs = rows("SELECT * FROM powercore_runs WHERE chat_id=? AND session_id!=? AND status='completed'", first_id, question["run"]["session_id"])
        assert scheduled_runs, "no independent completed background run"
        evidence["scheduled_task"] = task
        evidence["scheduled_runs"] = [{"run": run, "events": events(run["run_id"])} for run in scheduled_runs]
        print(f"PASS schedule_fire: task={task['id']} foreground question unchanged", flush=True)
        answer = chat("durable_answer", "蓝色")
        assert answer["run"]["run_id"] == question["run"]["run_id"]
        assert "蓝" in answer["reply"]
        delegated = chat("sub_agent", "请实际调用 sub_agent 委托一个只读子任务：分别查询贵州茅台600519和平安银行000001的最新行情，并简要对比两只股票的涨跌幅。子任务最多5轮，不创建任何本地记录。", tools=("sub_agent",))
        children = rows("SELECT * FROM powercore_runs WHERE chat_id=? AND session_id!=? AND allowed_tools_json NOT LIKE '%sub_agent%'", first_id, delegated["run"]["session_id"])
        assert any(run["status"] == "completed" for run in children), "no independent successful child run"
        evidence["child_runs"] = [{"run": run, "events": events(run["run_id"])} for run in children]
        chat("history_and_indicators", "请查询600519最近20条日K线，并用技术指标工具给出MA5和MA20。请实际调用两个工具。数据不可用就明确说，不要编造。", tools=("stock_history", "stock_indicators"))
        chat("investment_report", "请为本会话自选股生成一份本地日报，实际调用 investment_report 的 generate，并简要说明结果和数据不足之处。不提供买卖建议。", tools=("investment_report",))
        reports = rows("SELECT id,title,summary FROM investment_reports WHERE chat_id=?", first_id)
        assert reports, "report not persisted"
        evidence["reports"] = reports
        old_session = session(first)["session_id"]
        reset = client.post("/api/chat", json={"chat_id": first, "message": "/reset"})
        reset.raise_for_status()
        assert not rows("SELECT * FROM powercore_sessions WHERE chat_id=?", first_id)
        restored = chat("reset_keeps_business_state", "请读取当前结构化记忆、自选股和持仓，只读。", tools=("structured_memory_read", "watchlist_manage", "portfolio_manage"))
        assert restored["run"]["session_id"] != old_session
        assert marker in restored["reply"] and "600519" in restored["reply"]
        evidence["status"] = "passed"
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        output = args.data_dir.resolve() / "powercore" / f"acceptance-{suffix}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Evidence: {output}", flush=True)
        client.close()
        core.close()
        db.close()


if __name__ == "__main__":
    main()
