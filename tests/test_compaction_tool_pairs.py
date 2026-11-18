from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from xclaw.agent_engine import _repair_orphan_tool_results, _safe_compaction_cut, _compact_messages, _micro_compact
from xclaw.llm_types import Message, ToolUseBlock, ToolResultBlock, TextBlock, LLMResponse, StopReason
from xclaw.llm_responses import ResponsesProvider


def call(id): return ToolUseBlock(id=id,name='watchlist',input={'action':'add'})
def output(id): return ToolResultBlock(tool_use_id=id,content='already executed',is_error=False)

def assert_paired(messages):
    provider=ResponsesProvider(api_key='',model='test',base_url='http://unused')
    payload=provider._build_payload(messages,None,None,100)
    pending=set()
    for item in payload['input']:
        if item['type']=='function_call':pending.add(item['call_id'])
        if item['type']=='function_call_output':
            assert item['call_id'] in pending
            pending.remove(item['call_id'])


@pytest.mark.asyncio
async def test_compaction_preserves_parallel_tool_boundary():
    messages=[Message(role='user',content='old'),Message(role='assistant',content=[call('a'),call('b')]),
              Message(role='user',content=[output('a'),output('b')]),Message(role='assistant',content='done')]
    assert _safe_compaction_cut(messages,2)==1
    llm=SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(content=[TextBlock(text='summary')],stop_reason=StopReason.end_turn)))
    result=await _compact_messages(messages,SimpleNamespace(llm=llm),2)
    assert_paired(result)
    llm.chat.assert_awaited_once()


def test_legacy_orphan_preserved_as_text_no_synthesized_call():
    original=[Message(role='user',content='summary'),Message(role='user',content=[output('lost')]),
              Message(role='assistant',content=[call('ok')]),Message(role='user',content=[output('ok')])]
    result=_repair_orphan_tool_results(original)
    assert isinstance(result[1].content[0],TextBlock)
    assert 'already executed' in result[1].content[0].text
    assert isinstance(original[1].content[0],ToolResultBlock)
    assert_paired(result)
    assert _repair_orphan_tool_results(result)==result


def test_duplicate_output_is_not_replayed():
    messages=[Message(role='assistant',content=[call('a')]),Message(role='user',content=[output('a'),output('a')])]
    result=_repair_orphan_tool_results(messages)
    assert isinstance(result[1].content[1],TextBlock)
    assert_paired(result)


def test_microcompact_does_not_break_pairs():
    messages=[]
    for i in range(5):messages.extend([Message(role='assistant',content=[call(str(i))]),Message(role='user',content=[ToolResultBlock(tool_use_id=str(i),content='x'*500)])])
    _micro_compact(messages)
    assert_paired(messages)


def test_cut_moves_before_all_crossing_pairs():
    messages=[Message(role='assistant',content=[call('a')]),Message(role='assistant',content=[call('b')]),
              Message(role='user',content=[output('a')]),Message(role='user',content=[output('b')])]
    assert _safe_compaction_cut(messages,1)==0

@pytest.mark.parametrize('messages',[
 [Message(role='assistant',content=[call('a')])],
 [Message(role='user',content=[output('a')]),Message(role='assistant',content=[call('a')])],
 [Message(role='assistant',content=[call('a'),call('a')]),Message(role='user',content=[output('a')])],
 [Message(role='assistant',content=[call('a')]),Message(role='user',content='interrupt'),Message(role='user',content=[output('a')])],
 [Message(role='assistant',content=[call('a'),call('b')]),Message(role='user',content=[output('a')])],
 [Message(role='user',content=[call('a')]),Message(role='assistant',content=[output('a')])],
])
def test_damaged_groups_demoted_without_losing_original_blocks(messages):
    result=_repair_orphan_tool_results(messages)
    assert_paired(result)
    assert not any(isinstance(b,(ToolUseBlock,ToolResultBlock)) for m in result if isinstance(m.content,list) for b in m.content)
    old=[b.model_dump_json() for m in messages if isinstance(m.content,list) for b in m.content]
    import json
    retained=[json.loads(b.text.split('\n',1)[1]) for m in result if isinstance(m.content,list) for b in m.content]
    assert retained==[json.loads(v) for v in old]


def test_valid_parallel_results_reverse_order_and_multiple_messages():
    messages=[Message(role='assistant',content=[call('a'),call('b')]),Message(role='user',content=[output('b')]),Message(role='user',content=[output('a')])]
    assert _repair_orphan_tool_results(messages)==messages
    assert_paired(messages)


def test_microcompact_only_registry_confirmed_readonly():
    registry=SimpleNamespace(get=lambda name: SimpleNamespace(read_only=(name=='read')))
    messages=[]
    for i,name in enumerate(['write','read','read']):
        messages.extend([Message(role='assistant',content=[ToolUseBlock(id=str(i),name=name,input={})]),Message(role='user',content=[ToolResultBlock(tool_use_id=str(i),content='x'*500)])])
    _micro_compact(messages,keep_recent_tool_results=1,registry=registry)
    assert len(messages[1].content[0].content)==500
    assert '已压缩' in messages[3].content[0].content
    assert len(messages[5].content[0].content)==500


@pytest.mark.asyncio
async def test_macro_preserves_mutation_result_verbatim_across_compactions():
    registry=SimpleNamespace(get=lambda name: SimpleNamespace(read_only=False))
    llm=SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(content=[TextBlock(text='short')],stop_reason=StopReason.end_turn)))
    ctx=SimpleNamespace(llm=llm,tools=registry)
    messages=[Message(role='assistant',content=[call('a')]),Message(role='user',content=[output('a')]),Message(role='assistant',content='tail')]
    first=await _compact_messages(messages,ctx,1)
    second=await _compact_messages(first+[Message(role='user',content='new')],ctx,1)
    text='\n'.join(m.text_content() for m in second)
    assert 'already executed' in text and '"is_error": false' in text and '"action": "add"' in text
    assert_paired(second)


@pytest.mark.asyncio
async def test_execution_record_budget_fails_locally_before_summary():
    llm=SimpleNamespace(chat=AsyncMock())
    messages=[Message(role='assistant',content=[call('a')]),Message(role='user',content=[ToolResultBlock(tool_use_id='a',content='x'*200001)]),Message(role='user',content='tail')]
    with pytest.raises(ValueError,match='上限'):
        await _compact_messages(messages,SimpleNamespace(llm=llm),1)
    llm.chat.assert_not_awaited()

@pytest.mark.asyncio
async def test_long_session_multiple_compactions_parallel_reads_single_mutation(db):
    """Real agent_loop + temp SQLite, fake provider with strict payload validation."""
    from xclaw.agent_engine import AgentContext, agent_loop, _messages_from_serializable
    from xclaw.tools import Tool, ToolRegistry, ToolResult
    counts = {'mutation':0,'read':0}
    class Read(Tool):
        name='read';description='fixture read';parameters={'type':'object','properties':{}}
        read_only=True;independent=True
        async def execute(self,params,context):
            counts['read']+=1
            return ToolResult(content='read-evidence-'+'r'*150)
    class Write(Tool):
        name='mutation';description='fixture write';parameters={'type':'object','properties':{}}
        read_only=False
        async def execute(self,params,context):
            counts['mutation']+=1
            return ToolResult(content='unique-receipt-executed-once',is_error=False)
    registry=ToolRegistry();registry.register(Read());registry.register(Write())
    provider=ResponsesProvider(api_key='',model='offline',base_url='http://unused')
    class FakeLLM:
        summaries=0;turn=0;waiting=False;validated=0
        async def chat(self,messages,tools=None,system=None,max_tokens=4096):
            provider._build_payload(messages,tools,system,max_tokens)
            self.validated+=1
            if not tools:
                self.summaries+=1
                if self.summaries==1:raise RuntimeError('fixture summary failure')
                return LLMResponse(content=[TextBlock(text='truncated summary')],stop_reason=StopReason.max_tokens)
            if not self.waiting:
                calls=[ToolUseBlock(id=f'r{self.turn}-{i}',name='read',input={}) for i in range(2)]
                if self.turn==0:calls.append(ToolUseBlock(id='only-mutation',name='mutation',input={}))
                self.waiting=True
                return LLMResponse(content=calls,stop_reason=StopReason.tool_use)
            self.waiting=False;self.turn+=1
            return LLMResponse(content=[TextBlock(text='done')],stop_reason=StopReason.end_turn)
    fake=FakeLLM()
    chat_id=await db.get_or_create_chat('web','synthetic-compaction-regression')
    settings=SimpleNamespace(max_session_messages=6,compact_keep_recent=3,max_tool_iterations=4,max_tokens=1024,openui_enabled=False)
    ctx=AgentContext(chat_id=chat_id,channel='web',db=db,llm=fake,tools=registry,settings=settings)
    try:
        for turn in range(8):
            assert await agent_loop(ctx,f'synthetic turn {turn}')=='done'
            saved=_messages_from_serializable(await db.load_session(chat_id))
            provider._build_payload(saved,registry.get_definitions(),None,1024)
        assert fake.summaries>=3 and counts['mutation']==1 and counts['read']==16
        assert 'unique-receipt-executed-once' in '\n'.join(m.text_content() for m in saved)
        assert fake.validated>=19
    finally:
        await provider._client.aclose()
