"""Post-write UI snapshots read state, never execute or infer mutations."""
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xclaw.ui_evidence import load_evidence, save_evidence


def item(name='watchlist_manage', action='add', error=False, content='confirmed'):
    return {'name': name, 'arguments': {'action': action},
            'content': content, 'is_error': error}


def database(tmp_path):
    return SimpleNamespace(
        _path=tmp_path / 'isolated.sqlite',
        get_watchlist=AsyncMock(return_value=[{'symbol': '600519', 'market': 'CN',
                                             'name': '茅台', 'notes': ''}]),
        get_portfolio=AsyncMock(return_value=[{'symbol': '600519', 'market': 'CN',
                                             'shares': 100, 'avg_cost': 1200}]))


async def snapshot(db, evidence):
    await save_evidence(db, 72, 'reply', 'question', evidence)
    return (await load_evidence(db, 72, hashlib.sha256(b'reply').hexdigest()))['evidence']


@pytest.mark.parametrize('action', ['add', 'remove'])
async def test_watchlist_success_replaces_prewrite_list(tmp_path, action):
    db = database(tmp_path)
    rows = await snapshot(db, [item(action='list', content='STALE'), item(action=action)])
    assert len(rows) == 1
    assert rows[0]['arguments'] == {'action': 'list'}
    assert '600519' in rows[0]['content'] and 'STALE' not in rows[0]['content']
    assert not rows[0]['is_error']
    db.get_watchlist.assert_awaited_once_with(72)
    db.get_portfolio.assert_not_awaited()
    assert await load_evidence(db, 73, hashlib.sha256(b'reply').hexdigest()) is None


async def test_empty_authoritative_state_not_reconstructed_from_add(tmp_path):
    db = database(tmp_path)
    db.get_watchlist.return_value = []
    rows = await snapshot(db, [item(action='add'), item(action='remove')])
    assert rows[0]['content'] == '自选股列表为空'
    db.get_watchlist.assert_awaited_once()


@pytest.mark.parametrize('status', [True, None])
async def test_failed_unknown_suppresses_stale_and_success_projection(tmp_path, status):
    db = database(tmp_path)
    rows = await snapshot(db, [item(action='list', content='STALE'),
                               item(error=status), item(action='remove')])
    assert rows == []
    db.get_watchlist.assert_not_awaited()


@pytest.mark.parametrize('action', ['buy', 'sell'])
async def test_portfolio_success_uses_exact_view_contract(tmp_path, action):
    db = database(tmp_path)
    rows = await snapshot(db, [item('portfolio_manage', 'view', content='STALE'),
                               item('portfolio_manage', action)])
    assert rows[0]['arguments'] == {'action': 'view'}
    assert '600519' in rows[0]['content'] and '1200' in rows[0]['content']
    db.get_portfolio.assert_awaited_once_with(72)
    db.get_watchlist.assert_not_awaited()


async def test_projection_failure_never_uses_prewrite_state(tmp_path):
    db = database(tmp_path)
    db.get_watchlist.side_effect = RuntimeError('read failed')
    assert await snapshot(db, [item(action='list'), item()]) == []


async def test_reads_preserved_without_writes_and_unknown_actions_not_executed(tmp_path):
    db = database(tmp_path)
    rows = await snapshot(db, [item(action='list', content='existing'), item(action='ADD')])
    assert len(rows) == 1 and rows[0]['content'] == 'existing'
    db.get_watchlist.assert_not_awaited()
