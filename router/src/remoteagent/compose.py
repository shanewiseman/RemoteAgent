from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .compose_contract import ComposeContract, ComposeContractError, validate_compose_document
from .environment import safe_compose_environment
from .schemas import AgentDefinition


class ComposeValidationError(ValueError):
    pass


class ComposeProjectValidator:
    """Ask Docker Compose to resolve the project and enumerate real services."""

    def __init__(self, binary: str, state_root: Path, *, wait_timeout_seconds: int = 120) -> None:
        self.binary = binary
        self.state_root = state_root
        self.wait_timeout_seconds = wait_timeout_seconds

    async def validate(self, definition: AgentDefinition) -> None:
        expected_project = f"remoteagent-{definition.id}"
        if definition.project_name != expected_project:
            raise ComposeValidationError(f"Compose project name must be {expected_project!r}")
        placeholder = self.state_root / "compose-validation" / definition.id
        environment = safe_compose_environment(definition.environment)
        environment.update(
            {
                "REMOTEAGENT_CONVERSATION_KEY": "validation",
                "REMOTEAGENT_JOB_ID": "validation",
                "REMOTEAGENT_WORKSPACE_PATH": str(placeholder / "workspace"),
                "REMOTEAGENT_SESSIONS_PATH": str(placeholder / "sessions"),
                "REMOTEAGENT_ARTIFACTS_PATH": str(placeholder / "artifacts"),
            }
        )
        process = await asyncio.create_subprocess_exec(
            self.binary,
            "compose",
            "-f",
            str(definition.compose_file),
            "-p",
            expected_project,
            "--profile",
            "*",
            "config",
            "--format",
            "json",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            detail = (stderr or stdout).decode("utf-8", errors="replace")[-8_000:]
            raise ComposeValidationError(f"invalid Compose project: {detail}")
        try:
            document = json.loads(stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComposeValidationError(
                "Docker Compose did not return a valid JSON project model"
            ) from exc
        services = document.get("services") if isinstance(document, dict) else None
        if not isinstance(services, dict):
            raise ComposeValidationError("Compose project has no services model")
        contract = ComposeContract(
            agent_id=definition.id,
            project_name=expected_project,
            runner_service=definition.runner_service,
            dependency_services=tuple(definition.dependency_services),
            workspace_path=str(placeholder / "workspace"),
            sessions_path=str(placeholder / "sessions"),
            artifacts_path=str(placeholder / "artifacts"),
            version=environment.get("REMOTEAGENT_VERSION", "local"),
            uid=environment.get("REMOTEAGENT_UID", "1000"),
            gid=environment.get("REMOTEAGENT_GID", "1000"),
            auth_volume=environment.get("REMOTEAGENT_AUTH_VOLUME", "remoteagent-codex-auth"),
            skills_volume=environment.get(
                "REMOTEAGENT_SKILLS_VOLUME", "remoteagent-common-skills"
            ),
            instance_id=environment.get("REMOTEAGENT_INSTANCE_ID", "remoteagent"),
            cpu_limit=environment.get("REMOTEAGENT_AGENT_CPU_LIMIT", "2.0"),
            memory_limit=environment.get("REMOTEAGENT_AGENT_MEMORY_LIMIT", "2g"),
            wait_timeout_seconds=self.wait_timeout_seconds,
        )
        try:
            validate_compose_document(document, contract)
        except ComposeContractError as exc:
            raise ComposeValidationError(str(exc)) from exc

    @staticmethod
    def validate_services(definition: AgentDefinition, services: dict[str, Any]) -> None:
        """Compatibility helper for dependency-health unit tests.

        Full registration validation is performed by ``validate_compose_document``.
        """

        required = {definition.runner_service, *definition.dependency_services}
        missing = required - set(services)
        if missing:
            raise ComposeValidationError(
                f"Compose project is missing services: {', '.join(sorted(missing))}"
            )
        unhealthy: list[str] = []
        for name in definition.dependency_services:
            service = services.get(name)
            healthcheck = service.get("healthcheck") if isinstance(service, dict) else None
            test = healthcheck.get("test") if isinstance(healthcheck, dict) else None
            disabled = bool(healthcheck.get("disable")) if isinstance(healthcheck, dict) else True
            if disabled or not isinstance(test, list) or not test or str(test[0]).upper() == "NONE":
                unhealthy.append(name)
        if unhealthy:
            raise ComposeValidationError(
                "dependency services require enabled healthchecks: " + ", ".join(sorted(unhealthy))
            )
