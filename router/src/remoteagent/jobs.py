from __future__ import annotations

import asyncio
import logging
import uuid
import weakref
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agents import AgentNotFoundError
from .cache import Cache
from .conversation_lock import lock_conversation
from .models import (
    AgentRecord,
    AgentRevisionRecord,
    CompanionStageRecord,
    ConversationCompanionRecord,
    ConversationRecord,
    JobEventRecord,
    JobRecord,
)
from .schemas import (
    CONVERSATION_KEY_RE,
    ConversationStatus,
    ConversationCompanionView,
    JobStatus,
    JobView,
    PromptAccepted,
    PromptRequest,
    ReasoningEffort,
    UsageTotals,
    execution_profile_from_config,
)
from .workspace import WorkspaceManager

if TYPE_CHECKING:
    from .companions import CompanionService

logger = logging.getLogger(__name__)


class JobNotFoundError(LookupError):
    pass


class ConversationConflictError(RuntimeError):
    pass


class ConversationNotFoundError(LookupError):
    pass


class InvalidTransitionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JobExecution:
    id: str
    agent_id: str
    conversation_key: str
    sequence: int
    prompt: str
    agent_revision: int
    thread_id: str | None
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None


ACTIVE_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.PROVISIONING,
    JobStatus.WAITING_FOR_LEASE,
    JobStatus.RUNNING,
    JobStatus.COLLECTING,
}

TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {JobStatus.PROVISIONING, JobStatus.CANCELLED, JobStatus.EXPIRED},
    JobStatus.PROVISIONING: {
        JobStatus.WAITING_FOR_LEASE,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    },
    JobStatus.WAITING_FOR_LEASE: {
        JobStatus.RUNNING,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    },
    JobStatus.RUNNING: {
        JobStatus.COLLECTING,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    },
    JobStatus.COLLECTING: {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    },
}


def _job_view(
    record: JobRecord,
    companion_additions: list[ConversationCompanionView] | None = None,
) -> JobView:
    usage = UsageTotals.model_validate(record.usage) if record.usage is not None else None
    return JobView(
        id=record.id,
        agent_id=record.agent_id,
        conversation_key=record.conversation_key,
        sequence=record.sequence,
        status=JobStatus(record.status),
        model=record.model,
        reasoning_effort=(
            ReasoningEffort(record.reasoning_effort) if record.reasoning_effort else None
        ),
        result=record.result,
        error=record.error,
        usage=usage,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        companion_additions=companion_additions or [],
    )


def _prompt_accepted(
    record: JobRecord,
    companion_additions: list[ConversationCompanionView] | None = None,
) -> PromptAccepted:
    return PromptAccepted(
        job_id=record.id,
        conversation_key=record.conversation_key,
        status=JobStatus(record.status),
        model=record.model,
        reasoning_effort=(
            ReasoningEffort(record.reasoning_effort) if record.reasoning_effort else None
        ),
        companion_additions=companion_additions or [],
    )


class JobService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        workspaces: WorkspaceManager,
        cache: Cache,
        activity_channel: str = "activity",
        companion_service: CompanionService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.workspaces = workspaces
        self.cache = cache
        self.activity_channel = activity_channel
        self.companion_service = companion_service
        # In-flight callers keep their keyed locks alive; idle keys disappear
        # automatically so one-shot conversations/idempotency keys do not form
        # an unbounded process-lifetime registry.
        self._submission_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    async def submit(self, request: PromptRequest) -> PromptAccepted:
        conversation_key = request.conversation_key or f"c_{uuid.uuid4().hex}"
        if not CONVERSATION_KEY_RE.fullmatch(conversation_key):
            raise ValueError("invalid conversation key")
        lock_keys = [f"conversation:{conversation_key}"]
        if request.idempotency_key:
            lock_keys.append(f"idempotency:{request.agent_id}:{request.idempotency_key}")
        async with AsyncExitStack() as lock_stack:
            for lock_key in sorted(lock_keys):
                await lock_stack.enter_async_context(
                    self._submission_locks.setdefault(lock_key, asyncio.Lock())
                )
            try:
                async with self.session_factory() as session, session.begin():
                    if request.idempotency_key:
                        existing = await session.scalar(
                            select(JobRecord).where(
                                JobRecord.agent_id == request.agent_id,
                                JobRecord.idempotency_key == request.idempotency_key,
                            )
                        )
                        if existing is not None:
                            additions = await self._companion_additions(
                                existing.id, session=session
                            )
                            return _prompt_accepted(existing, additions)
                    agent = await session.scalar(
                        select(AgentRecord)
                        .where(AgentRecord.id == request.agent_id)
                        .with_for_update()
                    )
                    if agent is None or not agent.enabled:
                        raise AgentNotFoundError(request.agent_id)
                    # The agent row lock serializes same-agent submissions across
                    # router processes. Recheck after waiting so an idempotent
                    # retry cannot advance into conversation/stage validation
                    # after its original request commits.
                    if request.idempotency_key:
                        existing = await session.scalar(
                            select(JobRecord).where(
                                JobRecord.agent_id == request.agent_id,
                                JobRecord.idempotency_key == request.idempotency_key,
                            )
                        )
                        if existing is not None:
                            additions = await self._companion_additions(
                                existing.id, session=session
                            )
                            return _prompt_accepted(existing, additions)
                    conversation = await lock_conversation(session, conversation_key)
                    if conversation is None:
                        config_toml = await session.scalar(
                            select(AgentRevisionRecord.config_toml).where(
                                AgentRevisionRecord.agent_id == agent.id,
                                AgentRevisionRecord.revision == agent.current_revision,
                            )
                        )
                        if config_toml is None:
                            raise ConversationConflictError(
                                "agent configuration revision disappeared"
                            )
                        configured_model, configured_reasoning = execution_profile_from_config(
                            config_toml
                        )
                        selected_model = (
                            request.model if request.model is not None else configured_model
                        )
                        selected_reasoning = (
                            request.reasoning_effort
                            if request.reasoning_effort is not None
                            else configured_reasoning
                        )
                        paths = self.workspaces.paths(conversation_key)
                        conversation = ConversationRecord(
                            key=conversation_key,
                            agent_id=request.agent_id,
                            codex_thread_id=None,
                            workspace_path=str(paths.workspace),
                            codex_home_path=str(paths.sessions),
                            artifact_path=str(paths.artifacts),
                            agent_revision=agent.current_revision,
                            model=selected_model,
                            reasoning_effort=(
                                selected_reasoning.value if selected_reasoning is not None else None
                            ),
                            status=ConversationStatus.ACTIVE.value,
                        )
                        session.add(conversation)
                        sequence = 1
                    else:
                        if conversation.agent_id != request.agent_id:
                            raise ConversationConflictError(
                                "a conversation key cannot be moved to another agent"
                            )
                        if conversation.status != ConversationStatus.ACTIVE.value:
                            raise ConversationConflictError("conversation is not active")
                        if request.model is not None and request.model != conversation.model:
                            raise ConversationConflictError(
                                "model cannot be changed within an existing conversation"
                            )
                        if (
                            request.reasoning_effort is not None
                            and request.reasoning_effort.value != conversation.reasoning_effort
                        ):
                            raise ConversationConflictError(
                                "reasoning_effort cannot be changed within an existing conversation"
                            )
                        conversation.agent_revision = agent.current_revision
                        conversation.updated_at = datetime.now(UTC)
                        latest_job_sequence = int(
                            await session.scalar(
                                select(func.coalesce(func.max(JobRecord.sequence), 0)).where(
                                    JobRecord.conversation_key == conversation_key
                                )
                            )
                            or 0
                        )
                        # Companion metadata deliberately outlives introducing
                        # jobs. Include its sequence watermark so routine job
                        # retention cannot rewind a conversation and hide
                        # companions from later turns.
                        latest_companion_sequence = int(
                            await session.scalar(
                                select(
                                    func.coalesce(
                                        func.max(
                                            ConversationCompanionRecord.introducing_sequence
                                        ),
                                        0,
                                    )
                                ).where(
                                    ConversationCompanionRecord.conversation_key
                                    == conversation_key
                                )
                            )
                            or 0
                        )
                        sequence = max(latest_job_sequence, latest_companion_sequence) + 1
                    job = JobRecord(
                        id=f"j_{uuid.uuid4().hex}",
                        agent_id=request.agent_id,
                        conversation_key=conversation_key,
                        sequence=sequence,
                        prompt=request.prompt,
                        idempotency_key=request.idempotency_key,
                        status=JobStatus.QUEUED.value,
                        agent_revision=agent.current_revision,
                        thread_id_snapshot=conversation.codex_thread_id,
                        model=conversation.model,
                        reasoning_effort=conversation.reasoning_effort,
                    )
                    session.add(job)
                    await session.flush()
                    additions: list[ConversationCompanionView] = []
                    if request.companions:
                        if self.companion_service is None:
                            raise RuntimeError("companion data is not configured")
                        additions = await self.companion_service.bind_ready_stages(
                            session=session,
                            conversation_key=conversation_key,
                            job_id=job.id,
                            sequence=sequence,
                            bindings=request.companions,
                        )
                    await self._add_event(session, job, "job.queued", {})
                    result = _prompt_accepted(job, additions)
            except IntegrityError as exc:
                # PostgreSQL advisory locks prevent this in normal operation,
                # but retain a deterministic API outcome for SQLite and any
                # cross-version deployment that races a new key/idempotency row.
                if request.idempotency_key:
                    async with self.session_factory() as session:
                        existing = await session.scalar(
                            select(JobRecord).where(
                                JobRecord.agent_id == request.agent_id,
                                JobRecord.idempotency_key == request.idempotency_key,
                            )
                        )
                    if existing is not None:
                        additions = await self._companion_additions(existing.id)
                        return _prompt_accepted(existing, additions)
                raise ConversationConflictError(
                    "conversation was modified by another request"
                ) from exc
        await self._publish(result.job_id, conversation_key, JobStatus.QUEUED)
        return result

    async def get(self, job_id: str) -> JobView:
        async with self.session_factory() as session:
            record = await session.get(JobRecord, job_id)
            if record is None:
                raise JobNotFoundError(job_id)
            additions = await self._companion_additions(job_id, session=session)
            return _job_view(record, additions)

    async def list(
        self,
        *,
        conversation_key: str | None = None,
        status: JobStatus | None = None,
        limit: int = 100,
    ) -> list[JobView]:
        async with self.session_factory() as session:
            statement = select(JobRecord).order_by(JobRecord.created_at.desc()).limit(limit)
            if conversation_key:
                statement = statement.where(JobRecord.conversation_key == conversation_key)
            if status:
                statement = statement.where(JobRecord.status == status.value)
            records = list((await session.scalars(statement)).all())
            return [
                _job_view(
                    item,
                    await self._companion_additions(item.id, session=session),
                )
                for item in records
            ]

    async def claim_next(self) -> JobExecution | None:
        """Atomically claim the oldest turn whose conversation predecessor is done."""

        async with self.session_factory() as session, session.begin():
            candidates = (
                await session.scalars(
                    select(JobRecord)
                    .where(JobRecord.status == JobStatus.QUEUED.value)
                    .order_by(JobRecord.created_at, JobRecord.id)
                    .limit(100)
                )
            ).all()
            for candidate in candidates:
                prior_active = await session.scalar(
                    select(func.count())
                    .select_from(JobRecord)
                    .where(
                        JobRecord.conversation_key == candidate.conversation_key,
                        JobRecord.sequence < candidate.sequence,
                        JobRecord.status.in_([item.value for item in ACTIVE_STATUSES]),
                    )
                )
                if not prior_active:
                    result = await session.execute(
                        update(JobRecord)
                        .where(
                            JobRecord.id == candidate.id,
                            JobRecord.status == JobStatus.QUEUED.value,
                        )
                        .values(status=JobStatus.PROVISIONING.value)
                    )
                    if not result.rowcount:
                        continue
                    candidate.status = JobStatus.PROVISIONING.value
                    conversation = await session.get(ConversationRecord, candidate.conversation_key)
                    if conversation is None:
                        continue
                    # Resolve continuation only when the turn becomes runnable;
                    # its predecessor may have established the thread after this
                    # job was originally queued.
                    candidate.thread_id_snapshot = conversation.codex_thread_id
                    await self._add_event(session, candidate, "job.provisioning", {})
                    execution = JobExecution(
                        id=candidate.id,
                        agent_id=candidate.agent_id,
                        conversation_key=candidate.conversation_key,
                        sequence=candidate.sequence,
                        prompt=candidate.prompt,
                        agent_revision=candidate.agent_revision,
                        thread_id=conversation.codex_thread_id,
                        model=candidate.model,
                        reasoning_effort=(
                            ReasoningEffort(candidate.reasoning_effort)
                            if candidate.reasoning_effort
                            else None
                        ),
                    )
                    break
            else:
                return None
        await self._publish(execution.id, execution.conversation_key, JobStatus.PROVISIONING)
        return execution

    async def transition(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        usage: dict[str, int] | None = None,
        runtime_metadata: dict[str, Any] | None = None,
    ) -> JobView:
        async with self.session_factory() as session, session.begin():
            record = await session.scalar(
                select(JobRecord).where(JobRecord.id == job_id).with_for_update()
            )
            if record is None:
                raise JobNotFoundError(job_id)
            current = JobStatus(record.status)
            if current != status:
                allowed = TRANSITIONS.get(current, set())
                if status not in allowed:
                    raise InvalidTransitionError(f"cannot transition {current} to {status}")
                record.status = status.value
            now = datetime.now(UTC)
            if status is JobStatus.RUNNING and record.started_at is None:
                record.started_at = now
            if status.terminal:
                record.completed_at = now
                conversation = await session.get(ConversationRecord, record.conversation_key)
                if conversation is not None:
                    conversation.updated_at = now
                    conversation.agent_revision = record.agent_revision
            if result is not None:
                record.result = result
            if error is not None:
                record.error = error
            if usage is not None:
                record.usage = usage
            if runtime_metadata is not None:
                record.runtime_metadata = runtime_metadata
            await self._add_event(
                session,
                record,
                f"job.{status.value}",
                {"error": error} if error else {},
            )
            await session.flush()
            additions = await self._companion_additions(job_id, session=session)
            view = _job_view(record, additions)
        await self._publish(job_id, view.conversation_key, status)
        return view

    async def store_thread_id(self, job_id: str, thread_id: str) -> None:
        async with self.session_factory() as session, session.begin():
            job = await session.scalar(
                select(JobRecord).where(JobRecord.id == job_id).with_for_update()
            )
            if job is None:
                raise JobNotFoundError(job_id)
            conversation = await session.get(ConversationRecord, job.conversation_key)
            if conversation is None:
                raise ConversationConflictError("conversation disappeared")
            if conversation.codex_thread_id not in (None, thread_id):
                raise ConversationConflictError("Codex returned a different thread id")
            conversation.codex_thread_id = thread_id
            job.thread_id_snapshot = thread_id
            await self._add_event(session, job, "codex.thread", {"thread_id": thread_id})

    async def cancel(self, job_id: str) -> JobView:
        async with self.session_factory() as session, session.begin():
            record = await session.scalar(
                select(JobRecord).where(JobRecord.id == job_id).with_for_update()
            )
            if record is None:
                raise JobNotFoundError(job_id)
            status = JobStatus(record.status)
            if status.terminal:
                additions = await self._companion_additions(job_id, session=session)
                return _job_view(record, additions)
            if status is JobStatus.QUEUED:
                record.status = JobStatus.CANCELLED.value
                record.completed_at = datetime.now(UTC)
                await self._add_event(session, record, "job.cancelled", {})
            else:
                record.cancel_requested = True
                await self._add_event(session, record, "job.cancel_requested", {})
            additions = await self._companion_additions(job_id, session=session)
            view = _job_view(record, additions)
        await self._publish(job_id, view.conversation_key, JobStatus(view.status))
        return view

    async def cancellation_requested(self, job_id: str) -> bool:
        async with self.session_factory() as session:
            value = await session.scalar(
                select(JobRecord.cancel_requested).where(JobRecord.id == job_id)
            )
            return bool(value)

    async def archive_conversation(self, conversation_key: str) -> None:
        async with self.session_factory() as session, session.begin():
            conversation = await lock_conversation(session, conversation_key)
            if conversation is None:
                raise ConversationNotFoundError(conversation_key)
            if conversation.status == ConversationStatus.DELETED.value:
                raise ConversationConflictError("conversation is being deleted")
            active = await session.scalar(
                select(func.count())
                .select_from(JobRecord)
                .where(
                    JobRecord.conversation_key == conversation_key,
                    JobRecord.status.in_([item.value for item in ACTIVE_STATUSES]),
                )
            )
            if active:
                raise ConversationConflictError("conversation has active jobs")
            conversation.status = ConversationStatus.ARCHIVED.value
            conversation.updated_at = datetime.now(UTC)

    async def delete_conversation(self, conversation_key: str) -> list[str]:
        async with self.session_factory() as session, session.begin():
            conversation = await lock_conversation(session, conversation_key)
            if conversation is None:
                raise ConversationNotFoundError(conversation_key)
            active = await session.scalar(
                select(func.count())
                .select_from(JobRecord)
                .where(
                    JobRecord.conversation_key == conversation_key,
                    JobRecord.status.in_([item.value for item in ACTIVE_STATUSES]),
                )
            )
            if active:
                raise ConversationConflictError("conversation has active jobs")
            job_ids = list(
                await session.scalars(
                    select(JobRecord.id).where(JobRecord.conversation_key == conversation_key)
                )
            )
            # Commit a tombstone before touching the filesystem. A submitter
            # waiting on this row will observe the non-active state instead of
            # recreating the key and having its new workspace removed (ABA).
            conversation.status = ConversationStatus.DELETED.value
            conversation.updated_at = datetime.now(UTC)
        self.workspaces.remove_conversation(conversation_key)
        async with self.session_factory() as session, session.begin():
            conversation = await lock_conversation(session, conversation_key)
            if conversation is not None and conversation.status == ConversationStatus.DELETED.value:
                stage_ids = list(
                    await session.scalars(
                        select(ConversationCompanionRecord.stage_id).where(
                            ConversationCompanionRecord.conversation_key == conversation_key
                        )
                    )
                )
                await session.delete(conversation)
                await session.flush()
                if stage_ids:
                    await session.execute(
                        delete(CompanionStageRecord).where(CompanionStageRecord.id.in_(stage_ids))
                    )
        return job_ids

    async def recover_interrupted(self) -> list[str]:
        recovered: list[str] = []
        async with self.session_factory() as session, session.begin():
            records = (
                await session.scalars(
                    select(JobRecord).where(
                        JobRecord.status.in_(
                            [
                                JobStatus.PROVISIONING.value,
                                JobStatus.WAITING_FOR_LEASE.value,
                                JobStatus.RUNNING.value,
                                JobStatus.COLLECTING.value,
                            ]
                        )
                    )
                )
            ).all()
            for record in records:
                record.status = JobStatus.INTERRUPTED.value
                record.error = "router restarted while the turn was active"
                record.completed_at = datetime.now(UTC)
                await self._add_event(session, record, "job.interrupted", {"recovered": True})
                conversation = await session.get(ConversationRecord, record.conversation_key)
                if conversation is not None:
                    conversation.updated_at = record.completed_at
                recovered.append(record.id)
        return recovered

    async def _add_event(
        self,
        session: AsyncSession,
        job: JobRecord,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        sequence = (
            int(
                await session.scalar(
                    select(func.coalesce(func.max(JobEventRecord.sequence), 0)).where(
                        JobEventRecord.job_id == job.id
                    )
                )
                or 0
            )
            + 1
        )
        session.add(
            JobEventRecord(
                job_id=job.id,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
            )
        )

    async def _companion_additions(
        self,
        job_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> list[ConversationCompanionView]:
        if self.companion_service is None:
            return []
        return await self.companion_service.additions_for_job(job_id, session=session)

    async def _publish(self, job_id: str, conversation_key: str, status: JobStatus) -> None:
        try:
            await self.cache.publish(
                self.activity_channel,
                {
                    "type": "job.status",
                    "job_id": job_id,
                    "conversation_key": conversation_key,
                    "status": status.value,
                },
            )
        except Exception:  # noqa: BLE001 - SQL state is authoritative.
            logger.warning("activity publication failed; durable job state was retained")
