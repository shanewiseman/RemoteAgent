from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tomllib
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .environment import safe_compose_environment
from .schemas import AgentDefinition, ReasoningEffort, UsageTotals
from .telemetry import TokenTelemetryCollector, TokenUsageRecord
from .workspace import ConversationPaths

logger = logging.getLogger(__name__)

EventCallback = Callable[[Mapping[str, Any]], Awaitable[None]]
CancellationCheck = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    job_id: str
    conversation_key: str
    prompt: str
    thread_id: str | None
    definition: AgentDefinition
    paths: ConversationPaths
    output_path: Path
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None


@dataclass(slots=True)
class RuntimeResult:
    response: str
    thread_id: str | None
    usage: UsageTotals | None
    token_record: TokenUsageRecord
    metadata: dict[str, Any] = field(default_factory=dict)


class RuntimeExecutionError(RuntimeError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.metadata = metadata or {}


class AgentRuntime(Protocol):
    async def provision(self, request: RuntimeRequest) -> None: ...

    async def release(self, request: RuntimeRequest) -> None: ...

    async def close(self) -> None: ...

    async def recover(self, job_ids: list[str]) -> None: ...

    async def run(
        self,
        request: RuntimeRequest,
        *,
        on_event: EventCallback,
        cancelled: CancellationCheck,
    ) -> RuntimeResult: ...


class CodexJSONLParser:
    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.last_message: str | None = None
        self.event_counts: Counter[str] = Counter()
        self.telemetry = TokenTelemetryCollector()

    def observe(self, event: Mapping[str, Any]) -> None:
        event_type = str(event.get("type", "unknown"))
        self.event_counts[event_type] += 1
        if event_type in {"thread.started", "thread_started"}:
            value = event.get("thread_id") or event.get("threadId")
            if isinstance(value, str):
                self.thread_id = value
        if event_type in {"item.completed", "item_completed"}:
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") in {
                "agent_message",
                "assistant_message",
            }:
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    self.last_message = text
        self.telemetry.observe_event(event)


@dataclass(slots=True)
class _DependencyState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    references: int = 0
    stop_task: asyncio.Task[None] | None = None
    last_request: RuntimeRequest | None = None


class DockerComposeRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._dependencies: dict[str, _DependencyState] = {}

    def _environment(self, request: RuntimeRequest) -> dict[str, str]:
        environment = safe_compose_environment(request.definition.environment)
        if self.settings.auth_mode == "chatgpt":
            # Subscription mode must not accidentally switch providers because
            # the router host happens to carry unrelated API credentials.
            for name in ("OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT"):
                environment.pop(name, None)
        environment.update(
            {
                "REMOTEAGENT_WORKSPACE_PATH": str(request.paths.workspace),
                "REMOTEAGENT_SESSIONS_PATH": str(request.paths.sessions),
                "REMOTEAGENT_ARTIFACTS_PATH": str(request.paths.artifacts),
                "REMOTEAGENT_JOB_ID": request.job_id,
                "REMOTEAGENT_CONVERSATION_KEY": request.conversation_key,
            }
        )
        return environment

    def _compose(self, request: RuntimeRequest) -> list[str]:
        return [
            self.settings.compose_binary,
            "compose",
            "-f",
            str(request.definition.compose_file),
            "-p",
            str(request.definition.project_name),
        ]

    async def provision(self, request: RuntimeRequest) -> None:
        services = list(request.definition.dependency_services)
        if not services:
            return
        key = str(request.definition.project_name)
        state = self._dependencies.setdefault(key, _DependencyState())
        async with state.lock:
            if state.stop_task is not None:
                state.stop_task.cancel()
                await asyncio.gather(state.stop_task, return_exceptions=True)
                state.stop_task = None
            # Record cleanup provenance before Compose can create or start a
            # dependency. Provisioning may be cancelled after Docker has
            # applied side effects but before the command reports success; the
            # scheduler's unconditional release and router shutdown still need
            # the exact request required to stop those services.
            state.last_request = request
            argv = self._compose(request) + [
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                str(self.settings.compose_wait_timeout_seconds),
                *services,
            ]
            await self._checked_compose(argv, request, "dependency provisioning")
            state.references += 1

    async def release(self, request: RuntimeRequest) -> None:
        services = list(request.definition.dependency_services)
        if not services:
            return
        key = str(request.definition.project_name)
        state = self._dependencies.get(key)
        if state is None:
            return
        async with state.lock:
            state.references = max(0, state.references - 1)
            if state.references == 0 and state.stop_task is None:
                state.stop_task = asyncio.create_task(
                    self._stop_when_cold(request, state), name=f"compose-warm-stop:{key}"
                )

    async def _stop_when_cold(self, request: RuntimeRequest, state: _DependencyState) -> None:
        try:
            await asyncio.sleep(self.settings.dependency_warm_seconds)
            async with state.lock:
                if state.references:
                    return
                argv = self._compose(request) + [
                    "stop",
                    "--timeout",
                    str(self.settings.compose_stop_timeout_seconds),
                    *request.definition.dependency_services,
                ]
                await self._checked_compose(argv, request, "dependency warm stop")
        except RuntimeExecutionError:
            # Warm-stop failure is non-fatal to the completed job. A later
            # provision performs an idempotent `up --wait` reconciliation.
            pass
        finally:
            if state.stop_task is asyncio.current_task():
                state.stop_task = None

    async def _checked_compose(
        self, argv: list[str], request: RuntimeRequest, operation: str
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=self._environment(request),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, _stderr = await process.communicate()
        except BaseException:
            deadline = (
                asyncio.get_running_loop().time() + self.settings.job_cleanup_timeout_seconds
            )
            reaped = await self._terminate_and_reap(process, deadline=deadline)
            if not reaped:
                logger.error("%s cleanup exceeded the configured cleanup budget", operation)
            raise
        if process.returncode:
            raise RuntimeExecutionError(f"{operation} failed with exit code {process.returncode}")

    async def close(self) -> None:
        tasks = [
            state.stop_task for state in self._dependencies.values() if state.stop_task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for state in self._dependencies.values():
            request = state.last_request
            if request is None or not request.definition.dependency_services:
                continue
            async with state.lock:
                argv = self._compose(request) + [
                    "stop",
                    "--timeout",
                    str(self.settings.compose_stop_timeout_seconds),
                    *request.definition.dependency_services,
                ]
                try:
                    await self._checked_compose(argv, request, "dependency shutdown")
                except RuntimeExecutionError:
                    pass
                state.references = 0

    async def recover(self, job_ids: list[str]) -> None:
        for job_id in job_ids:
            await self._remove_worker_container(f"remoteagent-{job_id.replace('_', '-')}")

    def codex_argv(self, request: RuntimeRequest) -> list[str]:
        container_output = (
            f"{self.settings.workspace_container_path}/.remoteagent/jobs/{request.job_id}/final.txt"
        )
        configured = tomllib.loads(request.definition.config_toml or "")
        configured_sandbox = configured.get("sandbox_mode")
        order = {
            "read-only": 0,
            "workspace-write": 1,
            "danger-full-access": 2,
        }
        effective_sandbox = self.settings.codex_sandbox
        if configured_sandbox in order and order[configured_sandbox] < order[effective_sandbox]:
            effective_sandbox = configured_sandbox
        common = [
            "--json",
            "--output-last-message",
            container_output,
            "-c",
            f'sandbox_mode="{effective_sandbox}"',
            "-c",
            'approval_policy="never"',
        ]
        workspace_write = configured.get("sandbox_workspace_write")
        network_enabled = (
            isinstance(workspace_write, dict) and workspace_write.get("network_access") is True
        )
        effective_network = effective_sandbox == "workspace-write" and network_enabled
        # Codex 0.149.1 did not reliably materialize the static nested config
        # value for `codex exec`. Continue to repeat the already validated,
        # immutable revision value as an explicit one-run Boolean in both
        # directions so enforcement is independent of static materialization.
        common.extend(
            [
                "-c",
                "sandbox_workspace_write.network_access="
                + ("true" if effective_network else "false"),
            ]
        )
        if request.model is not None:
            common.extend(["--model", request.model])
        if request.reasoning_effort is not None:
            common.extend(["-c", f'model_reasoning_effort="{request.reasoning_effort.value}"'])
        if request.thread_id:
            # Never use --last: sessions share an auth volume and resume must be
            # bound to the durable conversation's exact Codex thread UUID.
            return ["codex", "exec", "resume", *common, request.thread_id, "-"]
        return [
            "codex",
            "exec",
            *common,
            "--color",
            "never",
            "--cd",
            self.settings.workspace_container_path,
            "-",
        ]

    async def run(
        self,
        request: RuntimeRequest,
        *,
        on_event: EventCallback,
        cancelled: CancellationCheck,
    ) -> RuntimeResult:
        container_name = f"remoteagent-{request.job_id.replace('_', '-')}"
        argv = self._compose(request) + [
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--name",
            container_name,
            "--label",
            f"remoteagent.job_id={request.job_id}",
            "--volume",
            (
                f"{request.paths.control / 'AGENTS.md'}:"
                f"{self.settings.workspace_container_path}/AGENTS.md:ro"
            ),
            "--volume",
            (
                f"{request.paths.control / 'config.toml'}:"
                f"{self.settings.codex_home_container_path}/config.toml:ro"
            ),
            "-e",
            "REMOTEAGENT_JOB_ID",
            "-e",
            "REMOTEAGENT_CONVERSATION_KEY",
            request.definition.runner_service,
            *self.codex_argv(request),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                env=self._environment(request),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=4 * 1024 * 1024,
            )
        except BaseException:
            await self._remove_worker_container(container_name)
            raise
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        parser = CodexJSONLParser()
        malformed = 0

        async def write_stdin() -> None:
            try:
                process.stdin.write(request.prompt.encode("utf-8"))
                await process.stdin.drain()
            finally:
                process.stdin.close()

        async def read_stdout() -> None:
            nonlocal malformed
            async for raw_line in process.stdout:
                if not raw_line.strip():
                    continue
                try:
                    event = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    malformed += 1
                    continue
                if not isinstance(event, Mapping):
                    malformed += 1
                    continue
                parser.observe(event)
                await on_event(event)

        async def read_stderr() -> bytes:
            chunks = bytearray()
            while chunk := await process.stderr.read(65_536):
                chunks.extend(chunk)
                if len(chunks) > 1_000_000:
                    del chunks[:-1_000_000]
            return bytes(chunks)

        stdin_task = asyncio.create_task(write_stdin(), name=f"runtime-stdin:{request.job_id}")
        stdout_task = asyncio.create_task(read_stdout(), name=f"runtime-stdout:{request.job_id}")
        stderr_task = asyncio.create_task(read_stderr(), name=f"runtime-stderr:{request.job_id}")
        wait_task = asyncio.create_task(process.wait(), name=f"runtime-wait:{request.job_id}")
        process_cleanup_started = False

        async def cleanup_process() -> None:
            nonlocal process_cleanup_started
            if process_cleanup_started:
                return
            process_cleanup_started = True
            deadline = (
                asyncio.get_running_loop().time() + self.settings.job_cleanup_timeout_seconds
            )
            reader_cleanup = asyncio.create_task(
                self._cancel_and_gather(stdin_task, stdout_task, stderr_task),
                name=f"runtime-io-cleanup:{request.job_id}",
            )
            try:
                reaped = await self._terminate_and_reap(
                    process, wait_task=wait_task, deadline=deadline
                )
                readers_stopped = await self._await_cleanup_task(
                    reader_cleanup, deadline=deadline
                )
            except asyncio.CancelledError:
                reader_cleanup.cancel()
                reader_cleanup.add_done_callback(self._consume_background_task)
                raise
            if not reaped:
                logger.error(
                    "Codex process cleanup exceeded the configured cleanup budget for %s",
                    request.job_id,
                )
            if not readers_stopped:
                logger.error(
                    "Codex IO cleanup exceeded the configured cleanup budget for %s",
                    request.job_id,
                )

        try:
            # Shield the IO task so lifecycle cancellation reaches this owner
            # immediately even if a stream implementation suppresses cancellation.
            # The bounded cleanup path then cancels and gathers all three IO tasks.
            await asyncio.shield(stdin_task)
            while not wait_task.done():
                if await cancelled():
                    await cleanup_process()
                    raise asyncio.CancelledError
                await asyncio.sleep(0.25)
            return_code = await wait_task
            # A subprocess can exit before a blocked callback or pipe reader
            # finishes. Shield both readers so cancelling this owner is never
            # delegated to cancellation-resistant child code. The exception
            # path below explicitly cancels and gathers them under one bounded
            # cleanup deadline.
            await asyncio.shield(stdout_task)
            stderr = await asyncio.shield(stderr_task)
        except asyncio.CancelledError:
            await cleanup_process()
            await self._remove_worker_container(container_name)
            raise
        except BaseException:
            await cleanup_process()
            await self._remove_worker_container(container_name)
            raise
        await self._remove_worker_container(container_name)
        metadata = {
            "exit_code": return_code,
            "event_counts": dict(parser.event_counts),
            "malformed_jsonl_lines": malformed,
            # Never persist stderr: agent code can print credentials, private
            # context, or hidden model data there. Counts retain diagnostic
            # value without making the database a secret sink.
            "stderr_bytes": len(stderr),
        }
        if return_code:
            raise RuntimeExecutionError(
                f"Codex exited with status {return_code}",
                metadata=metadata,
            )
        response: str | None = None
        try:
            response = request.output_path.read_text(encoding="utf-8")
        except OSError:
            response = parser.last_message
        if response is None:
            raise RuntimeExecutionError(
                "Codex completed without a final response", metadata=metadata
            )
        token_record = parser.telemetry.finalize(
            prompt=request.prompt,
            response=response,
            system=request.definition.base_context,
        )
        usage = None
        if token_record.input_tokens is not None and token_record.output_tokens is not None:
            usage = UsageTotals(
                input_tokens=token_record.input_tokens,
                cached_input_tokens=token_record.cached_input_tokens or 0,
                output_tokens=token_record.output_tokens,
                reasoning_output_tokens=token_record.reasoning_output_tokens or 0,
            )
        return RuntimeResult(
            response=response,
            thread_id=parser.thread_id or request.thread_id,
            usage=usage,
            token_record=token_record,
            metadata=metadata,
        )

    async def _remove_worker_container(self, container_name: str) -> None:
        """Remove only the stable, exact container name assigned to this job."""

        process: asyncio.subprocess.Process | None = None
        wait_task: asyncio.Task[int] | None = None
        try:
            async with asyncio.timeout(self.settings.job_cleanup_timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    self.settings.compose_binary,
                    "rm",
                    "-f",
                    container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                wait_task = asyncio.create_task(
                    process.wait(), name=f"worker-container-remove:{container_name}"
                )
                await asyncio.shield(wait_task)
        except TimeoutError:
            self._stop_cleanup_process(process, wait_task)
            logger.warning(
                "worker container removal exceeded %.1f seconds for %s",
                self.settings.job_cleanup_timeout_seconds,
                container_name,
            )
        except asyncio.CancelledError:
            self._stop_cleanup_process(process, wait_task)
            raise
        except Exception:  # noqa: BLE001 - Docker may already have applied --rm.
            # Docker may already have honored Compose's --rm. Cleanup is best
            # effort here; the stable job label supports later reconciliation.
            logger.warning("could not confirm removal of worker container %s", container_name)

    @staticmethod
    def _stop_cleanup_process(
        process: asyncio.subprocess.Process | None,
        wait_task: asyncio.Task[int] | None,
    ) -> None:
        """Stop a stuck cleanup CLI while allowing its child watcher to reap it."""

        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            # The configured cleanup timeout is the complete budget. Once it is
            # exhausted there is no second grace period in which cancellation
            # can be hidden, so force the helper process down immediately.
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
        if wait_task is not None:
            wait_task.add_done_callback(DockerComposeRuntime._consume_background_task)

    async def _terminate_and_reap(
        self,
        process: asyncio.subprocess.Process,
        *,
        deadline: float,
        wait_task: asyncio.Task[int] | None = None,
    ) -> bool:
        """Terminate, then kill and reap a child within one cleanup deadline."""

        if wait_task is None:
            wait_task = asyncio.create_task(process.wait(), name="runtime-process-reap")
        try:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                # Reserve part of the single budget for post-KILL reaping rather
                # than granting TERM and KILL a fresh full timeout apiece.
                term_budget = min(10.0, remaining / 2)
                if term_budget:
                    try:
                        await asyncio.wait_for(asyncio.shield(wait_task), timeout=term_budget)
                    except TimeoutError:
                        pass
                if not wait_task.done() and process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
            if not wait_task.done():
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                if remaining:
                    try:
                        await asyncio.wait_for(asyncio.shield(wait_task), timeout=remaining)
                    except TimeoutError:
                        pass
            return wait_task.done()
        except asyncio.CancelledError:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            raise
        finally:
            if not wait_task.done():
                wait_task.add_done_callback(self._consume_background_task)

    async def _await_cleanup_task(self, task: asyncio.Task[Any], *, deadline: float) -> bool:
        if task.done():
            self._consume_background_task(task)
            return True
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if remaining:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except TimeoutError:
                pass
        if task.done():
            self._consume_background_task(task)
            return True
        task.cancel()
        task.add_done_callback(self._consume_background_task)
        return False

    @staticmethod
    async def _cancel_and_gather(*tasks: asyncio.Task[Any]) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _consume_background_task(task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()


class FakeRuntime:
    """Deterministic runtime used by unit tests and local contract exercises."""

    def __init__(self) -> None:
        self.requests: list[RuntimeRequest] = []
        self.provisioned: list[str] = []
        self.released: list[str] = []
        self.recovered: list[str] = []
        self.responses: asyncio.Queue[RuntimeResult | BaseException] = asyncio.Queue()

    def enqueue(self, response: RuntimeResult | BaseException) -> None:
        self.responses.put_nowait(response)

    async def provision(self, request: RuntimeRequest) -> None:
        self.provisioned.append(request.job_id)

    async def release(self, request: RuntimeRequest) -> None:
        self.released.append(request.job_id)

    async def close(self) -> None:
        return None

    async def recover(self, job_ids: list[str]) -> None:
        self.recovered.extend(job_ids)

    async def run(
        self,
        request: RuntimeRequest,
        *,
        on_event: EventCallback,
        cancelled: CancellationCheck,
    ) -> RuntimeResult:
        self.requests.append(request)
        if await cancelled():
            raise asyncio.CancelledError
        queued = await self.responses.get()
        if isinstance(queued, BaseException):
            raise queued
        if queued.thread_id:
            await on_event({"type": "thread.started", "thread_id": queued.thread_id})
        return queued
