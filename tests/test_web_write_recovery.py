"""Human acknowledgement is scoped audit-only, never business execution."""
import sqlite3
import httpx
from xclaw.channels.web import create_web_app, _user_namespace
from xclaw.tool_execution_journal import _SCHEMA, _ACTIVE

async def test_recovery_scoped_authenticated_and_explicit(db):
    cid = await db.get_or_create_chat('web', 'web_' + _user_namespace('secret') + '_same')
    other = await db.get_or_create_chat('web', 'web_' + _user_namespace('secret') + '_other')
    with sqlite3.connect(db._path) as conn:
        conn.executescript(_SCHEMA)
        for ident, chat in [('pending', cid), ('foreign', other)]:
            conn.execute("INSERT INTO tool_execution_journal(id,chat_id,call_id,fingerprint,tool_name,arguments_json,state) VALUES(?,?,?,?,?,?,?)", (ident,str(chat),ident,ident,'portfolio_manage','{"action":"add"}','unknown'))
    calls=[]
    async def handler(*args, **kwargs):
        calls.append(args)
        return 'not invoked'
    app=create_web_app(handler,db=db,auth_token='secret',multi_user_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        path='/api/chat/write-recovery'
        assert (await c.get(path)).status_code==401
        c.headers['Authorization']='Bearer secret'
        listed=await c.get(path,params={'chat_id':'same'})
        assert listed.headers['cache-control']=='no-store'
        assert [r['id'] for r in listed.json()['records']]==['pending']
        payload=dict(chat_id='same',execution_id='pending',confirmed_reviewed=True,execution_stopped=True,note='已只读查到一条记录，原执行已停止')
        assert (await c.post(path+'/acknowledge',json={**payload,'execution_id':'foreign'})).status_code==409
        assert (await c.post(path+'/acknowledge',json={**payload,'confirmed_reviewed':False})).status_code==422
        assert (await c.post(path+'/acknowledge',json={**payload,'note':' '})).status_code==409
        _ACTIVE.add('pending')
        try:
            assert (await c.post(path+'/acknowledge',json=payload)).status_code==409
        finally:
            _ACTIVE.discard('pending')
        result=await c.post(path+'/acknowledge',json=payload)
        assert result.status_code==200 and result.json()['status']=='acknowledged'
        assert (await c.get(path,params={'chat_id':'same'})).json()['records']==[]
        assert (await c.post(path+'/acknowledge',json=payload)).status_code==409
        assert len((await c.get(path,params={'chat_id':'other'})).json()['records'])==1
    assert calls==[]
    with sqlite3.connect(db._path) as conn:
        row=conn.execute("SELECT state,note,result_json FROM tool_execution_journal WHERE id='pending'").fetchone()
        assert row==('acknowledged',payload['note'],None)

async def test_no_existing_chat_is_empty_without_creating_chat(db):
    async def handler(*args): raise AssertionError('must never execute')
    app=create_web_app(handler,db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        assert (await c.get('/api/chat/write-recovery',params={'chat_id':'absent'})).json()=={'records':[]}
