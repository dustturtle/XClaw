import asyncio
from types import SimpleNamespace

import httpx
import pytest

from xclaw.agent_engine import AgentContext, _execute_tool_batch
from xclaw.channels.web import create_web_app
from xclaw.tools import Tool, ToolRegistry, ToolResult


class Probe(Tool):
    def __init__(self, name, state, independent=True, read_only=True):
        self._name = name
        self.state = state
        self.independent = independent
        self.read_only = read_only
    @property
    def name(self): return self._name
    @property
    def description(self): return 'probe'
    @property
    def parameters(self): return {'type': 'object', 'properties': {}}
    async def execute(self, params, context):
        self.state['events'].append('start:' + self.name)
        self.state['active'] += 1
        self.state['max'] = max(self.state['max'], self.state['active'])
        await asyncio.sleep(.01)
        self.state['active'] -= 1
        self.state['events'].append('end:' + self.name)
        return ToolResult(content=self.name)


@pytest.mark.asyncio
async def test_independent_reads_bounded_and_write_barrier(db):
    state = {'events': [], 'active': 0, 'max': 0}
    registry = ToolRegistry()
    names = [f'r{i}' for i in range(6)] + ['write', 'after']
    for name in names:
        registry.register(Probe(name, state, read_only=name != 'write'))
    ctx = AgentContext(1, 'web', db, None, registry)
    updates = []
    async def progress(message): updates.append(message)
    ctx.progress_callback = progress
    result = await _execute_tool_batch(ctx, [SimpleNamespace(id='call-'+n,name=n, input={}) for n in names], None)
    assert [r.content for r in result] == names
    assert state['max'] == 4
    assert state['events'].index('start:write') > max(state['events'].index('end:' + n) for n in names[:6])
    assert state['events'].index('start:after') > state['events'].index('end:write')
    assert updates[-1] == '业务数据已返回（8/8）'


@pytest.mark.asyncio
async def test_read_only_without_independent_stays_serial():
    state = {'events': [], 'active': 0, 'max': 0}
    registry = ToolRegistry()
    for name in ['a', 'b']:
        registry.register(Probe(name, state, independent=False))
    ctx = AgentContext(1, 'web', None, None, registry)
    await _execute_tool_batch(ctx, [SimpleNamespace(name=n, input={}) for n in ['a','b']], None)
    assert state['max'] == 1


@pytest.mark.asyncio
async def test_native_receipt_progress_does_not_replace_durable_result(db):
    gate = asyncio.Event()
    seen = []
    async def handler(chat, message):
        await gate.wait()
        return 'done'
    async def progress(receipt_id):
        seen.append(receipt_id)
        return {'message': '正在查询业务数据（0/2 已完成）', 'elapsed_seconds': 2.5}
    app = create_web_app(handler, db=db, receipt_progress_handler=progress)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        request_id = 'a' * 32
        assert (await c.post('/api/chat', json={'request_id':request_id,'message':'test'})).status_code == 202
        r = (await c.get('/api/chat/receipts/' + request_id)).json()
        assert r['message'].startswith('正在查询业务数据')
        assert r['elapsed_seconds'] == 2.5
        assert 'progress' not in r  # no synthetic PowerCore run identity
        gate.set()
        await asyncio.gather(*app.state.chat_tasks)
        r = (await c.get('/api/chat/receipts/' + request_id)).json()
        assert r['status'] == 'completed' and r['reply'] == 'done'
        assert len(seen) == 1

@pytest.mark.asyncio
async def test_native_history_is_scoped_and_read_only(db):
    from xclaw.channels.web import _user_namespace
    token = 'test-history-token'
    own = await db.get_or_create_chat('web', f'web_{_user_namespace(token)}_same')
    other = await db.get_or_create_chat('web', 'same')
    nonweb = await db.get_or_create_chat('slack', f'web_{_user_namespace(token)}_same')
    await db.save_message(own, 'user', 'question')
    await db.save_message(own, 'assistant', 'answer')
    await db.save_message(own, 'tool', 'private tool detail')
    await db.save_message(other, 'assistant', 'other namespace')
    await db.save_message(nonweb, 'assistant', 'other channel')
    async def handler(*args): raise AssertionError('History must never execute model')
    app = create_web_app(handler, db=db, auth_token=token, multi_user_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        assert (await c.get('/api/chat/history?chat_id=same')).status_code == 401
        r = await c.get('/api/chat/history?chat_id=same', headers={'Authorization':'Bearer ' + token})
        assert r.headers['cache-control'] == 'no-store'
        assert r.json()['messages'] == [{'role':'user','content':'question'},{'role':'assistant','content':'answer'}]
        unknown = await c.get('/api/chat/history?chat_id=missing', headers={'Authorization':'Bearer ' + token})
        assert unknown.json() == {'messages': []}
        async with db.conn.execute('SELECT count(*) AS n FROM chats') as cursor:
            assert (await cursor.fetchone())['n'] == 3


def test_native_history_client_uses_non_generating_restore():
    from xclaw.channels.web import _build_chat_page_html
    page = _build_chat_page_html(supports_receipts=True, auth_enabled=False, model_name="test", provider_name="test")
    assert '/api/chat/history?' in page
    assert 'appendMessage(item.role, item.content, [], true, true, item)' in page
    assert 'messageId:identity.message_id,presentationId:identity.presentation_id' in page
    assert 'attach(item, text, {chatId:ctx.chatId, history,' in page
    assert 'if (!SUPPORTS_PROGRESS && SUPPORTS_RECEIPTS' in page


def test_history_renderer_forwards_exact_presentation_identity_without_generation():
    from test_chat_progress_web import run_page_script
    run_page_script(r'''
await initial();
evaluate(`window.historyMounts=[];window.XClawOpenUIHost={attach(item,text,o){window.historyMounts.push({text,history:o.history,messageId:o.messageId,presentationId:o.presentationId})}};appendMessage('assistant','相同正文',[],true,true,{message_id:21,presentation_id:'11111111111111111111111111111111'});appendMessage('assistant','相同正文',[],true,true,{message_id:22,presentation_id:'22222222222222222222222222222222'});`);
const mounts=evaluate('window.historyMounts');assert.equal(mounts.length,2);assert.equal(mounts[0].history,true);assert.equal(mounts[1].history,true);assert.equal(mounts[0].messageId,21);assert.equal(mounts[1].messageId,22);assert.notEqual(mounts[0].presentationId,mounts[1].presentationId);assert.ok(!requests.some(r=>r.url.includes('/generate')));
''',openui=True)
