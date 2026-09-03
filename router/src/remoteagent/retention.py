from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .artifacts import ArtifactService
from .config import Settings
from .conversation_lock import lock_conversation
from .models import (
    ArtifactRecord,
    CompanionStageRecord,
    ConversationCompanionRecord,
    ConversationRecord,
    JobRecord,
)
from .schemas import ConversationStatus, JobStatus

logger = logging.getLogger(__name__)


class RetentionWorker:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_service: ArtifactService,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.artifact_service = artifact_service
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="retention")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run_once(self) -> dict[str, int]:
        now = datetime.now(UTC)
        artifact_cutoff = now - timedelta(seconds=self.settings.artifact_retention_seconds)
        job_cutoff = now - timedelta(seconds=self.settings.job_retention_seconds)
        conversation_cutoff = now - timedelta(seconds=self.settings.conversation_retention_seconds)
        removed_artifacts = 0
        removed_jobs = 0
        removed_conversations = 0
        artifact_paths: list[Path] = []
        old_job_ids: list[str] = []
        conversation_keys: list[str] = []

        async with self.session_factory() as session, session.begin():
            artifacts = (
                await session.scalars(
                    select(ArtifactRecord).where(ArtifactRecord.created_at < artifact_cutoff)
                )
            ).all()
            for artifact in artifacts:
                path = Path(artifact.storage_path).resolve()
                try:
                    path.relative_to(self.artifact_service.store_root)
                except ValueError:
                    continue
                artifact_paths.append(path)
                await session.delete(artifact)
                removed_artifacts += 1

            terminal = [status.value for status in JobStatus if status.terminal]
            old_job_ids = (
                await session.scalars(
                    select(JobRecord.id).where(
                        JobRecord.status.in_(terminal),
                        JobRecord.completed_at.is_not(None),
                        JobRecord.completed_at < job_cutoff,
                    )
                )
            ).all()
            if old_job_ids:
                await session.execute(delete(JobRecord).where(JobRecord.id.in_(old_job_ids)))
                removed_jobs = len(old_job_ids)
            active_values = [status.value for status in JobStatus if not status.terminal]
            conversation_keys = list(
                await session.scalars(
                    select(ConversationRecord.key).where(
                        or_(
                            ConversationRecord.status == ConversationStatus.DELETED.value,
                            and_(
                                ConversationRecord.updated_at < conversation_cutoff,
                                ~exists().where(
                                    JobRecord.conversation_key == ConversationRecord.key,
                                    JobRecord.status.in_(active_values),
                                ),
                            ),
                        ),
                    )
                )
            )

        # External filesystem effects happen only after their corresponding DB
        # intent commits. Failures therefore cannot roll back durable state or
        # make another transaction observe a half-deleted conversation.
        for path in artifact_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("could not remove expired artifact file %s", path)
        for job_id in old_job_ids:
            await self.artifact_service.delete_storage(job_id)
        for conversation_key in conversation_keys:
            job_ids: list[str] = []
            async with self.session_factory() as session, session.begin():
                conversation = await lock_conversation(session, conversation_key)
                if conversation is None:
                    continue
                if conversation.status != ConversationStatus.DELETED.value:
                    still_expired = await session.scalar(
                        select(func.count())
                        .select_from(ConversationRecord)
                        .where(
                            ConversationRecord.key == conversation_key,
                            ConversationRecord.updated_at < conversation_cutoff,
                        )
                    )
                    active = await session.scalar(
                        select(func.count())
                        .select_from(JobRecord)
                        .where(
                            JobRecord.conversation_key == conversation_key,
                            JobRecord.status.in_(active_values),
                        )
                    )
                    if not still_expired or active:
                        continue
                    conversation.status = ConversationStatus.DELETED.value
                    conversation.updated_at = now
                job_ids = list(
                    await session.scalars(
                        select(JobRecord.id).where(JobRecord.conversation_key == conversation_key)
                    )
                )

            conversation_root = (self.settings.data_dir / "conversations").resolve()
            target = (conversation_root / conversation_key).resolve()
            try:
                target.relative_to(conversation_root)
                if target.exists():
                    shutil.rmtree(target)
                for job_id in job_ids:
                    await self.artifact_service.delete_storage(job_id)
            except (OSError, ValueError):
                logger.warning(
                    "conversation cleanup failed; tombstone retained for retry: %s",
                    conversation_key,
                )
                continue

            async with self.session_factory() as session, session.begin():
                conversation = await lock_conversation(session, conversation_key)
                if (
                    conversation is not None
                    and conversation.status == ConversationStatus.DELETED.value
                ):
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
                            delete(CompanionStageRecord).where(
                                CompanionStageRecord.id.in_(stage_ids)
                            )
                        )
                    removed_conversations += 1
        return {
            "artifacts": removed_artifacts,
            "jobs": removed_jobs,
            "conversations": removed_conversations,
        }

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - retention retries on the next interval.
                logger.warning("retention pass failed; it will be retried")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.settings.retention_interval_seconds
                )
            except TimeoutError:
                pass
