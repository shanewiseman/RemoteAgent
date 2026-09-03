from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from remoteagent_cron.config import Settings
from remoteagent_cron.db import create_engine, create_session_factory, initialize_schema
from remoteagent_cron.schemas import RouterAgent, RouterJobView, RouterPromptAccepted
from remoteagent_cron.service import CronService


@dataclass
class FakeClock:
    current: datetime = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, **values: float) -> None:
        self.current += timedelta(**values)

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@dataclass
class FakeRouter:
    enabled_agents: set[str] = field(default_factory=lambda: {"alpha", "beta"})
    submit_calls: list[dict[str, Any]] = field(default_factory=list)
    jobs: dict[str, RouterJobView] = field(default_factory=dict)
    accepted_by_key: dict[str, RouterPromptAccepted] = field(default_factory=dict)
    fail_after_accept: int = 0
    submit_failures: int = 0
    cancel_failures: int = 0
    cancel_completes: bool = True
    cancel_calls: list[str] = field(default_factory=list)
    ready: bool = True

    async def get_agent(self, agent_id: str) -> RouterAgent:
        return RouterAgent(id=agent_id, enabled=agent_id in self.enabled_agents)

    async def submit_prompt(self, **arguments: Any) -> RouterPromptAccepted:
        self.submit_calls.append(dict(arguments))
        key = arguments["idempotency_key"]
        accepted = self.accepted_by_key.get(key)
        if accepted is None:
            number = len(self.accepted_by_key) + 1
            accepted = RouterPromptAccepted(
                job_id=f"job-{number}",
                conversation_key=arguments.get("conversation_key") or f"conversation-{number}",
                status="queued",
                model=arguments.get("model"),
                reasoning_effort=arguments.get("reasoning_effort"),
            )
            self.accepted_by_key[key] = accepted
            self.jobs[accepted.job_id] = RouterJobView(
                id=accepted.job_id,
                agent_id=arguments["agent_id"],
                conversation_key=accepted.conversation_key,
                status="queued",
                model=arguments.get("model"),
                reasoning_effort=arguments.get("reasoning_effort"),
            )
            if self.fail_after_accept:
                self.fail_after_accept -= 1
                raise RuntimeError("connection lost after acceptance")
        if self.submit_failures:
            self.submit_failures -= 1
            raise RuntimeError("router unavailable")
        return accepted

    async def get_prompt_status(self, job_id: str) -> RouterJobView:
        return self.jobs[job_id]

    async def cancel_prompt(self, job_id: str) -> RouterJobView:
        self.cancel_calls.append(job_id)
        if self.cancel_failures:
            self.cancel_failures -= 1
            raise RuntimeError("cancel transport failed")
        current = self.jobs[job_id]
        if not current.terminal and self.cancel_completes:
            current = current.model_copy(
                update={"status": "cancelled", "completed_at": datetime.now(UTC)}
            )
            self.jobs[job_id] = current
        return current

    async def check_ready(self) -> bool:
        return self.ready

    async def close(self) -> None:
        return None


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def router() -> FakeRouter:
    return FakeRouter()


@pytest_asyncio.fixture
async def service(tmp_path, clock: FakeClock, router: FakeRouter):  # type: ignore[no-untyped-def]
    database_url = f"sqlite+aiosqlite:///{tmp_path}/cron.db"
    settings = Settings(
        environment="test",
        database_url=database_url,
        initialize_schema=True,
        scheduler_enabled=False,
        run_timeout_seconds=10,
        retry_initial_seconds=0.01,
        retry_max_seconds=0.1,
        response_batch_limit=50,
        response_batch_bytes=4 * 1024 * 1024,
    )
    engine = create_engine(database_url)
    await initialize_schema(engine)
    value = CronService(
        settings,
        create_session_factory(engine),
        router,
        clock=clock,
    )
    yield value
    await engine.dispose()


@pytest.fixture
def schedule_body():  # type: ignore[no-untyped-def]
    from remoteagent_cron.schemas import ConfigureScheduleRequest

    return ConfigureScheduleRequest(
        cron_expression="* * * * *",
        timezone="UTC",
        agent_id="alpha",
        prompt="Run the report",
    )
