# syntax=docker/dockerfile:1.7

ARG REMOTEAGENT_VERSION=local
FROM remoteagent/agent-base:${REMOTEAGENT_VERSION}

ARG AGENT_ID=__AGENT_ID__
LABEL io.remoteagent.agent.id="${AGENT_ID}"

COPY --chown=agent __AGENT_ID__/agent.toml __AGENT_ID__/config.toml __AGENT_ID__/AGENTS.md /opt/remoteagent/agent/

# Install agent-specific dependencies here at image-build time. Runtime turns
# must not perform package installation.
