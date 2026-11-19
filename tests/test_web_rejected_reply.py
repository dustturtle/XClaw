"""Typed model failures remain failed across direct/receipt/history paths."""
import asyncio
import httpx
import pytest
from xclaw.channels.web import create_web_app
from xclaw.responses import RejectedReply


@pytest.mark.parametrize('receipt',[False,True])
async def test_rejected_answer_status_persists_without_replay_and_next_message_works(db,receipt):
    calls=[]
    text='模型暂时不可用，请稍后重试。'
    async def handler(cid,message):
        calls.append(message)
        internal=await db.get_or_create_chat('web',cid)
        await db.save_message(internal,'user',message)
        # Intentionally identical text on a successful normal answer tests typed, not string matching.
        await db.save_message(internal,'assistant',text)
        return RejectedReply(text) if message=='first' else text
    app=create_web_app(handler,db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        payload={'chat_id':'same','message':'first'}
        if receipt:payload['request_id']='e'*32
        response=await c.post('/api/chat',json=payload)
        if receipt:
            assert response.status_code==202
            await asyncio.gather(*list(app.state.chat_tasks))
            result=(await c.get('/api/chat/receipts/'+'e'*32,params={'chat_id':'same'})).json()
            assert result['status']=='failed' and result['reply']==text
            await c.post('/api/chat',json=payload)
            assert calls==['first']
        else:assert response.json()['status']=='failed'
        second=await c.post('/api/chat',json={'chat_id':'same','message':'next'})
        assert second.status_code==200 and 'status' not in second.json()
        history=(await c.get('/api/chat/history',params={'chat_id':'same'})).json()['messages']
        assert history==[{'role':'user','content':'first'},{'role':'assistant','content':text,'status':'failed'},
                         {'role':'user','content':'next'},{'role':'assistant','content':text}]
        foreign=(await c.get('/api/chat/history',params={'chat_id':'another'})).json()
        assert foreign['messages']==[]
    restarted=create_web_app(handler,db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted),base_url='http://test') as c:
        history=(await c.get('/api/chat/history',params={'chat_id':'same'})).json()['messages']
        assert history[1]['status']=='failed' and 'status' not in history[-1]
    assert calls==['first','next']


async def test_rejected_reply_does_not_mark_old_equal_message_when_handler_did_not_save(db):
    cid=await db.get_or_create_chat('web','same')
    await db.save_message(cid,'assistant','same text')
    async def handler(cid,message):return RejectedReply('same text')
    app=create_web_app(handler,db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        assert (await c.post('/api/chat',json={'chat_id':'same','message':'failure'})).json()['status']=='failed'
        history=(await c.get('/api/chat/history',params={'chat_id':'same'})).json()['messages']
        assert history==[{'role':'assistant','content':'same text'}]


async def test_two_identical_concurrent_failures_use_exact_message_identity_on_reload(db):
    cid=await db.get_or_create_chat('web','same')
    ready=asyncio.Event()
    arrived=0
    async def handler(external,message):
        nonlocal arrived
        await db.save_message(cid,'user',message)
        mid=await db.save_message(cid,'assistant','模型暂时不可用')
        arrived+=1
        if arrived==2:ready.set()
        await ready.wait() # Both matching rows exist before either handler returns.
        reply=RejectedReply('模型暂时不可用')
        reply.message_id=mid
        reply.chat_id=cid
        return reply
    app=create_web_app(handler,db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        results=await asyncio.gather(*(c.post('/api/chat',json={'chat_id':'same','message':name}) for name in ['first','second']))
        assert all(r.json()['status']=='failed' for r in results)
    restarted=create_web_app(handler,db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted),base_url='http://test') as c:
        history=(await c.get('/api/chat/history',params={'chat_id':'same'})).json()['messages']
        assistants=[r for r in history if r['role']=='assistant']
        assert len(assistants)==2 and all(r.get('status')=='failed' for r in assistants)
        assert len([r for r in history if r['role']=='user'])==2
    assert arrived==2


@pytest.mark.parametrize('invalid',['foreign_chat','user_role','bad_id','partial_metadata'])
async def test_invalid_typed_identity_never_falls_back_to_equal_failure_text(db,invalid):
    cid=await db.get_or_create_chat('web','same')
    other=await db.get_or_create_chat('web','other')
    foreign=await db.save_message(other,'assistant','failure')
    async def handler(external,message):
        uid=await db.save_message(cid,'user','failure')
        mid=await db.save_message(cid,'assistant','failure')
        reply=RejectedReply('failure')
        reply.message_id={'foreign_chat':foreign,'user_role':uid,'bad_id':-1,'partial_metadata':mid}[invalid]
        if invalid!='partial_metadata':reply.chat_id=other if invalid=='foreign_chat' else cid
        return reply
    app=create_web_app(handler,db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        assert (await c.post('/api/chat',json={'chat_id':'same','message':'request'})).json()['status']=='failed'
        for chat in ['same','other']:
            history=(await c.get('/api/chat/history',params={'chat_id':chat})).json()['messages']
            assert all('status' not in row for row in history)


@pytest.mark.parametrize('failure',[False,True])
async def test_stream_typed_failure_has_failed_terminal_not_success_done(failure):
    calls=[]
    async def handler(cid,message):return 'unused'
    async def stream(cid,message):
        calls.append(message)
        yield RejectedReply('模型暂时不可用') if failure else '没有满足条件的数据'
    app=create_web_app(handler,stream_handler=stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        response=await c.post('/api/chat/stream',json={'chat_id':'room','message':'query'})
    assert response.status_code==200 and calls==['query']
    if failure:
        assert 'event: error' in response.text
        assert '"status": "failed"' in response.text
        assert '模型暂时不可用' in response.text
        assert '[DONE]' not in response.text
    else:
        assert '[DONE]' in response.text and 'event: error' not in response.text
