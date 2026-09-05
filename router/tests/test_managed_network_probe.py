from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import tomllib
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import pytest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_probe() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / "probe_managed_network.py"
    spec = importlib.util.spec_from_file_location("remoteagent_managed_network_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_commands_use_real_configs_and_app_server_command_exec() -> None:
    probe = _load_probe()
    command = probe.sandbox_runner_command(
        image="remoteagent/repository-critic:test",
        container_name="remoteagent-network-critic-test",
        config_path=REPOSITORY_ROOT / "repository-critic" / "config.toml",
        network="probe-network",
        uid=1234,
        gid=2345,
        script=probe.CRITIC_POLICY_SCRIPT,
        private_host="probe-target",
    )

    assert command[:3] == ["docker", "run", "--rm"]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--name") + 1] == "remoteagent-network-critic-test"
    assert command[command.index("--network") + 1] == "probe-network"
    assert command[command.index("--user") + 1] == "1234:2345"
    assert command[command.index("--entrypoint") + 1] == "/bin/sh"
    assert command[-2:] == ["-ceu", probe.CRITIC_POLICY_SCRIPT]
    config_mount = command[command.index("--mount") + 1]
    assert str(REPOSITORY_ROOT / "repository-critic" / "config.toml") in config_mount
    assert "dst=/home/agent/.codex/config.toml,readonly" in config_mount

    assert '"app-server"' in probe.DEFAULT_POLICY_SCRIPT
    assert '"method": "command/exec"' in probe.DEFAULT_POLICY_SCRIPT
    assert (
        'f"sandbox_workspace_write.network_access={network_access}"'
        in probe.DEFAULT_POLICY_SCRIPT
    )
    assert "if run_managed_command" in probe.DEFAULT_POLICY_SCRIPT
    assert "$PROBE_PUBLIC_URL" in probe.DEFAULT_POLICY_SCRIPT
    assert "PROBE_NETWORK_ACCESS=false" in probe.DEFAULT_POLICY_SCRIPT
    assert '"app-server"' in probe.CRITIC_POLICY_SCRIPT
    assert "PROBE_NETWORK_ACCESS=true" in probe.CRITIC_POLICY_SCRIPT
    assert "run_managed_sequence" in probe.CRITIC_POLICY_SCRIPT
    assert "$PROBE_UNLISTED_PUBLIC_URL" in probe.CRITIC_POLICY_SCRIPT
    assert "codex sandbox" not in probe.DEFAULT_POLICY_SCRIPT
    assert "codex sandbox" not in probe.CRITIC_POLICY_SCRIPT
    assert f'codex-cli {probe.PINNED_CODEX_VERSION}' in probe.DEFAULT_POLICY_SCRIPT
    assert "http://127.0.0.1:" in probe.CRITIC_POLICY_SCRIPT
    assert "http://$PROBE_PRIVATE_HOST:" in probe.CRITIC_POLICY_SCRIPT
    assert '--unix-socket "$probe_socket"' in probe.CRITIC_POLICY_SCRIPT
    assert command[command.index("--env") + 1] == f"PROBE_PUBLIC_URL={probe.PUBLIC_URL}"
    assert "XDG_RUNTIME_DIR=/tmp/remoteagent-codex-runtime" in command


def test_critic_probe_repeats_locked_cache_cold_restores_through_one_app_server() -> None:
    probe = _load_probe()

    module_contract = " ".join((probe.__doc__ or "").split())
    assert "Each ``command/exec`` request has its own managed-proxy lifetime" in module_contract
    assert (
        "does not exercise Codex unified-exec's per-environment listener handoff"
        in module_contract
    )
    assert probe.PACKAGE_RESTORE_REPEAT_COUNT == 3
    assert len(probe.PACKAGE_RESTORE_COMMANDS) == 3
    assert probe.PACKAGE_RESTORE_SEQUENCE == (
        *probe.PACKAGE_RESTORE_COMMANDS,
        probe.PACKAGE_RESTORE_HEALTH_COMMAND,
    )
    assert "for request_id, command in enumerate(commands, start=2)" in (
        probe.APP_SERVER_COMMAND_FUNCTION
    )
    assert 'elif mode == "--sequence":' in probe.APP_SERVER_COMMAND_FUNCTION

    targets: set[str] = set()
    for command in probe.PACKAGE_RESTORE_COMMANDS:
        assert command[:4] == ["uv", "--no-config", "pip", "sync"]
        assert "--require-hashes" in command
        assert "--no-build" in command
        assert "--no-cache" in command
        assert "--no-python-downloads" in command
        assert command[command.index("--keyring-provider") + 1] == "disabled"
        assert command[command.index("--default-index") + 1] == (
            "https://pypi.org/simple"
        )
        assert command[-1] == probe.PACKAGE_RESTORE_REQUIREMENTS_PATH
        targets.add(command[command.index("--target") + 1])
        assert json.dumps(command) in probe.CRITIC_POLICY_SCRIPT

    assert len(targets) == probe.PACKAGE_RESTORE_REPEAT_COUNT
    assert probe.PACKAGE_RESTORE_HEALTH_COMMAND[-1] == probe.PUBLIC_URL
    assert json.dumps(probe.PACKAGE_RESTORE_HEALTH_COMMAND) in (
        probe.CRITIC_POLICY_SCRIPT
    )
    assert "UV_CONCURRENT_DOWNLOADS=8" in probe.CRITIC_POLICY_SCRIPT
    assert "UV_HTTP_RETRIES=2" in probe.CRITIC_POLICY_SCRIPT
    assert "UV_HTTP_TIMEOUT=20" in probe.CRITIC_POLICY_SCRIPT

    requirements = probe.PACKAGE_RESTORE_REQUIREMENTS
    assert len(re.findall(r"(?m)^[a-z][a-z0-9-]*==", requirements)) == 5
    hashes = re.findall(r"--hash=sha256:([0-9a-f]+)", requirements)
    assert hashes
    assert all(len(value) == 64 for value in hashes)
    assert "://" not in requirements
    assert "@" not in requirements


def test_probe_host_and_codex_version_match_the_managed_source_contract() -> None:
    probe = _load_probe()
    with (REPOSITORY_ROOT / "runtime" / "codex-requirements.toml").open("rb") as handle:
        requirements = tomllib.load(handle)

    domains = requirements["experimental_network"]["domains"]
    assert set(domains) == {
        "example.com",
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "proxy.golang.org",
        "sum.golang.org",
    }
    assert set(domains.values()) == {"allow"}
    assert urlparse(probe.PUBLIC_URL).hostname in domains
    assert "enabled" not in requirements["experimental_network"]
    assert requirements["features"]["network_proxy"] is True
    dockerfile = (REPOSITORY_ROOT / "runtime" / "agent.Dockerfile").read_text(
        encoding="utf-8"
    )
    assert f"ARG CODEX_CLI_VERSION={probe.PINNED_CODEX_VERSION}" in dockerfile


def test_probe_orchestrates_both_policies_and_always_removes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe()
    checked: list[tuple[list[str], str, int]] = []
    cleanup: list[list[str]] = []

    def fake_checked(
        argv: list[str], *, label: str, timeout: int = probe.COMMAND_TIMEOUT_SECONDS
    ) -> subprocess.CompletedProcess[str]:
        checked.append((list(argv), label, timeout))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        cleanup.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(probe, "run_checked", fake_checked)
    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    monkeypatch.setattr(probe.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    monkeypatch.setenv("REMOTEAGENT_VERSION", "test")
    monkeypatch.setenv("REMOTEAGENT_UID", "1234")
    monkeypatch.setenv("REMOTEAGENT_GID", "2345")

    result = probe.probe()

    assert result["checks"] == {
        "default_allowlisted_https_denied": True,
        "critic_allowlisted_https_allowed": True,
        "critic_repeated_locked_restore_allowed": True,
        "critic_unlisted_public_denied": True,
        "critic_loopback_denied": True,
        "critic_private_service_denied": True,
        "critic_unix_socket_denied": True,
    }
    labels = [label for _, label, _ in checked]
    assert labels[-2:] == [
        "default-agent allowlisted-host denial",
        "repository-critic managed-network policy",
    ]
    default_command, _, default_timeout = checked[-2]
    critic_command, _, critic_timeout = checked[-1]
    assert "runtime/codex-config.toml" in default_command[default_command.index("--mount") + 1]
    assert "repository-critic/config.toml" in critic_command[critic_command.index("--mount") + 1]
    assert default_command[default_command.index("--name") + 1] == (
        "remoteagent-network-default-aaaaaaaaaaaa"
    )
    assert critic_command[critic_command.index("--name") + 1] == (
        "remoteagent-network-critic-aaaaaaaaaaaa"
    )
    assert default_timeout == probe.COMMAND_TIMEOUT_SECONDS
    assert critic_timeout == probe.CRITIC_POLICY_TIMEOUT_SECONDS
    assert critic_timeout >= 540
    assert cleanup == [
        ["docker", "rm", "--force", "remoteagent-network-critic-aaaaaaaaaaaa"],
        ["docker", "rm", "--force", "remoteagent-network-default-aaaaaaaaaaaa"],
        ["docker", "rm", "--force", "remoteagent-network-target-aaaaaaaaaaaa"],
        ["docker", "network", "rm", "remoteagent-network-probe-aaaaaaaaaaaa"],
    ]


def test_probe_cleans_up_after_a_policy_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _load_probe()
    cleanup: list[list[str]] = []

    def fake_checked(
        argv: list[str], *, label: str, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        if label == "repository-critic managed-network policy":
            raise probe.ProbeFailure("policy failed")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        cleanup.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(probe, "run_checked", fake_checked)
    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    monkeypatch.setattr(probe.uuid, "uuid4", lambda: SimpleNamespace(hex="b" * 32))

    with pytest.raises(probe.ProbeFailure, match="policy failed"):
        probe.probe()

    assert cleanup == [
        ["docker", "rm", "--force", "remoteagent-network-critic-bbbbbbbbbbbb"],
        ["docker", "rm", "--force", "remoteagent-network-default-bbbbbbbbbbbb"],
        ["docker", "rm", "--force", "remoteagent-network-target-bbbbbbbbbbbb"],
        ["docker", "network", "rm", "remoteagent-network-probe-bbbbbbbbbbbb"],
    ]


def test_probe_cleanup_exceptions_do_not_skip_targets_or_mask_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe()
    cleanup: list[list[str]] = []

    def fake_checked(
        argv: list[str], *, label: str, timeout: int = probe.COMMAND_TIMEOUT_SECONDS
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        if label == "repository-critic managed-network policy":
            raise probe.ProbeFailure("primary policy failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        cleanup.append(list(argv))
        if len(cleanup) == 1:
            raise subprocess.TimeoutExpired(argv, 30)
        if len(cleanup) == 2:
            raise OSError("cleanup transport failed")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(probe, "run_checked", fake_checked)
    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    monkeypatch.setattr(probe.uuid, "uuid4", lambda: SimpleNamespace(hex="c" * 32))

    with pytest.raises(probe.ProbeFailure, match="primary policy failure"):
        probe.probe()

    assert cleanup == [
        ["docker", "rm", "--force", "remoteagent-network-critic-cccccccccccc"],
        ["docker", "rm", "--force", "remoteagent-network-default-cccccccccccc"],
        ["docker", "rm", "--force", "remoteagent-network-target-cccccccccccc"],
        ["docker", "network", "rm", "remoteagent-network-probe-cccccccccccc"],
    ]


def test_probe_reports_cleanup_exception_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _load_probe()
    cleanup: list[list[str]] = []

    def fake_checked(
        argv: list[str], *, label: str, timeout: int = probe.COMMAND_TIMEOUT_SECONDS
    ) -> subprocess.CompletedProcess[str]:
        del label, timeout
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        cleanup.append(list(argv))
        if len(cleanup) == 1:
            raise subprocess.TimeoutExpired(argv, 30)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(probe, "run_checked", fake_checked)
    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    monkeypatch.setattr(probe.uuid, "uuid4", lambda: SimpleNamespace(hex="d" * 32))

    with pytest.raises(probe.ProbeFailure, match="managed-network cleanup could not complete"):
        probe.probe()

    assert len(cleanup) == 4
