"""Tests for xclaw.db module."""

from __future__ import annotations

import pytest
import pytest_asyncio

from xclaw.db import Database


@pytest.mark.asyncio
async def test_get_or_create_chat(db: Database):
    """get_or_create_chat should return consistent IDs."""
    chat_id = await db.get_or_create_chat("web", "user123")
    assert isinstance(chat_id, int)
    # Second call should return same id
    chat_id2 = await db.get_or_create_chat("web", "user123")
    assert chat_id == chat_id2


@pytest.mark.asyncio
async def test_different_channels_give_different_ids(db: Database):
    """The same external id on different channels should give different rows."""
    id_web = await db.get_or_create_chat("web", "user1")
    id_feishu = await db.get_or_create_chat("feishu", "user1")
    assert id_web != id_feishu


@pytest.mark.asyncio
async def test_save_and_load_messages(db: Database):
    chat_id = await db.get_or_create_chat("web", "user_msg")
    await db.save_message(chat_id, "user", "Hello")
    await db.save_message(chat_id, "assistant", "Hi there!")
    msgs = await db.get_recent_messages(chat_id)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_session_persistence(db: Database):
    chat_id = await db.get_or_create_chat("web", "session_user")
    session_data = [{"role": "user", "content": "Test"}]
    await db.save_session(chat_id, session_data)
    loaded = await db.load_session(chat_id)
    assert loaded == session_data


@pytest.mark.asyncio
async def test_load_missing_session_returns_none(db: Database):
    chat_id = await db.get_or_create_chat("web", "no_session")
    result = await db.load_session(chat_id)
    assert result is None


@pytest.mark.asyncio
async def test_clear_session(db: Database):
    chat_id = await db.get_or_create_chat("web", "clear_session_user")
    session_data = [{"role": "user", "content": "Test"}]
    await db.save_session(chat_id, session_data)
    assert await db.load_session(chat_id) == session_data

    await db.clear_session(chat_id)
    assert await db.load_session(chat_id) is None


@pytest.mark.asyncio
async def test_memories_crud(db: Database):
    chat_id = await db.get_or_create_chat("web", "mem_user")
    mid = await db.add_memory(chat_id, "用户喜欢价值投资", category="偏好")
    assert isinstance(mid, int)
    mems = await db.get_memories(chat_id)
    assert len(mems) == 1
    assert mems[0]["content"] == "用户喜欢价值投资"

    await db.archive_memory(mid)
    mems_after = await db.get_memories(chat_id, include_archived=False)
    assert len(mems_after) == 0
    mems_all = await db.get_memories(chat_id, include_archived=True)
    assert len(mems_all) == 1


@pytest.mark.asyncio
async def test_watchlist_crud(db: Database):
    chat_id = await db.get_or_create_chat("web", "watch_user")
    await db.add_to_watchlist(chat_id, "600519", "CN", name="贵州茅台")
    await db.add_to_watchlist(chat_id, "AAPL", "US", name="Apple")
    wl = await db.get_watchlist(chat_id)
    assert len(wl) == 2

    removed = await db.remove_from_watchlist(chat_id, "AAPL", "US")
    assert removed is True
    wl2 = await db.get_watchlist(chat_id)
    assert len(wl2) == 1

    # Try removing something that doesn't exist
    removed2 = await db.remove_from_watchlist(chat_id, "AAPL", "US")
    assert removed2 is False


@pytest.mark.asyncio
async def test_portfolio_crud(db: Database):
    chat_id = await db.get_or_create_chat("web", "port_user")
    await db.upsert_portfolio(chat_id, "600519", "CN", 100.0, 1800.0)
    portfolio = await db.get_portfolio(chat_id)
    assert len(portfolio) == 1
    assert portfolio[0]["symbol"] == "600519"
    assert portfolio[0]["shares"] == 100.0

    # Upsert should update
    await db.upsert_portfolio(chat_id, "600519", "CN", 200.0, 1900.0)
    portfolio2 = await db.get_portfolio(chat_id)
    assert len(portfolio2) == 1
    assert portfolio2[0]["shares"] == 200.0

    removed = await db.remove_from_portfolio(chat_id, "600519", "CN")
    assert removed is True
    portfolio3 = await db.get_portfolio(chat_id)
    assert len(portfolio3) == 0


@pytest.mark.asyncio
async def test_scheduled_tasks(db: Database):
    chat_id = await db.get_or_create_chat("web", "task_user")
    tid = await db.add_scheduled_task(
        chat_id, "每日盘后推送", "获取今日大盘行情", cron_expression="0 15 * * 1-5"
    )
    assert isinstance(tid, int)
    tasks = await db.get_active_tasks()
    assert any(t["id"] == tid for t in tasks)

    await db.update_task_status(tid, "cancelled")
    tasks2 = await db.get_active_tasks()
    assert not any(t["id"] == tid for t in tasks2)


@pytest.mark.asyncio
async def test_llm_usage(db: Database):
    chat_id = await db.get_or_create_chat("web", "usage_user")
    await db.record_usage(chat_id, "claude-opus-4-5", 100, 200)
    # No assertion except it doesn't raise


@pytest.mark.asyncio
async def test_investment_report_crud(db: Database):
    chat_id = await db.get_or_create_chat("web", "report_user")
    report_id = await db.add_investment_report(
        chat_id=chat_id,
        report_type="daily_watchlist",
        title="2026-04-14 自选股日报",
        summary="2 只股票，其中 1 只偏多，1 只观望",
        content_markdown="# 自选股日报\n\n内容",
        symbol_count=2,
        trigger_source="manual",
    )

    assert isinstance(report_id, int)

    latest = await db.get_latest_investment_report(chat_id)
    assert latest is not None
    assert latest["id"] == report_id
    assert latest["report_type"] == "daily_watchlist"
    assert latest["symbol_count"] == 2

    history = await db.list_investment_reports(chat_id, limit=10)
    assert len(history) == 1
    assert history[0]["title"] == "2026-04-14 自选股日报"


@pytest.mark.asyncio
async def test_strategy_run_crud(db: Database):
    chat_id = await db.get_or_create_chat("web", "strategy_user")
    run_id = await db.add_strategy_run(
        chat_id=chat_id,
        symbol="600519",
        market="CN",
        strategies=[
            {
                "strategy_id": "bull_trend",
                "signal_status": "triggered",
                "bias_score": 78,
            },
            {
                "strategy_id": "ma_golden_cross",
                "signal_status": "no_signal",
                "bias_score": 42,
            },
        ],
        valuable_strategies=[
            {
                "strategy_id": "bull_trend",
                "signal_status": "triggered",
                "bias_score": 78,
            }
        ],
    )

    assert isinstance(run_id, int)

    rows = await db.list_strategy_runs(chat_id, limit=5)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "600519"
    assert rows[0]["market"] == "CN"
    assert rows[0]["strategies"][0]["strategy_id"] == "bull_trend"
    assert rows[0]["valuable_strategies"][0]["signal_status"] == "triggered"


@pytest.mark.asyncio
async def test_report_export_crud(db: Database):
    chat_id = await db.get_or_create_chat("web", "export_user")
    report_id = await db.add_investment_report(
        chat_id=chat_id,
        report_type="daily_watchlist",
        title="2026-04-14 自选股日报",
        summary="导出测试",
        content_markdown="# 导出测试",
        symbol_count=1,
        trigger_source="manual",
    )

    export_id = await db.add_report_export(
        report_id=report_id,
        asset_type="pdf",
        mime_type="application/pdf",
        file_path="/tmp/report.pdf",
        status="ready",
    )
    assert isinstance(export_id, int)

    rows = await db.list_report_exports(report_id)
    assert len(rows) == 1
    assert rows[0]["asset_type"] == "pdf"
    assert rows[0]["file_path"] == "/tmp/report.pdf"
