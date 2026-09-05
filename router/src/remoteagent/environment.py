from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path

# Only deployment values required by Docker/agent Compose interpolation cross
# this boundary. In particular, router bearer tokens, PostgreSQL credentials,
# DATABASE_URL, and unrelated host API keys are never inherited.
SAFE_HOST_ENVIRONMENT = frozenset(
    {
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "REMOTEAGENT_AGENT_CPU_LIMIT",
        "REMOTEAGENT_AGENT_MEMORY_LIMIT",
        "REMOTEAGENT_AUTH_VOLUME",
        "REMOTEAGENT_BUILD_NETWORK",
        "REMOTEAGENT_GID",
        "REMOTEAGENT_INSTANCE_ID",
        "REMOTEAGENT_SKILLS_VOLUME",
        "REMOTEAGENT_UID",
        "REMOTEAGENT_VERSION",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
    }
)

RESERVED_AGENT_ENVIRONMENT = SAFE_HOST_ENVIRONMENT | frozenset(
    {
        "DATABASE_URL",
        "COMPOSE_DISABLE_ENV_FILE",
        "COMPOSE_ENV_FILES",
        "REMOTEAGENT_ARTIFACTS_PATH",
        "REMOTEAGENT_BEARER_TOKEN",
        "REMOTEAGENT_CONVERSATION_KEY",
        "REMOTEAGENT_DATABASE_URL",
        "REMOTEAGENT_JOB_ID",
        "REMOTEAGENT_SESSIONS_PATH",
        "REMOTEAGENT_WORKSPACE_PATH",
        "ROUTER_BEARER_TOKEN",
    }
)


def safe_compose_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(extra or {})
    environment.update(
        {name: os.environ[name] for name in SAFE_HOST_ENVIRONMENT if name in os.environ}
    )
    # Compose must not repopulate omitted credentials from an implicit .env or
    # an inherited alternate env-file after this boundary has filtered them.
    environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
    environment["COMPOSE_ENV_FILES"] = ""
    return environment


def main() -> int:
    """Launch local agent Compose with the same interpolation boundary as the router."""

    if len(sys.argv) < 3:
        print("usage: environment.py MANIFEST [compose arguments...]", file=sys.stderr)
        return 2
    try:
        with Path(sys.argv[1]).open("rb") as handle:
            manifest = tomllib.load(handle)
        extra = manifest.get("environment", {})
        if not isinstance(extra, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in extra.items()
        ):
            raise ValueError("agent environment must contain string keys and values")
        forbidden = set(extra) & RESERVED_AGENT_ENVIRONMENT
        forbidden.update(name for name in extra if name.startswith("DOCKER_"))
        if forbidden:
            raise ValueError(
                f"agent environment uses controller-reserved keys: {sorted(forbidden)}"
            )
        environment = safe_compose_environment(extra)
        # These values belong to the caller/controller, never the manifest.
        # Validation uses fixed placeholders; runtime/build wrappers may supply
        # their own paths without forwarding unrelated deployment credentials.
        for name in (
            "REMOTEAGENT_ARTIFACTS_PATH",
            "REMOTEAGENT_CONVERSATION_KEY",
            "REMOTEAGENT_JOB_ID",
            "REMOTEAGENT_SESSIONS_PATH",
            "REMOTEAGENT_WORKSPACE_PATH",
        ):
            if name in os.environ:
                environment[name] = os.environ[name]
        os.execvpe(
            "docker",
            ["docker", "compose", "--env-file", os.devnull, *sys.argv[2:]],
            environment,
        )
    except (OSError, ValueError) as exc:
        print(f"agent Compose launch failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
