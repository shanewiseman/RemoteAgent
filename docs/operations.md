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

`doctor` also starts an ephemeral, command-network-disabled runner with a dummy
`auth.json`. It verifies that the image's managed requirements are immutable,
Bubblewrap starts under the Compose security model, the dummy credential is
unreadable to a sandboxed command, and the conversation workspace remains
writable. A separate no-network preflight verifies that the effective
`code_mode_host` feature is enabled and its pinned executable sibling starts.
These probes consume no Codex subscription/API capacity and never mount the
real auth volume.

After any managed-network, Codex CLI, or critic-image change, also run the
non-model policy probe:

```sh
scripts/remotectl smoke network
```

It first proves ordinary container egress and private-service reachability, then
proves default-agent denial to the otherwise allowlisted `example.com`, critic
HTTPS access to that probe host, and critic loopback/private-service/Unix-socket
denial through app-server `command/exec`; it also proves denial to one reachable
unlisted public hostname. A separate source contract test pins the full
allowlist (`example.com`, the Python and npm registries, and the Go proxy/sum
service). The probe does not claim that plain HTTP or alternate ports/methods on
an admitted host, link-local/metadata destinations, DNS rebinding,
upstream-proxy bypass, or every possible Unix-socket path was actively probed.

Credential-changing auth commands refuse to run while the router is active,
because running jobs may be reading the shared Codex credential volume. Stop
the router, perform login/import/logout, then start it again. `auth import`
preserves the current `REMOTEAGENT_AUTH_MODE`; set the intended mode in `.env`
before importing a credential file of a different type.

`stop` preserves containers and all data. `down` removes core containers and
networks but never volumes. `--force` may interrupt active jobs and should be
reserved for a failed drain or unhealthy daemon.

## Readiness and scheduler health

`GET /health` and `/healthz` report process liveness only. Bearer-protected
`/readyz` reports a structured top-level `ready`, `degraded`, or `not_ready`
state with database, cache, scheduler, cron, and dashboard components. Database
availability and every configured scheduler worker are mandatory when
`REMOTEAGENT_SCHEDULER_ENABLED=true`; a missing worker or database failure
returns `503`. Redis/cache, cron, and dashboard problems are reported as
optional degradation with HTTP `200`, and a disabled scheduler is explicitly
non-mandatory. `doctor` requires a fully ready response and treats degradation
as a failed operational check.

## Job deadlines and cleanup

`REMOTEAGENT_JOB_TIMEOUT_SECONDS` is an absolute end-to-end deadline measured
from the durable job creation timestamp, not a new timeout started when Codex
runs. Queueing, revision/workspace/companion preparation, lease wait,
dependency provisioning, Codex execution, artifact collection, and success
persistence all consume the same four-hour default budget. Work that expires
before claim becomes `expired`; work that exceeds the deadline after claim
becomes `failed`. User cancellation becomes `cancelled`, while router shutdown
or lease loss becomes `interrupted`.

Runtime release and fenced lease release are independently bounded by
`REMOTEAGENT_JOB_CLEANUP_TIMEOUT_SECONDS`, 60 seconds by default. Runtime
cleanup failure is logged without rewriting a durable terminal outcome. Lease
cleanup preserves an original lifecycle failure; if an otherwise successful
attempt cannot release its lease before terminal commit, the job fails rather
than reporting success with uncertain ownership.

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

Cron worker tests cover startup recovery, occurrence ownership and
deduplication, ambiguous submission replay, post-dispatch/cleanup retries,
deadline cancellation, and response-lease fencing. A real FastMCP Streamable
HTTP test covers bearer initialization, typed calls, malformed payloads, and
idempotent replay across the router/cron boundary. These deterministic checks
do not replace the local PostgreSQL or recovery smokes described below.

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

`agent validate` and API registration use the same dependency-free validator on
Docker Compose's resolved JSON model. It requires exactly the declared runner
and dependencies, one agent-owned network, the exact ordered platform mounts,
required labels and runner identity/isolation, bounded resources/logs, prebuilt
bounded dependencies, and health timing within the Compose wait budget. Unsafe
ports, namespaces, sockets, binds, services, profiles, volumes, and ownership
controls are rejected rather than delegated to review. Health-command semantics
and trusted image contents still require review. The reference projects pass
`scripts/remotectl validate --all`.

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

Restore is destructive. Before stopping services or replacing either durable
store, it verifies the strict outer archive and checksums, validates and extracts
the two allowed runtime trees, rejects special archive members and symlink
restore targets, and uses an isolated PostgreSQL tool container with networking
disabled and no live volumes or secrets to list and fully render the dump as
SQL. It then stops cron, router, Redis, and PostgreSQL and verifies quiescence
before it moves the existing conversation and
artifact-store trees into owner-only staging; installs the verified replacement
trees; and starts PostgreSQL alone.

One `psql --single-transaction` invocation resets and restores only the
application `public` schema and reconciles unclaimed companion-stage rows whose
ephemeral bytes were intentionally excluded. This removes current objects
absent from an older archive, including cron tables when restoring a pre-cron
backup. A tree-swap failure occurs before database mutation and restores the old
trees. Failure to start PostgreSQL restores both prior trees before any SQL
mutation. A database failure rolls back the transaction and then restores both old
trees. If either rollback cannot complete, all core services remain stopped and
the retained staging directory is reported for manual recovery. Only after the
database commit and both tree replacements succeed are the predecessors
removed; router starts with PostgreSQL and Redis, then cron starts after router
health. This is coordinated rollback across two durability systems, not a
single atomic database/filesystem transaction. Take and verify an external copy
before restoring over a working deployment.

The repository includes a disposable recovery harness for current and pre-cron
archives, continuation and content hashes, removal of newer objects, the tree
and database rollback branches, rollback failure, and router-before-cron startup
order. Its CLI/focused tests and the live local recovery smoke pass, including
disposable cleanup. This one local drill does not establish a scheduled/off-host
backup cadence, CI recovery gate, RPO, or RTO.

## Cleanup

`scripts/remotectl cleanup` is a dry run. `cleanup --apply --yes` only removes
terminal job containers carrying this deployment's instance label and older
than the selected cutoff. It never infers database state from directory names
and does not delete any conversation or job data on disk. No command invokes an
unscoped `docker system prune`.

The router's retention worker separately commits database deletion intent
before external filesystem deletion. At startup and on every retention pass it
reconciles the artifact store against active jobs and durable artifact rows.
With the default `REMOTEAGENT_ARTIFACT_ORPHAN_GRACE_SECONDS=3600`, only stale
paths beneath confined, syntactically valid job directories are eligible;
symlinks are removed without being followed. Every path failure is isolated and
retried later. Conversation tombstones and their job inventory remain durable
until workspace and artifact cleanup both succeed, preserving retry evidence.

## Acceptance smoke tests

```sh
scripts/remotectl smoke network
scripts/remotectl smoke postgres
scripts/remotectl smoke recovery --timeout 1800
scripts/remotectl smoke live --agent joke-agent --timeout 300
scripts/remotectl smoke live --agent repository-critic --timeout 900
```

`smoke postgres` refuses a non-local Docker context, starts one uniquely named
PostgreSQL 17.6 container and volume on an ephemeral loopback port, runs both
migration chains with four observed advisory-lock waiters, and exercises router lease contention/fencing,
conversation sequencing, companion claims, and cron response leases. Cleanup
of its exact, ownership-labeled container and volume runs even after an
ambiguous creation failure. The local release-verification run passed with
both resources confirmed removed. CI and off-host recovery remain deferred.

`smoke recovery` creates unique state, secrets, project, port, containers,
networks, and volumes without reading or writing the configured deployment. It
exercises the real backup/verify/restore commands and the recovery cases listed
above, then removes the disposable project and state. Its default total timeout
is 1,800 seconds, with a separate shared 90-second cleanup budget. Cleanup
verifies the absence of project containers, volumes, and networks before
removing local state; a cleanup failure preserves the exact project and recovery
files and reports both the primary and cleanup errors. The recorded local run
passed current and pre-cron restore,
continuation identity, companion/artifact hashes, newer-object removal, all
three rollback failpoints, router-before-cron startup, and disposable cleanup.
Repeat it on the reviewed local Docker daemon for future release qualification;
it is not a CI, scheduled, or off-host recovery control.

The joke workflow discovers the agent, submits one new asynchronous prompt,
polls it, then submits a second turn with the same conversation key. Unique
markers prove prompt transport and conversational memory; structural checks
verify one `JOKE:` paragraph. It does not attempt to score humor.

The critic workflow uploads a bounded repository companion whose documented
authorization rule disagrees with its implementation and whose denial branch
is untested. It verifies snapshot rather than pull-request framing, evidence
links, test/coverage provenance, high-priority risk reporting, bounded artifact
shape, unchanged companion content, and suppression of an install-hook
sentinel. An unlocked dependency result must publish its generated lock and be
marked non-reproducible.

The network probe invokes no model and needs no Codex authentication, but it
does require both built images and public Docker egress. Both `smoke live`
commands refuse to run in CI and consume authenticated Codex capacity. On
critic-smoke failure the stage, job, and conversation identifiers that exist
are reported and retained for diagnosis. Review and remove any disclosed critic
scratch residue before retrying; normal success cleans scratch and deletes the
smoke conversation.
