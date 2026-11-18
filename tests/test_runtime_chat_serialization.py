"""Native session turns serialize by resolved internal chat, not request ID."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from xclaw import runtime
from xclaw.config import Settings
from xclaw.db import Database
from xclaw.tools import ToolRegistry


@pytest.mark.asyncio
async def test_gate_independent_chats_waiter_cancel_and_exception_release():
    gate=runtime.ChatTurnGate();entered=asyncio.Event();release=asyncio.Event();other=asyncio.Event()
    async def owner():
        async with gate.hold(1):entered.set();await release.wait()
    async def waiter():
        async with gate.hold(1):raise AssertionError('cancelled waiter entered')
    async def independent():
        async with gate.hold(2):other.set()
    first=asyncio.create_task(owner());await entered.wait()
    queued=asyncio.create_task(waiter());await asyncio.sleep(0);queued.cancel()
    with pytest.raises(asyncio.CancelledError):await queued
    await asyncio.wait_for(independent(),1);assert other.is_set()
    release.set();await first
    with pytest.raises(ValueError):
        async with gate.hold(1):raise ValueError('failed turn')
    async with gate.hold(1):pass
    assert not gate._entries


@pytest.mark.asyncio
async def test_gate_cancelled_owner_releases_and_recursion_fails_closed():
    gate=runtime.ChatTurnGate();entered=asyncio.Event()
    async def owner():
        async with gate.hold(1):entered.set();await asyncio.Event().wait()
    first=asyncio.create_task(owner());await entered.wait();first.cancel()
    with pytest.raises(asyncio.CancelledError):await first
    async with gate.hold(1):
        with pytest.raises(RuntimeError,match='Recursive'):
            async with gate.hold(1):pass
    assert not gate._entries


@pytest.mark.asyncio
@pytest.mark.parametrize('second_text',['second','/reset'])
async def test_runtime_locks_actual_handler_through_persistence_and_reset(tmp_path,monkeypatch,second_text):
    settings=Settings(data_dir=str(tmp_path),agent_backend='native',openui_enabled=False)
    db=Database(settings.db_path);captured={};entered=asyncio.Event();release=asyncio.Event();loads=[]
    monkeypatch.setattr(runtime,'_setup_logging',lambda settings:None)
    monkeypatch.setattr(runtime,'Database',lambda path:db)
    monkeypatch.setattr(runtime,'create_provider',Mock())
    monkeypatch.setattr(runtime,'_build_tool_registry',lambda settings:ToolRegistry())
    monkeypatch.setattr('xclaw.scheduler.TaskScheduler',lambda **kw:SimpleNamespace(start=Mock(),stop=Mock()))
    def web_app(**kwargs):captured.update(kwargs);return object()
    monkeypatch.setattr('xclaw.channels.web.create_web_app',web_app)
    async def turn(ctx,text,attachments=None):
        old=await db.load_session(ctx.chat_id) or [];loads.append((text,list(old)))
        if text=='first':entered.set();await release.wait()
        if text=='/reset':await db.clear_session(ctx.chat_id)
        else:await db.save_session(ctx.chat_id,old+[{'role':'user','content':text}])
        return 'ok'
    monkeypatch.setattr(runtime,'agent_loop',turn)
    async def serve():
        handler=captured['message_handler']
        first=asyncio.create_task(handler('same','first'));await entered.wait()
        second=asyncio.create_task(handler('same',second_text));await asyncio.sleep(.02)
        assert [x[0] for x in loads]==['first']
        await asyncio.wait_for(handler('different','independent'),1)
        release.set();await asyncio.gather(first,second)
        latest=next(x[1] for x in loads if x[0]==second_text)
        assert latest==[{'role':'user','content':'first'}]
        ident=await db.get_or_create_chat('web','same')
        session=await db.load_session(ident)
        assert session is None if second_text=='/reset' else session==latest+[{'role':'user','content':'second'}]
    monkeypatch.setattr(runtime.uvicorn,'Config',lambda *a,**kw:None)
    monkeypatch.setattr(runtime.uvicorn,'Server',lambda config:SimpleNamespace(serve=serve))
    await runtime._run(settings)
