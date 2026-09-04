#!/bin/sh
set -eu

umask 077

workspace=${REMOTEAGENT_WORKSPACE:-/workspace}
artifacts=${REMOTEAGENT_ARTIFACTS:-${workspace}/artifacts}
sessions=${REMOTEAGENT_SESSIONS:-${CODEX_HOME:-/home/agent/.codex}/sessions}
skills=${REMOTEAGENT_SKILLS:-/opt/remoteagent/skills}
runtime_dir=${XDG_RUNTIME_DIR:-/tmp/remoteagent-codex-runtime}

# Codex's managed network proxy needs a writable XDG runtime directory. Keep it
# inside the runner's existing per-container /tmp tmpfs; do not introduce a host
# bind or a second writable mount for proxy state.
case "$runtime_dir" in
    /tmp/?*) ;;
    *)
        echo "remoteagent: XDG_RUNTIME_DIR must remain beneath /tmp" >&2
        exit 73
        ;;
esac
case "$runtime_dir/" in
    *"/../"*|*"/./"*|*"//"*)
        echo "remoteagent: XDG_RUNTIME_DIR must be a normalized path beneath /tmp" >&2
        exit 73
        ;;
esac

mkdir -p "$workspace" "$artifacts" "$sessions" "${CODEX_HOME:-/home/agent/.codex}" \
    "$runtime_dir"
if [ -L "$runtime_dir" ] || [ ! -d "$runtime_dir" ]; then
    echo "remoteagent: XDG_RUNTIME_DIR must be a real directory" >&2
    exit 73
fi
chmod 0700 "$runtime_dir"
export XDG_RUNTIME_DIR="$runtime_dir"

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
