"""Task scheduler (M8).

Wraps APScheduler's AsyncIOScheduler with cron-triggered full scans.
Supports hot-reload of the cron expression via ``reschedule()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import validate_cron
from app.exceptions import ScanBusyError
from app.scanner import ScanRunner

logger = logging.getLogger(__name__)


class ScrapeScheduler:
    """Cron-driven scheduler for full scan+scrape cycles."""

    def __init__(self, runner: ScanRunner) -> None:
        self._runner = runner
        self._scheduler = AsyncIOScheduler(
            timezone=os.environ.get("TZ", "Asia/Shanghai"),
        )
        self._started = False
        self._cron: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, cron: str) -> None:
        """Begin scheduling with the given cron expression."""
        trigger = validate_cron(cron)
        self._cron = cron
        self._scheduler.add_job(
            self._job,
            trigger,
            id="full_scrape",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        self._scheduler.start()
        self._started = True
        logger.info("Scheduler started: %s", cron)

    def reschedule(self, cron: str) -> None:
        """Hot-reload the cron expression.

        If the scheduler hasn't been started yet, the new cron is cached.
        """
        trigger = validate_cron(cron)
        self._cron = cron
        job = self._scheduler.get_job("full_scrape") if self._started else None
        if job is not None:
            job.reschedule(trigger)
            logger.info("Scheduler rescheduled: %s", cron)

    def pause(self) -> None:
        """Temporarily pause scheduling (shutdown precursor)."""
        if self._started:
            self._scheduler.pause()

    async def shutdown(self) -> None:
        """Wait for the running job (if any) and stop the scheduler."""
        if not self._started:
            return
        loop = asyncio.get_running_loop()
        stopped: asyncio.Future[None] = loop.create_future()

        def _on_shutdown(event: object) -> None:
            if not stopped.done():
                stopped.set_result(None)

        self._scheduler.add_listener(_on_shutdown, EVENT_SCHEDULER_SHUTDOWN)
        try:
            self._scheduler.shutdown(wait=False)
            await stopped
        finally:
            self._scheduler.remove_listener(_on_shutdown)
            self._started = False
            logger.info("Scheduler shut down")

    @property
    def next_run_time(self) -> datetime | None:
        """When the next scheduled job will fire (local time), or None."""
        job = self._scheduler.get_job("full_scrape") if self._started else None
        return job.next_run_time if job is not None else None  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _job(self) -> None:
        """The actual scheduled job."""
        try:
            await self._runner.run_full()
        except ScanBusyError:
            logger.warning("定时任务触发时已有任务在运行，跳过本轮")
        except asyncio.CancelledError:
            # Manual stop via the web "停止" button cancels the running scan
            # (which may be this scheduled job).  run_full has already marked
            # remaining items failed; log and let the cancellation finish.
            logger.info("定时任务被停止")
            raise
