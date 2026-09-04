# syntax=docker/dockerfile:1.7

# These multi-platform image indexes are immutable. The corresponding values
# are repeated in toolchain-manifest.json so a report can cite its toolchain.
ARG PYTHON_TOOLCHAIN_IMAGE=python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
ARG GO_TOOLCHAIN_IMAGE=golang:1.27.1-bookworm@sha256:648f440f42a0958804efb24df176f806f9d353b41f1c0627f666428e40310f6b
ARG COREPACK_RUNTIME_IMAGE=node:22.22.2-bookworm-slim@sha256:9f6d5975c7dca860947d3915877f85607946403fc55349f39b4bc3688448bb6e
ARG REMOTEAGENT_VERSION=local

FROM ${PYTHON_TOOLCHAIN_IMAGE} AS python-toolchain

COPY repository-critic/python-tools.lock /tmp/python-tools.lock
COPY repository-critic/coverage-tools.lock /tmp/coverage-tools.lock
RUN python -m pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        --only-binary=:all: \
        --require-hashes \
        --requirement /tmp/python-tools.lock \
    && mkdir -p /opt/python-wheelhouse \
    && python -m pip download \
        --disable-pip-version-check \
        --no-cache-dir \
        --only-binary=:all: \
        --require-hashes \
        --requirement /tmp/coverage-tools.lock \
        --dest /opt/python-wheelhouse \
    && rm /tmp/python-tools.lock /tmp/coverage-tools.lock

FROM ${GO_TOOLCHAIN_IMAGE} AS go-toolchain

FROM ${COREPACK_RUNTIME_IMAGE} AS corepack-runtime

FROM remoteagent/agent-base:${REMOTEAGENT_VERSION}

USER root

LABEL org.opencontainers.image.title="RemoteAgent Repository Critic" \
      org.opencontainers.image.description="Whole-repository documentation, test coverage, and architecture critic" \
      io.remoteagent.agent.id="repository-critic" \
      io.remoteagent.builtin="true" \
      io.remoteagent.review.mode="repository_snapshot"

# Python's official slim image installs CPython and its packages wholly beneath
# /usr/local. Go uses the same relocatable /usr/local/go layout.
COPY --from=python-toolchain /usr/local/ /usr/local/
COPY --from=python-toolchain /opt/python-wheelhouse/ /opt/remoteagent/agent/python-wheelhouse/
COPY --from=go-toolchain /usr/local/go/ /usr/local/go/
COPY --from=corepack-runtime /usr/local/bin/node /opt/remoteagent/corepack-node/node

ENV PATH="/usr/local/go/bin:/usr/local/bin:${PATH}" \
    COREPACK_HOME=/opt/remoteagent/corepack \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    GOTOOLCHAIN=local \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1

# c8 and Corepack are installed from a checked-in npm v3 lock with registry
# integrities. Corepack validates registry integrity metadata while preparing
# the exact manager versions; Corepack does not expose an immutable checked-in
# cache format, so this residual build-time registry dependency is recorded in
# toolchain-manifest.json and never occurs during a review turn.
COPY --chown=root:root repository-critic/node-tools/ /opt/remoteagent/node-tools/
COPY --chown=root:root repository-critic/corepack-shim /opt/remoteagent/corepack-shim
RUN set -eux; \
    cd /opt/remoteagent/node-tools \
    && /opt/remoteagent/corepack-node/node \
        /usr/local/lib/node_modules/npm/bin/npm-cli.js \
        ci --engine-strict --ignore-scripts --no-audit --no-fund \
    && test "$(node --version)" = "v22.19.0" \
    && test "$(npm --version)" = "10.9.3" \
    && test "$(/opt/remoteagent/corepack-node/node --version)" = "v22.22.2" \
    && test "$(/opt/remoteagent/corepack-node/node \
        ./node_modules/corepack/dist/corepack.js --version)" = "0.36.0" \
    && node -e \
        'if (require("/opt/remoteagent/node-tools/node_modules/c8/package.json").version !== "12.0.0") process.exit(1)' \
    && ./node_modules/.bin/c8 --help >/dev/null \
    && rm -f /usr/local/bin/corepack /usr/local/bin/c8 \
        /usr/local/bin/pnpm /usr/local/bin/pnpx \
        /usr/local/bin/yarn /usr/local/bin/yarnpkg \
    && install -m 0755 /opt/remoteagent/corepack-shim /usr/local/bin/corepack-shim \
    && ln -s /usr/local/bin/corepack-shim /usr/local/bin/corepack \
    && ln -s /usr/local/bin/corepack-shim /usr/local/bin/pnpm \
    && ln -s /usr/local/bin/corepack-shim /usr/local/bin/pnpx \
    && ln -s /usr/local/bin/corepack-shim /usr/local/bin/yarn \
    && ln -s /usr/local/bin/corepack-shim /usr/local/bin/yarnpkg \
    && ln -s /opt/remoteagent/node-tools/node_modules/.bin/c8 /usr/local/bin/c8 \
    && install -d -m 0755 "${COREPACK_HOME}" \
    && corepack install --global yarn@1.22.22 \
    && test "$(yarn --version)" = "1.22.22" \
    && corepack install --global yarn@4.18.0 \
    && test "$(yarn --version)" = "4.18.0" \
    && corepack install --global pnpm@9.15.9 \
    && test "$(pnpm --version)" = "9.15.9" \
    && corepack install --global pnpm@10.34.5 \
    && test "$(pnpm --version)" = "10.34.5" \
    && corepack install --global pnpm@11.25.0 \
    && test "$(pnpm --version)" = "11.25.0" \
    && rm -rf /root/.npm \
    && find "${COREPACK_HOME}" -type d -exec chmod 0555 {} + \
    && find "${COREPACK_HOME}" -type f -exec chmod 0444 {} + \
    && chmod 0555 /opt/remoteagent/corepack-node/node

ENV COREPACK_ENABLE_NETWORK=0

COPY --chown=root:root repository-critic/agent.toml \
     repository-critic/config.toml \
     repository-critic/AGENTS.md \
     repository-critic/toolchain-manifest.json \
     /opt/remoteagent/agent/
COPY --chown=root:root repository-critic/coverage-tools.lock /opt/remoteagent/agent/coverage-tools.lock
COPY --chown=root:root repository-critic/references/ /opt/remoteagent/agent/references/
COPY --chown=root:root repository-critic/tools/ /opt/remoteagent/agent/tools/
COPY --chown=root:root repository-critic/build-fixtures/ /opt/remoteagent/build-fixtures/

RUN set -eux; \
    chmod 0444 \
        /opt/remoteagent/agent/agent.toml \
        /opt/remoteagent/agent/config.toml \
        /opt/remoteagent/agent/AGENTS.md \
        /opt/remoteagent/agent/toolchain-manifest.json \
        /opt/remoteagent/agent/coverage-tools.lock \
    && find /opt/remoteagent/agent/references -type d -exec chmod 0555 {} + \
    && find /opt/remoteagent/agent/references -type f -exec chmod 0444 {} + \
    && find /opt/remoteagent/agent/tools -type d -exec chmod 0555 {} + \
    && find /opt/remoteagent/agent/tools -type f -exec chmod 0555 {} + \
    && ln -s /opt/remoteagent/agent/tools/repository_critic.py \
        /usr/local/bin/repository-critic-tools \
    && test "$(python3.12 -c 'import platform; print(platform.python_version())')" = "3.12.14" \
    && test "$(go version | awk '{print $3}')" = "go1.27.1" \
    && test "$(node --version)" = "v22.19.0" \
    && test "$(npm --version)" = "10.9.3" \
    && test "$(/opt/remoteagent/corepack-node/node --version)" = "v22.22.2" \
    && test "$(python3.12 -c 'import coverage; print(coverage.__version__)')" = "7.16.0" \
    && test "$(python3.12 -c 'import importlib.metadata; print(importlib.metadata.version("pytest-cov"))')" = "7.1.0" \
    && test "$(uv --version | awk '{print $1 " " $2}')" = "uv 0.12.9" \
    && test "$(poetry --version)" = "Poetry (version 2.4.2)" \
    && test "$(pipenv --version)" = "pipenv, version 2026.8.0" \
    && node -e \
        'if (require("/opt/remoteagent/node-tools/node_modules/c8/package.json").version !== "12.0.0") process.exit(1)' \
    && c8 --help >/dev/null \
    && test "$(corepack --version)" = "0.36.0" \
    && (cd /opt/remoteagent/build-fixtures/corepack/yarn-1 && test "$(yarn --version)" = "1.22.22") \
    && (cd /opt/remoteagent/build-fixtures/corepack/yarn-4 && test "$(yarn --version)" = "4.18.0") \
    && (cd /opt/remoteagent/build-fixtures/corepack/pnpm-9 && test "$(pnpm --version)" = "9.15.9") \
    && (cd /opt/remoteagent/build-fixtures/corepack/pnpm-10 && test "$(pnpm --version)" = "10.34.5") \
    && (cd /opt/remoteagent/build-fixtures/corepack/pnpm-11 && test "$(pnpm --version)" = "11.25.0") \
    && python3.12 -m venv /tmp/coverage-smoke \
    && uv pip install \
        --python /tmp/coverage-smoke/bin/python \
        --offline \
        --no-index \
        --find-links /opt/remoteagent/agent/python-wheelhouse \
        --require-hashes \
        --requirement /opt/remoteagent/agent/coverage-tools.lock \
    && /tmp/coverage-smoke/bin/python -m coverage --version \
    && /tmp/coverage-smoke/bin/python -c \
        'import importlib.util; assert importlib.util.find_spec("pytest") is None' \
    && (cd /opt/remoteagent/build-fixtures/python \
        && /tmp/coverage-smoke/bin/python -m coverage run --branch --source=. \
            --data-file=/tmp/python-coverage-data -m unittest discover \
        && /tmp/coverage-smoke/bin/python -m coverage json \
            --data-file=/tmp/python-coverage-data -o /tmp/python-coverage.json \
        && /tmp/coverage-smoke/bin/python -m coverage xml \
            --data-file=/tmp/python-coverage-data -o /tmp/python-coverage.xml) \
    && python3.12 -c \
        'import json; d=json.load(open("/tmp/python-coverage.json")); assert d["files"]["uncovered.py"]["summary"]["covered_lines"] == 0' \
    && (cd /opt/remoteagent/build-fixtures/javascript \
        && c8 --all --reporter=json --reporter=cobertura --reports-dir=/tmp/c8-coverage node --test) \
    && node -e \
        'const d=require("/tmp/c8-coverage/coverage-final.json"); const k=Object.keys(d).find(k=>k.endsWith("/uncovered.js")); if(!k || Object.values(d[k].s).some(Boolean)) process.exit(1)' \
    && (cd /opt/remoteagent/build-fixtures/go \
        && go test -count=1 -covermode=atomic -coverpkg=./... \
            -coverprofile=/tmp/go-coverage.out -json ./... \
        && go tool cover -func=/tmp/go-coverage.out) \
    && rm -rf /tmp/coverage-smoke /tmp/python-coverage-data \
        /tmp/python-coverage.json /tmp/python-coverage.xml /tmp/c8-coverage \
        /tmp/go-coverage.out

USER agent

# Qualify the immutable manager cache as the actual runtime user as well as in
# the root-owned assembly stage. No manager may attempt a network repair here.
RUN set -eux; \
    test "${COREPACK_ENABLE_NETWORK}" = "0" \
    && test ! -w /opt/remoteagent/agent/toolchain-manifest.json \
    && repository-critic-tools --help >/dev/null \
    && (cd /opt/remoteagent/build-fixtures/corepack/yarn-1 \
        && test "$(yarn --version)" = "1.22.22") \
    && (cd /opt/remoteagent/build-fixtures/corepack/yarn-4 \
        && test "$(yarn --version)" = "4.18.0") \
    && (cd /opt/remoteagent/build-fixtures/corepack/pnpm-9 \
        && test "$(pnpm --version)" = "9.15.9") \
    && (cd /opt/remoteagent/build-fixtures/corepack/pnpm-10 \
        && test "$(pnpm --version)" = "10.34.5") \
    && (cd /opt/remoteagent/build-fixtures/corepack/pnpm-11 \
        && test "$(pnpm --version)" = "11.25.0")
