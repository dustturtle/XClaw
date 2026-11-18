import asyncio
import hashlib
from types import SimpleNamespace

import pandas as pd
import pytest

from xclaw.datasources._history_requests import HistoryRequestCache, refresh_history_results
from xclaw.openui import OpenUITools, ToolRequest
from xclaw.tools import Tool, ToolRegistry, ToolResult
from xclaw.ui_evidence import save_evidence


async def refreshed(cache, key, producer):
    token=refresh_history_results.set(True)
    try:return await cache.get(key,producer)
    finally:refresh_history_results.reset(token)


async def test_explicit_refresh_runs_new_producer_normal_reads_stay_cached():
    cache=HistoryRequestCache();calls=0
    async def producer():
        nonlocal calls
        calls+=1
        return pd.DataFrame({'value':[calls]})
    assert (await cache.get(('x',),producer)).iloc[0,0]==1
    assert (await cache.get(('x',),producer)).attrs['cache_hit']
    value=await refreshed(cache,('x',),producer)
    assert value.iloc[0,0]==2 and not value.attrs['cache_hit'] and value.attrs['refresh_requested']
    assert (await cache.get(('x',),producer)).iloc[0,0]==2 and calls==2
    assert refresh_history_results.get() is False


async def test_concurrent_refresh_coalesces_and_cancel_preserves_producer_and_context():
    cache=HistoryRequestCache();started=asyncio.Event();release=asyncio.Event();calls=0
    async def old():return pd.DataFrame({'value':[1]})
    await cache.get(('x',),old)
    async def fresh():
        nonlocal calls
        calls+=1;started.set();await release.wait()
        return pd.DataFrame({'value':[2]})
    first=asyncio.create_task(refreshed(cache,('x',),fresh))
    await started.wait()
    second=asyncio.create_task(refreshed(cache,('x',),fresh))
    await asyncio.sleep(0)
    # This independent task stays on the ordinary cache while refresh is active.
    normal=await cache.get(('x',),fresh)
    assert normal.iloc[0,0]==1 and normal.attrs['cache_hit']
    first.cancel()
    with pytest.raises(asyncio.CancelledError):await first
    release.set()
    value=await second
    assert calls==1 and value.iloc[0,0]==2 and value.attrs['inflight_shared']
    assert not refresh_history_results.get()


class CachedHistory(Tool):
    name='stock_history';description='test';read_only=True
    parameters={'type':'object','properties':{},'additionalProperties':False}
    def __init__(self):self.cache=HistoryRequestCache();self.calls=0;self.fail=False;self.cancel=False
    async def execute(self,args,ctx):
        assert refresh_history_results.get() is True
        if self.fail:raise RuntimeError('failure')
        if self.cancel:raise asyncio.CancelledError()
        async def producer():
            self.calls+=1
            return pd.DataFrame({'value':[self.calls]})
        value=await self.cache.get(('x',),producer)
        return ToolResult(str(value.iloc[0,0]))


async def test_openui_snapshot_never_fetches_explicit_refresh_does_and_restores_context(db):
    t=CachedHistory();r=ToolRegistry();r.register(t)
    async def context(scope):return SimpleNamespace(db=db,chat_id=1)
    service=OpenUITools(r,context)
    await save_evidence(db,1,'reply','q',[{'name':t.name,'arguments':{},'content':'frozen','is_error':False}])
    request=ToolRequest(name=t.name,arguments={},reply_hash=hashlib.sha256(b'reply').hexdigest())
    result=await service.execute(request,'s',False)
    assert result['data']['content']=='frozen' and t.calls==0
    request=request.model_copy(update={'refresh':True})
    for expected in [1,2]:
        result=await service.execute(request,'s',False)
        assert result['data']['content']==str(expected)
        assert result['evidence']['history_result_cache']=='bypassed_completed_results'
        assert not refresh_history_results.get()
    t.fail=True
    # Registry converts tool exceptions into error results; caller rejects them.
    with pytest.raises(Exception):await service.execute(request,'s',False)
    assert not refresh_history_results.get()
    t.fail=False;t.cancel=True
    with pytest.raises(asyncio.CancelledError):await service.execute(request,'s',False)
    assert not refresh_history_results.get()
