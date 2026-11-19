import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from xclaw.llm_types import ToolUseBlock
from xclaw.tools import ToolResult
from xclaw.tool_execution_journal import execute_with_journal, inspect_journal, acknowledge_unknown


def setup(tmp_path, behavior=None):
    path = tmp_path / 'db.sqlite'
    sqlite3.connect(path).close()
    tool = SimpleNamespace(read_only=False, count=0)
    async def execute(params, context):
        tool.count += 1
        if behavior:
            return await behavior()
        return ToolResult('committed')
    tool.execute = execute
    async def registry_execute(name, params, context):
        return await execute(params, context)
    ctx = SimpleNamespace(db=SimpleNamespace(_path=path),chat_id=1,tools=SimpleNamespace(get=lambda name: tool,execute=registry_execute))
    return ctx, tool


def call(id='a', **params):
    return ToolUseBlock(id=id,name='mutation',input=params)


async def test_cache_and_conflict(tmp_path):
    ctx,t = setup(tmp_path)
    assert not (await execute_with_journal(ctx,call(x=1),None)).is_error
    assert (await execute_with_journal(ctx,call(x=1),None)).content == 'committed'
    assert (await execute_with_journal(ctx,call(x=2),None)).is_error
    assert t.count == 1


async def test_unknown_blocks_new_request_and_ack_never_replays(tmp_path):
    async def fail():
        raise RuntimeError('after commit')
    ctx,t = setup(tmp_path,fail)
    assert (await execute_with_journal(ctx,call(),None)).is_error
    assert (await execute_with_journal(ctx,call('new'),None)).is_error
    row = (await inspect_journal(ctx.db,1))[0]
    assert row['state'] == 'unknown'
    with pytest.raises(ValueError):
        await acknowledge_unknown(ctx.db,1,row['id'],confirmed_reviewed=True,note='checked')
    await acknowledge_unknown(ctx.db,1,row['id'],confirmed_reviewed=True,execution_stopped=True,note='user confirmed actual state')
    assert (await execute_with_journal(ctx,call(),None)).is_error
    assert t.count == 1
    assert (await inspect_journal(ctx.db,1))[0]['state'] == 'acknowledged'


async def test_active_ack_refused_and_cancel_remains_blocking(tmp_path):
    started=asyncio.Event()
    async def wait():
        started.set()
        await asyncio.Event().wait()
    ctx,t = setup(tmp_path,wait)
    task=asyncio.create_task(execute_with_journal(ctx,call(),None))
    await started.wait()
    row=(await inspect_journal(ctx.db,1))[0]
    with pytest.raises(ValueError,match='仍在'):
        await acknowledge_unknown(ctx.db,1,row['id'],confirmed_reviewed=True,execution_stopped=True,note='checked')
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await execute_with_journal(ctx,call('new'),None)).is_error
    assert t.count == 1


async def test_atomic_admission_separate_connections(tmp_path):
    ctx,t=setup(tmp_path)
    results=await asyncio.gather(*(execute_with_journal(ctx,call(),None) for _ in range(8)))
    assert t.count == 1
    assert any(not r.is_error for r in results)


async def test_error_preserves_actual_result_read_bypass_and_mock_fails_closed(tmp_path):
    async def fail():
        return ToolResult('partial change',True)
    ctx,t=setup(tmp_path,fail)
    await execute_with_journal(ctx,call(),None)
    row=(await inspect_journal(ctx.db,1))[0]
    assert 'partial change' in row['result_json']
    assert row['state']=='unknown'
    view=ToolUseBlock(id='read',name='portfolio_manage',input={'action':'view'})
    await execute_with_journal(ctx,view,None)
    assert t.count==2
    ctx.db=object()
    assert (await execute_with_journal(ctx,call('write'),None)).is_error
    assert t.count==2


async def test_reopened_database_keeps_fence_and_other_chat_allowed(tmp_path):
    async def fail():
        return ToolResult('possibly committed',True)
    ctx,t=setup(tmp_path,fail)
    await execute_with_journal(ctx,call(),None)
    # New runtime/db object cannot bypass persistent state with a new request ID.
    other=SimpleNamespace(db=SimpleNamespace(_path=ctx.db._path),chat_id=1,tools=ctx.tools)
    assert (await execute_with_journal(other,call('different'),None)).is_error
    assert t.count==1
    other.chat_id=2
    await execute_with_journal(other,call('different'),None)
    assert t.count==2


async def test_confirmed_read_only_without_persistent_db(tmp_path):
    ctx,t=setup(tmp_path)
    t.read_only=True
    ctx.db=object()
    assert not (await execute_with_journal(ctx,call(),None)).is_error
    assert t.count==1


def test_finish_cannot_overwrite_operator_ack(tmp_path):
    from xclaw.tool_execution_journal import _transaction, _claim, _finish, _ACTIVE
    path=tmp_path/'journal.sqlite'
    sqlite3.connect(path).close()
    _, identifier, _ = _transaction(str(path),lambda conn:_claim(conn,1,call()))
    try:
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE tool_execution_journal SET state='acknowledged',note='operator checked' WHERE id=?",(identifier,))
        with pytest.raises(ValueError,match='无法确认完成'):
            _transaction(str(path),lambda conn:_finish(conn,identifier,'completed',ToolResult('committed')))
        with sqlite3.connect(path) as conn:
            row=conn.execute('SELECT state,result_json,note FROM tool_execution_journal WHERE id=?',(identifier,)).fetchone()
            assert row==('acknowledged',None,'operator checked')
    finally:
        _ACTIVE.discard(identifier)
