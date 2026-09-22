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

# Codex installs its bundled .system skills beneath CODEX_HOME/skills. Keep
# that directory writable while individual common skills resolve into the
# read-only shared volume. Older images linked the entire directory there.
skills_home=${CODEX_HOME:-/home/agent/.codex}/skills
if [ -L "$skills_home" ]; then
    if [ "$(readlink "$skills_home")" != "$skills" ]; then
        echo "remoteagent: refusing to replace an unrelated CODEX_HOME/skills symlink" >&2
        exit 73
    fi
    rm -- "$skills_home"
fi
if [ -e "$skills_home" ] && [ ! -d "$skills_home" ]; then
    echo "remoteagent: CODEX_HOME/skills must be a directory" >&2
    exit 73
fi
mkdir -p "$skills_home"
if [ ! -w "$skills_home" ]; then
    echo "remoteagent: CODEX_HOME/skills must be writable for bundled system skills" >&2
    exit 73
fi

# Reconcile only links with the exact shape created here. Removed common skills
# disappear on the next turn; unrelated custom skills and .system stay intact.
for skill_link in "$skills_home"/*; do
    [ -L "$skill_link" ] || continue
    skill_name=${skill_link##*/}
    expected_target=$skills/$skill_name
    if [ "$(readlink "$skill_link")" = "$expected_target" ] \
        && [ ! -f "$expected_target/SKILL.md" ]; then
        rm -- "$skill_link"
    fi
done
if [ -d "$skills" ]; then
    for skill_source in "$skills"/*; do
        [ -d "$skill_source" ] && [ -f "$skill_source/SKILL.md" ] || continue
        skill_name=${skill_source##*/}
        skill_link=$skills_home/$skill_name
        if [ -L "$skill_link" ] && [ "$(readlink "$skill_link")" = "$skill_source" ]; then
            continue
        fi
        if [ -e "$skill_link" ] || [ -L "$skill_link" ]; then
            echo "remoteagent: common skill collides with existing CODEX_HOME/skills/$skill_name" >&2
            exit 73
        fi
        ln -s "$skill_source" "$skill_link"
    done
fi

exec "$@"
