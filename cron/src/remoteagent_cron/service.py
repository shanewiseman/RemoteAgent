from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .clock import Clock, SystemClock, as_utc
from .config import Settings
from .db import session_scope
from .mcp_client import RouterMCPClient, RouterMCPError, RouterMCPRejectedError
from .models import (
    ExecutionRecord,
    ResponseLeaseRecord,
    ResponseRecord,
    ScheduleRecord,
    ScheduleRevisionRecord,
)
from .scheduling import next_occurrence, validate_cron_expression, validate_timezone
from .schemas import (
    AcknowledgeResponsesResult,
    ConfigureScheduleRequest,
    ConversationMode,
    CronResponse,
    DeleteScheduleResult,
    LastFailure,
    LeaseResponsesRequest,
    ResponseLease,
    RouterJobView,
    ScheduleView,
)

TERMINAL_EXECUTION_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "interrupted", "expired"}
)


class CronServiceError(RuntimeError):
    pass


class ScheduleNotFoundError(CronServiceError):
    pass


class ExecutionNotFoundError(CronServiceError):
    pass


class LeaseNotFoundError(CronServiceError):
    pass


class ScheduleConflictError(CronServiceError):
    pass


class ScheduleValidationError(CronServiceError):
    pass


def _error_text(value: object, *, limit: int = 4096) -> str:
    text = str(value).strip() or value.__class__.__name__
    return text[:limit]


def _canonical_configuration(body: ConfigureScheduleRequest) -> tuple[dict[str, Any], str]:
    values: dict[str, Any] = {
        "cron_expression": validate_cron_expression(body.cron_expression),
        "timezone": validate_timezone(body.timezone),
        "agent_id": body.agent_id,
        "prompt": body.prompt,
        "model": body.model,
        "reasoning_effort": (
            str(body.reasoning_effort) if body.reasoning_effort is not None else None
        ),
        "conversation_mode": str(body.conversation_mode),
    }
    encoded = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return values, hashlib.sha256(encoded).hexdigest()


class CronService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        router: RouterMCPClient,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.router = router
        self.clock = clock or SystemClock()
        self._configuration_lock = asyncio.Lock()
        self._lease_lock = asyncio.Lock()

    async def configure(self, schedule_id: str, body: ConfigureScheduleRequest) -> ScheduleView:
        async with self._configuration_lock:
            return await self._configure(schedule_id, body)

    async def _configure(self, schedule_id: str, body: ConfigureScheduleRequest) -> ScheduleView:
        try:
            agent = await self.router.get_agent(body.agent_id)
        except RouterMCPRejectedError as exc:
            raise ScheduleValidationError("agent does not exist or is disabled") from exc
        except RouterMCPError:
            raise
        except Exception as exc:
            raise RouterMCPError(f"could not validate router agent: {exc}") from exc
        if agent.id != body.agent_id or not agent.enabled:
            raise ScheduleValidationError("agent does not exist or is disabled")

        values, checksum = _canonical_configuration(body)
        now = as_utc(self.clock.now())
        for attempt in range(3):
            try:
                async with session_scope(self.session_factory) as session:
                    statement = (
                        select(ScheduleRecord)
                        .where(ScheduleRecord.id == schedule_id)
                        .with_for_update()
                    )
                    schedule = (await session.execute(statement)).scalar_one_or_none()
                    if schedule is None:
                        generation_id = str(uuid4())
                        revision_id = str(uuid4())
                        schedule = ScheduleRecord(
                            id=schedule_id,
                            generation_id=generation_id,
                            status="enabled",
                            enabled=True,
                            current_revision=1,
                            current_revision_id=revision_id,
                            next_fire_at=next_occurrence(
                                values["cron_expression"], values["timezone"], now
                            ),
                            skipped_occurrences=0,
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(schedule)
                        session.add(
                            self._revision_from_values(
                                revision_id, schedule_id, 1, checksum, values, now
                            )
                        )
                    else:
                        if schedule.status == "deleting":
                            raise ScheduleConflictError("schedule deletion is in progress")
                        current = await session.get(
                            ScheduleRevisionRecord, schedule.current_revision_id
                        )
                        if current is None:
                            raise CronServiceError("current schedule revision is missing")
                        if current.checksum != checksum:
                            revision = schedule.current_revision + 1
                            revision_id = str(uuid4())
                            profile_changed = (
                                current.agent_id != values["agent_id"]
                                or current.model != values["model"]
                                or current.reasoning_effort != values["reasoning_effort"]
                                or current.conversation_mode != values["conversation_mode"]
                            )
                            session.add(
                                self._revision_from_values(
                                    revision_id,
                                    schedule_id,
                                    revision,
                                    checksum,
                                    values,
                                    now,
                                )
                            )
                            schedule.current_revision = revision
                            schedule.current_revision_id = revision_id
                            schedule.updated_at = now
                            if profile_changed:
                                schedule.persistent_conversation_key = None
                            if schedule.enabled:
                                schedule.next_fire_at = next_occurrence(
                                    values["cron_expression"], values["timezone"], now
                                )
                break
            except IntegrityError:
                if attempt == 2:
                    raise ScheduleConflictError("concurrent schedule update did not converge")
                continue
        return await self.get(schedule_id)

    @staticmethod
    def _revision_from_values(
        revision_id: str,
        schedule_id: str,
        revision: int,
        checksum: str,
        values: dict[str, Any],
        now: datetime,
    ) -> ScheduleRevisionRecord:
        return ScheduleRevisionRecord(
            id=revision_id,
            schedule_id=schedule_id,
            revision=revision,
            checksum=checksum,
            cron_expression=values["cron_expression"],
            timezone=values["timezone"],
            agent_id=values["agent_id"],
            prompt=values["prompt"],
            model=values["model"],
            reasoning_effort=values["reasoning_effort"],
            conversation_mode=values["conversation_mode"],
            created_at=now,
        )

    async def get(self, schedule_id: str) -> ScheduleView:
        async with self.session_factory() as session:
            schedule = await session.get(ScheduleRecord, schedule_id)
            if schedule is None:
                raise ScheduleNotFoundError(schedule_id)
            return await self._view(session, schedule)

    async def list(self, *, include_disabled: bool = False) -> list[ScheduleView]:
        async with self.session_factory() as session:
            statement = select(ScheduleRecord).order_by(ScheduleRecord.id)
            if not include_disabled:
                statement = statement.where(
                    or_(ScheduleRecord.enabled.is_(True), ScheduleRecord.status == "deleting")
                )
            schedules = (await session.execute(statement)).scalars().all()
            return [await self._view(session, item) for item in schedules]

    async def _view(self, session: AsyncSession, schedule: ScheduleRecord) -> ScheduleView:
        revision = await session.get(ScheduleRevisionRecord, schedule.current_revision_id)
        if revision is None:
            raise CronServiceError("current schedule revision is missing")
        count = await session.scalar(
            select(func.count(ResponseRecord.id)).where(ResponseRecord.schedule_id == schedule.id)
        )
        failure = (
            LastFailure.model_validate(schedule.last_failure) if schedule.last_failure else None
        )
        return ScheduleView(
            schedule_id=schedule.id,
            generation_id=schedule.generation_id,
            status=schedule.status,
            enabled=schedule.enabled,
            cron_expression=revision.cron_expression,
            timezone=revision.timezone,
            agent_id=revision.agent_id,
            prompt=revision.prompt,
            model=revision.model,
            reasoning_effort=revision.reasoning_effort,
            conversation_mode=revision.conversation_mode,
            revision=schedule.current_revision,
            revision_id=revision.id,
            next_fire_at=(
                as_utc(schedule.next_fire_at) if schedule.next_fire_at is not None else None
            ),
            active_execution_id=schedule.active_execution_id,
            last_execution_id=schedule.last_execution_id,
            pending_response_count=int(count or 0),
            skipped_occurrences=schedule.skipped_occurrences,
            last_failure=failure,
            created_at=as_utc(schedule.created_at),
            updated_at=as_utc(schedule.updated_at),
        )

    async def set_enabled(self, schedule_id: str, enabled: bool) -> ScheduleView:
        now = as_utc(self.clock.now())
        async with session_scope(self.session_factory) as session:
            schedule = (
                await session.execute(
                    select(ScheduleRecord).where(ScheduleRecord.id == schedule_id).with_for_update()
                )
            ).scalar_one_or_none()
            if schedule is None:
                raise ScheduleNotFoundError(schedule_id)
            if schedule.status == "deleting":
                raise ScheduleConflictError("schedule deletion is in progress")
            if schedule.enabled == enabled:
                return await self._view(session, schedule)
            schedule.enabled = enabled
            schedule.status = "enabled" if enabled else "disabled"
            schedule.updated_at = now
            if enabled:
                revision = await session.get(ScheduleRevisionRecord, schedule.current_revision_id)
                if revision is None:
                    raise CronServiceError("current schedule revision is missing")
                schedule.next_fire_at = next_occurrence(
                    revision.cron_expression, revision.timezone, now
                )
            else:
                schedule.next_fire_at = None
        return await self.get(schedule_id)

    async def delete(self, schedule_id: str) -> DeleteScheduleResult:
        now = as_utc(self.clock.now())
        result = "deleted"
        async with session_scope(self.session_factory) as session:
            schedule = (
                await session.execute(
                    select(ScheduleRecord).where(ScheduleRecord.id == schedule_id).with_for_update()
                )
            ).scalar_one_or_none()
            if schedule is None:
                raise ScheduleNotFoundError(schedule_id)
            active = (
                await session.get(ExecutionRecord, schedule.active_execution_id)
                if schedule.active_execution_id
                else None
            )
            if active is not None and active.state not in TERMINAL_EXECUTION_STATES:
                schedule.enabled = False
                schedule.status = "deleting"
                schedule.next_fire_at = None
                schedule.updated_at = now
                result = "deleting"
            else:
                await session.delete(schedule)
        return DeleteScheduleResult(schedule_id=schedule_id, status=result)

    async def recover(self) -> list[str]:
        """Skip downtime occurrences and return nonterminal executions to resume."""

        now = as_utc(self.clock.now())
        resume: list[str] = []
        async with session_scope(self.session_factory) as session:
            schedules = (
                (await session.execute(select(ScheduleRecord).with_for_update())).scalars().all()
            )
            for schedule in schedules:
                active = (
                    await session.get(ExecutionRecord, schedule.active_execution_id)
                    if schedule.active_execution_id
                    else None
                )
                if active is not None and active.state not in TERMINAL_EXECUTION_STATES:
                    resume.append(active.id)
                else:
                    schedule.active_execution_id = None
                    if schedule.status == "deleting":
                        await session.delete(schedule)
                        continue
                if schedule.enabled:
                    revision = await session.get(
                        ScheduleRevisionRecord, schedule.current_revision_id
                    )
                    if revision is None:
                        raise CronServiceError("current schedule revision is missing")
                    if schedule.next_fire_at is None or as_utc(schedule.next_fire_at) <= now:
                        schedule.next_fire_at = next_occurrence(
                            revision.cron_expression, revision.timezone, now
                        )
                        schedule.updated_at = now
        return resume

    async def dispatch_due(self) -> list[str]:
        """Persist each currently due occurrence and return execution IDs to run."""

        now = as_utc(self.clock.now())
        async with self.session_factory() as session:
            ids = (
                (
                    await session.execute(
                        select(ScheduleRecord.id)
                        .where(
                            ScheduleRecord.enabled.is_(True),
                            ScheduleRecord.status == "enabled",
                            ScheduleRecord.next_fire_at.is_not(None),
                            ScheduleRecord.next_fire_at <= now,
                        )
                        .order_by(ScheduleRecord.next_fire_at, ScheduleRecord.id)
                    )
                )
                .scalars()
                .all()
            )

        launched: list[str] = []
        for schedule_id in ids:
            execution_id = await self._dispatch_one(schedule_id, now)
            if execution_id is not None:
                launched.append(execution_id)
        return launched

    async def _dispatch_one(self, schedule_id: str, now: datetime) -> str | None:
        async with session_scope(self.session_factory) as session:
            schedule = (
                await session.execute(
                    select(ScheduleRecord)
                    .where(ScheduleRecord.id == schedule_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if (
                schedule is None
                or not schedule.enabled
                or schedule.status != "enabled"
                or schedule.next_fire_at is None
                or as_utc(schedule.next_fire_at) > now
            ):
                return None
            revision = await session.get(ScheduleRevisionRecord, schedule.current_revision_id)
            if revision is None:
                raise CronServiceError("current schedule revision is missing")
            scheduled_for = as_utc(schedule.next_fire_at)
            # One overdue boundary may run, but older boundaries are not replayed.
            schedule.next_fire_at = next_occurrence(
                revision.cron_expression,
                revision.timezone,
                max(now, scheduled_for),
            )
            schedule.updated_at = now

            active = (
                await session.get(ExecutionRecord, schedule.active_execution_id)
                if schedule.active_execution_id
                else None
            )
            if active is not None and active.state not in TERMINAL_EXECUTION_STATES:
                schedule.skipped_occurrences += 1
                return None
            schedule.active_execution_id = None

            existing = (
                await session.execute(
                    select(ExecutionRecord).where(
                        ExecutionRecord.generation_id == schedule.generation_id,
                        ExecutionRecord.scheduled_for == scheduled_for,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.state not in TERMINAL_EXECUTION_STATES:
                    schedule.active_execution_id = existing.id
                    return existing.id
                return None

            execution_id = str(uuid4())
            idempotency_key = f"cron:{schedule.generation_id}:{scheduled_for.isoformat()}"
            continuation = (
                schedule.persistent_conversation_key
                if revision.conversation_mode == ConversationMode.PERSISTENT
                else None
            )
            execution = ExecutionRecord(
                id=execution_id,
                schedule_id=schedule.id,
                generation_id=schedule.generation_id,
                revision_id=revision.id,
                scheduled_for=scheduled_for,
                idempotency_key=idempotency_key,
                continuation_key=continuation,
                state="pending",
                started_at=now,
                deadline_at=now + timedelta(seconds=self.settings.run_timeout_seconds),
                retry_count=0,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)
            schedule.active_execution_id = execution_id
            schedule.last_execution_id = execution_id
            return execution_id

    async def process_execution_once(self, execution_id: str) -> float | None:
        """Advance an execution once; return a delay or ``None`` when terminal."""

        now = as_utc(self.clock.now())
        async with self.session_factory() as session:
            execution = await session.get(ExecutionRecord, execution_id)
            if execution is None or execution.state in TERMINAL_EXECUTION_STATES:
                return None
            revision = await session.get(ScheduleRevisionRecord, execution.revision_id)
            if revision is None:
                await self._finish_without_job(
                    execution_id, "failed", "schedule revision is missing", now
                )
                return None
            job_id = execution.router_job_id
            state = execution.state
            deadline_at = as_utc(execution.deadline_at)
            submit_values = {
                "agent_id": revision.agent_id,
                "prompt": revision.prompt,
                "conversation_key": execution.continuation_key,
                "idempotency_key": execution.idempotency_key,
                "model": revision.model,
                "reasoning_effort": revision.reasoning_effort,
            }

        if job_id is None:
            # A transport failure after router acceptance is indistinguishable
            # from a failure before acceptance. Keep recovering the deterministic
            # idempotent submission even past the deadline; once its job ID is
            # known, the next step can safely request cancellation.
            await self._set_execution_state(execution_id, "submitting", now)
            try:
                accepted = await self.router.submit_prompt(**submit_values)
            except RouterMCPRejectedError as exc:
                await self._finish_without_job(execution_id, "failed", str(exc), now)
                return None
            except Exception as exc:
                return await self._record_retry(execution_id, exc, now)
            async with session_scope(self.session_factory) as session:
                execution = (
                    await session.execute(
                        select(ExecutionRecord)
                        .where(ExecutionRecord.id == execution_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if execution is None or execution.state in TERMINAL_EXECUTION_STATES:
                    return None
                execution.router_job_id = accepted.job_id
                execution.conversation_key = accepted.conversation_key
                execution.state = "polling"
                execution.error = None
                execution.retry_count = 0
                execution.updated_at = now
                schedule = (
                    await session.execute(
                        select(ScheduleRecord)
                        .where(ScheduleRecord.id == execution.schedule_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                revision = await session.get(ScheduleRevisionRecord, execution.revision_id)
                if schedule is not None and revision is not None:
                    await self._capture_persistent_conversation(
                        session, schedule, revision, accepted.conversation_key
                    )
            return self.settings.job_poll_seconds

        if now >= deadline_at and state != "cancelling":
            try:
                job = await self.router.cancel_prompt(job_id)
            except Exception as exc:
                await self._set_execution_state(execution_id, "cancel_pending", now)
                return await self._record_retry(execution_id, exc, now)
            if job.terminal:
                await self._finish_job(execution_id, job, now)
                return None
            await self._set_execution_state(execution_id, "cancelling", now, reset_retry=True)
            return self.settings.job_poll_seconds

        try:
            job = await self.router.get_prompt_status(job_id)
        except Exception as exc:
            return await self._record_retry(execution_id, exc, now)
        if job.terminal:
            await self._finish_job(execution_id, job, now)
            return None
        async with session_scope(self.session_factory) as session:
            execution = await session.get(ExecutionRecord, execution_id)
            if execution is not None and execution.state not in TERMINAL_EXECUTION_STATES:
                execution.state = "cancelling" if state == "cancelling" else "polling"
                execution.last_polled_at = now
                execution.error = None
                execution.retry_count = 0
                execution.updated_at = now
        return self.settings.job_poll_seconds

    async def _capture_persistent_conversation(
        self,
        session: AsyncSession,
        schedule: ScheduleRecord,
        revision: ScheduleRevisionRecord,
        conversation_key: str,
    ) -> None:
        if revision.conversation_mode != ConversationMode.PERSISTENT:
            return
        schedule = (
            await session.execute(
                select(ScheduleRecord)
                .where(ScheduleRecord.id == schedule.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if schedule is None:
            return
        current = await session.get(ScheduleRevisionRecord, schedule.current_revision_id)
        if current is None:
            return
        same_profile = (
            current.agent_id == revision.agent_id
            and current.model == revision.model
            and current.reasoning_effort == revision.reasoning_effort
            and current.conversation_mode == revision.conversation_mode
        )
        if same_profile:
            schedule.persistent_conversation_key = conversation_key

    async def _set_execution_state(
        self,
        execution_id: str,
        state: str,
        now: datetime,
        *,
        reset_retry: bool = False,
    ) -> None:
        async with session_scope(self.session_factory) as session:
            execution = await session.get(ExecutionRecord, execution_id)
            if execution is not None and execution.state not in TERMINAL_EXECUTION_STATES:
                execution.state = state
                if reset_retry:
                    execution.retry_count = 0
                    execution.error = None
                execution.updated_at = now

    async def _record_retry(
        self, execution_id: str, error: Exception, now: datetime
    ) -> float | None:
        retry_count = 1
        async with session_scope(self.session_factory) as session:
            execution = await session.get(ExecutionRecord, execution_id)
            if execution is None or execution.state in TERMINAL_EXECUTION_STATES:
                return None
            execution.retry_count += 1
            retry_count = execution.retry_count
            execution.error = _error_text(error)
            execution.updated_at = now
        return min(
            self.settings.retry_initial_seconds * (2 ** min(retry_count - 1, 20)),
            self.settings.retry_max_seconds,
        )

    async def _finish_without_job(
        self, execution_id: str, state: str, error: str, completed_at: datetime
    ) -> None:
        async with session_scope(self.session_factory) as session:
            execution = (
                await session.execute(
                    select(ExecutionRecord)
                    .where(ExecutionRecord.id == execution_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if execution is None or execution.state in TERMINAL_EXECUTION_STATES:
                return
            execution.state = state
            execution.error = _error_text(error)
            execution.completed_at = completed_at
            execution.updated_at = completed_at
            await self._complete_schedule(session, execution, state, error, completed_at)

    async def _finish_job(
        self, execution_id: str, job: RouterJobView, observed_at: datetime
    ) -> None:
        completed_at = as_utc(job.completed_at or observed_at)
        async with session_scope(self.session_factory) as session:
            execution = (
                await session.execute(
                    select(ExecutionRecord)
                    .where(ExecutionRecord.id == execution_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if execution is None or execution.state in TERMINAL_EXECUTION_STATES:
                return
            revision = await session.get(ScheduleRevisionRecord, execution.revision_id)
            if revision is None:
                await self._complete_schedule(
                    session,
                    execution,
                    "failed",
                    "schedule revision is missing",
                    completed_at,
                )
                execution.state = "failed"
                execution.error = "schedule revision is missing"
                execution.completed_at = completed_at
                return
            execution.state = job.status
            execution.conversation_key = job.conversation_key
            execution.error = job.error
            execution.completed_at = completed_at
            execution.last_polled_at = observed_at
            execution.updated_at = observed_at
            if job.status == "succeeded":
                existing = await session.scalar(
                    select(ResponseRecord.id).where(ResponseRecord.execution_id == execution.id)
                )
                if existing is None:
                    session.add(
                        ResponseRecord(
                            id=str(uuid4()),
                            execution_id=execution.id,
                            schedule_id=execution.schedule_id,
                            revision_id=revision.id,
                            revision=revision.revision,
                            agent_id=revision.agent_id,
                            router_job_id=job.id,
                            conversation_key=job.conversation_key,
                            scheduled_for=as_utc(execution.scheduled_for),
                            completed_at=completed_at,
                            model=job.model,
                            reasoning_effort=(
                                str(job.reasoning_effort)
                                if job.reasoning_effort is not None
                                else None
                            ),
                            usage=(
                                job.usage.model_dump(mode="json")
                                if hasattr(job.usage, "model_dump")
                                else dict(job.usage)
                                if job.usage is not None
                                else None
                            ),
                            result=job.result or "",
                            created_at=observed_at,
                        )
                    )
            await self._complete_schedule(
                session,
                execution,
                job.status,
                job.error or f"router job ended as {job.status}",
                completed_at,
            )

    async def _complete_schedule(
        self,
        session: AsyncSession,
        execution: ExecutionRecord,
        state: str,
        error: str,
        completed_at: datetime,
    ) -> None:
        schedule = (
            await session.execute(
                select(ScheduleRecord)
                .where(ScheduleRecord.id == execution.schedule_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if schedule is None:
            return
        if schedule.active_execution_id == execution.id:
            schedule.active_execution_id = None
        if state != "succeeded":
            schedule.last_failure = {
                "execution_id": execution.id,
                "status": state,
                "error": _error_text(error),
                "completed_at": completed_at.isoformat(),
            }
        schedule.updated_at = completed_at
        if schedule.status == "deleting":
            await session.delete(schedule)

    async def lease_responses(self, request: LeaseResponsesRequest) -> ResponseLease:
        async with self._lease_lock:
            return await self._lease_responses(request)

    async def _lease_responses(self, request: LeaseResponsesRequest) -> ResponseLease:
        now = as_utc(self.clock.now())
        limit = (
            1 if request.execution_id else min(request.limit, self.settings.response_batch_limit)
        )
        async with session_scope(self.session_factory) as session:
            if request.schedule_id is not None:
                if await session.get(ScheduleRecord, request.schedule_id) is None:
                    raise ScheduleNotFoundError(request.schedule_id)
            else:
                if await session.get(ExecutionRecord, request.execution_id) is None:
                    raise ExecutionNotFoundError(str(request.execution_id))

            available = or_(
                ResponseRecord.lease_id.is_(None),
                ResponseRecord.lease_expires_at <= now,
            )
            statement = select(ResponseRecord).where(available)
            if request.schedule_id is not None:
                statement = statement.where(ResponseRecord.schedule_id == request.schedule_id)
            else:
                statement = statement.where(ResponseRecord.execution_id == request.execution_id)
            statement = (
                statement.order_by(ResponseRecord.scheduled_for, ResponseRecord.created_at)
                .limit(limit + 1)
                .with_for_update(skip_locked=True)
            )
            candidates = (await session.execute(statement)).scalars().all()
            selected: list[ResponseRecord] = []
            serialized_bytes = 0
            for candidate in candidates[:limit]:
                view = self._response_view(candidate)
                size = len(
                    json.dumps(
                        view.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                if selected and serialized_bytes + size > self.settings.response_batch_bytes:
                    break
                selected.append(candidate)
                serialized_bytes += size
                if serialized_bytes >= self.settings.response_batch_bytes:
                    break
            if not selected:
                return ResponseLease(
                    lease_id=None,
                    expires_at=None,
                    responses=[],
                    more_available=False,
                )
            old_leases = {item.lease_id for item in selected if item.lease_id is not None}
            if old_leases:
                await session.execute(
                    update(ResponseLeaseRecord)
                    .where(
                        ResponseLeaseRecord.id.in_(old_leases),
                        ResponseLeaseRecord.status == "active",
                    )
                    .values(status="stale")
                )
            lease_id = str(uuid4())
            expires_at = now + timedelta(seconds=self.settings.lease_seconds)
            session.add(
                ResponseLeaseRecord(
                    id=lease_id,
                    status="active",
                    expires_at=expires_at,
                    deleted_count=0,
                    created_at=now,
                )
            )
            for item in selected:
                item.lease_id = lease_id
                item.lease_expires_at = expires_at
            more_available = len(candidates) > len(selected)
            responses = [self._response_view(item) for item in selected]
        return ResponseLease(
            lease_id=lease_id,
            expires_at=expires_at,
            responses=responses,
            more_available=more_available,
        )

    @staticmethod
    def _response_view(record: ResponseRecord) -> CronResponse:
        return CronResponse(
            response_id=record.id,
            schedule_id=record.schedule_id,
            execution_id=record.execution_id,
            revision_id=record.revision_id,
            revision=record.revision,
            agent_id=record.agent_id,
            router_job_id=record.router_job_id,
            conversation_key=record.conversation_key,
            scheduled_for=as_utc(record.scheduled_for),
            completed_at=as_utc(record.completed_at),
            model=record.model,
            reasoning_effort=record.reasoning_effort,
            usage=record.usage,
            result=record.result,
        )

    async def acknowledge(self, lease_id: str) -> AcknowledgeResponsesResult:
        async with self._lease_lock:
            return await self._acknowledge(lease_id)

    async def _acknowledge(self, lease_id: str) -> AcknowledgeResponsesResult:
        now = as_utc(self.clock.now())
        async with session_scope(self.session_factory) as session:
            # Lease acquisition locks response rows before updating an expired
            # lease. Use the same lock order here to avoid a PostgreSQL deadlock
            # when acknowledgement races expiry/re-leasing.
            await session.execute(
                select(ResponseRecord.id)
                .where(ResponseRecord.lease_id == lease_id)
                .with_for_update()
            )
            lease = (
                await session.execute(
                    select(ResponseLeaseRecord)
                    .where(ResponseLeaseRecord.id == lease_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if lease is None:
                raise LeaseNotFoundError(lease_id)
            if lease.status == "acknowledged":
                return AcknowledgeResponsesResult(
                    lease_id=lease.id,
                    status="already_acknowledged",
                    deleted_count=lease.deleted_count,
                )
            if lease.status != "active" or as_utc(lease.expires_at) <= now:
                lease.status = "stale"
                return AcknowledgeResponsesResult(
                    lease_id=lease.id, status="stale", deleted_count=0
                )
            result = await session.execute(
                delete(ResponseRecord).where(ResponseRecord.lease_id == lease.id)
            )
            deleted_count = int(result.rowcount or 0)  # type: ignore[attr-defined]
            lease.status = "acknowledged"
            lease.deleted_count = deleted_count
            lease.acknowledged_at = now
            return AcknowledgeResponsesResult(
                lease_id=lease.id,
                status="acknowledged",
                deleted_count=deleted_count,
            )

    async def cleanup(self) -> dict[str, int]:
        now = as_utc(self.clock.now())
        response_cutoff = now - timedelta(seconds=self.settings.response_retention_seconds)
        execution_cutoff = now - timedelta(seconds=self.settings.execution_retention_seconds)
        tombstone_cutoff = now - timedelta(seconds=self.settings.ack_tombstone_seconds)
        async with session_scope(self.session_factory) as session:
            expired_responses = await session.execute(
                delete(ResponseRecord).where(
                    ResponseRecord.created_at <= response_cutoff,
                    or_(
                        ResponseRecord.lease_id.is_(None),
                        ResponseRecord.lease_expires_at <= now,
                    ),
                )
            )
            await session.execute(
                update(ResponseLeaseRecord)
                .where(
                    ResponseLeaseRecord.status == "active",
                    ResponseLeaseRecord.expires_at <= now,
                )
                .values(status="stale")
            )
            old_leases = await session.execute(
                delete(ResponseLeaseRecord).where(
                    ResponseLeaseRecord.status.in_(("acknowledged", "stale")),
                    or_(
                        ResponseLeaseRecord.acknowledged_at <= tombstone_cutoff,
                        and_(
                            ResponseLeaseRecord.acknowledged_at.is_(None),
                            ResponseLeaseRecord.expires_at <= tombstone_cutoff,
                        ),
                    ),
                )
            )
            old_executions = await session.execute(
                delete(ExecutionRecord).where(
                    ExecutionRecord.state.in_(TERMINAL_EXECUTION_STATES),
                    ExecutionRecord.completed_at <= execution_cutoff,
                    ~select(ResponseRecord.id)
                    .where(ResponseRecord.execution_id == ExecutionRecord.id)
                    .exists(),
                )
            )
            return {
                "responses": int(expired_responses.rowcount or 0),  # type: ignore[attr-defined]
                "leases": int(old_leases.rowcount or 0),  # type: ignore[attr-defined]
                "executions": int(old_executions.rowcount or 0),  # type: ignore[attr-defined]
            }
