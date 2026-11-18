"""Independent adversarial checks; no model/network calls."""
import json
from types import SimpleNamespace
import pytest
from xclaw.inline_ui_stream import InlineUIProtocol
from xclaw.agent_engine import _stream_agent_response
from xclaw.llm_types import TextDeltaEvent, DoneEvent, LLMResponse, TextBlock, StopReason
from xclaw.text_stream import TextStreams

E=[dict(name='stock_history',arguments={'symbol':'600519'},content='actual',is_error=False)]
def wire(records):return ''.join(json.dumps(r,ensure_ascii=True)+'\n' for r in records)
R=[dict(type='text_delta',block_id='p0',text='中文😀'),dict(type='text_end',block_id='p0'),dict(type='card',after_block_id='p0',evidence_id='e0',component='HistoryChart')]

@pytest.mark.parametrize('split',range(len(wire(R))+1))
def test_all_two_chunk_unicode_boundaries(split):
 p=InlineUIProtocol(E,3);s=wire(R);p.feed(s[:split]);p.feed(s[split:]);assert p.finish()['blocks'][0]['source']=='中文😀'

@pytest.mark.parametrize('bad',[{'type':'card','after_block_id':'p0','evidence_id':'e0','component':'Mutation'}, {'type':'text_delta','block_id':'p2','text':'skip'}, {'type':'card','after_block_id':'p0','evidence_id':'e1','component':'HistoryChart'}])
def test_invalid_records_reject(bad):
 p=InlineUIProtocol(E,0)
 for r in R[:2]:p.record(r)
 with pytest.raises(ValueError):p.record(bad)

def test_duplicate_json_key_reject():
 p=InlineUIProtocol(E,0)
 with pytest.raises(ValueError):p.feed('{"type":"text_delta","block_id":"p0","text":"a","text":"b"}\n')

async def test_failed_draft_cannot_be_committed_or_revived():
 s=TextStreams();await s.publish('a',{'type':'reset','round':2})
 await s.publish('a',{'type':'ui_snapshot','round':2,'text':'保留','presentation':{'provisional':True}})
 await s.publish('a',{'type':'draft_failed','round':2})
 await s.publish('a',{'type':'ui_commit','round':2,'text':'错误成功','presentation':{'validated':True}})
 await s.publish('a',{'type':'reset','round':1})
 assert s.snapshot('a')['text']=='保留' and s.snapshot('a')['presentation'] is None
 assert s.snapshot('b')['text']==''

async def test_valid_prefix_survives_invalid_record_in_same_delta():
 s=TextStreams()
 async def sink(e):await s.publish('r',e)
 raw=wire(R[:2])+ '{"type":"unknown"}\n'
 async def stream(**kwargs):
  yield TextDeltaEvent(text=raw)
  yield DoneEvent(response=LLMResponse(content=[TextBlock(text=raw)],stop_reason=StopReason.end_turn))
 with pytest.raises(ValueError):await _stream_agent_response(SimpleNamespace(chat_stream=stream),sink,0,inline=InlineUIProtocol(E,0))
 assert s.snapshot('r')['text']=='中文😀'
 assert s.snapshot('r').get('presentation') is None

async def test_inline_persistence_rolls_back_evidence_if_presentation_write_fails(tmp_path):
 import aiosqlite
 from xclaw.ui_evidence import SCHEMA,PRESENTATIONS,save_inline_presentation,load_evidence
 import hashlib
 db=SimpleNamespace(_path=tmp_path/'atomic.db')
 async with aiosqlite.connect(db._path) as conn:
  await conn.execute(SCHEMA);await conn.execute(PRESENTATIONS)
  await conn.execute("CREATE TRIGGER reject_ui BEFORE INSERT ON ui_presentations BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
  await conn.commit()
 p=InlineUIProtocol(E,0);p.feed(wire(R))
 with pytest.raises(Exception,match='injected failure'):
  await save_inline_presentation(db,7,p.text,'question',E,p.finish())
 assert await load_evidence(db,7,hashlib.sha256(p.text.encode()).hexdigest()) is None

async def test_latest_failed_same_query_suppresses_old_success():
 from xclaw.inline_ui_stream import current_evidence
 ctx=SimpleNamespace(db=None,chat_id=7,tool_evidence=[E[0],{**E[0],'content':'source failed','is_error':True}])
 assert await current_evidence(ctx)==[]

@pytest.mark.parametrize('stop',[StopReason.max_tokens])
async def test_truncated_final_never_returns_success(stop):
 raw=wire(R)
 async def stream(**kwargs):
  yield TextDeltaEvent(text=raw)
  yield DoneEvent(response=LLMResponse(content=[TextBlock(text=raw)],stop_reason=stop))
 async def sink(e):pass
 with pytest.raises(ValueError,match='Incomplete'):
  await _stream_agent_response(SimpleNamespace(chat_stream=stream),sink,0,inline=InlineUIProtocol(E,0))
