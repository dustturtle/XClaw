"""APScheduler-based task runner for XClaw.

Polls the scheduled_tasks table and dispatches due tasks to the agent_loop.
Supports cron expressions and one-time tasks. Includes a built-in daily
after-market push job (weekdays 15:30 CST) when enabled.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from loguru import logger


class TaskScheduler:
    """Wraps APScheduler and dispatches database-stored tasks to an agent handler."""

    # Built-in after-market push: weekdays at 15:30 Beijing Time (UTC+8) = 07:30 UTC
    AFTER_MARKET_CRON = "30 7 * * 1-5"  # UTC equivalent of 15:30 Beijing Time (UTC+8)
    AFTER_MARKET_PROMPT = (
        "请获取今日A股大盘行情摘要，包括：上证指数、深证成指、创业板指数的涨跌幅，"
        "以及今日涨幅最大的3个板块和跌幅最大的3个板块，用简洁中文汇报。"
    )

    def __init__(
        self,
        message_handler: Callable[[str, str, str], Coroutine[Any, Any, str]],
        db: Any,
        timezone: str = "Asia/Shanghai",
        after_market_push_enabled: bool = False,
        after_market_chat_ids: list[str] | None = None,
    ) -> None:
        """
        Args:
            message_handler: async (external_chat_id, text, channel) → reply
            db: Database instance for reading scheduled_tasks
            timezone: Scheduler timezone string
            after_market_push_enabled: Enable the built-in daily push
            after_market_chat_ids: External chat IDs to send the daily push to
        """
        self._handler = message_handler
        self._db = db
        self._tz = timezone
        self._after_market_enabled = after_market_push_enabled
        self._after_market_chat_ids = after_market_chat_ids or []
        self._scheduler = AsyncIOScheduler(timezone=timezone)

    def start(self) -> None:
        """Start the APScheduler and register built-in jobs."""
        # Poll database tasks every minute
        self._scheduler.add_job(
            self._run_due_db_tasks,
            trigger=CronTrigger(minute="*"),
            id="poll_db_tasks",
            replace_existing=True,
        )

        # Built-in daily after-market push
        if self._after_market_enabled and self._after_market_chat_ids:
            self._scheduler.add_job(
                self._run_after_market_push,
                trigger=CronTrigger.from_crontab(self.AFTER_MARKET_CRON, timezone="UTC"),
                id="after_market_push",
                replace_existing=True,
            )
            logger.info(
                f"After-market push enabled for {len(self._after_market_chat_ids)} chat(s)"
            )

        self._scheduler.start()
        logger.info("TaskScheduler started")

    def stop(self) -> None:
        """Shutdown the scheduler."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        logger.info("TaskScheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._scheduler.running

    def schedule_from_db_row(self, task: dict[str, Any]) -> None:
        """Register a single DB task row with APScheduler."""
        task_id = task["id"]
        job_id = f"db_task_{task_id}"

        if self._scheduler.get_job(job_id):
            return  # already registered

        cron = task.get("cron_expression", "")
        next_run = task.get("next_run_at", "")
        prompt = task["prompt"]
        chat_id = str(task["chat_id"])

        async def run_task(p: str = prompt, cid: str = chat_id, tid: int = task_id) -> None:
            logger.info(f"Running scheduled task id={tid}")
            try:
                await self._handler(cid, p, "scheduler")
                # Update next_run_at for cron tasks (APScheduler handles this)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Scheduled task id={tid} error: {exc}")

        if cron:
            trigger = CronTrigger.from_crontab(cron, timezone=self._tz)
        elif next_run:
            try:
                dt = datetime.fromisoformat(next_run)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                trigger = DateTrigger(run_date=dt)
            except ValueError:
                logger.warning(f"Invalid next_run_at for task {task_id}: {next_run!r}")
                return
        else:
            return  # Nothing to schedule

        self._scheduler.add_job(
            run_task,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
        )

    async def _run_due_db_tasks(self) -> None:
        """Load active tasks from DB and ensure they are registered with APScheduler."""
        try:
            tasks = await self._db.get_active_tasks()
            for task in tasks:
                self.schedule_from_db_row(task)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Error polling scheduled tasks: {exc}")

    async def _run_after_market_push(self) -> None:
        """Send daily after-market summary to all registered chats."""
        logger.info("Running daily after-market push")
        for chat_id in self._after_market_chat_ids:
            try:
                reply = await self._handler(chat_id, self.AFTER_MARKET_PROMPT, "scheduler")
                logger.info(f"After-market push sent to chat_id={chat_id}: {reply[:80]}…")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"After-market push error for chat_id={chat_id}: {exc}")
