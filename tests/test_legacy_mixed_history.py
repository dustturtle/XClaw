"""Mixed-history upgrade regressions. Every write uses pytest's temporary DB."""
import hashlib
import json

import aiosqlite
import pytest
from fastapi import HTTPException

from xclaw.inline_ui_stream import InlineUIProtocol
from xclaw.legacy_ui_actions import legacy_request_id
from xclaw.openui import OpenUITools, ToolRequest
from xclaw.tools import ToolContext, ToolRegistry
from xclaw.tools.watchlist import WatchlistManageTool
from xclaw.tool_execution_journal import inspect_journal
from xclaw.ui_evidence import SCHEMA, save_presentation
from xclaw.ui_presentations_v2 import save_v2

ARGS = {'action': 'add', 'symbol': '600519.SH', 'market': 'CN'}
TOKEN_HASH = hashlib.sha256(b'test-token').hexdigest()
EXTERNAL = 'mixed-history'
SCOPE = EXTERNAL


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


async def legacy(db, chat, text='旧答复', args=None, evidence=True):
    args = ARGS if args is None else args
    mid = await db.save_message(chat, 'assistant', text)
    source = ('m = Mutation("watchlist_manage", ' + json.dumps(args) + ')\n'
              'q = Query("watchlist_manage", {action:"list"})\n'
              'root = Col([Button("确认添加", Action([@Run(m), @Run(q)]))])')
    await save_presentation(db, chat, sha(text), source)
    if evidence:
        async with aiosqlite.connect(str(db._path)) as conn:
            await conn.execute(SCHEMA)
            await conn.execute('INSERT OR REPLACE INTO ui_evidence(chat_id,reply_hash,payload) VALUES(?,?,?)',
                               (chat, sha(text), '{"question":"q","evidence":[]}'))
            await conn.commit()
    return ToolRequest(chat_id=EXTERNAL, name='watchlist_manage', arguments=args,
                       reply_hash=sha(text), message_id=mid, action_label='确认添加',
                       request_id=legacy_request_id(TOKEN_HASH,EXTERNAL,sha(text),'watchlist_manage',args))


async def upgrade(db, chat, text='新答复，尚未执行。', actions=True, pid='a'*32):
    parser = InlineUIProtocol([], 0, allow_actions=actions)
    parser.record({'type':'text_delta','block_id':'p0','text':text})
    parser.record({'type':'text_end','block_id':'p0'})
    if actions:
        parser.record({'type':'action','after_block_id':'p0','action_id':'a0',
                       'name':'watchlist_manage','arguments':ARGS})
    mid = await db.save_message(chat,'assistant',text)
    return await save_v2(db,chat,mid,pid,text,'q',[],parser.finish(),parser.actions)


async def setup(db):
    chat = await db.get_or_create_chat('web', EXTERNAL)
    registry = ToolRegistry()
    registry.register(WatchlistManageTool())
    async def context(scope):
        assert scope == SCOPE
        return ToolContext(chat_id=chat,channel='web',db=db)
    return chat, OpenUITools(registry,context)


async def execute(service, req, token_hash=TOKEN_HASH):
    return await service.execute(req,SCOPE,True,token_hash=token_hash)


async def test_frozen_legacy_button_survives_upgrade_and_is_exact_once(db):
    chat,service = await setup(db)
    req = await legacy(db,chat)
    await upgrade(db,chat)
    result = await execute(service,req)
    assert result == await execute(service,req)
    assert len(await db.get_watchlist(chat)) == 1
    assert len(await inspect_journal(db,chat)) == 1


@pytest.mark.parametrize('change', [
    {'reply_hash':'f'*64}, {'reply_hash':None}, {'request_id':'fresh'},
    {'arguments':{**ARGS,'symbol':'000001'}}, {'message_id':999999},
    {'action_plan_id':'a'*32+'_a0'},
])
async def test_legacy_grant_rejects_tampering_without_effect(db,change):
    chat,service = await setup(db)
    req = await legacy(db,chat)
    await upgrade(db,chat)
    with pytest.raises(HTTPException):
        await execute(service,req.model_copy(update=change))
    assert await db.get_watchlist(chat) == []
    assert await inspect_journal(db,chat) == []


async def test_v2_cannot_drop_ids_or_borrow_legacy_hash_for_other_arguments(db):
    chat,service = await setup(db)
    old = await legacy(db,chat,args={**ARGS,'symbol':'000001'})
    value = await upgrade(db,chat)
    for req in [
        ToolRequest(chat_id=EXTERNAL,name='watchlist_manage',arguments=ARGS,
                    reply_hash=value['reply_hash'],request_id='fresh'),
        old.model_copy(update={'arguments':ARGS,'request_id':'fresh'}),
        old.model_copy(update={'arguments':ARGS}),
    ]:
        with pytest.raises(HTTPException):
            await execute(service,req)
    assert await inspect_journal(db,chat) == []


@pytest.mark.parametrize('kind',['duplicate_old','v2_collision','no_evidence','late'])
async def test_ambiguous_or_unproven_history_never_granted(db,kind):
    chat,service = await setup(db)
    if kind == 'late':
        await upgrade(db,chat)
        req = await legacy(db,chat)
        await upgrade(db,chat,pid='b'*32)
    else:
        req = await legacy(db,chat,evidence=kind!='no_evidence')
        if kind == 'duplicate_old':
            await db.save_message(chat,'assistant','旧答复')
        await upgrade(db,chat,text='旧答复' if kind=='v2_collision' else '新答复')
    with pytest.raises(HTTPException):
        await execute(service,req)
    assert await inspect_journal(db,chat) == []


@pytest.mark.parametrize('kind',['edited','deleted'])
async def test_grant_is_invalid_after_origin_message_changes(db,kind):
    chat,service = await setup(db)
    req = await legacy(db,chat)
    await upgrade(db,chat)
    async with aiosqlite.connect(str(db._path)) as conn:
        if kind == 'edited':
            await conn.execute('UPDATE messages SET content=? WHERE id=?',('changed',req.message_id))
        else:
            await conn.execute('DELETE FROM messages WHERE id=?',(req.message_id,))
        await conn.commit()
    with pytest.raises(HTTPException):
        await execute(service,req)
    assert await inspect_journal(db,chat) == []


async def test_preupgrade_completed_action_does_not_repeat_after_upgrade(db):
    chat,service = await setup(db)
    req = await legacy(db,chat)
    before = await execute(service,req)
    await upgrade(db,chat)
    assert await execute(service,req) == before
    other_token = sha('rotated-token')
    changed = req.model_copy(update={'request_id':legacy_request_id(other_token,EXTERNAL,req.reply_hash,req.name,ARGS)})
    with pytest.raises(HTTPException):
        await execute(service,changed,other_token)
    assert len(await inspect_journal(db,chat)) == 1


async def test_readonly_v2_upgrade_and_service_restart_keep_legacy_authorization(db):
    chat,service = await setup(db)
    req = await legacy(db,chat)
    await upgrade(db,chat,actions=False)
    # Turning inline generation off does not remove persisted identity or grants.
    _, restarted = await setup(db)
    assert (await execute(restarted,req))['ok']
    assert len(await inspect_journal(db,chat)) == 1


async def test_freeze_cannot_be_extended_by_replacing_old_source(db):
    chat,service = await setup(db)
    req = await legacy(db,chat)
    await upgrade(db,chat)
    args = {**ARGS,'symbol':'000001'}
    await save_presentation(db,chat,req.reply_hash,
                           'm=Mutation("watchlist_manage",'+json.dumps(args)+')\n'
                           'root=Col([Button("确认",Action([@Run(m)]))])')
    forged = req.model_copy(update={
        'arguments':args,
        'request_id':legacy_request_id(TOKEN_HASH,EXTERNAL,req.reply_hash,req.name,args),
    })
    with pytest.raises(HTTPException):
        await execute(service,forged)
    assert await inspect_journal(db,chat) == []
    with pytest.raises(HTTPException):
        await execute(service,req)
    assert await inspect_journal(db,chat) == []


async def test_legacy_cannot_run_without_verified_server_token_scope(db):
    chat,service = await setup(db)
    req = await legacy(db,chat)
    await upgrade(db,chat)
    with pytest.raises(HTTPException):
        await service.execute(req,SCOPE,True)
    assert await inspect_journal(db,chat) == []


async def test_unbound_v2_request_cannot_borrow_old_success_receipt(db):
    chat,service = await setup(db)
    req = await legacy(db,chat)
    await execute(service,req)
    value = await upgrade(db,chat)
    with pytest.raises(HTTPException):
        await execute(service,req.model_copy(update={'reply_hash':value['reply_hash']}))
    assert len(await inspect_journal(db,chat)) == 1


async def test_new_duplicate_legacy_text_after_freeze_invalidates_unbound_request(db):
    chat,service = await setup(db)
    req = await legacy(db,chat)
    await upgrade(db,chat)
    await db.save_message(chat,'assistant','旧答复')
    with pytest.raises(HTTPException):
        await execute(service,req.model_copy(update={'message_id':None}))
    assert await inspect_journal(db,chat) == []


async def test_deleted_legacy_source_invalidates_original_request(db):
    chat,service = await setup(db)
    req = await legacy(db,chat)
    await upgrade(db,chat)
    async with aiosqlite.connect(str(db._path)) as conn:
        await conn.execute('DELETE FROM ui_presentations WHERE chat_id=? AND reply_hash=?',
                           (chat,req.reply_hash))
        await conn.commit()
    with pytest.raises(HTTPException):
        await execute(service,req)
    assert await inspect_journal(db,chat) == []


async def test_fresh_chat_requires_stored_button_before_first_v2(db):
    chat, service = await setup(db)
    request = ToolRequest(chat_id=EXTERNAL, name="watchlist_manage", arguments=ARGS, request_id="fixture")
    with pytest.raises(HTTPException) as error:
        await service.execute(request, SCOPE, True, token_hash=TOKEN_HASH)
    assert error.value.status_code == 403
    assert not await db.get_watchlist(chat)
    saved = await legacy(db, chat)
    await service.execute(saved, SCOPE, True, token_hash=TOKEN_HASH)
    assert len(await db.get_watchlist(chat)) == 1
