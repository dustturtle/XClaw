"""Tests for the TaskScheduler (APScheduler integration)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xclaw.scheduler import TaskScheduler


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_scheduler(
    handler=None,
    db=None,
    after_market_enabled=False,
    after_market_chat_ids=None,
) -> TaskScheduler:
    if handler is None:
        handler = AsyncMock(return_value="reply")
    if db is None:
        db = MagicMock()
        db.get_active_tasks = AsyncMock(return_value=[])
    return TaskScheduler(
        message_handler=handler,
        db=db,
        timezone="Asia/Shanghai",
        after_market_push_enabled=after_market_enabled,
        after_market_chat_ids=after_market_chat_ids,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_scheduler_starts_and_stops():
    """TaskScheduler should start and stop without error."""
    import asyncio

    async def _inner():
        sched = _make_scheduler()
        sched.start()
        assert sched.is_running
        sched.stop()
        # APScheduler shutdown(wait=False) is async; give it a tick to complete
        await asyncio.sleep(0.1)
        assert not sched.is_running

    asyncio.run(_inner())


def test_scheduler_stop_when_not_started():
    """Calling stop before start should not raise."""
    sched = _make_scheduler()
    sched.stop()  # Should not raise


def test_after_market_job_registered():
    """When after_market_push_enabled, the built-in job should be registered."""
    import asyncio

    async def _inner():
        sched = _make_scheduler(
            after_market_enabled=True,
            after_market_chat_ids=["chat_001"],
        )
        sched.start()
        job = sched._scheduler.get_job("after_market_push")
        assert job is not None
        sched.stop()

    asyncio.run(_inner())


def test_after_market_job_not_registered_when_disabled():
    """When disabled, no after_market_push job should exist."""
    import asyncio

    async def _inner():
        sched = _make_scheduler(after_market_enabled=False)
        sched.start()
        job = sched._scheduler.get_job("after_market_push")
        assert job is None
        sched.stop()

    asyncio.run(_inner())


def test_after_market_job_not_registered_without_chat_ids():
    """after_market_push requires at least one chat_id."""
    import asyncio

    async def _inner():
        sched = _make_scheduler(after_market_enabled=True, after_market_chat_ids=[])
        sched.start()
        job = sched._scheduler.get_job("after_market_push")
        assert job is None
        sched.stop()

    asyncio.run(_inner())


@pytest.mark.asyncio
async def test_run_due_db_tasks_empty():
    """_run_due_db_tasks with no active tasks should not raise."""
    sched = _make_scheduler()
    await sched._run_due_db_tasks()


@pytest.mark.asyncio
async def test_run_due_db_tasks_with_cron():
    """Active cron tasks should be registered with APScheduler."""
    task = {
        "id": 1,
        "chat_id": 42,
        "description": "Test task",
        "prompt": "查询大盘",
        "cron_expression": "0 15 * * 1-5",
        "next_run_at": None,
        "status": "active",
    }
    db = MagicMock()
    db.get_active_tasks = AsyncMock(return_value=[task])

    sched = _make_scheduler(db=db)
    sched.start()
    await sched._run_due_db_tasks()

    job = sched._scheduler.get_job("db_task_1")
    assert job is not None
    sched.stop()


@pytest.mark.asyncio
async def test_schedule_from_db_row_once():
    """One-time tasks with next_run_at should be scheduled."""
    from datetime import datetime, timezone, timedelta

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    task = {
        "id": 5,
        "chat_id": 10,
        "description": "once",
        "prompt": "一次性任务",
        "cron_expression": "",
        "next_run_at": future,
        "status": "active",
    }
    sched = _make_scheduler()
    sched.start()
    sched.schedule_from_db_row(task)
    job = sched._scheduler.get_job("db_task_5")
    assert job is not None
    sched.stop()


@pytest.mark.asyncio
async def test_schedule_from_db_row_invalid_date():
    """Rows with invalid next_run_at should be silently skipped."""
    task = {
        "id": 9,
        "chat_id": 10,
        "description": "bad date",
        "prompt": "test",
        "cron_expression": "",
        "next_run_at": "not-a-date",
        "status": "active",
    }
    sched = _make_scheduler()
    sched.start()
    sched.schedule_from_db_row(task)  # Should not raise
    assert sched._scheduler.get_job("db_task_9") is None
    sched.stop()


@pytest.mark.asyncio
async def test_schedule_from_db_row_no_trigger():
    """Rows with no cron_expression and no next_run_at should be skipped."""
    task = {
        "id": 7,
        "chat_id": 10,
        "description": "no trigger",
        "prompt": "test",
        "cron_expression": "",
        "next_run_at": "",
        "status": "active",
    }
    sched = _make_scheduler()
    sched.start()
    sched.schedule_from_db_row(task)
    assert sched._scheduler.get_job("db_task_7") is None
    sched.stop()


@pytest.mark.asyncio
async def test_run_after_market_push_calls_handler():
    """_run_after_market_push should invoke the handler for each chat_id."""
    handler = AsyncMock(return_value="盘后摘要")
    sched = _make_scheduler(
        handler=handler,
        after_market_enabled=True,
        after_market_chat_ids=["chat_1", "chat_2"],
    )
    await sched._run_after_market_push()
    assert handler.call_count == 2


@pytest.mark.asyncio
async def test_run_after_market_push_handler_error_does_not_raise():
    """Errors in handler during push should be logged but not propagate."""
    handler = AsyncMock(side_effect=RuntimeError("network error"))
    sched = _make_scheduler(
        handler=handler,
        after_market_enabled=True,
        after_market_chat_ids=["chat_x"],
    )
    await sched._run_after_market_push()  # Should not raise


@pytest.mark.asyncio
async def test_duplicate_registration_is_idempotent():
    """Registering the same DB task twice should not create duplicate jobs."""
    task = {
        "id": 3,
        "chat_id": 1,
        "description": "dup",
        "prompt": "test",
        "cron_expression": "0 9 * * *",
        "next_run_at": None,
        "status": "active",
    }
    sched = _make_scheduler()
    sched.start()
    sched.schedule_from_db_row(task)
    sched.schedule_from_db_row(task)  # Second call should be a no-op

    jobs = [j for j in sched._scheduler.get_jobs() if j.id == "db_task_3"]
    assert len(jobs) == 1
    sched.stop()
