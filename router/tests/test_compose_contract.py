from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from remoteagent.compose_contract import (
    _FORBIDDEN_SERVICE_KEYS,
    ComposeContract,
    ComposeContractError,
    validate_compose_document,
)


def contract(*, dependencies: tuple[str, ...] = ()) -> ComposeContract:
    return ComposeContract(
        agent_id="alpha",
        project_name="remoteagent-alpha",
        runner_service="agent",
        dependency_services=dependencies,
        workspace_path="/validation/workspace",
        sessions_path="/validation/sessions",
        artifacts_path="/validation/artifacts",
    )


def runner() -> dict[str, object]:
    return {
        "profiles": ["runner"],
        "cap_drop": ["ALL"],
        "cpus": 2,
        "command": None,
        "entrypoint": None,
        "image": "remoteagent/alpha:local",
        "labels": {
            "io.remoteagent.managed": "true",
            "io.remoteagent.instance": "remoteagent",
            "io.remoteagent.agent.id": "alpha",
            "io.remoteagent.job.id": "validation",
            "io.remoteagent.conversation.key": "validation",
        },
        "logging": {
            "driver": "local",
            "options": {"max-file": "3", "max-size": "10m"},
        },
        "mem_limit": "2147483648",
        "networks": {"egress": None},
        "pids_limit": 256,
        "read_only": True,
        "security_opt": [
            "no-new-privileges:true",
            "seccomp=unconfined",
            "apparmor=unconfined",
        ],
        "tmpfs": ["/tmp:size=64m,mode=1777"],
        "user": "1000:1000",
        "volumes": [
            {"type": "volume", "source": "codex-auth", "target": "/home/agent/.codex"},
            {
                "type": "bind",
                "source": "/validation/sessions",
                "target": "/home/agent/.codex/sessions",
            },
            {"type": "bind", "source": "/validation/workspace", "target": "/workspace"},
            {
                "type": "bind",
                "source": "/validation/artifacts",
                "target": "/workspace/artifacts",
            },
            {
                "type": "volume",
                "source": "common-skills",
                "target": "/opt/remoteagent/skills",
                "read_only": True,
            },
        ],
    }


def dependency() -> dict[str, object]:
    return {
        "image": "postgres:17",
        "cpus": 1,
        "mem_limit": "512m",
        "pids_limit": 128,
        "networks": {"egress": None},
        "logging": {
            "driver": "local",
            "options": {"max-file": "2", "max-size": "5m"},
        },
        "healthcheck": {
            "test": ["CMD", "pg_isready"],
            "interval": "5s",
            "timeout": "3s",
            "retries": 10,
            "start_period": "2s",
        },
    }


def model(*, dependencies: tuple[str, ...] = ()) -> dict[str, object]:
    services: dict[str, object] = {"agent": runner()}
    services.update({name: dependency() for name in dependencies})
    return {
        "name": "remoteagent-alpha",
        "services": services,
        "networks": {"egress": {"name": "remoteagent-alpha_egress", "ipam": {}}},
        "volumes": {
            "codex-auth": {"name": "remoteagent-codex-auth", "external": True},
            "common-skills": {"name": "remoteagent-common-skills", "external": True},
        },
    }


def test_valid_runner_and_dependency_contract() -> None:
    validate_compose_document(model(dependencies=("database",)), contract(dependencies=("database",)))


@pytest.mark.parametrize("control", sorted(_FORBIDDEN_SERVICE_KEYS))
def test_runner_rejects_every_forbidden_compose_control(control: str) -> None:
    document = model()
    document["services"]["agent"][control] = "unsafe"  # type: ignore[index]
    with pytest.raises(ComposeContractError, match="forbidden Compose controls"):
        validate_compose_document(document, contract())


@pytest.mark.parametrize("service_name", ["agent", "database"])
@pytest.mark.parametrize(
    ("control", "value"),
    [
        (
            "post_start",
            [{"command": ["id"], "user": "root", "privileged": True}],
        ),
        (
            "pre_stop",
            [{"command": ["id"], "user": "root", "privileged": True}],
        ),
        ("deploy", {"restart_policy": {"condition": "any"}}),
        ("deploy", {"replicas": 10000}),
        ("scale", 10000),
        ("memswap_limit", -1),
        ("oom_kill_disable", True),
        ("shm_size", "1t"),
        ("cpu_quota", -1),
        ("ulimits", {"nproc": {"soft": -1, "hard": -1}}),
        ("gpus", [{"count": -1, "capabilities": [["gpu"]]}]),
    ],
)
def test_alternate_privilege_restart_and_resource_controls_are_rejected(
    service_name: str, control: str, value: object
) -> None:
    # These are valid resolved-Compose controls. None may bypass the ordinary
    # user/privileged/device checks or the required resource fields, including
    # on dependencies provisioned by `compose up`.
    document = model(dependencies=("database",))
    document["services"][service_name][control] = value  # type: ignore[index]
    with pytest.raises(ComposeContractError, match="forbidden Compose controls"):
        validate_compose_document(document, contract(dependencies=("database",)))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("image", "example.invalid/alpha:1"), "image must be"),
        (("profiles", []), "runner profile"),
        (("user", "0:0"), "non-root UID:GID"),
        (("read_only", False), "read-only"),
        (("cap_drop", []), "drop all"),
        (("cap_add", ["SYS_ADMIN"]), "must not add"),
        (("security_opt", ["no-new-privileges:true"]), "security_opt"),
        (("tmpfs", ["/tmp"]), "bounded platform"),
        (("command", ["sh"]), "must not override"),
        (("entrypoint", ["sh"]), "must not override"),
        (("networks", {"other": None}), "egress network"),
        (("cpus", 3), "configured runner limit"),
        (("mem_limit", "3g"), "configured runner limit"),
        (("pids_limit", 257), "must be 256"),
        (("logging", None), "logging must be an object"),
        (("privileged", True), "must not be privileged"),
    ],
)
def test_runner_rejects_isolation_contract_departures(
    mutation: tuple[str, object], message: str
) -> None:
    document = model()
    document["services"]["agent"][mutation[0]] = mutation[1]  # type: ignore[index]
    with pytest.raises(ComposeContractError, match=message):
        validate_compose_document(document, contract())


def test_runner_rejects_mount_reordering_extra_bind_and_runtime_socket() -> None:
    for mutate in (
        lambda volumes: volumes.reverse(),
        lambda volumes: volumes.append(
            {"type": "bind", "source": "/host", "target": "/extra"}
        ),
        lambda volumes: volumes.__setitem__(
            2,
            {
                "type": "bind",
                "source": "/var/run/docker.sock",
                "target": "/workspace",
            },
        ),
    ):
        document = model()
        volumes = document["services"]["agent"]["volumes"]  # type: ignore[index]
        mutate(volumes)
        with pytest.raises(ComposeContractError, match="five ordered platform mounts"):
            validate_compose_document(document, contract())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("external_network", "egress network"),
        ("network_name", "owned by the agent"),
        ("extra_network", "only the agent-owned"),
        ("extra_service", "undeclared services"),
        ("external_volume", "must be owned"),
    ],
)
def test_project_rejects_unowned_or_undeclared_resources(change: str, message: str) -> None:
    document = model()
    if change == "external_network":
        document["networks"]["egress"]["external"] = True  # type: ignore[index]
    elif change == "network_name":
        document["networks"]["egress"]["name"] = "shared"  # type: ignore[index]
    elif change == "extra_network":
        document["networks"]["other"] = {"name": "other"}  # type: ignore[index]
    elif change == "extra_service":
        document["services"]["hidden"] = dependency()  # type: ignore[index]
    else:
        document["volumes"]["data"] = {"name": "shared", "external": True}  # type: ignore[index]
    with pytest.raises(ComposeContractError, match=message):
        validate_compose_document(document, contract())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("unpinned", "non-latest tag"),
        ("profile", "must not be hidden"),
        ("bind", "must not use host bind"),
        ("network", "runner network"),
        ("unbounded", "must be positive"),
        ("logging", "logging must be an object"),
        ("health_missing", "healthcheck is missing"),
        ("health_disabled", "must be enabled"),
        ("health_unbounded", "wait budget"),
        ("security", "unapproved security"),
        ("build", "prebuilt pinned image"),
        ("platform_volume", "agent-owned dependency volumes"),
    ],
)
def test_dependency_rejects_unbounded_or_unsafe_controls(change: str, message: str) -> None:
    document = model(dependencies=("database",))
    service = document["services"]["database"]  # type: ignore[index]
    if change == "unpinned":
        service["image"] = "postgres:latest"
    elif change == "profile":
        service["profiles"] = ["optional"]
    elif change == "bind":
        service["volumes"] = [{"type": "bind", "source": "/host", "target": "/data"}]
    elif change == "network":
        service["networks"] = {"other": None}
    elif change == "unbounded":
        service["cpus"] = 0
    elif change == "logging":
        service["logging"] = None
    elif change == "health_missing":
        del service["healthcheck"]["timeout"]
    elif change == "health_disabled":
        service["healthcheck"]["disable"] = True
    elif change == "health_unbounded":
        service["healthcheck"]["start_period"] = "2m"
    elif change == "build":
        service["build"] = {"context": "."}
    elif change == "platform_volume":
        service["volumes"] = [
            {"type": "volume", "source": "codex-auth", "target": "/credentials"}
        ]
    else:
        service["security_opt"] = ["seccomp=unconfined"]
    with pytest.raises(ComposeContractError, match=message):
        validate_compose_document(document, contract(dependencies=("database",)))


def test_dependency_healthcheck_budget_counts_every_check_timeout() -> None:
    document = model(dependencies=("database",))
    document["services"]["database"]["healthcheck"] = {  # type: ignore[index]
        "test": ["CMD", "healthcheck"],
        "interval": "1s",
        "timeout": "10s",
        "retries": 10,
        "start_period": "20s",
    }

    with pytest.raises(ComposeContractError, match="wait budget"):
        validate_compose_document(document, contract(dependencies=("database",)))


def test_cli_and_router_use_the_same_resolved_model_validator(tmp_path: Path) -> None:
    manifest = tmp_path / "agent.toml"
    manifest.write_text(
        '\n'.join(
            (
                'schema_version = 1',
                'id = "alpha"',
                'runner_service = "agent"',
                'project_name = "remoteagent-alpha"',
                'dependency_services = []',
            )
        ),
        encoding="utf-8",
    )
    document = model()
    document["services"]["agent"]["ports"] = ["8080:8080"]  # type: ignore[index]
    module = Path(__file__).parents[1] / "src" / "remoteagent" / "compose_contract.py"
    result = subprocess.run(
        [
            sys.executable,
            str(module),
            str(manifest),
            "--workspace",
            "/validation/workspace",
            "--sessions",
            "/validation/sessions",
            "--artifacts",
            "/validation/artifacts",
        ],
        input=json.dumps(document),
        text=True,
        capture_output=True,
        check=False,
    )
    with pytest.raises(ComposeContractError) as error:
        validate_compose_document(document, contract())
    assert result.returncode == 1
    assert str(error.value) in result.stderr
