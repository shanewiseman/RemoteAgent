from __future__ import annotations

import importlib.util
import pathlib
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
        config_path=REPOSITORY_ROOT / "repository-critic" / "config.toml",
        network="probe-network",
        uid=1234,
        gid=2345,
        script=probe.CRITIC_POLICY_SCRIPT,
        private_host="probe-target",
    )

    assert command[:3] == ["docker", "run", "--rm"]
    assert command[command.index("--pull") + 1] == "never"
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
    assert "$PROBE_UNLISTED_PUBLIC_URL" in probe.CRITIC_POLICY_SCRIPT
    assert "codex sandbox" not in probe.DEFAULT_POLICY_SCRIPT
    assert "codex sandbox" not in probe.CRITIC_POLICY_SCRIPT
    assert f'codex-cli {probe.PINNED_CODEX_VERSION}' in probe.DEFAULT_POLICY_SCRIPT
    assert "http://127.0.0.1:" in probe.CRITIC_POLICY_SCRIPT
    assert "http://$PROBE_PRIVATE_HOST:" in probe.CRITIC_POLICY_SCRIPT
    assert '--unix-socket "$probe_socket"' in probe.CRITIC_POLICY_SCRIPT
    assert command[command.index("--env") + 1] == f"PROBE_PUBLIC_URL={probe.PUBLIC_URL}"
    assert "XDG_RUNTIME_DIR=/tmp/remoteagent-codex-runtime" in command


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
    checked: list[tuple[list[str], str]] = []
    cleanup: list[list[str]] = []

    def fake_checked(
        argv: list[str], *, label: str, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        checked.append((list(argv), label))
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
        "critic_unlisted_public_denied": True,
        "critic_loopback_denied": True,
        "critic_private_service_denied": True,
        "critic_unix_socket_denied": True,
    }
    labels = [label for _, label in checked]
    assert labels[-2:] == [
        "default-agent allowlisted-host denial",
        "repository-critic managed-network policy",
    ]
    default_command = checked[-2][0]
    critic_command = checked[-1][0]
    assert "runtime/codex-config.toml" in default_command[default_command.index("--mount") + 1]
    assert "repository-critic/config.toml" in critic_command[critic_command.index("--mount") + 1]
    assert cleanup == [
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
        ["docker", "rm", "--force", "remoteagent-network-target-bbbbbbbbbbbb"],
        ["docker", "network", "rm", "remoteagent-network-probe-bbbbbbbbbbbb"],
    ]
