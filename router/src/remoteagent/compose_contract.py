from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class ComposeContractError(ValueError):
    """A resolved Compose project violates the RemoteAgent runner contract."""


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|ms|s|m|h)")
_MEMORY = re.compile(r"(\d+(?:\.\d+)?)([kmgt]i?b?|b)?", re.IGNORECASE)
_JOB_LABELS = {
    "io.remoteagent.managed": "true",
    "io.remoteagent.instance": "remoteagent",
    "io.remoteagent.job.id": "validation",
    "io.remoteagent.conversation.key": "validation",
}
# Reject alternate forms as well as their top-level equivalents. For example,
# lifecycle hooks can request their own privileged/root execution, deploy can
# introduce restart policies or extra replicas, and memswap_limit=-1 removes
# the swap bound even while mem_limit remains unchanged.
_FORBIDDEN_SERVICE_KEYS = frozenset(
    {
        "annotations",
        "configs",
        "cgroup",
        "cgroup_parent",
        "container_name",
        "cpu_count",
        "cpu_percent",
        "cpu_period",
        "cpu_quota",
        "cpu_rt_period",
        "cpu_rt_runtime",
        "cpu_shares",
        "cpuset",
        "credential_spec",
        "deploy",
        "device_cgroup_rules",
        "devices",
        "dns",
        "dns_opt",
        "dns_search",
        "domainname",
        "expose",
        "external_links",
        "extra_hosts",
        "gpus",
        "group_add",
        "ipc",
        "isolation",
        "links",
        "mem_reservation",
        "mem_swappiness",
        "memswap_limit",
        "network_mode",
        "oom_kill_disable",
        "oom_score_adj",
        "pid",
        "ports",
        "post_start",
        "pre_stop",
        "restart",
        "runtime",
        "scale",
        "secrets",
        "shm_size",
        "storage_opt",
        "sysctls",
        "ulimits",
        "use_api_socket",
        "uts",
        "userns_mode",
        "volumes_from",
    }
)
_SOCKET_NAMES = ("docker.sock", "podman.sock", "containerd.sock", "containerd-shim")


@dataclass(frozen=True)
class ComposeContract:
    agent_id: str
    project_name: str
    runner_service: str
    dependency_services: tuple[str, ...]
    workspace_path: str
    sessions_path: str
    artifacts_path: str
    version: str = "local"
    uid: str = "1000"
    gid: str = "1000"
    auth_volume: str = "remoteagent-codex-auth"
    skills_volume: str = "remoteagent-common-skills"
    instance_id: str = "remoteagent"
    cpu_limit: str = "2.0"
    memory_limit: str = "2g"
    wait_timeout_seconds: float = 120.0


def _fail(message: str) -> None:
    raise ComposeContractError(message)


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _parse_duration(value: Any, *, label: str, allow_zero: bool = False) -> float:
    if isinstance(value, bool):
        _fail(f"{label} must be a finite Compose duration")
    if isinstance(value, (int, float)):
        # Compose's JSON model may serialize duration values as nanoseconds.
        seconds = float(value) / 1_000_000_000
    elif isinstance(value, str) and value:
        offset = 0
        seconds = 0.0
        units = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
        for match in _DURATION_PART.finditer(value):
            if match.start() != offset:
                _fail(f"{label} must be a finite Compose duration")
            seconds += float(match.group(1)) * units[match.group(2)]
            offset = match.end()
        if offset != len(value):
            _fail(f"{label} must be a finite Compose duration")
    else:
        _fail(f"{label} must be a finite Compose duration")
    if not math.isfinite(seconds) or seconds < 0 or (not allow_zero and seconds == 0):
        _fail(f"{label} must be {'non-negative' if allow_zero else 'positive'} and finite")
    return seconds


def _parse_memory(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        _fail(f"{label} must be a positive finite byte limit")
    if isinstance(value, (int, float)):
        amount = float(value)
        multiplier = 1
    elif isinstance(value, str):
        match = _MEMORY.fullmatch(value.strip())
        if match is None:
            _fail(f"{label} must be a positive finite byte limit")
        amount = float(match.group(1))
        suffix = (match.group(2) or "b").lower()
        powers = {
            "b": 0,
            "k": 1,
            "kb": 1,
            "kib": 1,
            "m": 2,
            "mb": 2,
            "mib": 2,
            "g": 3,
            "gb": 3,
            "gib": 3,
            "t": 4,
            "tb": 4,
            "tib": 4,
        }
        multiplier = 1024 ** powers[suffix]
    else:
        _fail(f"{label} must be a positive finite byte limit")
    result = amount * multiplier
    if not math.isfinite(result) or result <= 0 or result > 1024**4:
        _fail(f"{label} must be positive and no greater than 1 TiB")
    return int(result)


def _positive_number(value: Any, *, label: str, maximum: float) -> float:
    if isinstance(value, bool):
        _fail(f"{label} must be a positive finite number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        _fail(f"{label} must be a positive finite number")
    if not math.isfinite(number) or number <= 0 or number > maximum:
        _fail(f"{label} must be positive and no greater than {maximum:g}")
    return number


def _require_absent_escape_controls(service: Mapping[str, Any], *, label: str) -> None:
    present = sorted(key for key in _FORBIDDEN_SERVICE_KEYS if key in service)
    if present:
        _fail(f"{label} uses forbidden Compose controls: {', '.join(present)}")
    if service.get("privileged") not in (None, False):
        _fail(f"{label} must not be privileged")
    cap_add = service.get("cap_add")
    if cap_add not in (None, []):
        _fail(f"{label} must not add Linux capabilities")


def _validate_logging(service: Mapping[str, Any], *, label: str) -> None:
    logging = _mapping(service.get("logging"), label=f"{label} logging")
    if logging.get("driver") not in {"local", "json-file"}:
        _fail(f"{label} must use the local or json-file logging driver")
    options = _mapping(logging.get("options"), label=f"{label} logging options")
    if set(options) != {"max-file", "max-size"}:
        _fail(f"{label} logging options must contain only max-file and max-size")
    try:
        max_files = int(options["max-file"])
    except (TypeError, ValueError):
        _fail(f"{label} logging max-file must be an integer")
    if not 1 <= max_files <= 10:
        _fail(f"{label} logging max-file must be between 1 and 10")
    if _parse_memory(options["max-size"], label=f"{label} logging max-size") > 100 * 1024**2:
        _fail(f"{label} logging max-size must not exceed 100 MiB")


def _contains_runtime_socket(value: object) -> bool:
    lowered = str(value).lower()
    return any(name in lowered for name in _SOCKET_NAMES)


def _validate_resource_bounds(
    service: Mapping[str, Any],
    *,
    label: str,
    expected_cpu: float | None = None,
    expected_memory: int | None = None,
    expected_pids: int | None = None,
) -> None:
    cpu = _positive_number(service.get("cpus"), label=f"{label} cpus", maximum=128)
    memory = _parse_memory(service.get("mem_limit"), label=f"{label} mem_limit")
    pids = _positive_number(service.get("pids_limit"), label=f"{label} pids_limit", maximum=4096)
    if expected_cpu is not None and cpu != expected_cpu:
        _fail(f"{label} cpus must equal the configured runner limit")
    if expected_memory is not None and memory != expected_memory:
        _fail(f"{label} mem_limit must equal the configured runner limit")
    if expected_pids is not None and pids != expected_pids:
        _fail(f"{label} pids_limit must be {expected_pids}")


def _validate_healthcheck(
    service: Mapping[str, Any], *, label: str, wait_timeout_seconds: float
) -> None:
    health = _mapping(service.get("healthcheck"), label=f"{label} healthcheck")
    required = {"test", "interval", "timeout", "retries", "start_period"}
    missing = sorted(required - set(health))
    if missing:
        _fail(f"{label} healthcheck is missing: {', '.join(missing)}")
    if health.get("disable") is True:
        _fail(f"{label} healthcheck must be enabled")
    test = health.get("test")
    if (
        not isinstance(test, list)
        or len(test) < 2
        or str(test[0]).upper() not in {"CMD", "CMD-SHELL"}
    ):
        _fail(f"{label} healthcheck requires a non-empty CMD or CMD-SHELL test")
    interval = _parse_duration(health["interval"], label=f"{label} healthcheck interval")
    timeout = _parse_duration(health["timeout"], label=f"{label} healthcheck timeout")
    start_period = _parse_duration(
        health["start_period"], label=f"{label} healthcheck start_period", allow_zero=True
    )
    retries = health["retries"]
    if isinstance(retries, bool) or not isinstance(retries, int) or retries <= 0:
        _fail(f"{label} healthcheck retries must be a positive integer")
    # Compose waits between checks and each individual check may consume its
    # full timeout. Use the conservative complete-failure envelope so a
    # dependency can reach a terminal unhealthy state within the controller's
    # finite `up --wait` budget.
    if start_period + retries * (interval + timeout) > wait_timeout_seconds:
        _fail(f"{label} healthcheck exceeds the Compose wait budget")


def _validate_pinned_image(value: Any, *, label: str) -> None:
    if not isinstance(value, str) or not value:
        _fail(f"{label} requires an image")
    if "@sha256:" in value:
        digest = value.rsplit("@sha256:", 1)[1]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            _fail(f"{label} image digest must be a full sha256")
        return
    final = value.rsplit("/", 1)[-1]
    if ":" not in final or final.rsplit(":", 1)[1] in {"", "latest"}:
        _fail(f"{label} image must use a non-latest tag or sha256 digest")


def _validate_dependency(
    name: str,
    service: Mapping[str, Any],
    *,
    network: str,
    wait_timeout_seconds: float,
    project_volumes: set[str],
) -> None:
    label = f"dependency service {name!r}"
    _require_absent_escape_controls(service, label=label)
    if "build" in service:
        _fail(f"{label} must use a prebuilt pinned image")
    _validate_pinned_image(service.get("image"), label=label)
    if service.get("profiles") not in (None, []):
        _fail(f"{label} must not be hidden behind a Compose profile")
    networks = _mapping(service.get("networks"), label=f"{label} networks")
    if set(networks) != {network}:
        _fail(f"{label} must use only the runner network")
    for option in service.get("security_opt", []):
        if option != "no-new-privileges:true":
            _fail(f"{label} uses an unapproved security option")
    for volume in service.get("volumes", []):
        volume = _mapping(volume, label=f"{label} volume")
        if volume.get("type") != "volume":
            _fail(f"{label} must not use host bind mounts")
        source = volume.get("source")
        if source not in project_volumes or source in {"codex-auth", "common-skills"}:
            _fail(f"{label} must use only agent-owned dependency volumes")
        if _contains_runtime_socket(volume.get("source")) or _contains_runtime_socket(
            volume.get("target")
        ):
            _fail(f"{label} must not mount a container-runtime socket")
    _validate_resource_bounds(service, label=label)
    _validate_logging(service, label=label)
    _validate_healthcheck(service, label=label, wait_timeout_seconds=wait_timeout_seconds)


def validate_compose_document(document: Mapping[str, Any], contract: ComposeContract) -> None:
    """Validate a Docker Compose JSON model after interpolation and normalization."""

    if document.get("name") != contract.project_name:
        _fail(f"Compose project name must be {contract.project_name!r}")
    services = _mapping(document.get("services"), label="Compose services")
    required = {contract.runner_service, *contract.dependency_services}
    missing = sorted(required - set(services))
    unexpected = sorted(set(services) - required)
    if missing:
        _fail(f"Compose project is missing services: {', '.join(missing)}")
    if unexpected:
        _fail(f"Compose project contains undeclared services: {', '.join(unexpected)}")

    network_key = "egress"
    networks = _mapping(document.get("networks"), label="Compose networks")
    if set(networks) != {network_key}:
        _fail("Compose project must define only the agent-owned egress network")
    network = _mapping(networks[network_key], label="egress network")
    if set(network) - {"ipam", "name"}:
        _fail("egress network contains unsupported ownership or driver controls")
    if network.get("name") != f"{contract.project_name}_egress":
        _fail("egress network must be owned by the agent Compose project")

    runner = _mapping(services[contract.runner_service], label="runner service")
    label = f"runner service {contract.runner_service!r}"
    _require_absent_escape_controls(runner, label=label)
    expected_image = f"remoteagent/{contract.agent_id}:{contract.version}"
    if runner.get("image") != expected_image:
        _fail(f"{label} image must be {expected_image!r}")
    if runner.get("profiles") != ["runner"]:
        _fail(f"{label} must use exactly the runner profile")
    if runner.get("user") != f"{contract.uid}:{contract.gid}":
        _fail(f"{label} must run as the configured non-root UID:GID")
    if contract.uid == "0" or contract.gid == "0":
        _fail(f"{label} must not run as root")
    if runner.get("read_only") is not True:
        _fail(f"{label} root filesystem must be read-only")
    if runner.get("cap_drop") != ["ALL"]:
        _fail(f"{label} must drop all Linux capabilities")
    if runner.get("security_opt") != [
        "no-new-privileges:true",
        "seccomp=unconfined",
        "apparmor=unconfined",
    ]:
        _fail(f"{label} has an unapproved security_opt contract")
    if runner.get("tmpfs") != ["/tmp:size=64m,mode=1777"]:
        _fail(f"{label} must use the bounded platform /tmp tmpfs")
    if runner.get("command") is not None or runner.get("entrypoint") is not None:
        _fail(f"{label} must not override the platform image command or entrypoint")
    runner_networks = _mapping(runner.get("networks"), label=f"{label} networks")
    if set(runner_networks) != {network_key}:
        _fail(f"{label} must use only the agent-owned egress network")

    expected_labels = {
        **_JOB_LABELS,
        "io.remoteagent.instance": contract.instance_id,
        "io.remoteagent.agent.id": contract.agent_id,
    }
    labels = _mapping(runner.get("labels"), label=f"{label} labels")
    incorrect = sorted(key for key, value in expected_labels.items() if labels.get(key) != value)
    if incorrect:
        _fail(f"{label} has missing or incorrect lifecycle labels: {', '.join(incorrect)}")

    expected_volumes = [
        {"type": "volume", "source": "codex-auth", "target": "/home/agent/.codex"},
        {"type": "bind", "source": contract.sessions_path, "target": "/home/agent/.codex/sessions"},
        {"type": "bind", "source": contract.workspace_path, "target": "/workspace"},
        {"type": "bind", "source": contract.artifacts_path, "target": "/workspace/artifacts"},
        {
            "type": "volume",
            "source": "common-skills",
            "target": "/opt/remoteagent/skills",
            "read_only": True,
        },
    ]
    if runner.get("volumes") != expected_volumes:
        _fail(f"{label} must use exactly the five ordered platform mounts")
    if any(_contains_runtime_socket(volume) for volume in runner.get("volumes", [])):
        _fail(f"{label} must not mount a container-runtime socket")

    volumes = _mapping(document.get("volumes"), label="Compose volumes")
    for key, expected_name in {
        "codex-auth": contract.auth_volume,
        "common-skills": contract.skills_volume,
    }.items():
        volume = _mapping(volumes.get(key), label=f"Compose volume {key!r}")
        if volume != {"external": True, "name": expected_name}:
            _fail(f"Compose volume {key!r} must be the configured external platform volume")
    for key, volume_value in volumes.items():
        if key in {"codex-auth", "common-skills"}:
            continue
        volume = _mapping(volume_value, label=f"Compose volume {key!r}")
        if set(volume) - {"name"}:
            _fail(f"dependency volume {key!r} must be owned by the agent project")
        name = volume.get("name")
        if name is not None and not str(name).startswith(f"{contract.project_name}_"):
            _fail(f"dependency volume {key!r} must be owned by the agent project")

    _validate_resource_bounds(
        runner,
        label=label,
        expected_cpu=_positive_number(contract.cpu_limit, label="configured CPU limit", maximum=128),
        expected_memory=_parse_memory(contract.memory_limit, label="configured memory limit"),
        expected_pids=256,
    )
    _validate_logging(runner, label=label)
    depends_on = runner.get("depends_on", {})
    if set(depends_on) - set(contract.dependency_services):
        _fail(f"{label} depends on an undeclared service")

    for dependency in contract.dependency_services:
        _validate_dependency(
            dependency,
            _mapping(services[dependency], label=f"dependency service {dependency!r}"),
            network=network_key,
            wait_timeout_seconds=contract.wait_timeout_seconds,
            project_volumes=set(volumes),
        )


def _contract_from_cli(arguments: argparse.Namespace, manifest: Mapping[str, Any]) -> ComposeContract:
    agent_id = manifest.get("id")
    runner = manifest.get("runner_service")
    dependencies = manifest.get("dependency_services", [])
    if not isinstance(agent_id, str) or not isinstance(runner, str) or not isinstance(
        dependencies, list
    ):
        _fail("agent manifest does not contain a valid Compose contract")
    return ComposeContract(
        agent_id=agent_id,
        project_name=str(manifest.get("project_name", f"remoteagent-{agent_id}")),
        runner_service=runner,
        dependency_services=tuple(str(value) for value in dependencies),
        workspace_path=arguments.workspace,
        sessions_path=arguments.sessions,
        artifacts_path=arguments.artifacts,
        version=os.environ.get("REMOTEAGENT_VERSION", "local"),
        uid=os.environ.get("REMOTEAGENT_UID", "1000"),
        gid=os.environ.get("REMOTEAGENT_GID", "1000"),
        auth_volume=os.environ.get("REMOTEAGENT_AUTH_VOLUME", "remoteagent-codex-auth"),
        skills_volume=os.environ.get("REMOTEAGENT_SKILLS_VOLUME", "remoteagent-common-skills"),
        instance_id=os.environ.get("REMOTEAGENT_INSTANCE_ID", "remoteagent"),
        cpu_limit=os.environ.get("REMOTEAGENT_AGENT_CPU_LIMIT", "2.0"),
        memory_limit=os.environ.get("REMOTEAGENT_AGENT_MEMORY_LIMIT", "2g"),
        wait_timeout_seconds=arguments.wait_timeout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="validate a resolved RemoteAgent Compose model")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--model-fd", type=int, default=0)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--sessions", required=True)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--wait-timeout", type=float, default=120)
    arguments = parser.parse_args(argv)
    try:
        with arguments.manifest.open("rb") as handle:
            manifest = tomllib.load(handle)
        with os.fdopen(arguments.model_fd, encoding="utf-8") as handle:
            document = json.load(handle)
        validate_compose_document(document, _contract_from_cli(arguments, manifest))
    except (ComposeContractError, OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"{arguments.manifest}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
