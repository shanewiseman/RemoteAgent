# syntax=docker/dockerfile:1.7

ARG DOCKER_CLI_VERSION=29.7.2
FROM docker:${DOCKER_CLI_VERSION}-cli AS docker-cli

FROM node:22.19.0-bookworm-slim

ARG CODEX_CLI_VERSION=0.149.1
ARG REMOTEAGENT_VERSION=local
ARG REMOTEAGENT_UID=1000
ARG REMOTEAGENT_GID=1000

LABEL org.opencontainers.image.title="RemoteAgent Codex runtime" \
      org.opencontainers.image.description="Pinned, ready-to-run Codex CLI agent base image" \
      org.opencontainers.image.version="${REMOTEAGENT_VERSION}" \
      io.remoteagent.codex.version="${CODEX_CLI_VERSION}"

ENV DEBIAN_FRONTEND=noninteractive \
    CODEX_HOME=/home/agent/.codex \
    REMOTEAGENT_WORKSPACE=/workspace \
    REMOTEAGENT_ARTIFACTS=/workspace/artifacts \
    REMOTEAGENT_SKILLS=/opt/remoteagent/skills

# The Node slim image omits the system trust bundle. Bootstrap it from the
# pinned Docker CLI stage so APT can use HTTPS from its first request; Debian's
# ca-certificates package then replaces it with the distribution copy.
COPY --from=docker-cli /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt

RUN sed -i \
        -e 's|http://deb.debian.org|https://deb.debian.org|g' \
        -e 's|http://security.debian.org|https://security.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::ForceIPv4=true update \
    && apt-get -o Acquire::ForceIPv4=true install --yes --no-install-recommends \
        bubblewrap \
        build-essential \
        ca-certificates \
        curl \
        git \
        jq \
        make \
        openssh-client \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@openai/codex@${CODEX_CLI_VERSION}" \
    && npm cache clean --force

RUN if ! getent group "${REMOTEAGENT_GID}" >/dev/null; then \
        groupadd --gid "${REMOTEAGENT_GID}" agent; \
       fi \
    && existing_user="$(getent passwd "${REMOTEAGENT_UID}" | cut -d: -f1 || true)" \
    && if [ "$existing_user" = node ]; then \
            usermod --login agent --home /home/agent --move-home \
                --gid "${REMOTEAGENT_GID}" --shell /bin/bash "$existing_user"; \
       elif [ "$existing_user" = agent ]; then \
            usermod --gid "${REMOTEAGENT_GID}" --shell /bin/bash agent; \
       elif [ -z "$existing_user" ]; then \
        useradd --uid "${REMOTEAGENT_UID}" --gid "${REMOTEAGENT_GID}" \
            --create-home --home-dir /home/agent --shell /bin/bash agent; \
       else \
        echo "REMOTEAGENT_UID collides with incompatible base user: $existing_user" >&2; \
        exit 2; \
       fi \
    && install -d -m 0755 /etc/codex \
    && install -d -o agent -g "${REMOTEAGENT_GID}" \
        /home/agent/.codex \
        /home/agent/.codex/sessions \
        /opt/remoteagent/agent \
        /opt/remoteagent/skills \
        /workspace

ENV HOME=/home/agent

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --chmod=0444 runtime/codex-requirements.toml /etc/codex/requirements.toml
COPY --chown=agent common-skills/ /opt/remoteagent/skills/
COPY --chown=agent runtime/codex-config.toml /home/agent/.codex/config.toml
COPY --chmod=0755 runtime/agent-entrypoint.sh /usr/local/bin/remoteagent-agent-entrypoint

USER agent
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/remoteagent-agent-entrypoint"]
CMD ["codex", "--version"]
