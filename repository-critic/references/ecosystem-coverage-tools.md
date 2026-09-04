# Ecosystem restore and coverage tools

The image manifest at `/opt/remoteagent/agent/toolchain-manifest.json` is the
source of truth for installed versions. Always validate dependency references,
use `restore-plan`, and execute each returned argv with `run`. These examples
explain the adapter; they are not permission to run an undocumented shell.

## Python 3.12

Official tools: [coverage.py](https://coverage.readthedocs.io/en/latest/),
[pytest](https://docs.pytest.org/), [pytest-cov](https://pytest-cov.readthedocs.io/),
[uv](https://docs.astral.sh/uv/), [Poetry](https://python-poetry.org/docs/), and
[Pipenv](https://pipenv.pypa.io/).

Lock preference is `uv.lock`, `poetry.lock`, `Pipfile.lock`, then a fully pinned
requirements file with hashes. Use no-root/project install and binary-only/no-
build settings where the manager supports them. An unlocked `pyproject.toml` or
requirements input may be resolved only in scratch; retain the generated lock,
`pip freeze --all`, source manifest hash, and available index integrity hashes.

Prefer a documented coverage command. For an ordinary pytest repository, the
generic form is conceptually:

```text
python3.12 -m coverage run --branch -m pytest
python3.12 -m coverage json -o <coverage-dir>/coverage.json
python3.12 -m coverage xml -o <coverage-dir>/coverage.xml
```

Respect repository `.coveragerc`, `pyproject.toml`, or equivalent source/omit
configuration and disclose it. Do not rewrite project source or configuration
to make instrumentation pass.

## JavaScript and TypeScript

Official tools: [Node test runner coverage](https://nodejs.org/api/test.html),
[Jest coverage configuration](https://jestjs.io/docs/configuration),
[Vitest coverage](https://vitest.dev/guide/coverage.html), and
[c8](https://github.com/bcoe/c8).

Use `npm ci`, frozen pnpm, or frozen/immutable Yarn according to the committed
lock and exact supported `packageManager` version. Suppress install scripts by
default. Reject a package-manager version absent from the image rather than
letting Corepack download it. Without a lock, resolve into scratch, retain the
generated lock and `npm ls --all --json` (or manager equivalent), and label the
run non-reproducible.

Project code runs on Node 22.19.0. The Corepack 0.36.0 shim and the package
managers it dispatches run on a private Node 22.22.2 support runtime to satisfy
Corepack's declared engine range. This is image plumbing, not permission to
select or download a different Node runtime for repository tests.

Prefer repository Jest/Vitest/Node coverage. Otherwise c8 can wrap the
documented test argv and emit JSON plus Cobertura under a dedicated coverage
directory. Include all configured source files when feasible; record glob and
source-map limitations. Do not run `build`, `prepare`, `postinstall`, `generate`,
or similarly named scripts unless the current-prompt build-hook sentinel exists.

## Go 1.27

Official guidance: [Go integration test coverage](https://go.dev/doc/build-cover),
[`go test`](https://pkg.go.dev/cmd/go#hdr-Test_packages), and
[`go tool cover`](https://pkg.go.dev/cmd/cover).

Prefer committed vendor state. Otherwise require `go.sum` for a locked restore;
an absent or changed sum is `resolved_unlocked` and must be retained as
evidence. Keep `GOTOOLCHAIN=local` and reject an incompatible `go` or `toolchain`
directive instead of downloading another toolchain.

The generic suite is:

```text
go test -count=1 -covermode=atomic -coverpkg=./... -coverprofile=<coverage-dir>/coverage.out -json ./...
go tool cover -func=<coverage-dir>/coverage.out
```

Go cover profiles report statement blocks. The normalized `lines` object uses
the covered/total statement counts from those blocks and identifies that metric
basis; `branches` and `functions` are `null`.

## Unsupported and partial projects

Do not improvise package-manager downloads, language installers, or coverage
plugins. Complete the static review, record the exact detected manifest/runtime
requirement, and use `unsupported_toolchain`. For monorepos, failure in one
project must not erase successful evidence from another.
