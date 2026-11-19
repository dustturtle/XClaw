"""Independent adversarial review. No live model or user-session writes."""
import json
from types import SimpleNamespace
import pytest
from xclaw.ui_streaming import PlacementStream
from xclaw.openui import OpenUITools, ToolRequest
from xclaw.tools import ToolRegistry
from xclaw.ui_evidence import save_evidence, load_presentation
from xclaw.llm_types import TextDeltaEvent, DoneEvent, LLMResponse, TextBlock, StopReason
import hashlib

REPLY='Original first paragraph.\n\nOriginal second paragraph.'
DSL='q=Query("stock_quote",{symbol:"DEMO.X"})\nroot=Col([QuoteCard("😀样本甲",q)])'


def test_all_two_piece_boundaries_preserve_unicode_and_ids():
    raw=json.dumps({'placements':[{'after':1,'source':DSL}]},ensure_ascii=True)
    for index in range(len(raw)+1):
        p=PlacementStream(REPLY)
        events=p.feed(raw[:index])+p.feed(raw[index:])
        assert ''.join(e['data']['text'] for e in events if e['event']=='placement_delta')==DSL
        result=json.loads(p.finish(raw))
        assert result['blocks'][-1]['source']==DSL
        assert result['blocks'][-1]['id']=='p1u0'


@pytest.mark.parametrize('bad',[r'\udc00',r'\ud800\u0041',r'\uZZZZ',r'\x41','\x01','\ud800'])
def test_unpaired_or_invalid_unicode_never_commits(bad):
    p=PlacementStream(REPLY)
    raw='{"placements":[{"after":0,"source":"root=Col(['+bad+'])"}]}'
    with pytest.raises(ValueError):
        for character in raw:p.feed(character)
        p.finish(raw)


@pytest.mark.parametrize('suffix',[',]',',{}]}',',"placements":[]}', ' garbage'])
def test_extra_json_and_trailing_data_rejected(suffix):
    p=PlacementStream(REPLY)
    raw='{"placements":[]}' + suffix
    with pytest.raises(ValueError):p.feed(raw);p.finish(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['mismatch','truncated','max_tokens','after_done'])
async def test_provisional_generation_failure_never_saves_presentation(db,mode):
    await save_evidence(db,1,REPLY,'请展示行情',[{'name':'stock_quote','arguments':{'symbol':'DEMO.X'},'content':'最新价: 7.29','is_error':False}])
    raw=json.dumps({'placements':[{'after':0,'source':DSL}]})
    class LLM:
        async def chat_stream(self,*args,**kwargs):
            for char in raw:yield TextDeltaEvent(text=char)
            if mode=='truncated':return
            final=raw if mode!='mismatch' else raw.replace('样本甲','changed')
            # ensure_ascii encodes the label, so use a guaranteed changed anchor.
            if mode=='mismatch':final=raw.replace('"after": 0','"after": 1')
            yield DoneEvent(response=LLMResponse(content=[TextBlock(text=final)],stop_reason=StopReason.max_tokens if mode=='max_tokens' else StopReason.end_turn))
            if mode=='after_done':yield TextDeltaEvent(text='extra')
    ctx=SimpleNamespace(db=db,chat_id=1,llm=LLM())
    service=OpenUITools(ToolRegistry(),lambda scope:None)
    events=[e async for e in service.generate(ctx,REPLY)]
    assert any(e['event']=='placement_delta' for e in events)
    assert events[-1]['event']=='error'
    assert not any(e['event']=='done' for e in events)
    assert await load_presentation(db,1,hashlib.sha256(REPLY.encode()).hexdigest()) is None


@pytest.mark.asyncio
async def test_stream_plan_with_mutation_does_not_execute_tool(db):
    # Generating declarative mutation is not permission to run it. Browser gating
    # is separately tested by frontend tests, not claimed by this backend test.
    await save_evidence(db,1,REPLY,'卡片',[{'name':'watchlist_manage','arguments':{'action':'list'},'content':'[]','is_error':False}])
    raw=json.dumps({'placements':[{'after':0,'source':'m=Mutation("watchlist_manage",{action:"add",symbol:"920414"})\nroot=Col([Button("确认",Action([@Run(m)]))])'}]})
    class LLM:
        async def chat_stream(self,*args,**kwargs):
            yield TextDeltaEvent(text=raw)
            yield DoneEvent(response=LLMResponse(content=[TextBlock(text=raw)],stop_reason=StopReason.end_turn))
    registry=ToolRegistry()
    async def forbidden(*args,**kwargs):raise AssertionError('generation executed a business tool')
    registry.execute=forbidden
    ctx=SimpleNamespace(db=db,chat_id=1,llm=LLM())
    events=[e async for e in OpenUITools(registry,lambda scope:None).generate(ctx,REPLY)]
    assert events[-1]['event']=='done'
