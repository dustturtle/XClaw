"""Fail-closed extraction of literal, button-bound actions from legacy UI.

This is deliberately a small subset parser, not a second OpenUI evaluator.
Unsupported/dynamic syntax grants no capabilities. Never use model-generated
text or a caller-supplied source as an authorization record.
"""
import json
import math
import re
from dataclasses import dataclass


_MAX_SOURCE_LENGTH = 65536
_MAX_SYNTAX_DEPTH = 64
_MAX_REFERENCE_DEPTH = 64
_MAX_RENDER_DEPTH = 128


def _check_nesting(source):
    # Bound the JSON decoder as well as DSL parsing before any recursive work.
    depth = 0
    quoted = escaped = False
    for char in source:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '([{':
            depth += 1
            if depth > _MAX_SYNTAX_DEPTH:
                raise ValueError('legacy syntax nesting limit')
        elif char in ')]}':
            depth -= 1
            if depth < 0:
                raise ValueError('unbalanced source')


@dataclass
class _Ref:
    name: str


@dataclass
class _Call:
    name: str
    args: list


_TOKEN = re.compile(r'\s*(?:(?P<string>"(?:\\.|[^"\\])*")|(?P<number>-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)|(?P<id>@?[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)|(?P<punct>[=,;(){}\[\]:]))')


class _Budget:
    def __init__(self):
        self.remaining = 50000

    def consume(self):
        self.remaining -= 1
        if self.remaining < 0:
            raise ValueError('legacy parsing work limit')


class _Parser:
    def __init__(self, source, budget):
        _check_nesting(source)
        self.tokens = []
        pos = 0
        end = len(source.rstrip())
        while pos < end:
            budget.consume()
            match = _TOKEN.match(source, pos)
            if not match:
                raise ValueError('unsupported syntax')
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
            pos = match.end()
        self.i = 0

    def take(self, expected=None):
        if self.i >= len(self.tokens):
            raise ValueError('truncated source')
        kind, value = self.tokens[self.i]
        if expected is not None and value != expected:
            raise ValueError('unexpected token')
        self.i += 1
        return kind, value

    def peek(self, value):
        return self.i < len(self.tokens) and self.tokens[self.i][1] == value

    def sequence(self, end, depth):
        values = []
        if not self.peek(end):
            while True:
                values.append(self.value(depth + 1))
                if not self.peek(','):
                    break
                self.take(',')
        self.take(end)
        return values

    def value(self, depth=0):
        if depth > _MAX_SYNTAX_DEPTH:
            raise ValueError('legacy expression depth limit')
        kind, value = self.take()
        if kind == 'string':
            return json.loads(value)
        if kind == 'number':
            n = json.loads(value)
            if not math.isfinite(n):
                raise ValueError('nonfinite number')
            return n
        if value == '[':
            return self.sequence(']', depth)
        if value == '{':
            obj = {}
            if not self.peek('}'):
                while True:
                    k, key = self.take()
                    if k == 'string':
                        key = json.loads(key)
                    elif k != 'id' or not re.fullmatch(r'[A-Za-z_]\w*', key):
                        raise ValueError('invalid key')
                    if key in obj or key in {'__proto__', 'constructor', 'prototype'}:
                        raise ValueError('ambiguous key')
                    self.take(':')
                    obj[key] = self.value(depth + 1)
                    if not self.peek(','):
                        break
                    self.take(',')
            self.take('}')
            return obj
        if kind == 'id':
            if value in {'true', 'false', 'null'}:
                return {'true': True, 'false': False, 'null': None}[value]
            if self.peek('('):
                self.take('(')
                return _Call(value, self.sequence(')', depth))
            return _Ref(value)
        raise ValueError('unsupported expression')

    def parse(self):
        definitions = {}
        while self.i < len(self.tokens):
            kind, name = self.take()
            if kind != 'id' or not re.fullmatch(r'[A-Za-z_]\w*', name) or name in definitions:
                raise ValueError('ambiguous definition')
            self.take('=')
            definitions[name] = self.value()
            if self.peek(';'):
                self.take(';')
        if 'root' not in definitions:
            raise ValueError('missing root')
        return definitions


def _literal(value, budget, depth=0):
    budget.consume()
    if depth > _MAX_SYNTAX_DEPTH:
        raise ValueError('legacy literal depth limit')
    if isinstance(value, (_Ref, _Call)):
        return False
    if isinstance(value, dict):
        return all(_literal(v, budget, depth + 1) for v in value.values())
    if isinstance(value, list):
        return all(_literal(v, budget, depth + 1) for v in value)
    return True


def _extract(source, budget):
    definitions = _Parser(source, budget).parse()
    found = []

    def walk(value, stack=(), depth=0):
        budget.consume()
        if depth > _MAX_RENDER_DEPTH or len(stack) > _MAX_REFERENCE_DEPTH:
            raise ValueError('legacy render/reference depth limit')
        if isinstance(value, _Ref):
            # A render slot must resolve to a component, never a property.
            if '.' in value.name:
                raise ValueError('property is not a renderable component')
            if value.name not in definitions or value.name in stack:
                raise ValueError('unresolved reference')
            walk(definitions[value.name], (*stack, value.name), depth + 1)
        elif isinstance(value, _Call):
            if value.name == 'Button':
                if len(value.args) != 2 or not isinstance(value.args[0], str) or not value.args[0].strip():
                    raise ValueError('unsupported button')
                action = value.args[1]
                if not isinstance(action, _Call) or action.name != 'Action' or len(action.args) != 1 or not isinstance(action.args[0], list):
                    raise ValueError('unsupported button action')
                for run in action.args[0]:
                    budget.consume()
                    if not isinstance(run, _Call) or run.name != '@Run' or len(run.args) != 1 or not isinstance(run.args[0], _Ref):
                        raise ValueError('unsupported run')
                    target = definitions.get(run.args[0].name)
                    if not isinstance(target, _Call) or target.name not in {'Query', 'Mutation'} or len(target.args) != 2:
                        raise ValueError('invalid target')
                    name, args = target.args
                    if not isinstance(name, str) or not isinstance(args, dict) or not _literal(args, budget):
                        raise ValueError('dynamic target')
                    if target.name == 'Mutation':
                        found.append((name, args))
                        if len(found) > 64:
                            raise ValueError('too many legacy actions')
            elif value.name in {'Action', '@Run', 'Mutation', 'ToAssistant'} or value.name.startswith('@'):
                raise ValueError('unbound action')
            elif value.name in {'Col', 'Row'}:
                if len(value.args) != 1 or not isinstance(value.args[0], list):
                    raise ValueError('unsupported container')
                for child in value.args[0]:
                    walk(child, stack, depth + 1)
            elif value.name in {
                'Text', 'Heading', 'Portfolio', 'Watchlist', 'MutationStatus',
                'FundFlowCard', 'QuoteCard', 'HistoryChart', 'IndicatorsCard',
                'FundamentalsCard', 'NewsList', 'BacktestCard', 'StrategyScanCard',
                'ReportCard', 'TaskList', 'EvidenceCard',
            }:
                # Display arguments are not child slots. Do not grant actions
                # hidden in an arbitrary component's props or a text string.
                for item in value.args:
                    budget.consume()
                    if isinstance(item, _Ref):
                        root, *properties = item.name.split('.')
                        target = definitions.get(root)
                        if not isinstance(target, _Call):
                            raise ValueError('unknown display reference')
                        if properties:
                            if target.name not in {'Query', 'Mutation'} or len(properties) != 1 or properties[0] not in {'status', 'error', 'data'}:
                                raise ValueError('unsupported display property')
                        elif target.name != 'Query':
                            raise ValueError('nonquery display reference')
                        if len(target.args) != 2 or not _literal(target.args, budget):
                            raise ValueError('dynamic display target')
                    elif not _literal(item, budget):
                        raise ValueError('nonliteral display argument')
            else:
                raise ValueError('unsupported component')
        else:
            raise ValueError('noncomponent in render slot')

    walk(definitions['root'], ('root',))
    return found


def legacy_actions(source):
    """Return exact (tool name, literal arguments) pairs, or [] on ambiguity.

    Supports standalone legacy DSL and xclaw-narrative-1 envelopes. Only openui
    blocks are executable. If any executable block is unsupported, fail closed
    for the whole presentation.
    """
    try:
        if not isinstance(source, str) or len(source) > _MAX_SOURCE_LENGTH:
            return []
        _check_nesting(source)
        budget = _Budget()
        if source.lstrip().startswith('{'):
            envelope = json.loads(source)
            if not isinstance(envelope, dict) or envelope.get('version') != 'xclaw-narrative-1' or not isinstance(envelope.get('blocks'), list):
                return []
            sources = []
            for block in envelope['blocks']:
                budget.consume()
                if not isinstance(block, dict) or block.get('type') not in {'text', 'openui'} or not isinstance(block.get('source'), str):
                    return []
                if block['type'] == 'openui':
                    sources.append(block['source'])
        else:
            sources = [source]
        result = []
        for item in sources:
            for action in _extract(item, budget):
                if action not in result:
                    result.append(action)
                    if len(result) > 64:
                        return []
        return result
    except (ValueError, TypeError, RecursionError, OverflowError):
        return []


def legacy_request_id(token_hash, external_chat_id, reply_hash, name, arguments):
    """Reproduce the old frontend's deterministic request ID for a safe subset.

    The first field is SHA256(token), NOT effective_chat_id. Property order is
    intentionally preserved because the browser hashes JSON.stringify(args).
    Return None rather than guess on JS number/property-order corner cases.
    """
    import hashlib

    def normalize(value):
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, str):
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
                raise ValueError('surrogate')
            return value
        if isinstance(value, (int, float)):
            if not math.isfinite(value) or abs(value) > 2**53 - 1:
                raise ValueError('unsafe number')
            if value == int(value):
                return int(value)
            # Python and JS ordinary shortest decimal formatting coincide here.
            # Exponent and very small magnitudes are deliberately unsupported.
            if 'e' in repr(value).lower() or abs(value) < 1e-6:
                raise ValueError('unsupported numeric notation')
            return value
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if not isinstance(key, str) or key.isdecimal():
                    raise ValueError('numeric property order')
                result[normalize(key)] = normalize(item)
            return result
        raise ValueError('unsupported JSON value')

    try:
        if not isinstance(token_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', token_hash):
            return None
        if not isinstance(reply_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', reply_hash):
            return None
        if not isinstance(external_chat_id, str) or not isinstance(name, str) or not isinstance(arguments, dict):
            return None
        encoded = json.dumps(normalize(arguments), ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        raw = f'{token_hash}:{external_chat_id}:{reply_hash}:{name}{encoded}'
        return hashlib.sha256(raw.encode()).hexdigest()
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        return None
