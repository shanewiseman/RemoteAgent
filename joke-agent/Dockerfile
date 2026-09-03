# syntax=docker/dockerfile:1.7

ARG REMOTEAGENT_VERSION=local
FROM remoteagent/agent-base:${REMOTEAGENT_VERSION}

LABEL org.opencontainers.image.title="RemoteAgent Joke Agent" \
      org.opencontainers.image.description="Built-in topical clean-joke and live smoke-test agent" \
      io.remoteagent.agent.id="joke-agent" \
      io.remoteagent.builtin="true"

COPY --chown=agent joke-agent/agent.toml joke-agent/config.toml joke-agent/AGENTS.md /opt/remoteagent/agent/
