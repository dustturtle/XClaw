import json

import pytest

from xclaw.legacy_ui_actions import legacy_actions

SOURCE = '''q = Query("portfolio_manage", {action:"view",symbol:"",market:"CN",shares:0,price:0})
m = Mutation("portfolio_manage", {action:"buy",symbol:"600519",market:"CN",shares:100,price:1000})
root = Col([Portfolio("本地模拟持仓",q),Button("确认记录模拟持仓",Action([@Run(m),@Run(q)])),MutationStatus(m.status,m.error)])'''
EXPECTED = [('portfolio_manage', {'action':'buy','symbol':'600519','market':'CN','shares':100,'price':1000})]


def test_actual_legacy_shape():
    assert legacy_actions(SOURCE) == EXPECTED


def test_narrative_only_executable_blocks():
    envelope = {'version':'xclaw-narrative-1','blocks':[
        {'type':'text','source':'m = Mutation("evil", {})'},
        {'type':'openui','source':SOURCE}]}
    assert legacy_actions(json.dumps(envelope)) == EXPECTED


@pytest.mark.parametrize('source', [
    SOURCE.replace('shares:100', 'shares:input.value'),
    SOURCE.replace('shares:100', 'shares:100,shares:200'),
    SOURCE.replace('root = Col', 'm = Mutation("evil", {})\nroot = Col'),
    SOURCE.replace('Action([@Run(m),@Run(q)])', 'Action([@Run(m),@ToAssistant("hi")])'),
    SOURCE.replace('Action([@Run(m),@Run(q)])', 'Action([@Run(m, {})])'),
    SOURCE.replace('Button("确认记录模拟持仓",Action([@Run(m),@Run(q)]))', '@Run(m)'),
    SOURCE.replace('price:1000', 'price:1e999'),
    SOURCE + '\nroot = Col([])',
    SOURCE.replace('shares:100', 'shares:100+1'),
    SOURCE.replace('Action([@Run(m),@Run(q)])', 'Action([@Run(missing)])'),
    'root = Col([root])',
])
def test_fail_closed(source):
    assert legacy_actions(source) == []


def test_unreachable_mutation_not_authorized():
    assert legacy_actions('m = Mutation("evil", {})\nroot = Col([])') == []


def test_json_strings_are_not_code():
    source = 'root = Col([Text("Button(\\\"Confirm\\\", Action([@Run(m)]))")])'
    assert legacy_actions(source) == []


def test_v2_envelope_not_legacy():
    assert legacy_actions(json.dumps({'version':'xclaw-inline-1','blocks':[]})) == []


def test_legacy_request_id_matches_browser_formula():
    import hashlib
    from xclaw.legacy_ui_actions import legacy_request_id
    token_hash = hashlib.sha256(b'test-token').hexdigest()
    digest = 'a' * 64
    name, args = EXPECTED[0]
    raw = token_hash + ':poc-example:' + digest + ':portfolio_manage' + '{"action":"buy","symbol":"600519","market":"CN","shares":100,"price":1000}'
    assert legacy_request_id(token_hash, 'poc-example', digest, name, args) == hashlib.sha256(raw.encode()).hexdigest()
    assert legacy_request_id(token_hash, 'poc-example', digest, name, {**args, 'price': 1000.0}) == hashlib.sha256(raw.encode()).hexdigest()


@pytest.mark.parametrize('args', [{'x':1e-8}, {'x':2**54}, {'0':'a'}, {'x':float('nan')}, {'x':'\ud800'}])
def test_request_id_unsupported_returns_none(args):
    from xclaw.legacy_ui_actions import legacy_request_id
    assert legacy_request_id('b'*64, 'chat', 'a'*64, 'tool', args) is None


@pytest.mark.parametrize('wrapper', ['Hidden', 'If', 'Text', 'Unknown'])
def test_button_in_non_child_slot_rejected(wrapper):
    source = SOURCE.replace('root = Col([', f'root = {wrapper}([')
    assert legacy_actions(source) == []


def test_duplicate_reference_fanout_is_bounded(monkeypatch):
    from xclaw import legacy_ui_actions as module
    visits = 0
    consume = module._Budget.consume

    def count(self):
        nonlocal visits
        visits += 1
        return consume(self)

    monkeypatch.setattr(module._Budget, 'consume', count)
    # A tiny DAG represents over a billion visits if references are expanded
    # without a work budget. A read-only leaf avoids action-count early exit.
    parts = ['leaf = Text("read only")']
    prev = 'leaf'
    for i in range(32):
        name = f'n{i}'
        parts.append(f'{name} = Col([{prev},{prev}])')
        prev = name
    parts.append(f'root = Col([{prev}])')
    assert module.legacy_actions('\n'.join(parts)) == []
    assert visits == 50001


def test_oversized_token_stream_is_bounded():
    source = 'unused = [' + ','.join('0' for _ in range(30000)) + ']\nroot = Col([])'
    assert legacy_actions(source) == []


def test_trailing_whitespace_is_supported():
    assert legacy_actions(SOURCE + ' \n\t' * 1000) == EXPECTED


@pytest.mark.parametrize('root', [
    '{hidden:Button("确认",Action([@Run(m)]))}',
    '[Button("确认",Action([@Run(m)]))]',
    'Col([{hidden:Button("确认",Action([@Run(m)]))}])',
    'Col([[Button("确认",Action([@Run(m)]))]])',
    'Row([{hidden:Button("确认",Action([@Run(m)]))}])',
    'Text({hidden:Button("确认",Action([@Run(m)]))})',
    'Text([Button("确认",Action([@Run(m)]))])',
    'Col([hidden])',
    'hidden',
    'Col([m.data])',
    'Col([q])',
])
def test_actions_hidden_in_non_render_slots_not_authorized(root):
    prefix = '\n'.join(SOURCE.splitlines()[:2])
    source = prefix + '\nhidden = {button:Button("确认",Action([@Run(m)]))}\nroot = ' + root
    assert legacy_actions(source) == []


def test_component_reference_is_renderable():
    prefix = '\n'.join(SOURCE.splitlines()[:2])
    source = prefix + '\nb = Button("确认",Action([@Run(m)]))\nroot = Col([Row([b])])'
    assert legacy_actions(source) == EXPECTED


def test_explicit_syntax_depth_limit():
    from xclaw.legacy_ui_actions import _Budget, _Parser
    with pytest.raises(ValueError, match='nesting limit'):
        _Parser('root = ' + 'Col([' * 40 + 'Text("x")' + '])' * 40, _Budget())
    assert legacy_actions('root = ' + 'Col([' * 40 + 'Text("x")' + '])' * 40) == []


def test_explicit_reference_depth_limit():
    from xclaw.legacy_ui_actions import _Budget, _extract
    source = '\n'.join(['n0 = Text("x")'] + [f'n{i} = n{i-1}' for i in range(1, 70)] + ['root = n69'])
    with pytest.raises(ValueError, match='reference depth limit'):
        _extract(source, _Budget())
    assert legacy_actions(source) == []


def test_source_length_limit():
    assert legacy_actions(SOURCE + ' ' * 65536) == []


def test_json_envelope_nesting_limit():
    assert legacy_actions('{"version":' + '[' * 70 + '0' + ']' * 70 + '}') == []
