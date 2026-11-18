import asyncio
from types import SimpleNamespace
import pytest
from xclaw.agent_engine import _stream_agent_response
from xclaw.llm_types import TextDeltaEvent,DoneEvent,LLMResponse,TextBlock,ToolUseBlock,StopReason

async def run(events):
    async def stream(**kwargs):
        for e in events:
            if isinstance(e,Exception): raise e
            yield e
    output=[]
    async def sink(e):output.append(e)
    result=await _stream_agent_response(SimpleNamespace(chat_stream=stream),sink,0)
    return result,output

def done(text='',calls=(),reason=StopReason.end_turn):
    return DoneEvent(response=LLMResponse(stop_reason=reason,content=([TextBlock(text=text)] if text else [])+list(calls)))

@pytest.mark.asyncio
async def test_real_incremental_order():
    r,e=await run([TextDeltaEvent(text='你'),TextDeltaEvent(text='好'),done('你好')])
    assert [x['type'] for x in e]==['reset','delta','delta']
    assert r.text()=='你好'

@pytest.mark.asyncio
@pytest.mark.parametrize('events',[
    [TextDeltaEvent(text='draft')],
    [TextDeltaEvent(text='draft'),done('different')],
    [done('x'),TextDeltaEvent(text='trailing')],
    [done('partial',reason=StopReason.max_tokens)],
    [TextDeltaEvent(text='draft'),RuntimeError('disconnect')],
])
async def test_fail_closed(events):
    with pytest.raises((ValueError,RuntimeError)):await run(events)

@pytest.mark.asyncio
async def test_tools_only_after_complete_reset():
    call=ToolUseBlock(id='c',name='test',input={})
    r,e=await run([TextDeltaEvent(text='checking'),done('checking',[call],StopReason.tool_use)])
    assert e[-1]['type']=='reset'
    assert r.tool_uses()==[call]

@pytest.mark.asyncio
async def test_cancellation_does_not_return_response():
    import asyncio
    reached=asyncio.Event()
    async def stream(**kwargs):
        yield TextDeltaEvent(text='draft')
        reached.set()
        await asyncio.Future()
    output=[]
    async def sink(e):output.append(e)
    task=asyncio.create_task(_stream_agent_response(SimpleNamespace(chat_stream=stream),sink,0))
    await reached.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert output[-1]['type']=='delta'

@pytest.mark.asyncio
async def test_sink_receives_delta_before_model_finishes():
    seen=asyncio.Event()
    async def stream(**kwargs):
        yield TextDeltaEvent(text='first')
        assert seen.is_set()
        yield done('first')
    async def sink(e):
        if e['type']=='delta':seen.set()
    result=await _stream_agent_response(SimpleNamespace(chat_stream=stream),sink,0)
    assert result.text()=='first'
