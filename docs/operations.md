# Operations

For the exact Docker objects affected by each command and the backup boundary,
see [Docker runtime and Compose contract](docker-runtime.md). Unmet production
controls remain visible in the [requirements baseline](requirements.md).

## Routine commands

```sh
scripts/remotectl status
scripts/remotectl doctor
scripts/remotectl logs router --follow --tail 200
scripts/remotectl logs cron --follow --tail 200
scripts/remotectl auth status
scripts/remotectl token status all
scripts/remotectl migrate status all
scripts/remotectl agent list
```

`doctor` also starts an ephemeral, network-disabled runner with a dummy
`auth.json`. It verifies that the image's managed requirements are immutable,
Bubblewrap starts under the Compose security model, the dummy credential is
unreadable to a sandboxed command, and the conversation workspace remains
writable. This probe consumes no Codex subscription/API capacity and never
mounts the real auth volume.

Credential-changing auth commands refuse to run while the router is active,
because running jobs may be reading the shared Codex credential volume. Stop
the router, perform login/import/logout, then start it again. `auth import`
preserves the current `REMOTEAGENT_AUTH_MODE`; set the intended mode in `.env`
before importing a credential file of a different type.

`stop` preserves containers and all data. `down` removes core containers and
networks but never volumes. `--force` may interrupt active jobs and should be
reserved for a failed drain or unhealthy daemon.

## Cron schedules

Schedules are managed through the seven cron MCP tools, not through public
REST or `remotectl`. The cron container is backend-only and starts after the
router is healthy. `doctor` calls cron's authenticated readiness endpoint from
inside the container; that check covers its database/schema state and scoped
MCP handshake. Router readiness reports cron loss as degraded information but
does not make core routing unavailable.

The default operational bounds are a 24-hour run timeout, five-minute response
lease, 50-record/4-MiB retrieval batch, 90-day unacknowledged response
retention, and 24-hour acknowledgement tombstones. Values are configured in
`.env` in seconds/bytes and require cron recreation to change.

Cron and router have independent Alembic histories. `migrate status` and
`migrate apply` default to `all`; pass `router` or `cron` to inspect/apply only
one chain. Normal service startup applies both chains under their own locks.

When upgrading a 0.1.x deployment, run
`scripts/remotectl init --non-interactive` from the new checkout before
`upgrade check`. This idempotently creates only missing cron tokens and extends
the internal Host allowlist; it does not rotate existing credentials.

Internal tokens should not be distributed to users:

```sh
scripts/remotectl token status cron
scripts/remotectl token rotate cron --yes
```

Rotating either internal token recreates router and cron together. `cron-mcp`
and `cron-api` can be selected separately; `all` also rotates the external
router bearer. Rotation interrupts active scheduled work, so use an idle
maintenance window.

## Dashboard

Open `http://SERVER:8080/dashboard/login` from the trusted internal network and
enter the same bearer token used by MCP callers. The dashboard creates a
server-side Redis session and never stores the bearer token in browser storage.
Its overview, agents, jobs, conversations, companion metadata, artifacts, and
system pages are read-only operational views; companion bytes cannot be opened
or downloaded there. Use `scripts/remotectl token print` only in a
private terminal, and close it after copying the token into the login form.

Completed-job telemetry reports Codex's terminal input, cached-input, output,
and reasoning-output totals as exact when the CLI emits them. The dashboard
labels locally visible prompt/base-context estimates separately and marks
hidden Codex instructions, tool schemas, internal context, and credential
material unavailable; contributor estimates are not presented as if they sum
to the exact provider total. Raw model reasoning is never stored or displayed.

Job and conversation history show the stored `model` and `reasoning_effort`
execution profile used for continuation. A `null` value means the router left
that selector to the current agent revision and then Codex/account inheritance;
it does not identify which catalog default Codex ultimately selected. These
fields are durable audit attributes, not token-attribution estimates, and do
not expose reasoning content.

`/metrics` is bearer-protected and exposes low-cardinality HTTP, auth, SSE,
token, and companion stage status/byte/duration metrics suitable for Prometheus.
Companion instruments label only bounded kind/status values and never URLs,
names, stage/job IDs, or conversation IDs. Current agent/job/conversation/artifact
counts remain available from the dashboard's database-backed summary. For
temporary troubleshooting, set `REMOTEAGENT_DASHBOARD_DEBUG_ENABLED=true` and
recreate the router to enable sanitized read-only diagnostics and job events.
Disable it again after diagnosis; debug output redacts secrets,
filesystem/controller details, and reasoning content.

Plain-HTTP dashboard cookies are enabled for this internal-only V1. After adding
a TLS reverse proxy, set `REMOTEAGENT_DASHBOARD_ALLOW_HTTP=false`, recreate the
router, and confirm forwarded scheme/host headers before relying on secure
cookies.

## Companion staging

Companion staging is managed through the authenticated REST/MCP interfaces, not
through `remotectl`. Uploaded bytes use REST; public Git import, stage polling,
and metadata listing are available through both supported surfaces as described
in the [API guide](api.md). The cron service cannot attach companions.

The reference defaults are 100 MiB per upload, archive expansion, Git mirror,
and selected checkout; 20,000 files per tree; 20 bindings per turn; 200 active
logical names; 1 GiB of accepted immutable sources per conversation; 5 GiB of
deployment-wide unclaimed staging; 24-hour stage expiry; two Git workers; and a
five-minute import timeout. Set the corresponding `REMOTEAGENT_COMPANION_*`
values in `.env` and recreate the router to change them. Capacity values are
admission bounds, not disk monitoring; continue alerting on `.runtime` free
space. The public contract fixes the maximum at 20 bindings per turn, so that
deployment setting can only lower the admitted count.

The background reconciler deletes expired/failed/unclaimed stage bytes. On
restart it removes partial Git state and requeues interrupted imports. It also
reconciles database/filesystem gaps: a restored unclaimed stage row whose
ephemeral bytes were deliberately excluded from backup becomes failed or
expired rather than claimable. Errors exposed to clients are bounded and do not
contain credential, proxy, or subprocess configuration.

Accepted immutable sources and editable working copies live under the
conversation tree and follow its archive, retention, backup, restore, and
tombstone-first deletion lifecycle. Terminal-job retention must not delete
companions still owned by a conversation. There is no standalone removal API;
delete the conversation through the existing guarded flow when its complete
state should be removed. Reusing a logical name on a later prompt atomically
replaces its working version and discards prior edits under that name.

## Agents

```sh
scripts/remotectl agent new research-agent --name "Research Agent"
scripts/remotectl agent validate research-agent
scripts/remotectl agent build research-agent --pull
scripts/remotectl agent register research-agent
```

Registration is idempotent for an unchanged definition. Replacing an existing
definition requires `--replace`; revisions are retained so existing job history
remains interpretable.

Agent `model` and `model_reasoning_effort` settings resolve a non-null default
when a new conversation is created. Callers can select a different profile on
that initial request. A non-null conversation field cannot switch in place:
continuations must omit it or repeat the stored value, and an intentional switch
requires a new conversation key. A null field remains dynamic inheritance and
may therefore follow a later agent revision or Codex account default. The router
validates selector syntax but does not expose or preflight the authenticated
Codex model catalog, so operators diagnose unavailable models or unsupported
model/effort combinations from the eventual failed job.

The schema migration that introduces these fields leaves pre-upgrade
conversation and job profiles `null`. This preserves their previous Codex/config
inheritance behavior. Normal startup/upgrade migration is additive; no workspace
or Codex session rewrite is required, and old clients that omit the new request
fields continue to work.

## Shared skills

On first creation Docker seeds the common volume from the agent base image.
After changing checked-in skills, merge them into the existing volume with:

```sh
scripts/remotectl build core
scripts/remotectl skills sync
```

`skills sync --prune --yes` mirrors the source and deletes stale skill folders;
review active agents before using it.

## Backup and restore

```sh
scripts/remotectl backup create
scripts/remotectl backup verify .runtime/backups/remoteagent-TIMESTAMP.tar.gz
scripts/remotectl restore FILE --yes
```

Backups contain a PostgreSQL custom dump—including cron-owned schedules,
executions, responses, and leases—conversation state, immutable served
artifact copies, accepted companion sources and working copies, a version
manifest, and checksums. Ephemeral `.runtime/companion-staging` is intentionally
excluded. Backups also exclude
all bearer tokens, the PostgreSQL password, Codex credentials, logs, images,
and build caches. Disaster recovery therefore requires retaining or rotating
deployment secrets, running Codex login again, and distributing the current/new
external router bearer token.

For database/filesystem consistency, backup first stops cron so no new scheduled
turn can enter the router, then refuses to interrupt queued or active router
work. When the router is idle, it opens a brief maintenance window, stops the
router, captures both stores, and restarts router then cron even if backup
fails. PostgreSQL stays available throughout.

Restore is destructive, stops cron before router and Redis, verifies checksums
first, and replaces database/runtime state. It renders the SQL archive before
mutation, then drops/recreates only the application `public` schema and restores
it in one database transaction. This removes current objects absent from an
older archive—for example cron tables when restoring a 0.1 backup—while leaving
the prior schema intact if a restore statement fails. After filesystem state is
swapped, the command always starts PostgreSQL/Redis/router, waits for router
health and migrations, then starts and waits for cron. Take and verify an
external copy before restoring over a working deployment.

## Cleanup

`scripts/remotectl cleanup` is a dry run. `cleanup --apply --yes` only removes
terminal job containers carrying this deployment's instance label and older
than the selected cutoff. It never infers database state from directory names
and does not delete any conversation or job data on disk. No command invokes an
unscoped `docker system prune`.

## Live smoke test

```sh
scripts/remotectl smoke live --agent joke-agent --timeout 300
```

The smoke workflow discovers the agent, submits one new asynchronous prompt,
polls it, then submits a second turn with the same conversation key. Unique
markers prove prompt transport and conversational memory; structural checks
verify one `JOKE:` paragraph. It does not attempt to score humor. On failure it
retains identifiers for diagnosis. The command refuses to run in CI.
