"""Freeze only provable pre-upgrade Web buttons; never learn grants from a request."""
import hashlib
import json

SCHEMA = """CREATE TABLE IF NOT EXISTS ui_legacy_grants (
 chat_id INTEGER NOT NULL, reply_hash TEXT NOT NULL, message_id INTEGER NOT NULL,
 name TEXT NOT NULL, arguments TEXT NOT NULL, source_hash TEXT NOT NULL,
 PRIMARY KEY(chat_id,reply_hash,name,arguments))"""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


async def freeze_before_first_v2(conn, chat_id, message_id=None):
    from xclaw.ui_evidence import SCHEMA as EVIDENCE, PRESENTATIONS
    from xclaw.legacy_ui_actions import legacy_actions

    await conn.execute(SCHEMA)
    if await (await conn.execute(
        "SELECT 1 FROM ui_presentations_v2 WHERE chat_id=? LIMIT 1", (chat_id,)
    )).fetchone():
        return
    await conn.execute(EVIDENCE)
    await conn.execute(PRESENTATIONS)
    rows = await (await conn.execute(
        """SELECT p.reply_hash,p.source FROM ui_presentations p
           JOIN ui_evidence e USING(chat_id,reply_hash)
           WHERE p.chat_id=? AND length(p.source)<=65536 AND instr(p.source,'Mutation')>0 LIMIT 65""", (chat_id,)
    )).fetchall()
    if not rows or len(rows)>64:
        return
    # Limit total migration CPU within the write lock on unexpectedly large histories.
    size=await (await conn.execute("""SELECT count(*),coalesce(sum(length(content)),0) FROM messages
        WHERE chat_id=? AND role='assistant'""",(chat_id,))).fetchone()
    if size[0]>4096 or size[1]>8_000_000 or sum(len(row[1]) for row in rows)>262144:
        return
    targets = {row[0]: [] for row in rows}
    async with conn.execute("SELECT id,content FROM messages WHERE chat_id=? AND role='assistant'", (chat_id,)) as cursor:
        async for mid, content in cursor:
            key = digest(content)
            if key in targets:
                targets[key].append(mid)
    for key, source in rows:
        ids = targets[key]
        if len(ids) != 1 or (message_id is not None and ids[0] >= message_id):
            continue
        for name, arguments in legacy_actions(source):
            await conn.execute("INSERT OR IGNORE INTO ui_legacy_grants VALUES(?,?,?,?,?,?)",
                               (chat_id,key,ids[0],name,json.dumps(arguments,ensure_ascii=False),digest(source)))


async def authorize_legacy(conn, chat_id, req, token_hash, scope, payload_digest):
    """Called inside the mutation admission write transaction; no policy/claim race."""
    from xclaw.legacy_ui_actions import legacy_request_id
    from xclaw.ui_presentations_v2 import SCHEMA as V2

    if not req.reply_hash or req.action_plan_id or not token_hash:
        return False
    await conn.execute(V2)
    # Before the first V2 record, verify the saved legacy button by the same
    # rules used during migration. A new chat has no saved button to authorize.
    await freeze_before_first_v2(conn, chat_id)
    if await (await conn.execute("SELECT 1 FROM ui_presentations_v2 WHERE chat_id=? AND reply_hash=?",
                                 (chat_id,req.reply_hash))).fetchone():
        return False
    from xclaw.ui_evidence import PRESENTATIONS
    await conn.execute(PRESENTATIONS)
    await conn.execute(SCHEMA)
    rows = await (await conn.execute(
        """SELECT m.content,g.arguments,g.message_id,p.source,g.source_hash FROM ui_legacy_grants g
           JOIN ui_presentations p USING(chat_id,reply_hash)
           JOIN messages m ON m.id=g.message_id AND m.chat_id=g.chat_id AND m.role='assistant'
           WHERE g.chat_id=? AND g.reply_hash=? AND g.name=?
           AND NOT EXISTS (SELECT 1 FROM messages other WHERE other.chat_id=m.chat_id
               AND other.role='assistant' AND other.content=m.content AND other.id<>m.id)""",
        (chat_id,req.reply_hash,req.name))).fetchall()
    for content, stored, mid, source, source_hash in rows:
        if digest(source)!=source_hash:
            continue
        if digest(content)!=req.reply_hash or req.message_id not in (None,mid):
            continue
        args=json.loads(stored)
        if json.dumps(args,sort_keys=True)!=json.dumps(req.arguments,sort_keys=True):
            continue
        if req.request_id!=legacy_request_id(token_hash,req.chat_id,req.reply_hash,req.name,args):
            continue
        # Even an older client, token rotation or differently ordered arguments
        # must not turn an already attempted legacy effect into a fresh claim.
        prior=await (await conn.execute(
            "SELECT 1 FROM openui_mutations WHERE scope=? AND digest=? AND request_id<>? LIMIT 1",
            (scope,payload_digest,req.request_id))).fetchone()
        return prior is None
    return False
