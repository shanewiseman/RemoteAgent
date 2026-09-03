from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .agents import AgentService
from .artifacts import ArtifactService
from .config import Settings
from .jobs import JobExecution, JobService
from .lease import LeaseManager
from .runtime import AgentRuntime, RuntimeExecutionError, RuntimeRequest
from .schemas import JobStatus
from .telemetry import DashboardMetrics
from .workspace import WorkspaceManager

if TYPE_CHECKING:
    from .companions import CompanionService

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        *,
        agent_service: AgentService,
        job_service: JobService,
        artifact_service: ArtifactService,
        workspaces: WorkspaceManager,
        runtime: AgentRuntime,
        lease_manager: LeaseManager,
        telemetry: DashboardMetrics,
        companion_service: CompanionService | None = None,
    ) -> None:
        self.settings = settings
        self.agent_service = agent_service
        self.job_service = job_service
        self.artifact_service = artifact_service
        self.workspaces = workspaces
        self.runtime = runtime
        self.lease_manager = lease_manager
        self.telemetry = telemetry
        self.companion_service = companion_service
        self._workers: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    @property
    def running(self) -> bool:
        return bool(self._workers) and not self._stopping.is_set()

    async def start(self) -> None:
        if self._workers:
            return
        self._stopping.clear()
        for index in range(self.settings.scheduler_concurrency):
            self._workers.append(
                asyncio.create_task(self._worker(index), name=f"agent-scheduler-{index}")
            )

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def run_once(self) -> bool:
        execution = await self.job_service.claim_next()
        if execution is None:
            return False
        await self._execute(execution)
        return True

    async def _worker(self, _index: int) -> None:
        while not self._stopping.is_set():
            try:
                did_work = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one job must not kill the worker loop.
                # `_execute` normally records a failure itself. Keeping the
                # worker alive prevents one malformed job from stopping intake.
                logger.error("scheduler worker recovered from an unexpected job failure")
                did_work = True
            if not did_work:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=self.settings.scheduler_poll_seconds,
                    )
                except TimeoutError:
                    pass

    async def _execute(self, job: JobExecution) -> None:
        request: RuntimeRequest | None = None
        provisioned = False
        companion_metadata: dict[str, Any] | None = None
        try:
            definition = await self.agent_service.definition(job.agent_id, job.agent_revision)
            paths, output_path = self.workspaces.materialize_turn(
                job.conversation_key, job.id, definition
            )
            effective_prompt = job.prompt
            if self.companion_service is not None:
                active_companions = await self.companion_service.prepare_for_turn(
                    job.conversation_key, job.sequence
                )
                if active_companions:
                    effective_prompt = self.companion_service.build_prompt_preamble(
                        job.prompt, active_companions
                    )
                    companion_metadata = {
                        "companion_preamble_version": getattr(
                            self.companion_service, "preamble_version", 1
                        ),
                        "visible_companion_ids": [
                            item.id for item in sorted(active_companions, key=lambda item: item.name)
                        ],
                    }
            request = RuntimeRequest(
                job_id=job.id,
                conversation_key=job.conversation_key,
                prompt=effective_prompt,
                thread_id=job.thread_id,
                definition=definition,
                paths=paths,
                output_path=output_path,
                model=job.model,
                reasoning_effort=job.reasoning_effort,
            )
            baseline = self.artifact_service.snapshot(paths.artifacts)
            # Persist the exact companion view before provisioning/Codex so an
            # abrupt router restart still leaves an auditable execution snapshot.
            await self.job_service.transition(
                job.id,
                JobStatus.WAITING_FOR_LEASE,
                runtime_metadata=companion_metadata,
            )

            async def database_cancelled() -> bool:
                return await self.job_service.cancellation_requested(job.id)

            async with self.lease_manager.hold(
                self.settings.subscription_lease_name,
                job.id,
                cancelled=database_cancelled,
            ) as lease:
                if await database_cancelled():
                    raise asyncio.CancelledError
                # Subscription-backed Codex execution is globally serialized.
                # Provision shared per-agent dependencies only after acquiring
                # that lease so a second queued turn cannot reconfigure a
                # Compose sidecar while the current agent is using it.
                await self.runtime.provision(request)
                provisioned = True
                if await database_cancelled():
                    raise asyncio.CancelledError
                await self.job_service.transition(job.id, JobStatus.RUNNING)

                async def cancelled() -> bool:
                    return lease.lost.is_set() or await database_cancelled()

                async def on_event(event: Mapping[str, Any]) -> None:
                    if str(event.get("type")) in {"thread.started", "thread_started"}:
                        thread_id = event.get("thread_id") or event.get("threadId")
                        if isinstance(thread_id, str):
                            await self.job_service.store_thread_id(job.id, thread_id)

                async with asyncio.timeout(self.settings.job_timeout_seconds):
                    result = await self.runtime.run(request, on_event=on_event, cancelled=cancelled)
                lease.check()
                if result.thread_id is not None:
                    await self.job_service.store_thread_id(job.id, result.thread_id)
                elif job.thread_id is None:
                    raise RuntimeExecutionError("new Codex turn did not report a thread id")

            if await self.job_service.cancellation_requested(job.id):
                raise asyncio.CancelledError
            await self.job_service.transition(job.id, JobStatus.COLLECTING)
            artifacts = await self.artifact_service.ingest_changed(
                job_id=job.id,
                conversation_key=job.conversation_key,
                source_root=paths.artifacts,
                before=baseline,
            )
            metadata = dict(result.metadata)
            if companion_metadata is not None:
                metadata.update(companion_metadata)
            metadata["artifact_count"] = len(artifacts)
            metadata["token_usage"] = result.token_record.as_dict()
            self.telemetry.observe_token_usage(result.token_record)
            await self.job_service.transition(
                job.id,
                JobStatus.SUCCEEDED,
                result=result.response,
                usage=result.usage.model_dump() if result.usage else None,
                runtime_metadata=metadata,
            )
        except asyncio.CancelledError:
            requested = await self.job_service.cancellation_requested(job.id)
            current = await self.job_service.get(job.id)
            if not current.status.terminal:
                await self.job_service.transition(
                    job.id,
                    JobStatus.CANCELLED if requested else JobStatus.INTERRUPTED,
                    error=(
                        "cancellation requested"
                        if requested
                        else "router stopped or subscription lease was lost"
                    ),
                    runtime_metadata=companion_metadata,
                )
            if not requested:
                raise
        except TimeoutError:
            await self._record_failure(
                job.id,
                "Codex turn exceeded the configured timeout",
                runtime_metadata=companion_metadata,
            )
        except Exception as exc:  # noqa: BLE001 - convert runtime failures to durable state.
            await self._record_failure(
                job.id,
                str(exc),
                runtime_metadata=companion_metadata,
            )
        finally:
            if provisioned and request is not None:
                with contextlib.suppress(Exception):
                    await self.runtime.release(request)

    async def _record_failure(
        self,
        job_id: str,
        message: str,
        *,
        runtime_metadata: dict[str, Any] | None = None,
    ) -> None:
        current = await self.job_service.get(job_id)
        if current.status.terminal:
            return
        await self.job_service.transition(
            job_id,
            JobStatus.FAILED,
            error=message[:16_000],
            runtime_metadata=runtime_metadata,
        )
