import json
from types import SimpleNamespace

import pytest

from xclaw.inline_ui_stream import InlineUIProtocol
from xclaw.agent_engine import _stream_agent_response
from xclaw.llm_types import TextDeltaEvent, DoneEvent, LLMResponse, TextBlock, StopReason
from xclaw.text_stream import TextStreams
from xclaw.ui_evidence import save_inline_presentation, load_evidence, load_presentation

EVIDENCE = [{'name': 'stock_history', 'arguments': {'symbol': '600519'}, 'content': 'actual', 'is_error': False}]
RECORDS = [dict(type='text_delta', block_id='p0', text='原文\n中文😀'),
           dict(type='text_end', block_id='p0'),
           dict(type='card', after_block_id='p0', evidence_id='e0', component='HistoryChart'),
           dict(type='text_delta', block_id='p1', text='后续文字'), dict(type='text_end', block_id='p1')]
WIRE = '\n'.join(json.dumps(r, ensure_ascii=False) for r in RECORDS) + '\n'


def test_every_character_split_preserves_original_text_and_exact_query():
    p = InlineUIProtocol(EVIDENCE, 2)
    for c in WIRE:
        p.feed(c)
    final = p.finish()
    assert p.text == '原文\n中文😀\n\n后续文字'
    assert len(final['blocks']) == 3
    assert 'Query("stock_history", {"symbol": "600519"})' in final['blocks'][1]['source']
    assert 'Mutation' not in final['blocks'][1]['source']


@pytest.mark.parametrize('changes', [{'evidence_id': 'e99'}, {'component': 'Text'},
                                   {'after_block_id': 'p99'}, {'arguments': {}}])
def test_untrusted_card_rejected(changes):
    p = InlineUIProtocol(EVIDENCE, 0)
    for r in RECORDS[:2]:p.record(r)
    with pytest.raises(ValueError):p.record({**RECORDS[2], **changes})


def test_duplicate_card_and_unclosed_text_fail():
    p = InlineUIProtocol(EVIDENCE, 0)
    for r in RECORDS[:3]:p.record(r)
    with pytest.raises(ValueError):p.record(RECORDS[2])
    p.record(RECORDS[3])
    with pytest.raises(ValueError):p.finish()


class LLM:
    def __init__(self, *, done=True, mismatch=False):self.done=done; self.mismatch=mismatch
    async def chat_stream(self, **kwargs):
        for c in WIRE:yield TextDeltaEvent(text=c)
        if self.done:
            yield DoneEvent(response=LLMResponse(content=[TextBlock(text=WIRE + ('bad' if self.mismatch else ''))], stop_reason=StopReason.end_turn))


async def test_provider_wire_checked_before_plaintext_extraction():
    events=[]
    async def sink(e):events.append(e)
    p=InlineUIProtocol(EVIDENCE, 0)
    response=await _stream_agent_response(LLM(), sink, 0, inline=p)
    assert response.text()==p.text
    assert any(e.get('presentation', {}).get('blocks', []) for e in events)
    assert not any(e.get('type')=='ui_commit' for e in events)
    with pytest.raises(ValueError):
        await _stream_agent_response(LLM(mismatch=True), sink, 0, inline=InlineUIProtocol(EVIDENCE, 0))
    with pytest.raises(ValueError):
        await _stream_agent_response(LLM(done=False), sink, 0, inline=InlineUIProtocol(EVIDENCE, 0))


async def test_failure_keeps_draft_but_revokes_cards_and_late_round():
    s=TextStreams()
    await s.publish('receipt', {'type':'reset','round':1})
    await s.publish('receipt', {'type':'ui_snapshot','round':1,'text':'draft','presentation':{'blocks':[]}})
    await s.publish('receipt', {'type':'draft_failed','round':1})
    state=s.snapshot('receipt')
    assert state['text']=='draft' and state['presentation'] is None and state['draft_failed']
    await s.publish('receipt', {'type':'ui_snapshot','round':0,'text':'stale','presentation':{}})
    assert s.snapshot('receipt')['text']=='draft'
    await s.publish('receipt', {'type':'terminal'})
    await s.publish('receipt', {'type':'ui_snapshot','round':1,'text':'late','presentation':{}})
    assert s.snapshot('receipt')['text']=='draft'


async def test_atomic_final_exact_snapshot(tmp_path):
    db=SimpleNamespace(_path=tmp_path/'db')
    p=InlineUIProtocol(EVIDENCE,0);p.feed(WIRE);final=p.finish()
    digest=await save_inline_presentation(db,7,p.text,'q',p.evidence,final)
    assert (await load_evidence(db,7,digest))['evidence']==EVIDENCE
    assert json.loads(await load_presentation(db,7,digest))==final
    assert await load_presentation(db,8,digest) is None

async def test_card_observed_before_later_text_and_completion():
    seen = []
    async def sink(event):seen.append(event)
    async def stream(**kwargs):
        first = '\n'.join(json.dumps(r, ensure_ascii=False) for r in RECORDS[:3])+'\n'
        yield TextDeltaEvent(text=first)
        assert seen[-1]['presentation']['blocks'][-1]['type']=='openui'
        assert '后续文字' not in seen[-1]['text']
        rest='\n'.join(json.dumps(r, ensure_ascii=False) for r in RECORDS[3:])+'\n'
        yield TextDeltaEvent(text=rest)
        yield DoneEvent(response=LLMResponse(content=[TextBlock(text=first+rest)],stop_reason=StopReason.end_turn))
    response=await _stream_agent_response(SimpleNamespace(chat_stream=stream),sink,0,inline=InlineUIProtocol(EVIDENCE,0))
    assert '后续文字' in response.text()


async def test_tool_round_discards_provisional_cards():
    from xclaw.llm_types import ToolUseBlock
    seen=[]
    async def sink(event):seen.append(event)
    async def stream(**kwargs):
        yield TextDeltaEvent(text=WIRE)
        yield DoneEvent(response=LLMResponse(content=[TextBlock(text=WIRE),ToolUseBlock(id='a',name='read',input={})],stop_reason=StopReason.tool_use))
    response=await _stream_agent_response(SimpleNamespace(chat_stream=stream),sink,0,inline=InlineUIProtocol(EVIDENCE,0))
    assert response.tool_uses() and seen[-1]['type']=='reset'


def test_snapshot_utf8_budget():
    from xclaw.inline_ui_stream import MAX_EVIDENCE_BYTES
    with pytest.raises(ValueError):
        InlineUIProtocol([{**EVIDENCE[0], 'content':'中'*MAX_EVIDENCE_BYTES}],0)


async def test_terminal_without_commit_revokes_cards():
    s=TextStreams()
    await s.publish('r',{'type':'reset','round':0})
    await s.publish('r',{'type':'ui_snapshot','round':0,'text':'partial','presentation':{'provisional':True}})
    await s.publish('r',{'type':'terminal'})
    state=s.snapshot('r')
    assert state['text']=='partial' and state['draft_failed'] and state['presentation'] is None

async def test_current_evidence_latest_exact_scope_and_strategy_projection():
    from unittest.mock import AsyncMock
    from xclaw.inline_ui_stream import current_evidence
    db=SimpleNamespace(list_strategy_runs=AsyncMock(return_value=[]))
    ctx=SimpleNamespace(db=db, chat_id=71, tool_evidence=[
        {**EVIDENCE[0], 'content':'older'}, EVIDENCE[0],
        {'name':'strategy_scan','arguments':{'symbol':'600519'},'content':'done','is_error':False},
        {'name':'stock_quote','arguments':{},'content':'failed','is_error':True},
    ])
    rows=await current_evidence(ctx)
    assert rows[0]['content']=='actual' and len(rows)==2
    assert rows[1]['name']=='strategy_runs'
    db.list_strategy_runs.assert_awaited_once_with(71,5)


def test_feature_is_default_off():
    from xclaw.config import Settings
    assert Settings.model_fields['openui_inline_stream'].default is False


async def test_late_reset_does_not_revive_old_round():
    s=TextStreams()
    await s.publish('r',{'type':'reset','round':2})
    await s.publish('r',{'type':'delta','round':2,'text':'current'})
    await s.publish('r',{'type':'reset','round':1})
    await s.publish('r',{'type':'delta','round':1,'text':'old'})
    assert s.snapshot('r')['text']=='current'

async def test_lone_surrogate_never_corrupts_valid_prefix():
    stream_state=TextStreams()
    async def sink(e):await stream_state.publish('r',e)
    raw=json.dumps(RECORDS[0],ensure_ascii=False)+'\n'+json.dumps(
        dict(type='text_delta',block_id='p0',text='\ud800'))+'\n'
    async def stream(**kwargs):
        yield TextDeltaEvent(text=raw)
        yield DoneEvent(response=LLMResponse(content=[TextBlock(text=raw)],stop_reason=StopReason.end_turn))
    with pytest.raises(ValueError,match='unicode'):
        await _stream_agent_response(SimpleNamespace(chat_stream=stream),sink,0,inline=InlineUIProtocol(EVIDENCE,0))
    state=stream_state.snapshot('r')
    assert state['text']==RECORDS[0]['text'] and state['presentation'] is None and state['draft_failed']
    state['text'].encode('utf-8')
