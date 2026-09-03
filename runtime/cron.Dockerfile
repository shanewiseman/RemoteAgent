# syntax=docker/dockerfile:1.7

FROM python:3.12.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY cron/ /build/
RUN sed -i \
        -e 's|http://deb.debian.org|https://deb.debian.org|g' \
        -e 's|http://security.debian.org|https://security.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources \
    && python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install .

FROM python:3.12.11-slim-bookworm AS runtime

ARG REMOTEAGENT_VERSION=local
ARG REMOTEAGENT_UID=1000
ARG REMOTEAGENT_GID=1000

LABEL org.opencontainers.image.title="RemoteAgent cron service" \
      org.opencontainers.image.description="Internal durable cron scheduler for RemoteAgent MCP agents" \
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
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true install --yes --no-install-recommends ca-certificates tini tzdata \
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
       fi

COPY --from=builder /opt/venv /opt/venv
COPY cron/alembic.ini /opt/remoteagent/cron/alembic.ini
COPY cron/migrations/ /opt/remoteagent/cron/migrations/
COPY --chmod=0755 runtime/cron-entrypoint.sh /usr/local/bin/remoteagent-cron-entrypoint

USER remoteagent
WORKDIR /opt/remoteagent/cron
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/remoteagent-cron-entrypoint"]
CMD ["remoteagent-cron"]
