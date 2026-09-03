# Agent authoring

The complete enforced-versus-review-only container contract is documented in
[Docker runtime and Compose contract](docker-runtime.md). Runtime registration
and client-visible fields are specified in the [API guide](api.md).

Each agent occupies one top-level folder whose name equals its lowercase agent
ID. Start from the template:

```sh
scripts/remotectl agent new release-notes --name "Release Notes"
```

The folder contains:

- `agent.toml`: discovery/runtime manifest.
- `AGENTS.md`: durable base context exposed through the router.
- `config.toml`: Codex CLI settings applied while the subscription lease is held.
- `Dockerfile`: dependencies installed at image-build time.
- `compose.yaml`: runner service and isolation/mount contract.

## Manifest

Required fields are `id`, `name`, `description`, `compose_file`,
`runner_service`, and `enabled`. Keep `compose_file`, `config_file`, and
`base_context_file` relative to the agent directory. The router resolves paths,
rejects symlink/traversal escapes, embeds config/context into the registered
revision, and retains past revisions for auditability.

Do not put credentials in TOML, base context, image layers, Compose environment,
or metadata. Agent-specific non-secret settings belong in the manifest
`[environment]` table.

## Compose contract

The router creates and passes these absolute host paths for every job:

- `REMOTEAGENT_WORKSPACE_PATH`
- `REMOTEAGENT_SESSIONS_PATH`
- `REMOTEAGENT_ARTIFACTS_PATH`

Mount them at `/workspace`, `$CODEX_HOME/sessions`, and
`/workspace/artifacts`. Mount the external shared auth volume at `$CODEX_HOME`
first and the isolated sessions directory second. Mount the common skills volume
read-only. Never set `container_name`; Compose must support multiple jobs and
cleanup by project/labels.

The router overrides the service command with `codex exec --json -` or
`codex exec resume --json THREAD_ID -`. Keep the shared
`remoteagent-agent-entrypoint`: it validates mounts, initializes an empty Git
repository, links common skills, and then executes that command. The router also
bind-mounts its revision-controlled `AGENTS.md` and `config.toml` directly at
`/workspace/AGENTS.md` and `$CODEX_HOME/config.toml` as read-only files; agent
Compose files must not shadow those paths with their own mounts.

Keep the template's complete isolation stanza, including non-root `user`,
read-only root, capability drop, `no-new-privileges`, and both
`seccomp=unconfined` and `apparmor=unconfined`. The last two allow the nested
Codex/Bubblewrap sandbox to create its namespace; removing either makes normal
Codex tool execution fail on the reference host. They do not replace the baked
managed Codex requirements policy.

Agent `config.toml` is intentionally narrower than a general interactive Codex
configuration. It may select normal model/reasoning/output settings and either
`read-only` or `workspace-write`, but it may not define custom model providers,
backend URLs, MCP servers, hooks, notification commands, plugins/apps, arbitrary
file-backed instructions/skills, named permission profiles, or additional
writable roots. Those features execute outside or can weaken the local command
sandbox and therefore require a reviewed platform release.

Top-level `model` and `model_reasoning_effort` values are defaults for new
RemoteAgent conversations. A caller may override them with the `model` and
`reasoning_effort` fields on its initial prompt. The router persists the resolved
nullable pair and reapplies non-null values to every new or resumed Codex run,
so later agent-config changes do not change an already explicit conversation
profile. Caller-selected values are execution settings, not new agent revisions.

Model identifiers must match `[a-z0-9][a-z0-9._-]{0,127}`. Supported request
values for reasoning effort are `minimal`, `low`, `medium`, `high`, `xhigh`,
`max`, and `ultra`. This is static input validation only: the router does not
currently expose the authenticated Codex model catalog or preflight whether the
account, model, and effort combination is available. Such a mismatch is
accepted into the asynchronous queue and later produces a failed job with the
Codex execution error.

The implementation follows OpenAI's documented
[one-off Codex CLI override](https://learn.chatgpt.com/docs/config-file/config-advanced#one-off-overrides-from-the-cli)
precedence: it uses the dedicated `--model` flag and the generic `--config`
override for `model_reasoning_effort`. These command-level values take precedence
over the read-only `config.toml` mounted for the turn.

## Dependency services

Agents may declare Compose services such as a browser, database, or local model
in `dependency_services`. The router starts only those named services, waits for
their Compose health checks, runs the short-lived `runner_service`, then retains
healthy dependencies for a bounded warm TTL before taking them down. Do not use
`container_name`; concurrent conversations need project-scoped names.

Every dependency must have a meaningful health check with a finite
`start_period`, interval, timeout, and retry count. Bind it only to the agent's
private Compose network, persist data in explicitly named project volumes when
required, and add CPU/memory/PID limits. Never publish a dependency port on the
host unless the agent's reviewed design requires it. Choose a warm TTL that
amortizes startup without leaving expensive or credential-bearing services
running indefinitely; zero disables warming. Dependency startup failure must
fail the job rather than silently running the agent without the service.

## Dependencies, companions, and artifacts

Install every dependency in the Dockerfile and pin material versions. A turn
must not wait for package managers or mutate the image. The base image already
contains pinned Codex CLI, Node, Python, Git, ripgrep, Bubblewrap, build tools,
and Docker CLI/Compose for trusted agent workflows.

Caller-bound companion data is exposed through stable names beneath
`/workspace/companions`. The router-owned prompt preamble lists each active
name, path, kind, version, digest, and resolved Git commit. Treat the files as
working reference inputs: the agent may edit them, and those edits persist into
later turns in the conversation. A later same-name binding atomically replaces
the complete working copy and discards prior edits under that name.

Do not assume a companion appears before its introducing sequence. An earlier
active turn cannot see an addition submitted with a later turn, while a
cancelled introducing turn leaves the accepted companion eligible for the next
runnable turn. Do not rewrite `.remoteagent/companions` or the stable links;
address companions only through the paths named in the preamble. Staging never
runs repository hooks, uploaded executables, package managers, submodule
fetches, or Git LFS downloads, so an agent that needs any such behavior must
make an explicit, reviewed decision during its normal sandboxed turn.

Write caller-requested files beneath `REMOTEAGENT_ARTIFACTS`. Do not publish
temporary files, auth material, environment dumps, or files obtained from an
untrusted path without validation. Editing a companion does not make it
downloadable and the router never publishes companion content automatically;
copy only intentional deliverables to `/workspace/artifacts`.

## Validation

```sh
scripts/remotectl agent validate release-notes
scripts/remotectl agent build release-notes
scripts/remotectl agent register release-notes
```

Validation checks schema essentials, directory identity, contained paths, safe
Codex settings, required dependency health checks, and the fully resolved
Compose model without contacting OpenAI.
