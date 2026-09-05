from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta

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
            try:
                await self.artifact_service.reconcile_orphans(
                    grace_seconds=self.settings.artifact_orphan_grace_seconds
                )
            except Exception:  # noqa: BLE001 - the scheduled pass will retry.
                logger.warning("startup artifact reconciliation failed; it will be retried")
            self._task = asyncio.create_task(self._loop(), name="retention")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run_once(self) -> dict[str, int]:
        try:
            await self.artifact_service.reconcile_orphans(
                grace_seconds=self.settings.artifact_orphan_grace_seconds
            )
        except Exception:  # noqa: BLE001 - durable retention must still make progress.
            logger.warning("artifact reconciliation failed; it will be retried")
        now = datetime.now(UTC)
        artifact_cutoff = now - timedelta(seconds=self.settings.artifact_retention_seconds)
        job_cutoff = now - timedelta(seconds=self.settings.job_retention_seconds)
        conversation_cutoff = now - timedelta(seconds=self.settings.conversation_retention_seconds)
        removed_artifacts = 0
        removed_jobs = 0
        removed_conversations = 0
        artifact_paths: list[tuple[str, str]] = []
        old_job_ids: list[str] = []
        conversation_keys: list[str] = []

        async with self.session_factory() as session, session.begin():
            artifacts = (
                await session.scalars(
                    select(ArtifactRecord).where(ArtifactRecord.created_at < artifact_cutoff)
                )
            ).all()
            for artifact in artifacts:
                canonical = self.artifact_service.canonical_storage_path(
                    job_id=artifact.job_id,
                    relative_path=artifact.relative_path,
                    storage_path=artifact.storage_path,
                )
                if canonical is None:
                    continue
                artifact_paths.append((artifact.job_id, canonical[1]))
                await session.delete(artifact)
                removed_artifacts += 1
            if artifact_paths:
                # Flush explicit artifact deletions before bulk-deleting an old
                # parent job. Otherwise the database cascade can remove the
                # same rows first and leave the ORM reporting a stale delete.
                await session.flush()

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
            terminal = [status.value for status in JobStatus if status.terminal]
            old_jobs = select(JobRecord.id).where(
                JobRecord.status.in_(terminal),
                JobRecord.completed_at.is_not(None),
                JobRecord.completed_at < job_cutoff,
            )
            if conversation_keys:
                # Keep these job rows until conversation filesystem cleanup
                # succeeds; deleting them now would lose the retry inventory and
                # allow the conversation tombstone to disappear prematurely.
                old_jobs = old_jobs.where(
                    JobRecord.conversation_key.not_in(conversation_keys)
                )
            old_job_ids = (await session.scalars(old_jobs)).all()
            if old_job_ids:
                await session.execute(delete(JobRecord).where(JobRecord.id.in_(old_job_ids)))
                removed_jobs = len(old_job_ids)

        # External filesystem effects happen only after their corresponding DB
        # intent commits. Failures therefore cannot roll back durable state or
        # make another transaction observe a half-deleted conversation.
        for job_id, relative_path in artifact_paths:
            try:
                await self.artifact_service.delete_artifact_file(
                    job_id=job_id, relative_path=relative_path
                )
            except Exception:  # noqa: BLE001 - the reconciler retries this path.
                logger.warning(
                    "could not remove expired artifact file %s/%s",
                    job_id,
                    relative_path,
                )
        for job_id in old_job_ids:
            try:
                await self.artifact_service.delete_storage(job_id)
            except Exception:  # noqa: BLE001 - isolate paths and retry via reconciliation.
                logger.warning(
                    "could not remove retained job artifact storage; retry pending: %s",
                    job_id,
                )
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
            cleanup_succeeded = True
            try:
                target.relative_to(conversation_root)
                if target.exists():
                    shutil.rmtree(target)
            except (OSError, ValueError):
                cleanup_succeeded = False
                logger.warning(
                    "conversation workspace cleanup failed; tombstone retained for retry: %s",
                    conversation_key,
                )
            for job_id in job_ids:
                try:
                    await self.artifact_service.delete_storage(job_id)
                except Exception:  # noqa: BLE001 - isolate job paths and retry the tombstone.
                    cleanup_succeeded = False
                    logger.warning(
                        "conversation artifact cleanup failed; tombstone retained for retry: %s/%s",
                        conversation_key,
                        job_id,
                    )
            if not cleanup_succeeded:
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
