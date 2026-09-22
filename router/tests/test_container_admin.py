from __future__ import annotations

import json
import os
import pty
import shutil
import socket
import subprocess
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("0.0.0.0", "http://127.0.0.1:8123"),
        ("", "http://127.0.0.1:8123"),
        ("192.168.20.111", "http://192.168.20.111:8123"),
        ("::", "http://[::1]:8123"),
        ("[::]", "http://[::1]:8123"),
        ("2001:db8::7", "http://[2001:db8::7]:8123"),
        ("[2001:db8::7]", "http://[2001:db8::7]:8123"),
    ],
)
def test_management_url_follows_bind_address(address: str, expected: str) -> None:
    result = subprocess.run(
        [
            "bash", "-c", 'source "$1" help >/dev/null; router_url',
            "test", str(REPOSITORY / "scripts" / "remotectl"),
        ],
        env={**os.environ, "REMOTEAGENT_BIND_ADDRESS": address, "REMOTEAGENT_PORT": "8123"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == expected


@pytest.fixture
def admin_fixture(tmp_path: Path):
    repository = tmp_path / "application with spaces"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPOSITORY / "scripts" / "remotectl-container", scripts)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "invocation.json"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "stdin = 'terminal' if sys.stdin.isatty() else sys.stdin.read()\n"
        "Path(os.environ['CAPTURE']).write_text(json.dumps({'argv': sys.argv[1:], 'stdin': stdin}))\n"
    )
    docker.chmod(0o755)
    socket_path = tmp_path / "engine.sock"
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(socket_path))
        environment = {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CAPTURE": str(capture),
            "REMOTEAGENT_DOCKER_SOCKET": str(socket_path),
        }
        yield repository, scripts / "remotectl-container", socket_path, capture, environment


def test_wrapper_preserves_pipe_and_host_identity_without_sourcing_env(admin_fixture) -> None:
    repository, wrapper, socket_path, capture, environment = admin_fixture
    marker = repository / "must-not-exist"
    (repository / ".env").write_text(
        "REMOTEAGENT_VERSION='deploy-test-1'\n"
        f'UNRELATED_SECRET=$(touch "{marker}")\n'
    )
    result = subprocess.run(
        [str(wrapper), "auth", "login", "--method", "chatgpt"],
        env=environment,
        input="dummy-input-bytes\n",
        capture_output=True,
        text=True,
        check=True,
    )
    invocation = json.loads(capture.read_text())
    argv = invocation["argv"]
    assert invocation["stdin"] == "dummy-input-bytes\n"
    assert argv[:5] == ["--host", f"unix://{socket_path}", "run", "--rm", "--pull"]
    assert argv[argv.index("--pull") + 1] == "never"
    assert "--interactive" in argv and "--tty" not in argv
    assert argv[argv.index("--network") + 1] == "host"
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert argv[argv.index("--group-add") + 1] == str(socket_path.stat().st_gid)
    assert f"type=bind,source={repository},target={repository}" in argv
    assert f"type=bind,source={socket_path},target={socket_path}" in argv
    assert f"DOCKER_HOST=unix://{socket_path}" in argv
    assert f"TMPDIR={repository}/.runtime/admin-tmp" in argv
    assert "remoteagent/router:deploy-test-1" in argv
    assert argv[-5:] == [
        str(repository / "scripts" / "remotectl"),
        "auth", "login", "--method", "chatgpt",
    ]
    assert not marker.exists()
    assert "dummy-input" not in result.stdout + result.stderr


def test_wrapper_allocates_tty_only_for_interactive_session(admin_fixture) -> None:
    _, wrapper, _, capture, environment = admin_fixture
    master, slave = pty.openpty()
    try:
        subprocess.run(
            [str(wrapper), "auth", "login"], env=environment,
            stdin=slave, stdout=slave, stderr=subprocess.PIPE, timeout=10, check=True,
        )
    finally:
        os.close(master)
        os.close(slave)
    invocation = json.loads(capture.read_text())
    assert "--tty" in invocation["argv"]
    assert invocation["stdin"] == "terminal"


def test_wrapper_selects_alternate_env_file_and_explicit_image(admin_fixture) -> None:
    repository, wrapper, _, capture, environment = admin_fixture
    alternate = repository / "deployment.env"
    alternate.write_text('REMOTEAGENT_VERSION="release-2"\n')
    subprocess.run(
        [str(wrapper), "--env-file", "deployment.env", "status"], cwd=repository,
        env=environment, input="", text=True, check=True,
    )
    argv = json.loads(capture.read_text())["argv"]
    assert "remoteagent/router:release-2" in argv
    assert argv[-3:] == ["--env-file", str(alternate), "status"]
    subprocess.run(
        [str(wrapper), "status"],
        env={**environment, "REMOTEAGENT_ADMIN_IMAGE": "remoteagent/router:approved-3"},
        input="", text=True, check=True,
    )
    assert "remoteagent/router:approved-3" in json.loads(capture.read_text())["argv"]


def test_wrapper_rejects_evaluated_version_before_docker(admin_fixture) -> None:
    repository, wrapper, _, capture, environment = admin_fixture
    marker = repository / "must-not-exist"
    (repository / ".env").write_text(f'REMOTEAGENT_VERSION=$(touch "{marker}")\n')
    result = subprocess.run(
        [str(wrapper), "status"], env=environment, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "literal Docker tag" in result.stderr
    assert not marker.exists() and not capture.exists()


def test_wrapper_rejects_unmounted_environment_file(admin_fixture, tmp_path: Path) -> None:
    _, wrapper, _, capture, environment = admin_fixture
    result = subprocess.run(
        [str(wrapper), "--env-file", str(tmp_path / "outside.env"), "status"],
        env=environment, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "inside the repository mount" in result.stderr
    assert not capture.exists()
