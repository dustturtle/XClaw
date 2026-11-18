from unittest.mock import AsyncMock
import asyncio
import httpx
from types import SimpleNamespace
from xclaw.channels.web import create_web_app
from xclaw.receipts import ReceiptStore

DEFAULT = 'codex/fixture-default'
ALT = 'codex/fixture-a'
def settings():
    return SimpleNamespace(model=DEFAULT, web_model_options=[ALT], llm_provider='openai-responses', agent_backend='native')
def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test')

async def test_allowlist_scope_and_replay(db):
    handler=AsyncMock(return_value='answer')
    cfg=settings()
    app=create_web_app(handler,settings=cfg,db=db,auth_token='real-token')
    headers={'Authorization':'Bearer real-token'}
    rid='1'*32
    body={'request_id':rid,'chat_id':'room','message':'hello'}
    async with client(app) as c:
        assert (await c.get('/api/chat/models')).status_code==401
        assert (await c.get('/api/chat/models',headers=headers)).json()=={'model_options':[DEFAULT,ALT],'default_model':DEFAULT}
        assert (await c.post('/api/chat',headers=headers,json={**body,'model':'unconfigured'})).status_code==400
        for _ in range(2):
            assert (await c.post('/api/chat',headers=headers,json={**body,'model':ALT})).status_code==202
        await asyncio.gather(*app.state.chat_tasks)
        assert (await c.post('/api/chat',headers=headers,json=body)).status_code==409
        assert (await c.get('/api/chat/receipts/'+rid,params={'chat_id':'room'},headers=headers)).json()['model']==ALT
        assert handler.await_count==1 and handler.await_args.kwargs['model']==ALT
        assert cfg.model==DEFAULT
        assert (await c.post('/api/chat',headers=headers,json={**body,'request_id':'2'*32})).status_code==202
        await asyncio.gather(*app.state.chat_tasks)
        assert handler.await_args.kwargs['model']==DEFAULT

async def test_old_receipt_not_relabeled(db):
    store=ReceiptStore(db)
    rid='3'*32
    old={'chat_id':'old','message':'hello','attachments':[],'execution_mode':None}
    await store.admit(store.scope('old',''),rid,old)
    handler=AsyncMock()
    app=create_web_app(handler,settings=settings(),db=db)
    async with client(app) as c:
        assert (await c.post('/api/chat',json={**old,'request_id':rid})).status_code==202
        assert (await c.post('/api/chat',json={**old,'request_id':rid,'model':ALT})).status_code==409
        assert 'model' not in (await c.get('/api/chat/receipts/'+rid,params={'chat_id':'old'})).json()
        handler.assert_not_awaited()

async def test_history_identity_and_ambiguous_hash(db):
    from xclaw.ui_evidence import ambiguous_model_reply
    import hashlib
    async with db.conn.execute("INSERT INTO chats(channel,external_chat_id) VALUES('web','room')") as cur:
        cid=cur.lastrowid
    await db.save_message(cid,'assistant','same')
    await db.save_message(cid,'assistant','same',response_model=ALT)
    digest=hashlib.sha256(b'same').hexdigest()
    assert await ambiguous_model_reply(db,cid,digest)
    assert not await ambiguous_model_reply(db,cid+1,digest)
    app=create_web_app(AsyncMock(),db=db)
    async with client(app) as c:
        entries=(await c.get('/api/chat/history',params={'chat_id':'room'})).json()['messages']
        assert entries==[{'role':'assistant','content':'same'},{'role':'assistant','content':'same','model':ALT}]

async def test_stale_inflight_formatter_cannot_restore_obsolete_layout(db):
    import hashlib
    import pytest
    from xclaw.ui_evidence import save_evidence,load_evidence,save_presentation,load_presentation
    digest=hashlib.sha256(b'same').hexdigest()
    await save_evidence(db,1,'same','q',[],model=DEFAULT)
    version=(await load_evidence(db,1,digest))['_version']
    await save_evidence(db,1,'same','q',[],model=ALT)
    with pytest.raises(ValueError,match='stale_presentation_evidence'):
        await save_presentation(db,1,digest,'stale',expected_version=version)
    assert await load_presentation(db,1,digest) is None
