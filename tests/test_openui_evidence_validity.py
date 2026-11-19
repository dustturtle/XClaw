"""Synthetic zero-activity quotes must not be treated as current trades."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from xclaw.db import Database
from xclaw.llm_types import DoneEvent, LLMResponse, TextBlock
from xclaw.openui import OpenUITools, ToolRequest, evidence_usable, quote_fallback_rejected
from xclaw.tools import Tool, ToolRegistry, ToolResult
from xclaw.ui_evidence import save_evidence

BAD_QUOTE = '''标的: 示例证券 (DEMO)
当前价: 20.50
涨跌幅: 0.00%
成交量: 0手
成交额: 0.00万元
今开: 0.00
最高: 0.00
最低: 0.00
昨收: 20.50
报价时间: 2025-01-07 09:00:00
数据源: 腾讯直连 HTTP'''
REJECTED_REPLY = '示例行情的开高低和成交量为零，不能作为有效成交报价。'


class Quote(Tool):
    name='stock_quote'
    description='test only'
    read_only=True
    parameters={'type':'object','properties':{'symbol':{'type':'string'}},'required':['symbol']}
    calls=0
    async def execute(self,args,ctx):
        self.calls+=1
        return ToolResult(BAD_QUOTE)


class EmptyLayout:
    calls=0
    async def chat_stream(self,*args,**kwargs):
        self.calls+=1
        yield DoneEvent(response=LLMResponse(stop_reason='end_turn',content=[TextBlock(text='{"placements":[]}')]))


@pytest.fixture
async def context(tmp_path):
    db=Database(tmp_path/'validity.db');await db.connect()
    tool=Quote();registry=ToolRegistry();registry.register(tool)
    llm=EmptyLayout()
    ctx=SimpleNamespace(db=db,chat_id=1,llm=llm)
    async def factory(scope):return ctx
    service=OpenUITools(registry,factory)
    yield service,ctx,tool
    await db.close()


@pytest.mark.parametrize('escaped',[False,True])
async def test_rejected_zero_activity_reference_quote_cannot_be_resurrected(context,escaped):
    service,ctx,tool=context
    content=BAD_QUOTE.replace('\n','\\n') if escaped else BAD_QUOTE
    await save_evidence(ctx.db,1,REJECTED_REPLY,'请检查示例报价是否有效',[{'name':'stock_quote','arguments':{'symbol':'DEMO'},'content':content,'is_error':False}])
    events=[e async for e in service.generate(ctx,REJECTED_REPLY)]
    result=json.loads(events[-1]['data'])
    assert result['outcome']=='text_only'
    assert 'QuoteCard' not in result['source'] and ctx.llm.calls==0
    req=ToolRequest(name='stock_quote',arguments={'symbol':'DEMO'},reply_hash=hashlib.sha256(REJECTED_REPLY.encode()).hexdigest())
    with pytest.raises(HTTPException) as error:await service.execute(req,'same-chat',False)
    assert error.value.detail['code']=='evidence_failed' and tool.calls==0
    with pytest.raises(HTTPException) as error:await service.execute(req.model_copy(update={'refresh':True}),'same-chat',False)
    assert error.value.detail['code']=='tool_failed' and tool.calls==1


@pytest.mark.parametrize('reply',[
    REJECTED_REPLY,
    '此报价不能作为有效盘中行情。',
    '这条行情不应使用，暂只保留文字分析。',
    '当前报价数据已排除不用。',
])
async def test_empty_plan_does_not_override_explicit_quote_rejection(context,reply):
    service,ctx,_=context
    valid_quote=BAD_QUOTE.replace('今开: 0.00','今开: 20.00')
    await save_evidence(ctx.db,1,reply,'查看行情',[{'name':'stock_quote','arguments':{'symbol':'DEMO'},'content':valid_quote,'is_error':False}])
    result=json.loads([e async for e in service.generate(ctx,reply)][-1]['data'])
    assert result['outcome']=='empty'
    assert 'QuoteCard' not in result['source']


@pytest.mark.parametrize('content',[
    '当前价: 20.50\n报价时间: 2025-01-06 15:00:00', # normal historical close, optional fields absent
    BAD_QUOTE.replace('今开: 0.00','今开: 20.00').replace('09:00:00','15:00:00'),
    BAD_QUOTE.replace('成交额: 0.00万元\n',''), # absence is not zero
    BAD_QUOTE.replace('成交量: 0手','成交量: 1手'),
])
def test_normal_close_or_optional_field_absence_not_classified_invalid(content):
    assert evidence_usable({'name':'stock_quote','content':content,'is_error':False})


def test_case16_and19_do_not_gain_unsafe_intraday_fallback():
    # #16 has only minute evidence; #19 explicitly excludes mapped-stock minute data.
    # The narrow fallback supports neither minute/history cards nor arbitrary generic cards.
    assert quote_fallback_rejected('另一路分时查询出现代码映射异常，已排除不用。')
    assert not quote_fallback_rejected('今日收盘报价正常；成交量字段未提供，不代表零。')


@pytest.mark.parametrize('reply', ['上午走势仅分析09:30至11:30。', '另一路分时查询出现代码映射异常，已排除不用。'])
async def test_minute_evidence_empty_plan_is_not_forced_into_a_chart(context,reply):
    service,ctx,_=context
    await save_evidence(ctx.db,1,reply,'分析走势',[{'name':'stock_intraday','arguments':{'symbol':'000852','date':'2025-01-07'},'content':'{"symbol":"000852.SZ","bars":[]}', 'is_error':False}])
    result=json.loads([e async for e in service.generate(ctx,reply)][-1]['data'])
    assert result['outcome']=='empty'
    assert 'HistoryChart' not in result['source'] and 'QuoteCard' not in result['source']
