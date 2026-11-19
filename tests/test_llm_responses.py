import json
import httpx
import pytest
from xclaw.llm import create_provider
from xclaw.llm_types import Message, DoneEvent, StopReason


def provider(body, content_type='text/event-stream'):
    p = create_provider('openai-responses', '', 'codex/fixture-default', base_url='https://fixture.invalid/v1')
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=body, headers={'content-type': content_type})))
    return p


@pytest.mark.asyncio
async def test_stream_and_tool_completion():
    result = {'output': [{'type': 'function_call', 'call_id': 'c1', 'name': 'quote', 'arguments': '{"symbol":"TEST"}'}]}
    body = ''.join('data: '+json.dumps(e)+'\n\n' for e in [
        {'type':'response.output_text.delta','delta':'checking'},
        {'type':'response.completed','response':result}])
    p = provider(body)
    events = [e async for e in p.chat_stream([Message(role='user', content='test')])]
    assert events[0].text == 'checking'
    assert events[-1].response.stop_reason == StopReason.tool_use
    assert events[-1].response.tool_uses()[0].input == {'symbol':'TEST'}
    await p.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('body', ['', 'data: {"type":"response.failed"}\n\n'])
async def test_truncated_or_failed_stream(body):
    p = provider(body)
    with pytest.raises(RuntimeError):
        await p.chat([Message(role='user', content='test')])
    await p.close()


def test_invalid_tool_arguments_rejected():
    p = provider('')
    with pytest.raises(ValueError):
        p._parse_response({'output':[{'type':'function_call','call_id':'a','name':'x','arguments':'invalid'}]})


@pytest.mark.asyncio
async def test_json_and_payload():
    p = provider(json.dumps({'output': [{'type':'message','content':[{'type':'output_text','text':'ok'}]}]}), 'application/json')
    assert (await p.chat([Message(role='user', content='test')])).text() == 'ok'
    payload = p._build_payload([Message(role='assistant',content='previous')], None, None, 100)
    assert payload['store'] is False
    assert payload['input'][0]['content'][0]['type'] == 'output_text'
    await p.close()

@pytest.mark.asyncio
async def test_cancel_propagates():
    import asyncio
    p = provider('')
    async def blocked(request):
        raise asyncio.CancelledError()
    await p._client.aclose()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    with pytest.raises(asyncio.CancelledError):
        await p.chat([Message(role='user', content='test')])
    await p.close()


@pytest.mark.asyncio
async def test_http_error():
    p = provider('')
    await p._client.aclose()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):
        await p.chat([Message(role='user', content='test')])
    await p.close()


@pytest.mark.asyncio
async def test_sse_arbitrary_byte_boundaries():
    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            body = ('data: '+json.dumps({'type':'response.output_text.delta','delta':'中文'},ensure_ascii=False)+'\r\n\r\n'+
                    'data: '+json.dumps({'type':'response.completed','response':{'output':[]}})+'\r\n\r\n').encode()
            for byte in body:
                yield bytes([byte])
    p = provider('')
    await p._client.aclose()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,stream=Bytes())))
    events = [e async for e in p.chat_stream([Message(role='user',content='test')])]
    assert events[0].text == '中文'
    assert isinstance(events[-1],DoneEvent)
    await p.close()


def test_system_messages_are_responses_instructions():
    p = provider('')
    payload = p._build_payload([Message(role='system', content='policy'), Message(role='user',content='test')], None, 'base', 100)
    assert payload['instructions'] == 'base\n\npolicy'
    assert [m['role'] for m in payload['input']] == ['user']
