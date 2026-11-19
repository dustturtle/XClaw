"""Independent v2 adversarial tests, temporary database only."""
import asyncio
import hashlib
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from xclaw.ui_presentations_v2 import save_v2,load_v2,canonical_action,v2_action_state
from xclaw.inline_ui_stream import InlineUIProtocol
from xclaw.openui import OpenUITools,ToolRequest
from xclaw.tools import ToolRegistry,ToolContext
from xclaw.tools.watchlist import WatchlistManageTool
from xclaw.tool_execution_journal import inspect_journal

ARGS={'action':'add','symbol':'600519.SH','market':'CN'}
async def setup(db,pid='c'*32):
 chat=await db.get_or_create_chat('web','v2-security-review')
 p=InlineUIProtocol([],0,allow_actions=True)
 p.record({'type':'text_delta','block_id':'p0','text':'仅准备，待确认'})
 p.record({'type':'text_end','block_id':'p0'})
 p.record({'type':'action','after_block_id':'p0','action_id':'a0','name':'watchlist_manage','arguments':ARGS})
 mid=await db.save_message(chat,'assistant',p.text)
 value=await save_v2(db,chat,mid,pid,p.text,'q',[],p.finish(),p.actions)
 registry=ToolRegistry();registry.register(WatchlistManageTool())
 async def context(scope):return ToolContext(chat_id=chat if scope!='other-chat' else chat+1,channel='web',db=db)
 service=OpenUITools(registry,context)
 req=ToolRequest(name='watchlist_manage',arguments=ARGS,presentation_id=pid,action_plan_id=value['plans'][0]['action_plan_id'],reply_hash=value['reply_hash'],message_id=mid)
 return chat,value,service,req

@pytest.mark.parametrize('change',[{'presentation_id':None},{'presentation_id':'d'*32},{'action_plan_id':None},{'message_id':99999},{'reply_hash':None},{'arguments':{**ARGS,'notes':'changed'}}])
async def test_identity_and_argument_tampering_never_writes(db,change):
 chat,value,s,req=await setup(db)
 with pytest.raises(HTTPException):await s.execute(req.model_copy(update=change),'scope',True)
 assert not await db.get_watchlist(chat)
 assert not await inspect_journal(db,chat)

async def test_cross_chat_never_writes(db):
 chat,value,s,req=await setup(db)
 with pytest.raises(HTTPException):await s.execute(req,'other-chat',True)
 assert not await db.get_watchlist(chat)

async def test_request_id_rotation_cannot_repeat_plan(db):
 chat,value,s,req=await setup(db)
 results=await asyncio.gather(*(s.execute(req.model_copy(update={'request_id':str(i)}),'scope',True) for i in range(3)))
 assert results[0]==results[1]==results[2]
 assert len(await inspect_journal(db,chat))==1
 assert len(await db.get_watchlist(chat))==1

@pytest.mark.parametrize('field,bad',[('shares',True),('shares',float('nan')),('shares',float('inf')),('price',True),('price',-1)])
def test_portfolio_numeric_types_not_equal_under_python_coercion(field,bad):
 args={'action':'buy','symbol':'600519','market':'CN','shares':1,'price':1,field:bad}
 with pytest.raises(ValueError):canonical_action('portfolio_manage',args)

async def test_save_must_bind_actual_assistant_message(db):
 chat=await db.get_or_create_chat('web','bad-binding')
 mid=await db.save_message(chat,'user','not the assistant')
 with pytest.raises(ValueError):
  await save_v2(db,chat,mid,'e'*32,'fabricated reply','q',[],{'version':'xclaw-narrative-1','blocks':[{'id':'p0','type':'text','source':'fabricated reply'}]},[])

async def test_unknown_other_call_blocks_new_plan_and_request(db):
 import sqlite3
 from xclaw.tool_execution_journal import _SCHEMA
 chat,value,s,req=await setup(db)
 with sqlite3.connect(str(db._path)) as conn:
  conn.executescript(_SCHEMA)
  conn.execute("INSERT INTO tool_execution_journal(id,chat_id,call_id,fingerprint,tool_name,arguments_json,state) VALUES(?,?,?,?,?,?,?)",('unknown-id',str(chat),'other-call','hash','portfolio_manage','{}','unknown'))
 state=await v2_action_state(db,chat,value,'scope')
 assert state['blocked'] and state['unknown_barrier']
 for request in ['new-one','new-two']:
  with pytest.raises(HTTPException):await s.execute(req.model_copy(update={'request_id':request}),'scope',True)
 assert not await db.get_watchlist(chat)
 assert len(await inspect_journal(db,chat))==1

async def test_deleted_message_invalidates_loaded_plan(db):
 import aiosqlite
 chat,value,s,req=await setup(db)
 async with aiosqlite.connect(str(db._path)) as conn:
  await conn.execute('DELETE FROM messages WHERE id=?',(value['message_id'],));await conn.commit()
 assert await load_v2(db,chat,value['presentation_id']) is None
 with pytest.raises(HTTPException):await s.execute(req,'scope',True)
 assert not await db.get_watchlist(chat)

async def test_same_reply_other_plan_cannot_substitute(db):
 chat,a,s,req=await setup(db,'a'*32)
 _,b,_,_=await setup(db,'b'*32)
 assert a['reply_hash']==b['reply_hash']
 with pytest.raises(HTTPException):
  await s.execute(req.model_copy(update={'action_plan_id':b['plans'][0]['action_plan_id']}),'scope',True)
 assert not await db.get_watchlist(chat)

@pytest.mark.parametrize('command',['记住: 不应写入这个偏好','/reset'])
async def test_prepare_shortcut_neither_memory_write_nor_session_reset(db,command):
 from unittest.mock import AsyncMock
 from xclaw.agent_engine import AgentContext,agent_loop
 from xclaw.memory import StructuredMemory
 chat=await db.get_or_create_chat('web','prepare-shortcuts-review')
 original=[{'role':'user','content':'保留原上下文'}]
 await db.save_session(chat,original)
 memory=StructuredMemory(db);llm=SimpleNamespace(chat=AsyncMock())
 ctx=AgentContext(chat_id=chat,channel='web',db=db,llm=llm,tools=ToolRegistry(),structured_memory=memory,execution_mode='prepare')
 result=await agent_loop(ctx,command)
 assert '未执行' in result
 assert await memory.get_all(chat)==[]
 assert await db.load_session(chat)==original
 llm.chat.assert_not_awaited()

async def test_subagent_subclass_does_not_inherit_prepare_exception(db):
 from xclaw.agent_engine import AgentContext,_execute_tool_batch
 from xclaw.tools.sub_agent import SubAgentTool
 from xclaw.llm_types import ToolUseBlock
 class Pretender(SubAgentTool):
  async def execute(self,args,context):raise AssertionError('must not execute')
 chat=await db.get_or_create_chat('web','subclass-prepare-review')
 registry=ToolRegistry();registry.register(Pretender(registry))
 ctx=AgentContext(chat_id=chat,channel='web',db=db,llm=None,tools=registry,execution_mode='prepare')
 result=await _execute_tool_batch(ctx,[ToolUseBlock(id='sub',name='sub_agent',input={'task':'write'})],ToolContext(chat_id=chat,channel='web',db=db))
 assert result[0].prepare_blocked
 assert not await inspect_journal(db,chat)

async def test_typed_not_applied_cannot_release_receipt_with_own_unknown_claim(db,monkeypatch):
 import sqlite3
 from xclaw.tool_execution_journal import _SCHEMA
 from xclaw.tools import ToolResult,ToolExecutionEffect
 chat,value,s,req=await setup(db)
 call='openui:'+value['plans'][0]['request_id']
 with sqlite3.connect(str(db._path)) as conn:
  conn.executescript(_SCHEMA)
  conn.execute("INSERT INTO tool_execution_journal(id,chat_id,call_id,fingerprint,tool_name,arguments_json,state) VALUES(?,?,?,?,?,?,?)",('own-unknown',str(chat),call,'hash','watchlist_manage','{}','unknown'))
 async def misleading(*args):return ToolResult('not applied',True,execution_effect=ToolExecutionEffect.NOT_APPLIED)
 monkeypatch.setattr('xclaw.tool_execution_journal.execute_with_journal',misleading)
 with pytest.raises(HTTPException) as result:await s.execute(req,'scope',True)
 assert result.value.detail['code']=='outcome_unknown'
 with sqlite3.connect(str(db._path)) as conn:
  row=conn.execute('SELECT result FROM openui_mutations WHERE scope=? AND request_id=?',('scope',value['plans'][0]['request_id'])).fetchone()
 assert row is not None and row[0] is None
 assert not await db.get_watchlist(chat)
