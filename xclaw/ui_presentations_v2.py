"""Immutable run-bound presentations and server-owned local action plans."""
import hashlib
import json
import math
import re
import aiosqlite

SCHEMA = '''CREATE TABLE IF NOT EXISTS ui_presentations_v2 (
 presentation_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
 reply_hash TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(chat_id,message_id))'''


def canonical_action(name, arguments):
    if not isinstance(arguments, dict):raise ValueError('invalid_action_arguments')
    args=dict(arguments)
    action=args.get('action')
    allowed={'watchlist_manage': {'add','remove'}, 'portfolio_manage': {'buy','sell'}}
    if name not in allowed or action not in allowed[name]:raise ValueError('action_not_allowed')
    required={'action','symbol','market'}
    if name=='portfolio_manage':required|={'shares'} | ({'price'} if action=='buy' else set())
    optional={'name','notes'} if name=='watchlist_manage' and action=='add' else set()
    if set(args)-required-optional or required-set(args):raise ValueError('invalid_action_fields')
    if args['market'] not in {'CN','US','HK'}:raise ValueError('invalid_action_market')
    if not isinstance(args['symbol'],str) or not re.fullmatch(r'[A-Za-z0-9.^-]{1,24}',args['symbol']):raise ValueError('invalid_action_symbol')
    patterns = {'CN':r'(?:[0-9]{6})(?:\.(?:SH|SZ|BJ))?',
                'HK':r'[0-9]{4,5}(?:\.HK)?', 'US':r'[A-Za-z][A-Za-z0-9.-]{0,15}'}
    if not re.fullmatch(patterns[args['market']],args['symbol']):raise ValueError('symbol_market_mismatch')
    for key in optional & set(args):
        if not isinstance(args[key],str) or len(args[key])>120:raise ValueError('invalid_action_note')
    for key in {'shares','price'} & set(args):
        value=args[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0 or value>1e12:raise ValueError('invalid_action_number')
    symbol=args['symbol'];market=args['market']
    if name=='watchlist_manage':
        summary=f"{'添加到' if action=='add' else '从'}自选股{'中移除' if action=='remove' else ''}：{symbol}（{market}）"
        if args.get('name'):summary+=f"；名称：{args['name']}"
        if args.get('notes'):summary+=f"；备注：{args['notes']}"
    elif action=='buy':summary=f"设置/覆盖本地模拟持仓：{symbol}（{market}）总计 {args['shares']:g} 股，平均成本 {args['price']:g}；不是追加买入，非真实交易"
    else:summary=f"减少本地模拟持仓：{symbol}（{market}）{args['shares']:g} 股；剩余均价不变，非真实交易"
    return args,summary


def finalize_actions(presentation, plans, presentation_id):
    result={**presentation,'presentation_id':presentation_id,'blocks':list(presentation['blocks'])}
    final_plans=[]
    for plan in plans:
        plan_id=presentation_id+'_'+plan['action_id']
        request_id='plan_'+plan_id
        name=plan['name'];args=plan['arguments'];summary=plan['summary']
        enc=lambda v:json.dumps(v,ensure_ascii=False,allow_nan=False)
        source=(f'm = Mutation({enc(name)}, {enc(args)})\n'
                f'root = Col([Text({enc(summary)}), Button("确认上述操作", Action([@Run(m)])), MutationStatus(m.status,m.error)])')
        block={'id':'action-'+plan_id,'type':'action','source':source,
               'action_plan_id':plan_id,'summary':summary}
        # Insert at explicit closed-text anchor, after any existing reads/actions.
        index=next(i for i,b in enumerate(result['blocks']) if b['id']==plan['after_block_id'])+1
        while index<len(result['blocks']) and result['blocks'][index]['type']!='text':index+=1
        result['blocks'].insert(index,block)
        final_plans.append({**plan,'action_plan_id':plan_id,'request_id':request_id})
    return result,final_plans


async def save_v2(db,chat_id,message_id,presentation_id,reply,question,evidence,presentation,plans):
    if not re.fullmatch(r'[a-f0-9]{32}',presentation_id):raise ValueError('invalid_presentation_id')
    digest=hashlib.sha256(reply.encode()).hexdigest()
    source,actions=finalize_actions(presentation,plans,presentation_id)
    payload={'presentation_id':presentation_id,'message_id':message_id,'reply_hash':digest,
             'question':question[:12000],'evidence':evidence,'source':json.dumps(source,ensure_ascii=False),
             'plans':actions,'mode':'inline_v2'}
    async with aiosqlite.connect(str(db._path)) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(SCHEMA)
        async with conn.execute("SELECT content FROM messages WHERE id=? AND chat_id=? AND role='assistant'",(message_id,chat_id)) as cursor:
            message=await cursor.fetchone()
        if message is None or hashlib.sha256(message[0].encode()).hexdigest()!=digest:
            raise ValueError('presentation_message_mismatch')
        from xclaw.legacy_ui_grants import freeze_before_first_v2
        await freeze_before_first_v2(conn,chat_id,message_id)
        await conn.execute('INSERT INTO ui_presentations_v2 VALUES(?,?,?,?,?)',
                           (presentation_id,chat_id,message_id,digest,json.dumps(payload,ensure_ascii=False)))
        await conn.commit()
    return payload


async def load_v2(db,chat_id,presentation_id):
    async with aiosqlite.connect(str(db._path)) as conn:
        await conn.execute(SCHEMA)
        async with conn.execute('SELECT payload FROM ui_presentations_v2 WHERE chat_id=? AND presentation_id=?',(chat_id,presentation_id)) as cursor:
            row=await cursor.fetchone()
        if row:
            value=json.loads(row[0])
            async with conn.execute("SELECT content FROM messages WHERE id=? AND chat_id=? AND role='assistant'",(value['message_id'],chat_id)) as cursor:
                message=await cursor.fetchone()
            if message is None or hashlib.sha256(message[0].encode()).hexdigest()!=value['reply_hash']:
                return None
            return value
    return None


async def has_v2(db,chat_id,reply_hash=None):
    async with aiosqlite.connect(str(db._path)) as conn:
        await conn.execute(SCHEMA)
        sql='SELECT 1 FROM ui_presentations_v2 WHERE chat_id=?';args=[chat_id]
        if reply_hash is not None:sql+=' AND reply_hash=?';args.append(reply_hash)
        async with conn.execute(sql+' LIMIT 1',args) as cursor:return await cursor.fetchone() is not None


async def presentation_metadata_for_messages(db,chat_id,ids):
    ids=list(ids)[:1000]
    if not ids:return {}
    async with aiosqlite.connect(str(db._path)) as conn:
        await conn.execute(SCHEMA)
        async with conn.execute('SELECT message_id,presentation_id FROM ui_presentations_v2 WHERE chat_id=? AND message_id IN ('+','.join('?'*len(ids))+')',[chat_id,*ids]) as cursor:
            rows=await cursor.fetchall()
    return {row[0]:{'message_id':row[0],'presentation_id':row[1]} for row in rows}


async def v2_action_state(db,chat_id,payload,scope=None):
    states=[];dirty=set();completed=[];blocked=False
    async with aiosqlite.connect(str(db._path)) as conn:
        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:tables={r[0] for r in await cursor.fetchall()}
        if 'tool_execution_journal' in tables:
            async with conn.execute("SELECT 1 FROM tool_execution_journal WHERE chat_id=? AND state IN ('started','unknown') LIMIT 1",(str(chat_id),)) as cursor:
                blocked=await cursor.fetchone() is not None
        for plan in payload['plans']:
            status='ready'
            if 'tool_execution_journal' in tables:
                async with conn.execute('SELECT state FROM tool_execution_journal WHERE chat_id=? AND call_id=?',(str(chat_id),'openui:'+plan['request_id'])) as cursor:row=await cursor.fetchone()
                if row:status='completed' if row[0]=='completed' else 'unknown'
            if 'openui_mutations' in tables and scope is not None:
                async with conn.execute('SELECT result FROM openui_mutations WHERE scope=? AND request_id=?',(scope,plan['request_id'])) as cursor:row=await cursor.fetchone()
                if row and not row[0] and status!='completed':status='unknown'
            if status=='completed':dirty.add(plan['name']);completed.append(plan['summary'])
            states.append({'action_plan_id':plan['action_plan_id'],'status':status,'summary':plan['summary']})
    return {'dirty':sorted(dirty),'completed':completed,'actions':states,'blocked':blocked,'unknown_barrier':blocked}
