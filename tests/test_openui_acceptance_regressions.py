"""Deterministic API/SQLite acceptance regressions, not live model/browser E2E.

Only disposable tmp_path databases and instrumented business tools are used.
The actual OpenUI router, scope boundary, receipts and narrative compiler run.
"""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException

from xclaw.db import Database
from xclaw.llm_types import DoneEvent, LLMResponse, TextBlock
from xclaw.openui import OpenUITools, ToolRequest, create_openui_router
from xclaw.tools import Tool, ToolRegistry, ToolResult
from xclaw.ui_evidence import action_state, save_evidence, save_presentation
from xclaw.ui_narrative import compile_narrative, paragraph_blocks


class ControlledWatchlist(Tool):
    name = 'watchlist_manage'
    description = 'Disposable acceptance instrumentation'
    parameters = {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['list', 'add', 'remove']},
        'symbol': {'type': 'string', 'default': ''}}, 'required': ['action']}

    def __init__(self):
        self.calls = []
        self.mode = 'ok'
        self.records = {}

    async def execute(self, args, ctx):
        self.calls.append((ctx.chat_id, dict(args)))
        if self.mode == 'failed':
            return ToolResult('private upstream details DO_NOT_LEAK', is_error=True)
        if args['action'] == 'add':
            self.records.setdefault(ctx.chat_id, []).append(args.get('symbol'))
            if self.mode == 'cancel_after_write':
                raise asyncio.CancelledError()
        return ToolResult(json.dumps(self.records.get(ctx.chat_id, [])))


@pytest.fixture
async def boundary(tmp_path):
    db = Database(tmp_path / 'acceptance.db')
    await db.connect()
    tool = ControlledWatchlist()
    registry = ToolRegistry()
    registry.register(tool)

    async def context(scope):
        return SimpleNamespace(db=db, chat_id={'A': 1, 'B': 2}[scope])

    service = OpenUITools(registry, context)
    app = FastAPI()

    async def auth(authorization: str = Header(default='')):
        if authorization != 'Bearer acceptance-token':
            raise HTTPException(401)

    # Chat mapping is an explicit fixture; production authentication is not mocked as verified.
    app.include_router(create_openui_router(service, auth, lambda chat_id, token: chat_id))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                headers={'Authorization': 'Bearer acceptance-token'}) as client:
        yield SimpleNamespace(db=db, tool=tool, service=service, client=client, context=context)
    await db.close()


def digest(reply):
    return hashlib.sha256(reply.encode()).hexdigest()


async def seed(b, *, failed=False, content='original', question='查询自选股', reply='原始答复'):
    await save_evidence(b.db, 1, reply, question, [{'name': 'watchlist_manage',
        'arguments': {'action': 'list'}, 'content': content, 'is_error': failed}])
    return digest(reply)


@pytest.mark.parametrize('failure,expected', [('failed', 'evidence_failed'), ('different_args', 'evidence_not_found'), ('other_chat', 'snapshot_missing')])
async def test_failed_mismatched_or_foreign_snapshot_never_fetches_or_fabricates(boundary, failure, expected):
    b = boundary
    h = await seed(b, failed=failure == 'failed')
    payload = {'chat_id': 'B' if failure == 'other_chat' else 'A', 'name': b.tool.name,
               'arguments': {'action': 'list'}, 'reply_hash': h}
    if failure == 'different_args':
        payload['arguments']['symbol'] = 'not-the-original-query'
    response = await b.client.post('/api/openui/query', json=payload)
    assert response.status_code in {409, 422}
    assert response.json()['detail']['code'] == expected
    assert 'data' not in response.json()
    assert not b.tool.calls


async def test_explicit_refresh_failure_does_not_return_stale_success(boundary):
    b = boundary
    h = await seed(b)
    payload = {'chat_id': 'A', 'name': b.tool.name, 'arguments': {'action': 'list'}, 'reply_hash': h}
    saved = await b.client.post('/api/openui/query', json=payload)
    assert saved.json()['data']['content'] == 'original'
    b.tool.mode = 'failed'
    fresh = await b.client.post('/api/openui/query', json={**payload, 'refresh': True})
    assert fresh.status_code == 422
    assert fresh.json()['detail']['code'] == 'tool_failed'
    assert 'DO_NOT_LEAK' not in fresh.text and 'original' not in fresh.text
    assert len(b.tool.calls) == 1


@pytest.mark.parametrize('extra', [{'chat_id': 2}, {'user_id': 2}, {'scope': 'B'}])
async def test_mutation_cannot_override_effective_context_in_tool_args(boundary, extra):
    b = boundary
    result = await b.client.post('/api/openui/mutation', json={'chat_id': 'A', 'name': b.tool.name,
        'arguments': {'action': 'add', 'symbol': '600519', **extra}, 'request_id': 'override'})
    assert result.status_code == 422
    assert result.json()['detail']['code'] == 'invalid_arguments'
    assert not b.tool.calls


async def test_failed_mutation_cannot_be_replayed_or_marked_complete(boundary, saved_legacy_button):
    b = boundary
    b.tool.mode = 'failed'
    req, _ = await saved_legacy_button(b.db, 'A', {'action': 'add'}, '确认')
    payload = req.model_dump(exclude_none=True)
    first = await b.client.post('/api/openui/mutation', json=payload)
    second = await b.client.post('/api/openui/mutation', json=payload)
    assert first.status_code == 422
    assert second.status_code == 409
    assert second.json()['detail']['code'] == 'outcome_unknown'
    assert len(b.tool.calls) == 1 and not b.tool.records
    assert await action_state(b.db, 1, req.reply_hash) == {'dirty': [], 'completed': []}


async def test_interruption_after_effect_restart_never_repeats_unknown_write(boundary, saved_legacy_button):
    b = boundary
    b.tool.mode = 'cancel_after_write'
    req, token_hash = await saved_legacy_button(b.db, 'A', {'action': 'add', 'symbol': '600519'})
    with pytest.raises(asyncio.CancelledError):
        await b.service.execute(req, 'A', True, token_hash=token_hash)
    assert b.tool.records == {1: ['600519']}
    restarted = OpenUITools(b.service.registry, b.context)
    with pytest.raises(HTTPException) as error:
        await restarted.execute(req, 'A', True, token_hash=token_hash)
    assert error.value.detail['code'] == 'outcome_unknown'
    assert len(b.tool.calls) == 1
    assert await action_state(b.db, 1, req.reply_hash) == {'dirty': [], 'completed': []}


async def test_history_endpoints_do_not_execute_saved_mutation_and_are_chat_scoped(boundary):
    b = boundary
    h = 'c' * 64
    source = 'm=Mutation("watchlist_manage",{action:"add"})\nroot=Col([Button("确认",Action([@Run(m)]))])'
    await save_presentation(b.db, 1, h, source)
    for _ in range(3):
        saved = await b.client.post('/api/openui/presentation', json={'chat_id': 'A', 'reply_hash': h})
        state = await b.client.post('/api/openui/action-state', json={'chat_id': 'A', 'reply_hash': h})
        assert saved.json()['source'] == source
        assert state.json() == {'dirty': [], 'completed': []}
    other = await b.client.post('/api/openui/presentation', json={'chat_id': 'B', 'reply_hash': h})
    assert other.json()['source'] is None
    assert not b.tool.calls


async def test_completed_receipt_restore_restart_and_conflicting_payload(boundary, saved_legacy_button):
    b = boundary
    req, token_hash = await saved_legacy_button(b.db, 'A', {'action': 'add', 'symbol': '600519'})
    original = await b.service.execute(req, 'A', True, token_hash=token_hash)
    new_service = OpenUITools(b.service.registry, b.context)
    assert await new_service.execute(req, 'A', True, token_hash=token_hash) == original
    for scope, expected in [('A', {'dirty': ['watchlist_manage'], 'completed': ['确认添加']}),
                             ('B', {'dirty': [], 'completed': []})]:
        response = await b.client.post('/api/openui/action-state', json={'chat_id': scope, 'reply_hash': req.reply_hash})
        assert response.json() == expected
    with pytest.raises(HTTPException) as error:
        await new_service.execute(req.model_copy(update={'arguments': {'action': 'remove'}}), 'A', True, token_hash=token_hash)
    assert error.value.detail['code'] == 'presentation_id_required'
    assert len(b.tool.calls) == 1


@pytest.mark.parametrize('question,failed,outcome', [('谢谢', False, 'text_only'), ('画图表', True, 'no_data')])
async def test_empty_result_metadata_survives_actual_history_api_without_model(boundary, question, failed, outcome):
    b = boundary
    if failed:
        await seed(b, failed=True, question=question)
    else:
        await save_evidence(b.db, 1, '原始答复', question, [])
    class NoModel:
        def chat_stream(self, *args, **kwargs):
            pytest.fail('No usable evidence must not call a model')
    events = [e async for e in b.service.generate(SimpleNamespace(db=b.db, chat_id=1, llm=NoModel()), '原始答复')]
    assert [e['event'] for e in events] == ['done']
    assert json.loads(events[0]['data'])['outcome'] == outcome
    restored = await b.client.post('/api/openui/presentation', json={'chat_id': 'A', 'reply_hash': digest('原始答复')})
    assert json.loads(restored.json()['source']) == {'version': 'xclaw-empty-1', 'outcome': outcome}
    assert not b.tool.calls


@pytest.mark.parametrize('plan', [
    {'placements': [{'after': 0, 'source': 'root=Col([Text("价格变成999")])'}]},
    {'placements': [{'after': -1, 'source': 'root=Col([])'}]},
    {'placements': [{'after': True, 'source': 'root=Col([])'}]},
    {'placements': [{'after': 0, 'source': 'root=Col([])', 'text': '改写'}]},
])
def test_model_cannot_replace_prose_or_use_invalid_anchor(plan):
    with pytest.raises(ValueError):
        compile_narrative(json.dumps(plan), '原价17.03。\n\n盘中而非收盘。')


@pytest.mark.parametrize('reply', ['原价17.03。\n\n盘中而非收盘。',
    '```python\na=1\n\nb=2\n```\n\n结论', '参考[数据][src]。\n\n[src]: https://example.com'])
def test_valid_card_placement_retains_every_original_text_block_once(reply):
    plan = {'placements': [{'after': 0, 'source': 'q=Query("watchlist_manage",{action:"list"})\nroot=Col([Watchlist("自选",q)])'}]}
    result = json.loads(compile_narrative(json.dumps(plan), reply))
    text = [b['source'] for b in result['blocks'] if b['type'] == 'text']
    assert text == paragraph_blocks(reply)
    assert len([b for b in result['blocks'] if b['type'] == 'openui']) == 1


@pytest.mark.parametrize('failure', ['exception', 'truncated', 'invalid_plan'])
async def test_generator_failure_is_explicit_and_never_persists_partial_source(boundary, failure):
    b = boundary
    h = await seed(b)
    class BrokenModel:
        async def chat_stream(self, *args, **kwargs):
            if failure == 'exception':
                raise RuntimeError('DO_NOT_LEAK')
            text = '{"placements":[]}' if failure == 'truncated' else '{"rewritten_answer":"假的结果"}'
            yield DoneEvent(response=LLMResponse(stop_reason='max_tokens' if failure == 'truncated' else 'end_turn',
                                                content=[TextBlock(text=text)]))
    events = [e async for e in b.service.generate(SimpleNamespace(db=b.db, chat_id=1, llm=BrokenModel()), '原始答复')]
    assert events[-1]['event'] == 'error'
    assert json.loads(events[-1]['data'])['clear'] is True
    assert not any(e['event'] == 'done' for e in events)
    assert 'DO_NOT_LEAK' not in str(events)
    restored = await b.client.post('/api/openui/presentation', json={'chat_id': 'A', 'reply_hash': h})
    assert restored.json()['source'] is None
    assert not b.tool.calls
