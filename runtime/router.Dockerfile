# syntax=docker/dockerfile:1.7

ARG DOCKER_CLI_VERSION=29.7.2
FROM docker:${DOCKER_CLI_VERSION}-cli AS docker-cli

FROM python:3.12.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY router/ /build/
RUN sed -i \
        -e 's|http://deb.debian.org|https://deb.debian.org|g' \
        -e 's|http://security.debian.org|https://security.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources \
    && python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install ".[postgres]"

FROM python:3.12.11-slim-bookworm AS runtime

ARG REMOTEAGENT_VERSION=local
ARG REMOTEAGENT_UID=1000
ARG REMOTEAGENT_GID=1000

LABEL org.opencontainers.image.title="RemoteAgent router" \
      org.opencontainers.image.description="HTTP MCP router for isolated Codex agent runtimes" \
      org.opencontainers.image.version="${REMOTEAGENT_VERSION}"

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    REMOTEAGENT_VERSION=${REMOTEAGENT_VERSION}

RUN sed -i \
        -e 's|http://deb.debian.org|https://deb.debian.org|g' \
        -e 's|http://security.debian.org|https://security.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::ForceIPv4=true update \
    && apt-get -o Acquire::ForceIPv4=true install --yes --no-install-recommends ca-certificates curl git tini \
    && rm -rf /var/lib/apt/lists/* \
    && if ! getent group "${REMOTEAGENT_GID}" >/dev/null; then \
        groupadd --gid "${REMOTEAGENT_GID}" remoteagent; \
       fi \
    && existing_user="$(getent passwd "${REMOTEAGENT_UID}" | cut -d: -f1 || true)" \
    && if [ -n "$existing_user" ] && [ "$existing_user" != remoteagent ]; then \
        echo "REMOTEAGENT_UID collides with incompatible base user: $existing_user" >&2; \
        exit 2; \
       elif [ -z "$existing_user" ]; then \
        useradd --uid "${REMOTEAGENT_UID}" --gid "${REMOTEAGENT_GID}" \
            --create-home --home-dir /home/remoteagent --shell /usr/sbin/nologin remoteagent; \
       fi \
    && install -d -o remoteagent -g "${REMOTEAGENT_GID}" /var/lib/remoteagent

COPY --from=builder /opt/venv /opt/venv
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY router/alembic.ini /opt/remoteagent/router/alembic.ini
COPY router/migrations/ /opt/remoteagent/router/migrations/
COPY --chmod=0755 runtime/router-entrypoint.sh /usr/local/bin/remoteagent-router-entrypoint

USER remoteagent
WORKDIR /opt/remoteagent/router
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/remoteagent-router-entrypoint"]
CMD ["uvicorn", "remoteagent.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
