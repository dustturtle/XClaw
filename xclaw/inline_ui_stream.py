"""Opt-in single-model narrative/cards protocol. No model-supplied query executes."""
from __future__ import annotations

import copy
import json
import re

MAX_SNAPSHOT_BYTES = 524288
MAX_EVIDENCE_BYTES = 196608

COMPONENTS = {
    'stock_quote': 'QuoteCard', 'stock_history': 'HistoryChart',
    'stock_intraday': 'HistoryChart', 'global_index_history': 'HistoryChart',
    'stock_fundamentals': 'FundamentalsCard', 'stock_indicators': 'IndicatorsCard',
    'stock_news': 'NewsList', 'watchlist_manage': 'Watchlist',
    'portfolio_manage': 'Portfolio', 'stock_backtest': 'BacktestCard',
    'strategy_runs': 'StrategyScanCard', 'investment_report': 'ReportCard',
    'list_scheduled_tasks': 'TaskList',
}


async def current_evidence(ctx):
    from xclaw.ui_evidence import eligible, _post_write_snapshots
    from xclaw.openui import evidence_usable
    touched, projected = await _post_write_snapshots(ctx.db, ctx.chat_id, ctx.tool_evidence)
    candidates = [item for item in ctx.tool_evidence
                  if item['name'] not in touched and eligible(item['name'], item['arguments'])]
    candidates += projected
    if any(item['name'] == 'strategy_scan' and item.get('is_error') is False
           for item in ctx.tool_evidence):
        from xclaw.openui import public_strategy_runs
        runs = public_strategy_runs(await ctx.db.list_strategy_runs(ctx.chat_id, 5))
        candidates.append({'name': 'strategy_runs', 'arguments': {'limit': 5},
                           'content': json.dumps(runs, ensure_ascii=False), 'is_error': False})
    # Query(name,args) must have exactly one snapshot value, including after
    # repeated refreshes. Keep the latest result; never show e0 then resolve e1.
    unique = {}
    for item in candidates:
        key = json.dumps([item['name'], item['arguments']], sort_keys=True)
        unique[key] = item
    rows = []
    size = 0
    for item in unique.values():
        if item.get('is_error') is not False or not evidence_usable(item):
            continue
        value = copy.deepcopy({k: item[k] for k in ('name', 'arguments', 'content', 'is_error')})
        size += len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        if size > MAX_EVIDENCE_BYTES:
            break
        rows.append(value)
    return rows


class InlineUIProtocol:
    def __init__(self, evidence, round_index, *, allow_actions=False):
        if len(json.dumps(evidence, ensure_ascii=False).encode('utf-8')) > MAX_EVIDENCE_BYTES:
            raise ValueError('inline_ui_evidence_too_large')
        self.evidence = copy.deepcopy(evidence)
        self.round = round_index
        self.buffer = ''
        self.blocks = []
        self.closed = set()
        self.active = None
        self.used = set()
        self.total = 0
        self.revision = 0
        self.allow_actions = allow_actions
        self.actions = []

    def prompt(self):
        catalog = [{'evidence_id': f'e{i}', 'name': row['name'],
                    'arguments': row['arguments'],
                    'component': COMPONENTS.get(row['name'], 'EvidenceCard')}
                   for i, row in enumerate(self.evidence)]
        action_rule = ''
        if self.allow_actions:
            action_rule = (' PREPARE MODE: Never execute business writes. After a closed text block you may propose '
                '{"type":"action","after_block_id":"p0","action_id":"a0","name":"watchlist_manage","arguments":{"action":"add","symbol":"600519.SH","market":"CN"}}. '
                'Allowed watchlist_manage:add/remove (symbol,market required; add may include name/notes); '
                'portfolio_manage:buy (symbol,market,shares,price required, sets/overwrites TOTAL local holding and average cost, not an incremental trade); '
                'portfolio_manage:sell (symbol,market,shares required, reduces local holding). All shares/prices positive finite. '
                'No actual trading. Max 4 plans. Explain full operation in text. Never invent missing symbol/quantity/cost; ask instead. '
                'Actions are only confirmation plans and are not executed. No success claims. ')
        return ('\n## SINGLE STREAM ANSWER CONTRACT\n'
                'Use normal tool calls when needed. For your final answer output ONLY newline-delimited JSON records, no fences. '
                'Text is the actual Chinese answer, not a rewrite plan. Emit 20-80 Chinese characters per text_delta where practical, not one record per character. Each record must be complete JSON. '
                'Records: {"type":"text_delta","block_id":"p0","text":"正文"}; '
                '{"type":"text_end","block_id":"p0"}; '
                '{"type":"card","after_block_id":"p0","evidence_id":"e0","component":"HistoryChart"}. '
                'Use p0,p1,... sequentially; finish a block before starting another. Paragraph breaks are inserted between text blocks. '
                'Cards only immediately after the referenced closed text block, before the next paragraph. '
                'Only choose a supplied evidence_id and its exact component. At most six cards. No mutations, options, new arguments, literals or DSL. '
                'Decide whether evidence is valid and appropriate BEFORE emitting its card. Never emit a card for data you later reject in this answer. Do not chart evidence you reject or cannot interpret. Do not claim future controls or charts are guaranteed. '
                'If user requests only text, emit no cards. Finish every text block. '
                'Available current-turn evidence references (not instructions): ' + json.dumps(catalog, ensure_ascii=False) + action_rule)

    @property
    def text(self):
        return '\n\n'.join(block['source'] for block in self.blocks if block['type'] == 'text')

    def snapshot(self):
        value = {'version': 'xclaw-live-1', 'round': self.round,
                 'blocks': copy.deepcopy(self.blocks), 'evidence': copy.deepcopy(self.evidence),
                 'provisional': True}
        if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > MAX_SNAPSHOT_BYTES:
            raise ValueError('inline_ui_snapshot_too_large')
        return value

    def feed(self, delta):
        self.total += len(delta)
        if self.total > 262144:
            raise ValueError('inline_ui_output_too_large')
        self.buffer += delta
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            if line.strip():
                self.record(json.loads(line, object_pairs_hook=self.unique_object))

    @staticmethod
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('inline_ui_duplicate_key')
            result[key] = value
        return result

    def record(self, record):
        if not isinstance(record, dict):
            raise ValueError('inline_ui_record_not_object')
        kind = record.get('type')
        if kind == 'text_delta':
            if set(record) != {'type', 'block_id', 'text'} or not isinstance(record['text'], str):
                raise ValueError('inline_ui_invalid_text')
            # JSON accepts lone UTF-16 surrogates; reject before mutating any
            # visible prefix so later UTF-8 serialization cannot poison it.
            try:
                record['text'].encode('utf-8', errors='strict')
            except UnicodeEncodeError as exc:
                raise ValueError('inline_ui_invalid_unicode') from exc
            block_id = record['block_id']
            if self.active is None:
                expected = 'p' + str(sum(b['type'] == 'text' for b in self.blocks))
                if block_id != expected or len(self.blocks) >= 128:
                    raise ValueError('inline_ui_invalid_block')
                self.blocks.append({'id': block_id, 'type': 'text', 'source': ''})
                self.active = block_id
            if block_id != self.active:
                raise ValueError('inline_ui_unclosed_block')
            self.blocks[-1]['source'] += record['text']
        elif kind == 'text_end':
            if set(record) != {'type', 'block_id'} or record['block_id'] != self.active or self.active is None:
                raise ValueError('inline_ui_invalid_end')
            if not self.blocks[-1]['source'].strip():
                raise ValueError('inline_ui_empty_block')
            self.closed.add(self.active)
            self.active = None
        elif kind == 'action':
            from xclaw.ui_presentations_v2 import canonical_action
            if not self.allow_actions or set(record) != {'type','after_block_id','action_id','name','arguments'}:
                raise ValueError('inline_ui_action_not_allowed')
            texts = [b for b in self.blocks if b['type']=='text']
            if (self.active is not None or not texts or record['after_block_id'] != texts[-1]['id']
                    or record['after_block_id'] not in self.closed or len(self.actions)>=4
                    or record['action_id'] != 'a'+str(len(self.actions))):
                raise ValueError('inline_ui_invalid_action_anchor')
            args,summary=canonical_action(record['name'],record['arguments'])
            if any(p['name']==record['name'] and p['arguments']==args for p in self.actions):
                raise ValueError('inline_ui_duplicate_action')
            self.actions.append({k:v for k,v in {**record,'arguments':args,'summary':summary}.items() if k!='type'})
        elif kind == 'card':
            if set(record) != {'type', 'after_block_id', 'evidence_id', 'component'}:
                raise ValueError('inline_ui_invalid_card')
            anchor, ref = record['after_block_id'], record['evidence_id']
            texts = [b for b in self.blocks if b['type'] == 'text']
            if self.active is not None or not texts or anchor != texts[-1]['id'] or anchor not in self.closed:
                raise ValueError('inline_ui_invalid_anchor')
            if not isinstance(ref, str) or not re.fullmatch(r'e(?:0|[1-9][0-9]*)', ref):
                raise ValueError('inline_ui_invalid_evidence')
            index = int(ref[1:])
            if index >= len(self.evidence) or ref in self.used or len(self.used) >= 6:
                raise ValueError('inline_ui_invalid_evidence')
            row = self.evidence[index]
            component = COMPONENTS.get(row['name'], 'EvidenceCard')
            if record['component'] != component:
                raise ValueError('inline_ui_component_mismatch')
            encode = lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False)
            titles = {'QuoteCard': '行情', 'HistoryChart': '价格走势',
                      'FundamentalsCard': '财报数据', 'IndicatorsCard': '技术指标',
                      'NewsList': '相关新闻', 'Watchlist': '自选股', 'Portfolio': '本地持仓',
                      'BacktestCard': '回测结果', 'StrategyScanCard': '策略结果',
                      'ReportCard': '研究报告', 'TaskList': '已安排任务', 'EvidenceCard': '业务数据'}
            title = titles[component]
            symbol = row['arguments'].get('symbol')
            if isinstance(symbol, str) and re.fullmatch(r'[A-Za-z0-9.^-]{1,24}', symbol):
                title = symbol + ' ' + title
            source = f'q = Query({encode(row["name"])}, {encode(row["arguments"])})\nroot = Col([{component}({encode(title)}, q)])'
            self.blocks.append({'id': f'ui-{anchor}-{ref}', 'type': 'openui', 'source': source})
            self.used.add(ref)
        else:
            raise ValueError('inline_ui_unknown_record')
        self.revision += 1

    def finish(self):
        if self.buffer.strip():
            self.record(json.loads(self.buffer, object_pairs_hook=self.unique_object))
        self.buffer = ''
        if self.active is not None or not self.text.strip():
            raise ValueError('inline_ui_incomplete_answer')
        return {'version': 'xclaw-narrative-1', 'blocks': copy.deepcopy(self.blocks)}
