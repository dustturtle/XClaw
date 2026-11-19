"""Request-local selection never changes shared config/provider or child model."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from xclaw import runtime
from xclaw.config import Settings
from xclaw.db import Database
from xclaw.llm_responses import ResponsesProvider, ResponsesProtocolError
from xclaw.llm_types import Message
from xclaw.model_routing import configured_models, resolve_request_model, bind_request_model
from xclaw.tools import ToolRegistry, ToolContext
from xclaw.tools.sub_agent import SubAgentTool

DEFAULT='codex/fixture-default'
MODEL_A='codex/fixture-a'
MODEL_B='codex/fixture-b'


def settings(**kw):
    return Settings(_env_file=None,llm_provider='openai-responses',model=DEFAULT,
                    web_model_options=[MODEL_A,MODEL_B],**kw)


def test_allowlist_explicit_and_backend_limited():
    s=settings()
    assert configured_models(s)==[DEFAULT,MODEL_A,MODEL_B]
    assert resolve_request_model(s)==DEFAULT
    for bad in ['', 'fixture-a', 'codex/unknown', 42]:
        with pytest.raises(ValueError): resolve_request_model(s,bad)
    assert configured_models(s.model_copy(update={'agent_backend':'powercore'}))==[DEFAULT]
    assert configured_models(s.model_copy(update={'llm_provider':'anthropic'}))==[DEFAULT]
    assert configured_models(s.model_copy(update={'web_model_options':[]}))==[DEFAULT]


@pytest.mark.asyncio
async def test_bound_provider_concurrent_payload_identity_and_no_fallback():
    s=settings();base=ResponsesProvider(api_key='fake',model=DEFAULT,base_url='http://test')
    sent=[]
    async def reply(req):
        import json
        payload=json.loads(req.content);sent.append(payload['model'])
        await asyncio.sleep(0)
        return httpx.Response(200,json={'model':payload['model'].removeprefix('codex/'),
                'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'ok'}]}]})
    await base._client.aclose();base._client=httpx.AsyncClient(transport=httpx.MockTransport(reply))
    a,sa=bind_request_model(base,s,MODEL_A);b,sb=bind_request_model(base,s,MODEL_B)
    assert a._client is b._client is base._client
    assert sa.model==MODEL_A and sb.model==MODEL_B and s.model==base.model==DEFAULT
    await asyncio.gather(a.chat([Message(role='user',content='a')]),b.chat([Message(role='user',content='b')]))
    assert sorted(sent)==sorted([MODEL_A,MODEL_B])
    for wrong in [DEFAULT, '', None]:
        with pytest.raises(ResponsesProtocolError,match='response_model_mismatch'):
            a._parse_response({'model':wrong,'output':[]})
    await base.close()


@pytest.mark.asyncio
async def test_actual_runtime_binds_waiting_and_concurrent_turns(tmp_path,monkeypatch):
    s=settings(data_dir=str(tmp_path),openui_enabled=False)
    db=Database(s.db_path);captured={};entered=asyncio.Event();release=asyncio.Event();seen=[]
    base=ResponsesProvider(api_key='',model=DEFAULT,base_url='http://test')
    monkeypatch.setattr(runtime,'_setup_logging',lambda settings:None)
    monkeypatch.setattr(runtime,'Database',lambda path:db)
    monkeypatch.setattr(runtime,'create_provider',lambda *a,**kw:base)
    monkeypatch.setattr(runtime,'_build_tool_registry',lambda settings:ToolRegistry())
    monkeypatch.setattr('xclaw.scheduler.TaskScheduler',lambda **kw:SimpleNamespace(start=Mock(),stop=Mock()))
    monkeypatch.setattr('xclaw.channels.web.create_web_app',lambda **kw:captured.update(kw))
    async def turn(ctx,text,attachments=None):
        before=(ctx.llm.model,ctx.settings.model)
        if text=='first':entered.set();await release.wait()
        await asyncio.sleep(0)
        seen.append((text,before,(ctx.llm.model,ctx.settings.model)))
        return 'ok'
    monkeypatch.setattr(runtime,'agent_loop',turn)
    async def serve():
        handler=captured['message_handler']
        first=asyncio.create_task(handler('same','first',model=MODEL_A));await entered.wait()
        second=asyncio.create_task(handler('same','second',model=MODEL_B));await asyncio.sleep(.01)
        await handler('different','parallel',model=MODEL_B)
        assert [x[0] for x in seen]==['parallel']
        release.set();await asyncio.gather(first,second)
        await handler('legacy','default')
        with pytest.raises(ValueError):await handler('invalid','must not run',model='unknown')
    monkeypatch.setattr(runtime.uvicorn,'Config',lambda *a,**kw:None)
    monkeypatch.setattr(runtime.uvicorn,'Server',lambda config:SimpleNamespace(serve=serve))
    await runtime._run(s)
    assert sorted(seen)==sorted([(t,(m,m),(m,m)) for t,m in [('first',MODEL_A),('second',MODEL_B),('parallel',MODEL_B),('default',DEFAULT)]])
    assert base.model==s.model==DEFAULT


@pytest.mark.asyncio
async def test_subagent_inherits_bound_model_not_params(monkeypatch):
    s=settings();base=ResponsesProvider(api_key='',model=DEFAULT,base_url='http://test')
    bound,turn_settings=bind_request_model(base,s,MODEL_A)
    context=ToolContext(chat_id=1,channel='web',db=object(),llm=bound,settings=turn_settings)
    async def inner(ctx,task,max_iterations):
        assert ctx.llm is bound and ctx.settings.model==MODEL_A
        assert ctx.skip_session_persistence and ctx.skip_message_persistence
        return 'ok'
    monkeypatch.setattr('xclaw.agent_engine.agent_loop',inner)
    result=await SubAgentTool(ToolRegistry()).execute({'task':'test','model':MODEL_B},context)
    assert result.content=='ok'
    await base.close()

@pytest.mark.asyncio
async def test_compaction_and_followup_formatter_use_selected_model(tmp_path,monkeypatch):
    from xclaw.agent_engine import AgentContext, _compact_messages
    from xclaw.llm_types import LLMResponse, TextBlock, DoneEvent
    from xclaw.openui import OpenUITools
    from xclaw.ui_evidence import save_evidence, load_evidence
    import hashlib
    s=settings();base=ResponsesProvider(api_key='',model=DEFAULT,base_url='http://test')
    bound,turn_settings=bind_request_model(base,s,MODEL_A)
    calls=[]
    async def chat(self,*a,**kw):
        calls.append(('compact',self.model))
        return LLMResponse(stop_reason='end_turn',model=self.model,content=[TextBlock(text='summary')])
    async def stream(self,*a,**kw):
        calls.append(('formatter',self.model))
        yield DoneEvent(response=LLMResponse(stop_reason='end_turn',model=self.model,content=[TextBlock(text='{"placements":[]}')]))
    monkeypatch.setattr(ResponsesProvider,'chat',chat)
    monkeypatch.setattr(ResponsesProvider,'chat_stream',stream)
    db=Database(tmp_path/'models.db');await db.connect()
    try:
        ctx=AgentContext(chat_id=1,channel='web',db=db,llm=bound,settings=turn_settings,tools=ToolRegistry())
        await _compact_messages([Message(role='user',content='old'),Message(role='assistant',content='old reply'),Message(role='user',content='new')],ctx,1)
        await save_evidence(db,1,'answer','查看行情',[{'name':'stock_quote','arguments':{'symbol':'600519'},'content':'当前价: 100\n今开: 99\n最高: 101\n最低: 98\n成交量: 100手','is_error':False}],model=MODEL_A)
        snapshot=await load_evidence(db,1,hashlib.sha256(b'answer').hexdigest())
        assert snapshot['model']==MODEL_A
        # The later formatter starts from the global default context.
        later=SimpleNamespace(chat_id=1,db=db,llm=base,settings=s)
        async def factory(scope):return later
        service=OpenUITools(ToolRegistry(),factory)
        events=[event async for event in service.generate(later,'answer')]
        assert calls==[('compact',MODEL_A),('formatter',MODEL_A)]
        assert base.model==s.model==DEFAULT
    finally:
        await db.close();await base.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('created', [None, {}, {'model':None}])
async def test_strict_stream_withholds_unverified_deltas_until_terminal(created):
    events=[]
    if created is not None: events.append({'type':'response.created','response':created})
    events += [{'type':'response.output_text.delta','delta':'unverified text must not appear'},
               {'type':'response.completed','response':{'model':'fixture-a','output':[{'type':'message','content':[{'type':'output_text','text':'verified final'}]}]}}]
    p=await _strict_stream_provider(events)
    try:
        result=[e async for e in p.chat_stream([Message(role='user',content='x')])]
        assert [e.text for e in result if hasattr(e,'text')]==['verified final']
    finally: await p.close()

async def _strict_stream_provider(events):
    import json
    p=ResponsesProvider(api_key='',model=MODEL_A,base_url='http://test')
    p.require_model_identity=True
    body=''.join('data: '+json.dumps(e)+'\n\n' for e in events)
    await p._client.aclose()
    p._client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,text=body,headers={'content-type':'text/event-stream'})))
    return p

@pytest.mark.asyncio
@pytest.mark.parametrize('created', [None, [], 'invalid', {'model':MODEL_B}, {'model':[]}, {'model':''}])
async def test_strict_stream_rejects_malformed_or_wrong_created_before_delta(created):
    p=await _strict_stream_provider([{'type':'response.created','response':created},
        {'type':'response.output_text.delta','delta':'must not show'}])
    emitted=[]
    try:
        with pytest.raises(ResponsesProtocolError):
            async for e in p.chat_stream([Message(role='user',content='x')]):emitted.append(e)
        assert emitted==[]
    finally:await p.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('terminal_model', [None, MODEL_B, [], ''])
@pytest.mark.parametrize('verified_created', [False, True])
async def test_strict_stream_terminal_identity_always_checked(terminal_model,verified_created):
    events=([{'type':'response.created','response':{'model':MODEL_A}}] if verified_created else [])
    events += [{'type':'response.output_text.delta','delta':'provisional'},
        {'type':'response.completed','response':{'model':terminal_model,'output':[]}}]
    p=await _strict_stream_provider(events);emitted=[]
    try:
        with pytest.raises(ResponsesProtocolError):
            async for e in p.chat_stream([Message(role='user',content='x')]):emitted.append(e)
        assert [e.text for e in emitted]==(['provisional'] if verified_created else [])
        # No DoneEvent means the agent emits reset/draft_failed, never commits.
    finally:await p.close()

@pytest.mark.asyncio
async def test_strict_valid_created_preserves_incremental_stream():
    p=await _strict_stream_provider([{'type':'response.created','response':{'model':'fixture-a'}},
        {'type':'response.output_text.delta','delta':'part1'},
        {'type':'response.output_text.delta','delta':'part2'},
        {'type':'response.completed','response':{'model':MODEL_A,'output':[{'type':'message','content':[{'type':'output_text','text':'part1part2'}]}]}}])
    try:
        result=[e async for e in p.chat_stream([Message(role='user',content='x')])]
        assert [e.text for e in result if hasattr(e,'text')]==['part1','part2']
        assert result[-1].response.model==MODEL_A
    finally:await p.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('inline',[False,True])
async def test_terminal_identity_contradiction_retracts_agent_draft(tmp_path,inline):
    import json
    from xclaw.agent_engine import AgentContext,agent_loop
    from xclaw.text_stream import TextStreams
    from xclaw.responses import RejectedReply
    text=(json.dumps({'type':'text_delta','block_id':'p0','text':'provisional'})+'\n') if inline else 'provisional'
    p=await _strict_stream_provider([{'type':'response.created','response':{'model':MODEL_A}},
        {'type':'response.output_text.delta','delta':text},
        {'type':'response.completed','response':{'model':MODEL_B,'output':[]}}])
    db=Database(tmp_path/'retract.db');await db.connect()
    s=settings(openui_enabled=inline,openui_inline_stream=inline)
    ctx=AgentContext(chat_id=1,channel='web',db=db,llm=p,settings=s,tools=ToolRegistry(),skip_session_persistence=True,skip_message_persistence=True)
    streams=TextStreams();events=[]
    async def sink(event):
        events.append(event)
        await streams.publish('receipt',event)
    try:
        result=await agent_loop(ctx,'synthetic test',on_text_event=sink)
        assert isinstance(result,RejectedReply)
        assert any(e['type'] in {'delta','ui_snapshot'} for e in events)
        assert streams.snapshot('receipt')['text']==''
        assert not any(e['type']=='ui_commit' for e in events)
    finally:await db.close();await p.close()
