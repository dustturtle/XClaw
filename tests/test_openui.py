import hashlib
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from xclaw.db import Database
from xclaw.openui import OpenUITools, ToolRequest, GenerateRequest
from xclaw.tools import Tool, ToolRegistry, ToolResult

class Watch(Tool):
    name='watchlist_manage'
    description='test'
    parameters={'type':'object','properties':{'action':{'type':'string','enum':['list','add','remove']}},'required':['action']}
    calls=0
    async def execute(self,args,ctx):
        self.calls+=1
        return ToolResult('ok', args['action']=='remove')

@pytest.fixture
async def setup(tmp_path):
    db=Database(tmp_path/'test.db');await db.connect()
    t=Watch();r=ToolRegistry();r.register(t)
    async def context(scope):return SimpleNamespace(db=db,chat_id=1)
    yield OpenUITools(r,context),t,db,context
    await db.close()

async def test_idempotency_restart_conflict(setup,saved_legacy_button):
    s,t,db,ctx=setup
    req,token_hash=await saved_legacy_button(db,'u',{'action':'add'})
    a=await s.execute(req,'u',True,token_hash=token_hash)
    assert await OpenUITools(s.registry,ctx).execute(req,'u',True,token_hash=token_hash)==a
    assert t.calls==1
    with pytest.raises(HTTPException) as e:await s.execute(req.model_copy(update={'arguments':{'action':'remove'}}),'u',True,token_hash=token_hash)
    assert e.value.status_code==403

async def test_query_boundary_schema_failure(setup,saved_legacy_button):
    s,t,db,_=setup
    with pytest.raises(HTTPException) as e:await s.execute(ToolRequest(name=t.name,arguments={'action':'add'}),'u',False)
    assert e.value.status_code==403
    with pytest.raises(HTTPException) as e:await s.execute(ToolRequest(name=t.name,arguments={'action':'list','chat_id':2}),'u',False)
    assert e.value.status_code==422
    req,token_hash=await saved_legacy_button(db,'u',{'action':'remove'})
    with pytest.raises(HTTPException):await s.execute(req,'u',True,token_hash=token_hash)
    with pytest.raises(HTTPException) as e:await s.execute(req,'u',True,token_hash=token_hash)
    assert e.value.detail['code']=='outcome_unknown'
    assert t.calls==1

async def test_concurrent(setup,saved_legacy_button):
    import asyncio
    s,t,db,_=setup
    req,token_hash=await saved_legacy_button(db,'u',{'action':'add'})
    await asyncio.gather(*(s.execute(req,'u',True,token_hash=token_hash) for _ in range(5)))
    assert t.calls==1
    assert s.capabilities()['queries'][0]['parameters']['properties']['action']['enum']==['list']

async def test_generation_rejects_unbound_reply(setup):
    s,_,_,_=setup
    with pytest.raises(HTTPException) as e:await s.generation_context(GenerateRequest(reply_hash='0'*64),'u')
    assert e.value.status_code==409

async def test_auth(setup):
    from xclaw.channels.web import create_web_app
    import httpx
    s,_,_,_=setup
    async def message(*args):return 'ok'
    app=create_web_app(message,auth_token='test',openui_service=s)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get('/api/openui/capabilities')).status_code==401
        assert (await client.get('/api/openui/capabilities',headers={'Authorization':'Bearer test'})).status_code==200

async def test_generation_success_and_stale(setup):
    from xclaw.llm_types import TextDeltaEvent, DoneEvent, LLMResponse, TextBlock
    s,_,db,ctx=setup
    await seed_presentation_evidence(db)
    # Replace only DB read seam; never touch real business data.
    async def recent(*args,**kwargs):return [{'role':'assistant','content':'answer'}]
    db.get_recent_messages=recent
    class LLM:
        async def chat_stream(self,*args,**kwargs):
            yield TextDeltaEvent(text='root = Col([])')
            yield DoneEvent(response=LLMResponse(stop_reason='end_turn', content=[TextBlock(text='root = Col([])')]))
    async def context(scope):return SimpleNamespace(db=db,chat_id=1,llm=LLM())
    s.context_factory=context
    with pytest.raises(HTTPException) as e:await s.generation_context(GenerateRequest(reply_hash='0'*64),'u')
    assert e.value.detail['code']=='stale_reply'
    c,r=await s.generation_context(GenerateRequest(reply_hash=hashlib.sha256(b'answer').hexdigest()),'u')
    events=[event async for event in s.generate(c,r)]
    assert [e['event'] for e in events]==['status','narrative_start','done']

async def test_generation_failure_retains_text(setup):
    s,_,_,_=setup
    await seed_presentation_evidence(setup[2])
    class LLM:
        async def chat_stream(self,*args,**kwargs):
            raise RuntimeError('SECRET')
            yield
    events=[event async for event in s.generate(SimpleNamespace(llm=LLM(),db=setup[2],chat_id=1),'answer')]
    assert events[-1]['event']=='error'
    assert 'SECRET' not in str(events)

async def test_resource_limits(setup):
    s,_,_,_=setup
    s.active=8
    with pytest.raises(HTTPException) as e:
        async with s.admitted():pass
    assert e.value.status_code==429
    s.active=0
    for _ in range(60):
        async with s.admitted():pass
    with pytest.raises(HTTPException):
        async with s.admitted():pass

@pytest.mark.parametrize('value,schema',[(1,{'minimum':2}),(9,{'maximum':8}),('x',{'minLength':2}),('abc',{'maxLength':2}),('x',{'pattern':'^[0-9]+$'})])
def test_schema_constraints(value,schema):
    from xclaw.openui import validate
    with pytest.raises(ValueError):validate(value,schema)

async def test_body_limit(setup):
    from xclaw.channels.web import create_web_app
    import httpx
    s,_,_,_=setup
    async def message(*args):return 'ok'
    app=create_web_app(message,openui_service=s)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post('/api/openui/query',content=b' '*65537)).status_code==413

async def test_strategy_runs_projection_isolation(setup):
    s,_,db,_=setup
    class Strategy(Watch):name='strategy_scan'
    s.registry.register(Strategy())
    for chat_id in (1,2):
        await db.add_strategy_run(chat_id=chat_id,symbol=f'S{chat_id}',market='CN',
            strategies=[{'strategy_id':'demo','bias_score':5,'chat_id':99,'path':'SECRET'}],valuable_strategies=[])
    async def context(scope):return SimpleNamespace(db=db,chat_id={'a':1,'b':2}[scope])
    s.context_factory=context
    req=ToolRequest(name='strategy_runs',arguments={'limit':1})
    a=await s.execute(req,'a',False)
    b=await s.execute(req,'b',False)
    assert 'S1' in str(a) and 'S2' not in str(a)
    assert 'S2' in str(b) and 'S1' not in str(b)
    assert 'SECRET' not in str(a) and 'chat_id' not in str(a)
    assert any(t['name']=='strategy_runs' for t in s.capabilities()['queries'])
    for args in ({'limit':0},{'limit':21},{'chat_id':2}):
        with pytest.raises(HTTPException):await s.execute(req.model_copy(update={'arguments':args}),'a',False)
    with pytest.raises(HTTPException):await s.execute(req,'a',True)

async def test_strategy_runs_requires_enabled_capability(setup):
    s,_,_,_=setup
    assert not any(t['name']=='strategy_runs' for t in s.capabilities()['queries'])
    with pytest.raises(HTTPException):await s.execute(ToolRequest(name='strategy_runs'),'a',False)

@pytest.mark.parametrize('mode',['missing_done','truncated','tool','mismatch'])
async def test_generation_rejects_incomplete_stream(setup,mode):
    await seed_presentation_evidence(setup[2])
    from xclaw.llm_types import TextDeltaEvent, DoneEvent, LLMResponse, TextBlock, ToolUseBlock
    s,_,_,_=setup
    class LLM:
        async def chat_stream(self,*args,**kwargs):
            yield TextDeltaEvent(text='partial')
            if mode=='missing_done':return
            blocks=[TextBlock(text='different' if mode=='mismatch' else 'partial')]
            if mode=='tool':blocks.append(ToolUseBlock(id='x',name='bash',input={}))
            yield DoneEvent(response=LLMResponse(stop_reason='max_tokens' if mode=='truncated' else 'end_turn',content=blocks))
    events=[event async for event in s.generate(SimpleNamespace(llm=LLM(),db=setup[2],chat_id=1),'answer')]
    assert events[-1]['event']=='error'
    assert all(event['event']!='done' for event in events)
    assert '"clear": true' in events[-1]['data']

async def test_snapshot_is_bound_and_does_not_refetch(setup):
    from xclaw.ui_evidence import save_evidence
    s,t,db,_=setup
    h=hashlib.sha256(b'reply').hexdigest()
    await save_evidence(db,1,'reply','question',[{'name':t.name,'arguments':{'action':'list'},'content':'original','is_error':False}])
    req=ToolRequest(name=t.name,arguments={'action':'list'},reply_hash=h)
    result=await s.execute(req,'u',False)
    assert result['data']['content']=='original'
    assert result['evidence']['mode']=='snapshot'
    assert t.calls==0
    await s.execute(req.model_copy(update={'refresh':True}),'u',False)
    assert t.calls==1

async def test_snapshot_missing_cross_chat_and_failed_are_closed(setup):
    from xclaw.ui_evidence import save_evidence
    s,t,db,_=setup
    h=hashlib.sha256(b'reply').hexdigest()
    evidence=[{'name':t.name,'arguments':{'action':'list'},'content':'bad','is_error':True}]
    await save_evidence(db,2,'reply','question',evidence)
    req=ToolRequest(name=t.name,arguments={'action':'list'},reply_hash=h)
    with pytest.raises(HTTPException) as error: await s.execute(req,'u',False)
    assert error.value.detail['code']=='snapshot_missing'
    await save_evidence(db,1,'reply','question',evidence)
    with pytest.raises(HTTPException) as error: await s.execute(req,'u',False)
    assert error.value.detail['code']=='evidence_failed'
    assert t.calls==0

async def test_snapshot_excludes_private_and_mutating_tools(setup):
    from xclaw.ui_evidence import save_evidence,load_evidence
    _,_,db,_=setup
    await save_evidence(db,1,'reply','question',[{'name':'read_memory','arguments':{},'content':'private','is_error':False}, {'name':'watchlist_manage','arguments':{'action':'add'},'content':'write','is_error':False}])
    result=await load_evidence(db,1,hashlib.sha256(b'reply').hexdigest())
    # Mutating/private output remains excluded; the only allowed addition is an
    # actual read-only post-write DB projection, never the claimed write text.
    assert result['evidence']==[{'name':'watchlist_manage','arguments':{'action':'list'},'content':'自选股列表为空','is_error':False}]
    assert all(item['content'] not in {'private','write'} for item in result['evidence'])

async def test_presentation_persistence_is_chat_scoped(setup):
    from xclaw.ui_evidence import save_presentation,load_presentation
    _,_,db,_=setup
    await save_presentation(db,1,'a'*64,'root = Col([])')
    assert await load_presentation(db,1,'a'*64)=='root = Col([])'
    assert await load_presentation(db,2,'a'*64) is None
    assert await load_presentation(db,1,'b'*64) is None

async def test_completed_action_state_survives_new_instance_and_is_chat_scoped(setup,saved_legacy_button):
    from xclaw.ui_evidence import action_state
    s,t,db,ctx=setup
    req,token_hash=await saved_legacy_button(db,'u',{'action':'add'})
    h=req.reply_hash
    await s.execute(req,'u',True,token_hash=token_hash)
    assert await action_state(db,1,h)=={'dirty':['watchlist_manage'],'completed':['确认添加']}
    assert await action_state(db,2,h)=={'dirty':[],'completed':[]}
    await OpenUITools(s.registry,ctx).execute(req,'u',True,token_hash=token_hash)
    assert t.calls==1

async def test_narrative_generation_preserves_prose_and_persists(setup):
    import json
    from xclaw.llm_types import TextDeltaEvent,DoneEvent,LLMResponse,TextBlock
    from xclaw.ui_evidence import load_presentation
    s,_,db,_=setup
    reply='结论 17.03，盘中。\n\n后续解读，非投资建议。'
    await seed_presentation_evidence(db, reply)
    source=json.dumps({'placements':[{'after':0,'source':'q=Query("watchlist_manage", {action:"list"})\nroot=Col([Watchlist("自选",q)])'}]})
    class LLM:
        async def chat_stream(self,*args,**kwargs):
            yield TextDeltaEvent(text=source)
            yield DoneEvent(response=LLMResponse(stop_reason='end_turn',content=[TextBlock(text=source)]))
    events=[event async for event in s.generate(SimpleNamespace(llm=LLM(),db=db,chat_id=1),reply)]
    rendered=json.loads(events[-1]['data'])['source']
    blocks=json.loads(rendered)['blocks']
    assert [b['type'] for b in blocks]==['text','openui','text']
    assert blocks[0]['source']=='结论 17.03，盘中。'
    assert await load_presentation(db,1,hashlib.sha256(reply.encode()).hexdigest())==rendered


async def seed_presentation_evidence(db, reply='answer', question='查询行情', name='stock_quote', arguments=None, is_error=False):
    from xclaw.ui_evidence import save_evidence
    await save_evidence(db, 1, reply, question, [{'name':name, 'arguments':arguments or {'symbol':'600519','market':'CN','asset_type':'stock'}, 'content':'真实测试数据', 'is_error':is_error}])

@pytest.mark.parametrize('question,outcome', [('谢谢','text_only'), ('画走势图','no_data'), ('不要图表，只回复一句话','text_only')])
async def test_no_evidence_skips_model_and_organizing_status(setup,question,outcome):
    import json
    from xclaw.ui_evidence import save_evidence
    await save_evidence(setup[2],1,'answer',question,[])
    class Never:
        def chat_stream(self,*args,**kwargs): raise AssertionError('model must not run')
    events=[e async for e in setup[0].generate(SimpleNamespace(db=setup[2],chat_id=1,llm=Never()),'answer')]
    assert [e['event'] for e in events]==['done']
    assert json.loads(events[0]['data'])['outcome']==outcome

async def test_empty_layout_has_exact_readonly_quote_fallback(setup):
    import json
    from xclaw.llm_types import DoneEvent,LLMResponse,TextBlock
    args={'symbol':'600519','market':'CN','asset_type':'stock'}
    await seed_presentation_evidence(setup[2],arguments=args)
    class Empty:
        async def chat_stream(self,*args,**kwargs):
            yield DoneEvent(response=LLMResponse(stop_reason='end_turn',content=[TextBlock(text='{"placements":[]}')]))
    events=[e async for e in setup[0].generate(SimpleNamespace(db=setup[2],chat_id=1,llm=Empty()),'answer')]
    result=json.loads(events[-1]['data'])
    assert result['outcome']=='rendered'
    blocks=json.loads(result['source'])['blocks']
    assert blocks[0]['source']=='answer'
    assert json.dumps(args,ensure_ascii=False) in blocks[1]['source']
    assert 'QuoteCard' in blocks[1]['source'] and 'Mutation' not in blocks[1]['source']

async def test_explicit_text_only_skips_even_successful_quote(setup):
    await seed_presentation_evidence(setup[2],question='只用文字，不要图表')
    class Never:
        def chat_stream(self,*args,**kwargs): raise AssertionError('model must not run')
    events=[e async for e in setup[0].generate(SimpleNamespace(db=setup[2],chat_id=1,llm=Never()),'answer')]
    assert [e['event'] for e in events]==['done']

@pytest.mark.parametrize('rejected', [False, True])
async def test_ambiguous_or_rejected_quote_empty_is_persisted_not_silent(setup,rejected):
    import json
    from xclaw.ui_evidence import save_evidence,load_presentation
    from xclaw.llm_types import DoneEvent,LLMResponse,TextBlock
    reply='数据异常，不采用' if rejected else '两个不同时间的报价'
    rows=[{'name':'stock_quote','arguments':{'symbol':x},'content':'test','is_error':False} for x in (['600519'] if rejected else ['600519','000001'])]
    await save_evidence(setup[2],1,reply,'查询行情',rows)
    class Empty:
        async def chat_stream(self,*args,**kwargs):
            yield DoneEvent(response=LLMResponse(stop_reason='end_turn',content=[TextBlock(text='{"placements":[]}')]))
    events=[e async for e in setup[0].generate(SimpleNamespace(db=setup[2],chat_id=1,llm=Empty()),reply)]
    result=json.loads(events[-1]['data'])
    assert result['outcome']=='empty'
    persisted=await load_presentation(setup[2],1,hashlib.sha256(reply.encode()).hexdigest())
    assert json.loads(persisted)=={'version':'xclaw-empty-1','outcome':'empty'}

async def test_unknown_native_write_blocks_new_ui_mutation_but_allows_read(setup):
    from xclaw.tool_execution_journal import execute_with_journal, inspect_journal
    s,t,db,ctx=setup
    native=SimpleNamespace(db=db,chat_id=1,tools=s.registry)
    failed=await execute_with_journal(native,SimpleNamespace(id='native-failed',name=t.name,input={'action':'remove'}),await ctx('u'))
    assert failed.is_error and t.calls==1
    with pytest.raises(HTTPException):await s.execute(ToolRequest(name=t.name,arguments={'action':'add'},request_id='different-ui-id'),'u',True)
    assert t.calls==1
    assert (await s.execute(ToolRequest(name=t.name,arguments={'action':'list'}),'u',False))['ok']
    assert t.calls==2
    assert any(r['state']=='unknown' for r in await inspect_journal(db,1))

async def test_unknown_ui_write_blocks_new_native_write_without_replay(setup,saved_legacy_button):
    from xclaw.tool_execution_journal import execute_with_journal
    s,t,db,ctx=setup
    req,token_hash=await saved_legacy_button(db,'u',{'action':'remove'})
    with pytest.raises(HTTPException):await s.execute(req,'u',True,token_hash=token_hash)
    result=await execute_with_journal(SimpleNamespace(db=db,chat_id=1,tools=s.registry),SimpleNamespace(id='new-native-id',name=t.name,input={'action':'add'}),await ctx('u'))
    assert result.is_error and t.calls==1

async def test_server_generation_guard_blocks_writes_and_live_reads_without_client_flag(setup):
    from xclaw.llm_types import TextDeltaEvent,DoneEvent,LLMResponse,TextBlock
    s,tool,db,context=setup
    await seed_presentation_evidence(db)
    class LLM:
        async def chat_stream(self,*a,**kw):
            yield TextDeltaEvent(text='{"placements":[]}')
            yield DoneEvent(response=LLMResponse(stop_reason='end_turn',content=[TextBlock(text='{"placements":[]}')]))
    ctx=SimpleNamespace(llm=LLM(),db=db,chat_id=1)
    stream=s.generate(ctx,'answer');await anext(stream)
    before=tool.calls
    for mutation,args in [(True,{'action':'add'}),(False,{'action':'list'})]:
        with pytest.raises(HTTPException) as error:
            await s.execute(ToolRequest(name='watchlist_manage',arguments=args,request_id='blocked'), 'u', mutation)
        assert error.value.detail['code']==('presentation_in_progress' if mutation else 'snapshot_required')
    with pytest.raises(HTTPException) as error:
        await s.execute(ToolRequest(name='watchlist_manage',arguments={'action':'list'},reply_hash=hashlib.sha256(b'answer').hexdigest(),refresh=True),'u',False)
    assert error.value.detail['code']=='snapshot_required'
    assert tool.calls==before
    await stream.aclose();assert not s.generating
