#!/bin/sh
set -eu

umask 077

workspace=${REMOTEAGENT_WORKSPACE:-/workspace}
artifacts=${REMOTEAGENT_ARTIFACTS:-${workspace}/artifacts}
sessions=${REMOTEAGENT_SESSIONS:-${CODEX_HOME:-/home/agent/.codex}/sessions}
skills=${REMOTEAGENT_SKILLS:-/opt/remoteagent/skills}

mkdir -p "$workspace" "$artifacts" "$sessions" "${CODEX_HOME:-/home/agent/.codex}"

if [ ! -w "$workspace" ] || [ ! -w "$artifacts" ] || [ ! -w "$sessions" ]; then
    echo "remoteagent: conversation workspace, artifacts, and sessions must be writable" >&2
    exit 73
fi

if [ ! -e "$workspace/.git" ]; then
    git -C "$workspace" init --quiet
fi

if [ -d "$skills" ] && [ ! -e "${CODEX_HOME:-/home/agent/.codex}/skills" ]; then
    ln -s "$skills" "${CODEX_HOME:-/home/agent/.codex}/skills"
fi

exec "$@"
