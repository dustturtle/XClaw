"""Live XClaw acceptance against a running local PowerCore demo stack.

Uses real model/data services, creates uniquely named test chats and paper records,
and reads SQLite plus public Run events back. No credentials or mocked tools.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
import sqlite3
import time
import uuid

import httpx


def json_reply(text):
    return json.loads(text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())


def tool_texts_from_checkpoint(checkpoint):
    messages = checkpoint["history"]
    names = {block["id"]: block["name"] for message in messages for block in message["content"] if block["type"] == "tool_use"}
    sources = {}
    for message in messages:
        for block in message["content"]:
            if block["type"] != "tool_result" or block.get("is_error"):
                continue
            name = names.get(block["tool_use_id"])
            if name is None:
                continue
            result = json.loads(block["content"][0]["text"])["result"]["result"]
            sources[name] = "\n".join(item["text"] for item in result["content"] if item["type"] == "text")
    return sources


def verify_quote_continuation(source, reply, run_events):
    assert not any(event["type"] == "tool.started" for event in run_events), "continuation must reuse evidence without new tools"
    expected = {}
    for field in ("昨收", "涨跌幅"):
        match = re.search(rf"^{field}:\s*([+-]?\d+(?:\.\d+)?%?)\s*$", source, re.MULTILINE)
        assert match, f"quote source lacks a numeric {field}"
        expected[field] = match[1]
        assert match[1] in reply, f"continuation {field} differs from the original tool evidence"
    assert "600519" in reply and "贵州茅台" in reply
    return expected


def verify_memory_reference(marker, source, reply):
    assert marker in source, "persisted memory must be present in the actual read-tool result"
    label, separator, identifier = marker.rpartition("-")
    assert separator and label and identifier, "acceptance marker must contain a unique identifier"
    assert label in reply and identifier in reply, "display must preserve memory content and identity"


def visual_fixture(path):
    from PIL import Image, ImageDraw, ImageFont

    challenge = uuid.uuid4().hex[:8].upper()
    row_b = f"{1100 + int(uuid.uuid4().hex[:3], 16) % 900}.{int(uuid.uuid4().hex[:2], 16) % 100:02d}"
    image = Image.new("RGB", (1200, 780), "#fffaf2")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=48)
    draw.text((60, 35), f"CHALLENGE: {challenge}", fill="#222222", font=font)
    draw.polygon([(280, 150), (130, 450), (430, 450)], fill="#df241d")
    draw.rectangle((730, 150, 1030, 450), fill="#185ddb")
    draw.text((190, 475), "LEFT", fill="#222222", font=font)
    draw.text((780, 475), "RIGHT", fill="#222222", font=font)
    draw.text((100, 585), "ROW_A   1288.00", fill="#222222", font=font)
    draw.text((100, 670), f"ROW_B   {row_b}", fill="#222222", font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")
    attachment = {"name": "vision.png", "media_type": "image/png", "data": base64.b64encode(path.read_bytes()).decode()}
    expected = {"challenge": challenge, "left_shape": "triangle", "left_color": "red", "right_shape": "square", "right_color": "blue", "row_a": "1288.00", "row_b": row_b}
    return attachment, expected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--powercore-url", default="http://127.0.0.1:18080")
    parser.add_argument("--expected-model", help="Require the UI configuration and every Run to use this model")
    parser.add_argument("--multimodal", action="store_true", help="Verify actual image input and follow-up through XClaw")
    parser.add_argument("--numeric-transcription", action="store_true", help="Compare the final numeric table with actual tool results")
    parser.add_argument("--timeout", type=float, default=750, help="Observation timeout; must outlast the admitted run budget")
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
    client = httpx.Client(base_url=args.url, headers=headers, timeout=args.timeout, trust_env=False)
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

    def chat(name, prompt, *, chat_id=first, tools=(), status="completed", attachments=None):
        started = time.monotonic()
        payload = {"chat_id": chat_id, "message": prompt}
        if attachments:
            payload["attachments"] = attachments
        response = client.post("/api/chat", json=payload)
        response.raise_for_status()
        reply = response.json()["reply"]
        run = session(chat_id)
        run_events = events(run["run_id"])
        record = {"name": name, "prompt": prompt, "reply": reply, "run": run,
                  "events": run_events, "seconds": round(time.monotonic() - started, 2)}
        evidence["cases"].append(record)
        assert reply.strip(), f"{name}: empty response"
        assert run["status"] == status, f"{name}: unexpected status {run['status']}"
        if status == "completed":
            assert run_events[-1]["type"] == "run.completed" and run_events[-1].get("response") == reply
            assert run_events[-1].get("finish_reason") not in {"length", "max_tokens", "max_output_tokens"}, f"{name}: truncated final answer"
        if args.expected_model:
            assert any(event.get("type") == "run.started" and event.get("model") == args.expected_model for event in run_events), f"{name}: wrong model"
        for tool in tools:
            assert any(event.get("type") == "tool.completed" and event.get("name", "").endswith(tool)
                       and event.get("is_error") is False for event in run_events), f"{name}: no successful {tool}"
        print(f"RUN {name}: {run['run_id']} {run['status']} ({record['seconds']}s)", flush=True)
        return record

    try:
        config = client.get("/api/config")
        config.raise_for_status()
        assert config.json()["agent_backend"] == "powercore", "Not running PowerCore backend"
        evidence["configuration"] = {key: config.json().get(key) for key in ("agent_backend", "model", "max_tokens", "supports_images")}
        if args.expected_model:
            assert config.json()["model"] == args.expected_model, "UI model differs from expected model"
        if args.multimodal:
            assert config.json().get("supports_images") is True
            path = args.data_dir.resolve() / "powercore" / f"vision-{suffix}.png"
            attachment, expected = visual_fixture(path)
            vision_chat = f"pc-vision-{suffix}"
            evidence["chats"].append(vision_chat)
            evidence["vision_fixture"] = {"path": str(path), "expected": expected}
            visual = chat("image_reading", "读取附件，严格输出JSON字段 challenge、left_shape、left_color、right_shape、right_color、row_a、row_b。颜色和形状用英文小写，数字逐字转录为字符串，不要计算或调用工具。", chat_id=vision_chat, attachments=[attachment])
            assert json_reply(visual["reply"]) == expected, "image contents were not transcribed exactly"
            assert not any(event["type"] == "tool.started" for event in visual["events"])
            follow = chat("image_followup", "刚才图片中的挑战码和ROW_A是什么？只返回JSON字段challenge和row_a，值都用字符串。不调用工具。", chat_id=vision_chat)
            assert follow["run"]["session_id"] == visual["run"]["session_id"]
            assert json_reply(follow["reply"]) == {"challenge": expected["challenge"], "row_a": expected["row_a"]}
        quote = chat("quote", "请用行情工具查询贵州茅台 600519，只列代码、价格、报价时间和数据源。", tools=("stock_quote",))
        assert "600519" in quote["reply"]
        followup = chat("continuation", "刚才是哪只股票？请给出股票名称、代码、昨收和涨跌幅，不要重新查询。")
        assert "600519" in followup["reply"]
        assert followup["run"]["session_id"] == quote["run"]["session_id"]
        quote_audit = core.get(f"/admin/runs/{quote['run']['run_id']}/audit")
        quote_audit.raise_for_status()
        quote_source = next(text for name, text in tool_texts_from_checkpoint(quote_audit.json()["checkpoint"]).items() if name.endswith("stock_quote"))
        evidence["continuation_evidence"] = verify_quote_continuation(quote_source, followup["reply"], followup["events"])
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
        history_prompt = "请查询600519最近20条日K线，并用技术指标工具给出MA5和MA20。请实际调用两个工具。数据不可用就明确说，不要编造。"
        if args.numeric_transcription:
            history_prompt += ' 严格只输出JSON：{"rows":[{"date":"YYYY-MM-DD","open":"数字","close":"数字","high":"数字","low":"数字"}],"ma5":"数字","ma20":"数字"}。完整返回20行，所有数值按工具输出保留两位小数并用字符串，不要手算。'
        history = chat("history_and_indicators", history_prompt, tools=("stock_history", "stock_indicators"))
        if args.numeric_transcription:
            audit_response = core.get(f"/admin/runs/{history['run']['run_id']}/audit")
            audit_response.raise_for_status()
            sources = {tool: text for name, text in tool_texts_from_checkpoint(audit_response.json()["checkpoint"]).items()
                       for tool in ("stock_history", "stock_indicators") if name.endswith(tool)}
            expected_rows = []
            for line in sources["stock_history"].splitlines():
                fields = line.split()
                if fields and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[0]):
                    expected_rows.append(dict(zip(("date", "open", "close", "high", "low"), fields[:5])))
            expected = {"rows": expected_rows}
            for name in ("ma5", "ma20"):
                expected[name] = re.search(rf"^{name.upper()}:\s*(\d+\.\d+)", sources["stock_indicators"], re.MULTILINE)[1]
            evidence["numeric_transcription"] = {"sources": sources, "expected": expected, "actual": json_reply(history["reply"])}
            assert len(expected_rows) == 20 and json_reply(history["reply"]) == expected, "numeric transcription differs from actual tool data"
            print("PASS numeric_transcription: 20 rows and MA5/MA20 match original tool data", flush=True)
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
        assert rows("SELECT * FROM memories WHERE chat_id=? AND content LIKE ?", first_id, f"%{marker}%")
        assert rows("SELECT * FROM watchlist WHERE chat_id=? AND symbol='600519'", first_id)
        assert rows("SELECT * FROM portfolio WHERE chat_id=? AND symbol='600519' AND shares=2 AND avg_cost=1000", first_id)
        restored_audit = core.get(f"/admin/runs/{restored['run']['run_id']}/audit")
        restored_audit.raise_for_status()
        memory_source = next(text for name, text in tool_texts_from_checkpoint(restored_audit.json()["checkpoint"]).items() if name.endswith("structured_memory_read"))
        verify_memory_reference(marker, memory_source, restored["reply"])
        assert "600519" in restored["reply"]
        evidence["status"] = "passed"
        print("PASS complete business acceptance, including audited continuation and reset persistence", flush=True)
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
