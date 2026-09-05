from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from remoteagent_cron.worker import CronWorker


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        retry_max_seconds=7.0,
        tick_seconds=5.0,
        cleanup_interval_seconds=10,
    )


@dataclass
class BlockingClock:
    current: datetime = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    sleeping: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def now(self) -> datetime:
        return self.current

    async def sleep(self, _seconds: float) -> None:
        self.sleeping.set()
        await self.release.wait()


async def test_start_recovers_once_deduplicates_and_stop_cancels_tasks() -> None:
    clock = BlockingClock()
    execution_started = asyncio.Event()
    execution_release = asyncio.Event()

    class Service:
        settings = _settings()
        clock = None
        recover_calls = 0
        process_calls: list[str] = []
        cleanup_calls = 0

        async def recover(self) -> list[str]:
            self.recover_calls += 1
            return ["execution-1", "execution-1"]

        async def process_execution_once(self, execution_id: str) -> None:
            self.process_calls.append(execution_id)
            execution_started.set()
            await execution_release.wait()

        async def dispatch_due(self) -> list[str]:
            return []

        async def cleanup(self) -> dict[str, int]:
            self.cleanup_calls += 1
            return {}

    service = Service()
    worker = CronWorker(service, clock=clock)  # type: ignore[arg-type]
    await worker.start()
    await execution_started.wait()
    await clock.sleeping.wait()

    assert worker.running
    assert service.recover_calls == 1
    assert service.process_calls == ["execution-1"]
    assert list(worker._execution_tasks) == ["execution-1"]

    await worker.start()
    worker._launch("execution-1")
    await asyncio.sleep(0)
    assert service.recover_calls == 1
    assert service.process_calls == ["execution-1"]

    loop_task = worker._loop_task
    execution_task = worker._execution_tasks["execution-1"]
    await worker.stop()

    assert loop_task is not None and loop_task.cancelled()
    assert execution_task.cancelled()
    assert not worker.running
    assert worker._execution_tasks == {}


async def test_completion_callback_only_removes_the_task_that_owns_the_key() -> None:
    service = SimpleNamespace(settings=_settings(), clock=None)
    worker = CronWorker(service)  # type: ignore[arg-type]

    async def complete() -> None:
        return None

    replacement_release = asyncio.Event()
    completed = asyncio.create_task(complete())
    await completed
    replacement = asyncio.create_task(replacement_release.wait())
    worker._execution_tasks["execution-1"] = replacement

    worker._execution_done("execution-1", completed)

    assert worker._execution_tasks["execution-1"] is replacement
    replacement.cancel()
    await asyncio.gather(replacement, return_exceptions=True)


async def test_execution_retries_failures_and_service_delays_until_complete() -> None:
    class RecordingClock:
        def __init__(self) -> None:
            self.sleeps: list[float] = []

        def now(self) -> datetime:
            return datetime(2026, 9, 4, tzinfo=UTC)

        async def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            await asyncio.sleep(0)

    class Service:
        settings = _settings()
        clock = None
        actions: list[Any] = [RuntimeError("temporary failure"), 0.25, None]
        calls = 0

        async def process_execution_once(self, _execution_id: str) -> float | None:
            self.calls += 1
            action = self.actions.pop(0)
            if isinstance(action, Exception):
                raise action
            return action

    clock = RecordingClock()
    service = Service()
    worker = CronWorker(service, clock=clock)  # type: ignore[arg-type]

    await worker._run_execution("execution-1")

    assert service.calls == 3
    assert clock.sleeps == [service.settings.retry_max_seconds, 0.25]


async def test_scheduler_retries_tick_and_cleanup_failures_on_next_tick() -> None:
    class Service:
        settings = _settings()
        clock = None
        dispatch_calls = 0
        cleanup_calls = 0
        processed: list[str] = []

        async def recover(self) -> list[str]:
            return []

        async def dispatch_due(self) -> list[str]:
            self.dispatch_calls += 1
            if self.dispatch_calls == 1:
                raise RuntimeError("dispatch unavailable")
            if self.dispatch_calls == 2:
                return ["execution-1"]
            return []

        async def cleanup(self) -> dict[str, int]:
            self.cleanup_calls += 1
            if self.cleanup_calls == 1:
                raise RuntimeError("cleanup unavailable")
            return {"executions": 1}

        async def process_execution_once(self, execution_id: str) -> None:
            self.processed.append(execution_id)

    class TickingClock:
        def __init__(self) -> None:
            self.current = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
            self.sleeps: list[float] = []
            self.worker: CronWorker | None = None

        def now(self) -> datetime:
            return self.current

        async def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.current += timedelta(seconds=seconds)
            await asyncio.sleep(0)
            if len(self.sleeps) == 3:
                assert self.worker is not None
                self.worker._stopping = True

    service = Service()
    clock = TickingClock()
    worker = CronWorker(service, clock=clock)  # type: ignore[arg-type]
    clock.worker = worker

    await worker._run()
    await asyncio.sleep(0)

    assert service.dispatch_calls == 3
    assert service.cleanup_calls == 2
    assert service.processed == ["execution-1"]
    assert clock.sleeps == [5.0, 5.0, 5.0]
    assert worker._execution_tasks == {}
