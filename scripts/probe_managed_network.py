#!/usr/bin/env python3
"""Exercise managed command networking without invoking a model.

The probe uses the built runner images and app-server ``command/exec`` so each
command receives the same computed config policy used by normal Codex turns.
Docker egress is proved independently before each sandbox assertion so an
unavailable network cannot masquerade as a successful denial test.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import uuid
from collections.abc import Sequence


PUBLIC_URL = "https://example.com/"
UNLISTED_PUBLIC_URL = "https://www.iana.org/"
TARGET_PORT = 8765
COMMAND_TIMEOUT_SECONDS = 120
PINNED_CODEX_VERSION = "0.149.1"


APP_SERVER_COMMAND_FUNCTION = rf"""
set -eu
test "$(codex --version)" = "codex-cli {PINNED_CODEX_VERSION}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 0700 "$XDG_RUNTIME_DIR"

run_managed_command() {{
    python3 - "$@" <<'PY'
import json
import os
import subprocess
import sys


network_access = os.environ.get("PROBE_NETWORK_ACCESS")
if network_access not in {{"true", "false"}}:
    raise RuntimeError("PROBE_NETWORK_ACCESS must be true or false")

server = subprocess.Popen(
    [
        "codex",
        "app-server",
        "-c",
        f"sandbox_workspace_write.network_access={{network_access}}",
        "--stdio",
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
    encoding="utf-8",
)


def send(message):
    assert server.stdin is not None
    server.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    server.stdin.flush()


def receive(request_id):
    assert server.stdout is not None
    for line in server.stdout:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Codex app-server emitted invalid JSON") from exc
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(f"Codex app-server request failed: {{message['error']!r}}")
            return message["result"]
    raise RuntimeError("Codex app-server exited before replying")


try:
    send({{
        "id": 1,
        "method": "initialize",
        "params": {{
            "clientInfo": {{"name": "remoteagent-network-probe", "version": "1"}},
            "capabilities": {{"experimentalApi": True}},
        }},
    }})
    receive(1)
    send({{"method": "initialized"}})
    send({{
        "id": 2,
        "method": "command/exec",
        "params": {{
            "command": sys.argv[1:],
            "cwd": "/workspace",
            "outputBytesCap": 8192,
            "timeoutMs": 30000,
        }},
    }})
    result = receive(2)
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    raise SystemExit(result["exitCode"])
finally:
    if server.stdin is not None:
        server.stdin.close()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)
PY
}}
"""


class ProbeFailure(RuntimeError):
    pass


DEFAULT_POLICY_SCRIPT = APP_SERVER_COMMAND_FUNCTION + r"""
export PROBE_NETWORK_ACCESS=false
test -r /etc/codex/requirements.toml
curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
    "$PROBE_PUBLIC_URL" >/dev/null
run_managed_command true >/dev/null
if run_managed_command \
    curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
        "$PROBE_PUBLIC_URL" >/dev/null 2>&1; then
    echo 'default agent unexpectedly reached the allowlisted probe host' >&2
    exit 70
fi
"""


CRITIC_POLICY_SCRIPT = APP_SERVER_COMMAND_FUNCTION + rf"""
set -eu
export PROBE_NETWORK_ACCESS=true
curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
    "$PROBE_PUBLIC_URL" >/dev/null
curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
    "$PROBE_UNLISTED_PUBLIC_URL" >/dev/null
curl --fail --silent --show-error --retry 20 --retry-connrefused --retry-delay 0 \
    --connect-timeout 1 --max-time 20 \
    "http://$PROBE_PRIVATE_HOST:{TARGET_PORT}/" >/dev/null

python3 -m http.server {TARGET_PORT} --bind 127.0.0.1 >/tmp/network-probe-http.log 2>&1 &
probe_server=$!
probe_socket=/workspace/.remoteagent-network-probe.sock
cat > /tmp/network-probe-unix.py <<'PY'
import socketserver
import sys


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(4096)
        self.request.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")


with socketserver.UnixStreamServer(sys.argv[1], Handler) as server:
    server.serve_forever()
PY
python3 /tmp/network-probe-unix.py "$probe_socket" >/tmp/network-probe-unix.log 2>&1 &
unix_server=$!
cleanup_probe_servers() {{
    kill "$probe_server" "$unix_server" >/dev/null 2>&1 || true
    wait "$probe_server" "$unix_server" >/dev/null 2>&1 || true
    rm -f "$probe_socket"
}}
trap cleanup_probe_servers EXIT
curl --fail --silent --show-error --retry 20 --retry-connrefused --retry-delay 0 \
    --connect-timeout 1 --max-time 20 "http://127.0.0.1:{TARGET_PORT}/" >/dev/null
probe_socket_attempts=0
while [ ! -S "$probe_socket" ]; do
    probe_socket_attempts=$((probe_socket_attempts + 1))
    if [ "$probe_socket_attempts" -ge 50 ]; then
        echo 'Unix-socket probe fixture did not become ready' >&2
        exit 75
    fi
    sleep 0.1
done
curl --fail --silent --show-error --retry 20 --retry-connrefused --retry-delay 0 \
    --connect-timeout 1 --max-time 20 --unix-socket "$probe_socket" \
    http://localhost/ >/dev/null

run_managed_command true >/dev/null
run_managed_command \
    curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
        "$PROBE_PUBLIC_URL" >/dev/null
if run_managed_command \
    curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
        "$PROBE_UNLISTED_PUBLIC_URL" >/dev/null 2>&1; then
    echo 'critic command sandbox unexpectedly reached an unlisted public host' >&2
    exit 74
fi
if run_managed_command \
    curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
        "http://127.0.0.1:{TARGET_PORT}/" >/dev/null 2>&1; then
    echo 'critic command sandbox unexpectedly reached its loopback service' >&2
    exit 71
fi
if run_managed_command \
    curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
        "http://$PROBE_PRIVATE_HOST:{TARGET_PORT}/" >/dev/null 2>&1; then
    echo 'critic command sandbox unexpectedly reached a private Docker service' >&2
    exit 72
fi
if run_managed_command \
    curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
        --unix-socket "$probe_socket" http://localhost/ >/dev/null 2>&1; then
    echo 'critic command sandbox unexpectedly reached a local Unix socket' >&2
    exit 73
fi
"""


def _bounded_detail(completed: subprocess.CompletedProcess[str]) -> str:
    detail = "\n".join(
        value.strip() for value in (completed.stdout, completed.stderr) if value.strip()
    )
    if not detail:
        return "no output"
    encoded = detail.encode("utf-8", errors="replace")
    if len(encoded) > 8 * 1024:
        return encoded[-8 * 1024 :].decode("utf-8", errors="replace")
    return detail


def run_checked(
    argv: Sequence[str],
    *,
    label: str,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeFailure(f"{label} could not complete: {exc}") from exc
    if completed.returncode != 0:
        raise ProbeFailure(
            f"{label} failed with exit {completed.returncode}: "
            f"{_bounded_detail(completed)}"
        )
    return completed


def sandbox_runner_command(
    *,
    image: str,
    config_path: pathlib.Path,
    network: str,
    uid: int,
    gid: int,
    script: str,
    private_host: str,
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        network,
        "--read-only",
        "--user",
        f"{uid}:{gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--security-opt",
        "seccomp=unconfined",
        "--security-opt",
        "apparmor=unconfined",
        "--pids-limit",
        "256",
        "--memory",
        "1g",
        "--tmpfs",
        f"/tmp:rw,exec,nosuid,nodev,size=32m,mode=1777,uid={uid},gid={gid}",
        "--tmpfs",
        f"/home/agent/.codex:rw,exec,nosuid,nodev,size=32m,mode=0700,uid={uid},gid={gid}",
        "--tmpfs",
        f"/workspace:rw,exec,nosuid,nodev,size=32m,mode=0700,uid={uid},gid={gid}",
        "--mount",
        (f"type=bind,src={config_path},dst=/home/agent/.codex/config.toml,readonly"),
        "--env",
        f"PROBE_PUBLIC_URL={PUBLIC_URL}",
        "--env",
        f"PROBE_UNLISTED_PUBLIC_URL={UNLISTED_PUBLIC_URL}",
        "--env",
        f"PROBE_PRIVATE_HOST={private_host}",
        "--env",
        "XDG_RUNTIME_DIR=/tmp/remoteagent-codex-runtime",
        "--entrypoint",
        "/bin/sh",
        image,
        "-ceu",
        script,
    ]


def _integer_environment(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ProbeFailure(f"{name} must be an integer") from exc
    if value < 0:
        raise ProbeFailure(f"{name} must not be negative")
    return value


def probe() -> dict[str, object]:
    repository = pathlib.Path(__file__).resolve().parents[1]
    version = os.environ.get("REMOTEAGENT_VERSION", "local")
    uid = _integer_environment("REMOTEAGENT_UID", 1000)
    gid = _integer_environment("REMOTEAGENT_GID", 1000)
    default_config = repository / "runtime" / "codex-config.toml"
    critic_config = repository / "repository-critic" / "config.toml"
    for config in (default_config, critic_config):
        if not config.is_file():
            raise ProbeFailure(f"missing managed-network probe config: {config}")

    base_image = f"remoteagent/agent-base:{version}"
    critic_image = f"remoteagent/repository-critic:{version}"
    suffix = uuid.uuid4().hex[:12]
    network = f"remoteagent-network-probe-{suffix}"
    target = f"remoteagent-network-target-{suffix}"
    network_created = False
    target_started = False
    try:
        for image in (base_image, critic_image):
            run_checked(
                ["docker", "image", "inspect", image],
                label=f"required image {image}",
            )
        run_checked(
            [
                "docker",
                "network",
                "create",
                "--label",
                "io.remoteagent.network-probe=true",
                network,
            ],
            label="probe network creation",
        )
        network_created = True
        run_checked(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--pull",
                "never",
                "--network",
                network,
                "--name",
                target,
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=8m,mode=1777",
                "--entrypoint",
                "python3",
                base_image,
                "-m",
                "http.server",
                str(TARGET_PORT),
                "--bind",
                "0.0.0.0",
            ],
            label="private probe target startup",
        )
        target_started = True

        run_checked(
            sandbox_runner_command(
                image=base_image,
                config_path=default_config,
                network=network,
                uid=uid,
                gid=gid,
                script=DEFAULT_POLICY_SCRIPT,
                private_host=target,
            ),
            label="default-agent allowlisted-host denial",
        )
        run_checked(
            sandbox_runner_command(
                image=critic_image,
                config_path=critic_config,
                network=network,
                uid=uid,
                gid=gid,
                script=CRITIC_POLICY_SCRIPT,
                private_host=target,
            ),
            label="repository-critic managed-network policy",
        )
    finally:
        if target_started:
            subprocess.run(
                ["docker", "rm", "--force", target],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        if network_created:
            subprocess.run(
                ["docker", "network", "rm", network],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

    return {
        "ok": True,
        "public_url": PUBLIC_URL,
        "checks": {
            "default_allowlisted_https_denied": True,
            "critic_allowlisted_https_allowed": True,
            "critic_unlisted_public_denied": True,
            "critic_loopback_denied": True,
            "critic_private_service_denied": True,
            "critic_unix_socket_denied": True,
        },
    }


def main() -> int:
    result = probe()
    if os.environ.get("REMOTEAGENT_NETWORK_PROBE_JSON", "0") == "1":
        print(json.dumps(result, sort_keys=True))
    else:
        print("managed network probe passed with codex-cli 0.149.1")
        print("default example.com=denied; critic allowlisted example.com HTTPS=allowed")
        print("critic unlisted public/loopback/private service/Unix socket=denied")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProbeFailure as exc:
        print(f"remoteagent: managed network probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
