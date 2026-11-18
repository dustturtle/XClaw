import pytest
from xclaw.text_stream import TextStreams

@pytest.mark.asyncio
async def test_reset_and_bounded_snapshot_reconnect():
    streams=TextStreams(capacity=2,max_chars=5)
    await streams.publish('a',{'type':'reset','round':1})
    await streams.publish('a',{'type':'delta','round':1,'text':'hello world'})
    assert streams.snapshot('a')['text']=='hello'
    assert streams.snapshot('a')['truncated']
    assert streams.snapshot('a')['seq']==2
    await streams.publish('a',{'type':'reset','round':2})
    assert streams.snapshot('a')['text']==''
    await streams.publish('a',{'type':'delta','round':2,'text':'OK'})
    assert streams.snapshot('a')['text']=='OK'
    await streams.publish('a',{'type':'terminal'})
    assert streams.snapshot('a')['terminal']
    await streams.publish('b',{'type':'reset'});await streams.publish('c',{'type':'reset'})
    assert len(streams.states)==2

@pytest.mark.asyncio
async def test_observer_is_scoped_and_never_dispatches_business(db):
    import httpx
    from xclaw.channels.web import create_web_app
    calls=[]
    async def handler(*args):calls.append(args);return 'should not run'
    app=create_web_app(handler,db=db,auth_token='secret')
    store=app.state.receipts
    receipt,_=await store.admit(store.scope('room','secret'),'a'*32,{'message':'test'})
    await store.text_streams.publish(receipt['id'],{'type':'reset','round':0})
    await store.text_streams.publish(receipt['id'],{'type':'delta','round':0,'text':'真实预览'})
    await store.text_streams.publish(receipt['id'],{'type':'terminal'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        path='/api/chat/receipts/'+'a'*32+'/events'
        denied=await c.get(path,params={'chat_id':'room'});assert denied.status_code==401
        wrong=await c.get(path,params={'chat_id':'other'},headers={'Authorization':'Bearer secret'});assert wrong.status_code==404
        response=await c.get(path,params={'chat_id':'room'},headers={'Authorization':'Bearer secret'})
        assert response.status_code==200 and '真实预览' in response.text and '"seq": 3' in response.text
        again=await c.get(path,params={'chat_id':'room'},headers={'Authorization':'Bearer secret'})
        assert '真实预览' in again.text
    assert calls==[]
