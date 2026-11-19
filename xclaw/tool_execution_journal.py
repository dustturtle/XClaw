"""Durable write-intent fence, not distributed business exactly-once.

Uses a separate SQLite connection and short atomic claims. All participating writers
must use this wrapper and the same database. Operator acknowledgement is deliberately
not exposed as a model tool/API: stop the affected execution, inspect real business
state, obtain explicit human confirmation, then acknowledge. Reset never clears it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from xclaw.tools import ToolResult, ToolExecutionEffect

_ACTIVE: set[str] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_execution_journal (
 id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, call_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL,
 state TEXT NOT NULL, result_json TEXT, note TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(chat_id, call_id)
);
"""


def is_read_only_call(registry, call) -> bool:
    if call.name == 'watchlist_manage' and call.input.get('action') == 'list':
        return True
    if call.name == 'portfolio_manage' and call.input.get('action') == 'view':
        return True
    tool = registry.get(call.name)
    return tool is not None and getattr(tool, 'read_only', False) is True


def _path(db) -> str:
    path = getattr(db, '_path', None)
    if not isinstance(path, (str, Path)) or str(path) == ':memory:':
        raise ValueError('写入日志需要真实持久数据库，当前环境禁止写工具')
    if not Path(path).is_file():
        raise ValueError('写入日志数据库不存在，禁止降级执行写工具')
    return str(path)


def _transaction(path, operation):
    with sqlite3.connect(path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(_SCHEMA)
        conn.execute('BEGIN IMMEDIATE')
        return operation(conn)


def _claim(conn, chat_id, call):
    args = json.dumps(call.input, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    fingerprint = hashlib.sha256((call.name + '\n' + args).encode()).hexdigest()
    row = conn.execute('SELECT * FROM tool_execution_journal WHERE chat_id=? AND call_id=?', (str(chat_id), call.id)).fetchone()
    if row:
        if row['fingerprint'] != fingerprint:
            raise ValueError('同一工具调用 ID 的工具或参数发生变化，已拒绝执行')
        if row['state'] in {'completed', 'failed_not_applied'}:
            return 'cached', row['id'], json.loads(row['result_json'])
        raise ValueError(f"调用 {row['id']} 已尝试执行或已人工核对，禁止自动重放；状态 {row['state']}")
    blocked = conn.execute("SELECT id FROM tool_execution_journal WHERE chat_id=? AND state IN ('started','unknown') LIMIT 1", (str(chat_id),)).fetchone()
    if blocked:
        raise ValueError(f"本会话存在执行结果不确定的写操作 {blocked['id']}，可能已生效；已阻止后续写入。可以只读核对业务状态，停止原执行后由助手取得你的明确确认并解除，不能直接重试。")
    identifier = uuid.uuid4().hex
    conn.execute('INSERT INTO tool_execution_journal(id,chat_id,call_id,fingerprint,tool_name,arguments_json,state) VALUES(?,?,?,?,?,?,?)', (identifier,str(chat_id),call.id,fingerprint,call.name,args,'started'))
    _ACTIVE.add(identifier)
    return 'started', identifier, None


def _finish(conn, identifier, state, result=None):
    stored = {'content': result.content, 'is_error': result.is_error} if result is not None else None
    if stored is not None and state == 'failed_not_applied':
        stored['execution_effect'] = ToolExecutionEffect.NOT_APPLIED.value
    raw = json.dumps(stored, ensure_ascii=False) if stored is not None else None
    cur = conn.execute("UPDATE tool_execution_journal SET state=?,result_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND state='started'", (state,raw,identifier))
    if cur.rowcount != 1:
        raise ValueError("执行日志状态已变化，无法确认完成；不能返回执行成功")


async def execute_with_journal(ctx, call, tool_context):
    """Execute one call; only confirmed reads bypass durable admission."""
    if ctx.tools.get(call.name) is None:
        return ToolResult(f'未知工具 {call.name}，未执行', is_error=True)
    # Trusted orchestration only: current SubAgentTool creates a read-only registry
    # and calls the same journal-integrated agent_loop with the same chat/database.
    # Exact type avoids allowing arbitrary tools/subclasses to impersonate it.
    from xclaw.tools.sub_agent import SubAgentTool
    if type(ctx.tools.get(call.name)) is SubAgentTool:
        return await ctx.tools.execute(call.name, call.input, tool_context)
    if is_read_only_call(ctx.tools, call):
        return await ctx.tools.execute(call.name, call.input, tool_context)
    try:
        path = _path(ctx.db)
        if not isinstance(call.id, str) or not call.id.strip():
            raise ValueError('写工具调用缺少稳定 ID，已拒绝执行')
        admission = asyncio.create_task(asyncio.to_thread(_transaction, path, lambda c: _claim(c, ctx.chat_id, call)))
        try:
            state, identifier, cached = await asyncio.shield(admission)
        except asyncio.CancelledError:
            def release(task):
                if not task.cancelled() and task.exception() is None:
                    _ACTIVE.discard(task.result()[1])
            admission.add_done_callback(release)
            raise
    except Exception as exc:
        # Admission failed before tool.execute is reached. Consumers must still
        # check for a durable own-call claim before releasing their receipts.
        return ToolResult(str(exc), is_error=True, execution_effect=ToolExecutionEffect.NOT_APPLIED)
    if state == 'cached':
        if cached.get('execution_effect') == ToolExecutionEffect.NOT_APPLIED.value:
            cached['execution_effect'] = ToolExecutionEffect.NOT_APPLIED
        return ToolResult(**cached)
    try:
        # Bypass registry exception-to-result conversion: a thrown write exception
        # does not prove that its business transaction never committed.
        tool = ctx.tools.get(call.name)
        if tool is None:
            raise ValueError('未知工具，无法确认执行性质')
        result = await tool.execute(call.input, tool_context)
        # Exact implementation allowlist: model text/arguments and third-party
        # tools cannot assert a no-write outcome. Only this audited pre-write path
        # is trusted; ordinary errors and exceptions remain uncertain.
        from xclaw.tools.strategy_scan import StrategyScanTool
        not_applied = (type(tool) is StrategyScanTool and result.is_error is True
                       and result.execution_effect is ToolExecutionEffect.NOT_APPLIED)
        terminal = 'failed_not_applied' if not_applied else ('unknown' if result.is_error else 'completed')
        await asyncio.to_thread(_transaction, path, lambda c: _finish(c, identifier, terminal, result))
        if not_applied:
            return result
        if result.is_error:
            return ToolResult(result.content + f'\n写操作结果需要核对（记录 {identifier}）；可能已部分生效，后续写入已暂停。', True)
        return result
    except BaseException as exc:
        # Cancellation/crash may leave started, which is equally blocking.
        try:
            await asyncio.shield(asyncio.to_thread(_transaction, path, lambda c: _finish(c, identifier, 'unknown')))
        except BaseException:
            pass
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        return ToolResult(f'写操作执行状态不确定（记录 {identifier}），可能已生效，禁止自动重试；请只读核对后人工确认解除。', True)

    finally:
        _ACTIVE.discard(identifier)


async def inspect_journal(db, chat_id):
    """Read-only operator diagnostics, including original returned errors/results."""
    path = _path(db)
    def read():
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_execution_journal'").fetchone()
            if not exists:
                return []
            return [dict(r) for r in conn.execute('SELECT * FROM tool_execution_journal WHERE chat_id=? ORDER BY created_at,id', (str(chat_id),))]
    return await asyncio.to_thread(read)


async def acknowledge_unknown(db, chat_id, execution_id, *, confirmed_reviewed=False, note='', execution_stopped=False):
    """Operator-only explicit acknowledgement; never executes/replays a tool.

    Caller must enforce user authorization. confirmed_reviewed means actual business
    state was checked; execution_stopped requires the actual operation to have stopped,
    not merely cancellation of its awaiting coroutine (worker threads may still run).
    This is not an authenticated public endpoint and must not be registered as a tool.
    """
    if confirmed_reviewed is not True or execution_stopped is not True or not isinstance(note, str) or not note.strip():
        raise ValueError('必须停止原执行、人工核对业务状态并取得明确确认，记录核对说明')
    def update(conn):
        if execution_id in _ACTIVE:
            raise ValueError('该操作仍在当前进程执行，不能解除屏障')
        cur = conn.execute("UPDATE tool_execution_journal SET state='acknowledged',note=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND chat_id=? AND state IN ('started','unknown')", (note.strip(),execution_id,str(chat_id)))
        if cur.rowcount != 1:
            raise ValueError('未找到该会话待核对记录；未解除任何屏障')
    await asyncio.to_thread(_transaction, _path(db), update)
