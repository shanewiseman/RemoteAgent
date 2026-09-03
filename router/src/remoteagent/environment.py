from __future__ import annotations

import os
from collections.abc import Mapping

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
    return environment
