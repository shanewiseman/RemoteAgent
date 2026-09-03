from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from .clock import Clock, SystemClock, as_utc
from .service import CronService

logger = logging.getLogger(__name__)


class CronWorker:
    def __init__(
        self,
        service: CronService,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.service = service
        self.settings = service.settings
        self.clock = clock or service.clock or SystemClock()
        self._loop_task: asyncio.Task[None] | None = None
        self._execution_tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stopping = False
        resume = await self.service.recover()
        for execution_id in resume:
            self._launch(execution_id)
        self._loop_task = asyncio.create_task(self._run(), name="remoteagent-cron-scheduler")

    async def stop(self) -> None:
        self._stopping = True
        tasks: list[asyncio.Task[None]] = []
        if self._loop_task is not None:
            self._loop_task.cancel()
            tasks.append(self._loop_task)
            self._loop_task = None
        tasks.extend(self._execution_tasks.values())
        for task in self._execution_tasks.values():
            task.cancel()
        self._execution_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _launch(self, execution_id: str) -> None:
        existing = self._execution_tasks.get(execution_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_execution(execution_id),
            name=f"remoteagent-cron-execution-{execution_id}",
        )
        self._execution_tasks[execution_id] = task
        task.add_done_callback(
            lambda completed, key=execution_id: self._execution_done(key, completed)
        )

    def _execution_done(self, execution_id: str, task: asyncio.Task[None]) -> None:
        self._execution_tasks.pop(execution_id, None)
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.error(
                "cron execution task %s stopped unexpectedly",
                execution_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _run_execution(self, execution_id: str) -> None:
        while not self._stopping:
            try:
                delay = await self.service.process_execution_once(execution_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to advance cron execution %s", execution_id)
                delay = self.settings.retry_max_seconds
            if delay is None:
                return
            await self.clock.sleep(delay)

    async def _run(self) -> None:
        next_cleanup = as_utc(self.clock.now())
        while not self._stopping:
            try:
                for execution_id in await self.service.dispatch_due():
                    self._launch(execution_id)
                now = as_utc(self.clock.now())
                if now >= next_cleanup:
                    await self.service.cleanup()
                    next_cleanup = now + timedelta(seconds=self.settings.cleanup_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("cron scheduler tick failed")
            await self.clock.sleep(self.settings.tick_seconds)
