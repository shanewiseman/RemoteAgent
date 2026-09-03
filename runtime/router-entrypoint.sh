#!/bin/sh
set -eu

umask 077

read_secret() {
    secret_path=$1
    secret_name=$2

    if [ ! -r "$secret_path" ]; then
        echo "remoteagent: required ${secret_name} secret is not readable: ${secret_path}" >&2
        exit 78
    fi

    secret_value=$(tr -d '\r\n' < "$secret_path")
    if [ -z "$secret_value" ]; then
        echo "remoteagent: required ${secret_name} secret is empty" >&2
        exit 78
    fi
    printf '%s' "$secret_value"
}

if [ -n "${REMOTEAGENT_BEARER_TOKEN_FILE:-}" ]; then
    BEARER_TOKEN=$(read_secret "$REMOTEAGENT_BEARER_TOKEN_FILE" "router bearer token")
    export BEARER_TOKEN
    REMOTEAGENT_BEARER_TOKEN=$BEARER_TOKEN
    export REMOTEAGENT_BEARER_TOKEN
    # Compatibility alias for dashboard/config consumers using a prefixed key.
    ROUTER_BEARER_TOKEN=$BEARER_TOKEN
    export ROUTER_BEARER_TOKEN
fi

if [ -z "${DATABASE_URL:-}" ]; then
    database_password=$(read_secret "${REMOTEAGENT_DATABASE_PASSWORD_FILE:-/run/secrets/postgres_password}" "PostgreSQL password")
    encoded_password=$(printf '%s' "$database_password" | python -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')
    unset database_password

    database_user=${POSTGRES_USER:-remoteagent}
    database_host=${POSTGRES_HOST:-postgres}
    database_port=${POSTGRES_PORT:-5432}
    database_name=${POSTGRES_DB:-remoteagent}
    DATABASE_URL="postgresql+asyncpg://${database_user}:${encoded_password}@${database_host}:${database_port}/${database_name}"
    unset encoded_password
    export DATABASE_URL
    REMOTEAGENT_DATABASE_URL=$DATABASE_URL
    export REMOTEAGENT_DATABASE_URL
fi

if [ "${REMOTEAGENT_AUTO_MIGRATE:-false}" = "true" ]; then
    alembic -c /opt/remoteagent/router/alembic.ini upgrade head
fi

exec "$@"
