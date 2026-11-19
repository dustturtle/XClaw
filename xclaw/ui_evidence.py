"""Reply-bound, chat-scoped business evidence. UI never silently refetches a snapshot."""
from __future__ import annotations
import hashlib
import json
import aiosqlite

SCHEMA = '''CREATE TABLE IF NOT EXISTS ui_evidence (
 chat_id INTEGER NOT NULL, reply_hash TEXT NOT NULL, payload TEXT NOT NULL,
 created_at TEXT DEFAULT (datetime('now')), PRIMARY KEY(chat_id,reply_hash))'''

def eligible(name, arguments):
    from xclaw.openui import READ_TOOLS
    return (name in READ_TOOLS or
            name == 'watchlist_manage' and arguments.get('action') == 'list' or
            name == 'portfolio_manage' and arguments.get('action') == 'view' or
            name == 'investment_report' and arguments.get('action') in {'latest', 'history'})

async def _post_write_snapshots(db, chat_id, evidence):
    """Read authoritative state after confirmed writes; never replay a write.

    A later failed/unknown write suppresses projection for that tool. Pre-write
    reads are discarded even then, so they cannot masquerade as final state.
    """
    from xclaw.tools import ToolContext
    from xclaw.tools.watchlist import WatchlistManageTool
    from xclaw.tools.portfolio import PortfolioManageTool

    specs = {
        'watchlist_manage': ({'add', 'remove'}, 'list', WatchlistManageTool),
        'portfolio_manage': ({'buy', 'sell'}, 'view', PortfolioManageTool),
    }
    touched = set()
    snapshots = []
    for name, (writes, read, tool_type) in specs.items():
        attempts = [item for item in evidence if item['name'] == name
                    and item['arguments'].get('action') in writes]
        if not attempts:
            continue
        touched.add(name)
        # Missing status is not confirmation. All writes must have a known
        # success, not merely the last one: an earlier unknown stays unknown.
        if not all(item.get('is_error') is False for item in attempts):
            continue
        try:
            result = await tool_type().execute(
                {'action': read}, ToolContext(chat_id=chat_id, channel='web', db=db))
        except Exception:
            # A failed projection must not fall back to stale pre-write data.
            continue
        snapshots.append({'name': name, 'arguments': {'action': read},
                          'content': result.content, 'is_error': result.is_error})
    return touched, snapshots


async def save_evidence(db, chat_id, reply, user_message, evidence, *, model=None):
    touched, snapshots = await _post_write_snapshots(db, chat_id, evidence)
    rows = []
    size = 0
    for item in evidence:
        if item['name'] in touched:
            continue
        if not eligible(item['name'], item['arguments']):
            continue
        value = {k: item[k] for k in ('name', 'arguments', 'content', 'is_error')}
        size += len(json.dumps(value, ensure_ascii=False))
        if size > 160000:
            break
        rows.append(value)
    if any(item['name'] == 'strategy_scan' and not item['is_error'] for item in evidence):
        from xclaw.openui import public_strategy_runs
        values = public_strategy_runs(await db.list_strategy_runs(chat_id, 5))
        rows.append({'name': 'strategy_runs', 'arguments': {'limit': 5},
                     'content': json.dumps(values, ensure_ascii=False), 'is_error': False})
    rows.extend(snapshots)
    payload = json.dumps({'question': user_message[:12000], 'evidence': rows,
                          **({'model': model} if model is not None else {})}, ensure_ascii=False)
    async with aiosqlite.connect(str(db._path)) as conn:
        await conn.execute(SCHEMA)
        await conn.execute(PRESENTATIONS)
        await conn.execute('BEGIN IMMEDIATE')
        await conn.execute('DELETE FROM ui_presentations WHERE chat_id=? AND reply_hash=?',
                           (chat_id, hashlib.sha256(reply.encode()).hexdigest()))
        await conn.execute('INSERT OR REPLACE INTO ui_evidence(chat_id,reply_hash,payload) VALUES(?,?,?)',
                           (chat_id, hashlib.sha256(reply.encode()).hexdigest(), payload))
        await conn.commit()

async def load_evidence(db, chat_id, reply_hash):
    async with aiosqlite.connect(str(db._path)) as conn:
        await conn.execute(SCHEMA)
        async with conn.execute('SELECT payload,created_at FROM ui_evidence WHERE chat_id=? AND reply_hash=?',
                                (chat_id, reply_hash)) as cursor:
            row = await cursor.fetchone()
    return {**json.loads(row[0]), 'created_at': row[1], '_version':hashlib.sha256(row[0].encode()).hexdigest()} if row else None

PRESENTATIONS = '''CREATE TABLE IF NOT EXISTS ui_presentations (
 chat_id INTEGER NOT NULL, reply_hash TEXT NOT NULL, source TEXT NOT NULL,
 PRIMARY KEY(chat_id,reply_hash))'''

_UNCONDITIONAL = object()

async def save_presentation(db, chat_id, reply_hash, source, *, expected_version=_UNCONDITIONAL):
    async with aiosqlite.connect(str(db._path)) as conn:
        await conn.execute(PRESENTATIONS)
        if expected_version is not _UNCONDITIONAL:
            await conn.execute(SCHEMA)
            await conn.execute('BEGIN IMMEDIATE')
            async with conn.execute('SELECT payload FROM ui_evidence WHERE chat_id=? AND reply_hash=?', (chat_id, reply_hash)) as cursor:
                row = await cursor.fetchone()
            current = hashlib.sha256(row[0].encode()).hexdigest() if row else None
            if current != expected_version:
                raise ValueError('stale_presentation_evidence')
        await conn.execute('INSERT OR REPLACE INTO ui_presentations VALUES(?,?,?)', (chat_id,reply_hash,source))
        await conn.commit()

async def load_presentation(db, chat_id, reply_hash):
    async with aiosqlite.connect(str(db._path)) as conn:
        await conn.execute(PRESENTATIONS)
        async with conn.execute('SELECT source FROM ui_presentations WHERE chat_id=? AND reply_hash=?',(chat_id,reply_hash)) as cursor:
            row=await cursor.fetchone()
    return row[0] if row else None

ACTION_STATE = '''CREATE TABLE IF NOT EXISTS ui_action_state (
 chat_id INTEGER NOT NULL, reply_hash TEXT NOT NULL, request_id TEXT NOT NULL,
 tool_name TEXT NOT NULL, label TEXT NOT NULL,
 PRIMARY KEY(chat_id,reply_hash,request_id))'''

async def action_state(db, chat_id, reply_hash):
    async with aiosqlite.connect(str(db._path)) as conn:
        await conn.execute(ACTION_STATE)
        async with conn.execute('SELECT tool_name,label FROM ui_action_state WHERE chat_id=? AND reply_hash=?',(chat_id,reply_hash)) as cursor:
            rows=await cursor.fetchall()
    return {'dirty': sorted({row[0] for row in rows}), 'completed': sorted({row[1] for row in rows if row[1]})}


async def save_inline_presentation(db, chat_id, reply, question, evidence, presentation):
    """Atomically bind the exact validated stream snapshot, never re-fetch it."""
    digest = hashlib.sha256(reply.encode()).hexdigest()
    payload = json.dumps({'question': question[:12000], 'evidence': evidence}, ensure_ascii=False)
    source = json.dumps(presentation, ensure_ascii=False)
    async with aiosqlite.connect(str(db._path)) as conn:
        await conn.execute(SCHEMA)
        await conn.execute(PRESENTATIONS)
        await conn.execute('BEGIN IMMEDIATE')
        await conn.execute('INSERT OR REPLACE INTO ui_evidence(chat_id,reply_hash,payload) VALUES(?,?,?)',
                           (chat_id, digest, payload))
        await conn.execute('INSERT OR REPLACE INTO ui_presentations VALUES(?,?,?)',
                           (chat_id, digest, source))
        await conn.commit()
    return digest

async def ambiguous_model_reply(db, chat_id, digest):
    """Hash-only legacy clients cannot identify the original of repeated model text."""
    async with aiosqlite.connect(str(db._path)) as conn:
        async with conn.execute("PRAGMA table_info(messages)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "response_hash" not in columns:
            return False
        async with conn.execute(
            "SELECT 1 FROM messages m WHERE m.chat_id=? AND m.role='assistant' AND m.content=("
            "SELECT content FROM messages WHERE chat_id=? AND response_hash=? LIMIT 1) LIMIT 1 OFFSET 1",
            (chat_id, chat_id, digest),
        ) as cursor:
            return await cursor.fetchone() is not None
