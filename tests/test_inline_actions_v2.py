import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from xclaw.inline_ui_stream import InlineUIProtocol
from xclaw.ui_presentations_v2 import save_v2, load_v2, canonical_action, presentation_metadata_for_messages, v2_action_state
from xclaw.openui import OpenUITools, ToolRequest
from xclaw.tools import ToolRegistry, ToolContext
from xclaw.tools.watchlist import WatchlistManageTool
from xclaw.llm_types import ToolUseBlock
from xclaw.agent_engine import AgentContext, _execute_tool_batch
from xclaw.tool_execution_journal import inspect_journal

ARGS={'action':'add','symbol':'600519.SH','market':'CN'}


def proposal():
    parser=InlineUIProtocol([],0,allow_actions=True)
    parser.record({'type':'text_delta','block_id':'p0','text':'待确认添加自选，尚未执行。'})
    parser.record({'type':'text_end','block_id':'p0'})
    parser.record({'type':'action','after_block_id':'p0','action_id':'a0','name':'watchlist_manage','arguments':ARGS})
    return parser


async def saved(db,pid='a'*32):
    chat=await db.get_or_create_chat('web','isolated')
    p=proposal();mid=await db.save_message(chat,'assistant',p.text)
    value=await save_v2(db,chat,mid,pid,p.text,'先确认',[],p.finish(),p.actions)
    return chat,value


def test_action_only_emerges_at_final_persist():
    p=proposal()
    assert all(b['type']!='action' for b in p.snapshot()['blocks'])
    assert all(b['type']!='action' for b in p.finish()['blocks'])
    assert 'Mutation' not in json.dumps(p.snapshot())
    q=InlineUIProtocol([],0)
    with pytest.raises(ValueError):q.record({'type':'action'})


@pytest.mark.parametrize('name,args',[
    ('stock_quote',ARGS), ('watchlist_manage',{**ARGS,'market':'OTHER'}),
    ('watchlist_manage',{**ARGS,'symbol':{}}),('watchlist_manage',{**ARGS,'notes':{}}),
    ('portfolio_manage',{'action':'sell','symbol':'x','market':'CN'}),
    ('portfolio_manage',{'action':'buy','symbol':'x','market':'CN','shares':True,'price':5}),
])
def test_actions_strict(name,args):
    with pytest.raises(ValueError):canonical_action(name,args)


async def test_same_reply_independent_immutable_plans_and_history(db):
    chat,a=await saved(db,'a'*32);_,b=await saved(db,'b'*32)
    assert a['reply_hash']==b['reply_hash']
    assert a['plans'][0]['action_plan_id']!=b['plans'][0]['action_plan_id']
    assert a['message_id']!=b['message_id']
    metadata=await presentation_metadata_for_messages(db,chat,[a['message_id'],b['message_id']])
    assert metadata[a['message_id']]['presentation_id']=='a'*32
    assert await load_v2(db,chat+1,'a'*32) is None
    with pytest.raises(Exception):await saved(db,'a'*32)
    assert (await load_v2(db,chat,'a'*32))['message_id']==a['message_id']


async def test_bound_mutation_exact_once_and_no_legacy_escape(db):
    chat,value=await saved(db)
    registry=ToolRegistry();registry.register(WatchlistManageTool())
    async def context(scope):return ToolContext(chat_id=chat,channel='web',db=db)
    service=OpenUITools(registry,context)
    req=ToolRequest(name='watchlist_manage',arguments=ARGS,presentation_id=value['presentation_id'],
                    action_plan_id=value['plans'][0]['action_plan_id'],reply_hash=value['reply_hash'])
    import asyncio
    results=await asyncio.gather(service.execute(req,'scope',True),service.execute(req,'scope',True))
    assert results[0]==results[1]
    assert len(await db.get_watchlist(chat))==1
    assert len(await inspect_journal(db,chat))==1
    state=await v2_action_state(db,chat,value)
    assert state['actions'][0]['status']=='completed'
    for invalid in [req.model_copy(update={'arguments':{**ARGS,'symbol':'000001'}}),
                    req.model_copy(update={'action_plan_id':'b'*32+'_a0'}),
                    req.model_copy(update={'presentation_id':None,'action_plan_id':None}),
                    req.model_copy(update={'reply_hash':None})]:
        with pytest.raises(HTTPException):await service.execute(invalid,'scope',True)


async def test_prepare_blocks_write_before_journal_but_allows_read_and_plan(db):
    chat=await db.get_or_create_chat('web','prepare')
    registry=ToolRegistry();registry.register(WatchlistManageTool())
    ctx=AgentContext(chat_id=chat,channel='web',db=db,llm=None,tools=registry,execution_mode='prepare')
    toolctx=ToolContext(chat_id=chat,channel='web',db=db)
    calls=[ToolUseBlock(id='a',name='watchlist_manage',input=ARGS),
           ToolUseBlock(id='b',name='watchlist_manage',input={'action':'list'})]
    results=await _execute_tool_batch(ctx,calls,toolctx)
    assert results[0].is_error and results[0].prepare_blocked
    assert not results[1].is_error
    assert await db.get_watchlist(chat)==[] and await inspect_journal(db,chat)==[]
    assert len(proposal().actions)==1

@pytest.mark.parametrize('kind',['wrong_chat','wrong_message','wrong_text'])
async def test_wrong_actual_message_cannot_mint_plan(db,kind):
    chat=await db.get_or_create_chat('web','authentic')
    p=proposal();mid=await db.save_message(chat,'assistant',p.text)
    if kind=='wrong_chat':chat+=100
    elif kind=='wrong_message':mid+=100
    text=p.text if kind!='wrong_text' else 'different'
    with pytest.raises(ValueError,match='message_mismatch'):
        await save_v2(db,chat,mid,'c'*32,text,'q',[],p.finish(),p.actions)
    assert await load_v2(db,chat,'c'*32) is None


@pytest.mark.parametrize('text',['记住：不要执行','/reset'])
async def test_prepare_builtin_write_paths_are_not_executed(db,text):
    from unittest.mock import AsyncMock
    from xclaw.agent_engine import agent_loop
    from xclaw.responses import RejectedReply
    memory=SimpleNamespace(add=AsyncMock())
    ctx=AgentContext(chat_id=1,channel='web',db=db,llm=None,tools=ToolRegistry(),
                     execution_mode='prepare',structured_memory=memory)
    reply=await agent_loop(ctx,text)
    assert isinstance(reply,RejectedReply)
    memory.add.assert_not_awaited()

async def test_model_attempts_direct_write_then_generates_confirm_plan(db):
    from xclaw.agent_engine import agent_loop
    from xclaw.config import Settings
    from xclaw.llm_types import DoneEvent, LLMResponse, TextDeltaEvent, TextBlock, StopReason
    chat=await db.get_or_create_chat('web','attempt')
    registry=ToolRegistry();registry.register(WatchlistManageTool())
    class Model:
        count=0
        async def chat(self, **kwargs):
            raise AssertionError('stream expected')
        async def chat_stream(self,**kwargs):
            self.count+=1
            if self.count==1:
                yield DoneEvent(response=LLMResponse(stop_reason=StopReason.tool_use,
                    content=[ToolUseBlock(id='attempt',name='watchlist_manage',input=ARGS)]))
                return
            records=[{'type':'text_delta','block_id':'p0','text':'尚未执行，请确认。'},
                     {'type':'text_end','block_id':'p0'},
                     {'type':'action','after_block_id':'p0','action_id':'a0','name':'watchlist_manage','arguments':ARGS}]
            wire='\n'.join(json.dumps(r,ensure_ascii=False) for r in records)+'\n'
            yield TextDeltaEvent(text=wire)
            yield DoneEvent(response=LLMResponse(stop_reason=StopReason.end_turn,content=[TextBlock(text=wire)]))
    settings=Settings(openui_enabled=True,openui_inline_stream=True,max_tool_iterations=3)
    ctx=AgentContext(chat_id=chat,channel='web',db=db,llm=Model(),tools=registry,settings=settings,execution_mode='prepare')
    events=[]
    async def sink(e):events.append(e)
    reply=await agent_loop(ctx,'先展示确认，不要直接执行',on_text_event=sink)
    assert reply=='尚未执行，请确认。' and len(ctx.inline_actions)==1
    assert ctx.assistant_message_id>0
    assert ctx.tool_evidence==[]
    assert await db.get_watchlist(chat)==[] and await inspect_journal(db,chat)==[]
    assert all(not any(b['type']=='action' for b in e.get('presentation',{}).get('blocks',[])) for e in events)

async def test_preclaim_block_does_not_poison_plan_after_explicit_ack(db):
    from xclaw.tool_execution_journal import _SCHEMA, acknowledge_unknown
    chat,value=await saved(db)
    await db.conn.executescript(_SCHEMA)
    await db.conn.execute("INSERT INTO tool_execution_journal(id,chat_id,call_id,fingerprint,tool_name,arguments_json,state) VALUES(?,?,?,?,?,?,?)",
        ('old-unknown',str(chat),'old-call','hash','watchlist_manage','{}','unknown'))
    await db.conn.commit()
    registry=ToolRegistry();registry.register(WatchlistManageTool())
    async def context(scope):return ToolContext(chat_id=chat,channel='web',db=db)
    service=OpenUITools(registry,context)
    req=ToolRequest(name='watchlist_manage',arguments=ARGS,presentation_id=value['presentation_id'],
        action_plan_id=value['plans'][0]['action_plan_id'],reply_hash=value['reply_hash'])
    with pytest.raises(HTTPException) as denied:await service.execute(req,'scope',True)
    assert denied.value.detail['code']=='not_started'
    async with db.conn.execute('SELECT count(*) FROM openui_mutations') as cursor:assert (await cursor.fetchone())[0]==0
    assert await db.get_watchlist(chat)==[]
    await acknowledge_unknown(db,chat,'old-unknown',confirmed_reviewed=True,execution_stopped=True,note='Isolated test verified no write')
    # Acknowledgement alone is not execution. Only the next explicit request runs.
    assert await db.get_watchlist(chat)==[]
    result=await service.execute(req,'scope',True)
    assert result['ok'] and len(await db.get_watchlist(chat))==1
    await service.execute(req,'scope',True)
    assert len(await db.get_watchlist(chat))==1


def test_nav_notice_keeps_exact_visible_prefix_and_whitespace():
    from xclaw.agent_engine import _finalize_evidence_limits
    from xclaw.llm_types import Message
    text='净值数据尚未独立核验。  \n'
    messages=[Message(role='assistant',content=text)]
    result=_finalize_evidence_limits(text,messages,[{'name':'cn_fund_nav','is_error':False,
        'content':json.dumps({'symbol':'000001','identity_verified':False,'nav':[{'date':'2026-11-17','unit_nav':1.0}]})}])
    assert result.startswith(text+'\n\n')
    assert '基金身份未独立核验' in result
    assert messages[-1].content==result
