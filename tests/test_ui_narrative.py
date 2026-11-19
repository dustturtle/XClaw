import json
import pytest
from xclaw.ui_narrative import compile_narrative,paragraph_blocks

REPLY='结论：盘中，尚未收盘。\n\n## 走势\n\n价格 17.03。\n\n## 财报\n\n收入 100，累计而非单季；缺失不代表零。'

def test_immutable_text_complete_once_with_semantic_insertions():
    source=compile_narrative(json.dumps({'placements':[{'after':2,'source':'root = Col([HistoryChart("走势", q)])'},{'after':4,'source':'root = Col([FundamentalsCard("财报", f)])'}]}),REPLY)
    blocks=json.loads(source)['blocks']
    assert [b['source'] for b in blocks if b['type']=='text']==paragraph_blocks(REPLY)
    assert [b['id'] for b in blocks]==['p0','p1','p2','p2u0','p3','p4','p4u0']

@pytest.mark.parametrize('plan',[{'placements':[{'after':True,'source':'root = Col([])'}]}, {'placements':[{'after':100,'source':'root = Col([])'}]}, {'placements':[{'after':-1,'source':'root = Col([])'}]}, {'placements':[{'after':0,'source':'missing root'}]}, {'placements':[], 'text':'rewritten'}])
def test_invalid_layout_fails_closed(plan):
    with pytest.raises(ValueError):compile_narrative(json.dumps(plan),REPLY)

def test_no_component_preserves_original_path():
    assert compile_narrative('{"placements":[]}',REPLY)=='root = Col([])'
    assert compile_narrative('root = Col([])',REPLY)=='root = Col([])'

def test_table_not_split_or_rewritten():
    reply='说明\n\n|项|值|\n|---|---|\n|金额|100|\n\n注：来源A'
    blocks=json.loads(compile_narrative('{"placements":[{"after":1,"source":"root = Col([])"}]}',reply))['blocks']
    assert [x['source'] for x in blocks if x['type']=='text']==paragraph_blocks(reply)
    assert '|金额|100|' in blocks[1]['source']


def test_rewritten_prose_in_component_is_rejected():
    with pytest.raises(ValueError):
        compile_narrative(json.dumps({'placements':[{'after':0,'source':'root = Col([Text("changed facts")])'}]}),REPLY)


def test_cross_block_markdown_keeps_original_parser_scope():
    code='说明\n\n```python\na=1\n\nb=2\n```\n\n结论'
    assert paragraph_blocks(code)==['说明', '```python\na=1\n\nb=2\n```', '结论']
    reference='[来源][s]\n\n[s]: https://example.com'
    assert paragraph_blocks(reference)==[reference]
    assert paragraph_blocks('- one\n\n- two')==['- one\n\n- two']


@pytest.mark.parametrize('marker', ['```', '~~~', '`````', '~~~~~'])
def test_fenced_scope_allows_anchors_before_and_after(marker):
    code = marker + 'text\nfirst\n\nsecond\n' + marker
    reply = '结论\n\n' + code + '\n\n解释'
    assert paragraph_blocks(reply) == ['结论', code, '解释']
    rendered = json.loads(compile_narrative(json.dumps({'placements': [
        {'after': 0, 'source': 'root = Col([HistoryChart("走势", q)])'}]}), reply))['blocks']
    assert [block['source'] for block in rendered if block['type'] == 'text'] == ['结论', code, '解释']
    assert [block['type'] for block in rendered] == ['text', 'openui', 'text', 'text']


@pytest.mark.parametrize('closer', ['```', '~~~~', '```` trailing'])
def test_short_wrong_or_suffixed_fence_is_not_a_closer(closer):
    fence = '````text\na\n\n' + closer + '\n\nb\n````'
    assert paragraph_blocks('before\n\n' + fence + '\n\nafter') == ['before', fence, 'after']


def test_longer_closing_fence_and_multiple_fences():
    a = '```python\na\n\nb\n`````  '
    b = '~~~\nx\n\ny\n~~~~'
    assert paragraph_blocks('first\n\n' + a + '\n\nmiddle\n\n' + b + '\n\nlast') == ['first', a, 'middle', b, 'last']


def test_unclosed_fence_keeps_all_remaining_text_exactly():
    tail = '~~~text\na\n\n  b\n\ntrailing text\n'
    assert paragraph_blocks('before\n\n' + tail) == ['before', tail]


def test_fence_in_list_retains_list_scope():
    reply = '- first\n\n  ```text\n  a\n\n  b\n  ```\n\n- second'
    assert paragraph_blocks(reply) == [reply]


def test_reference_definition_still_retains_entire_scope_with_fences():
    reply = '[来源][s]\n\n```text\na\n\nb\n```\n\n[s]: https://example.com'
    assert paragraph_blocks(reply) == [reply]
