# Architecture

This overview is descriptive. Normative behavior and implementation status live
in the [requirements baseline](requirements.md); client and container boundaries
are specified in the [API](api.md) and [Docker runtime](docker-runtime.md)
contracts.

## Control plane

The FastAPI router is the only host-facing service. It mounts Streamable
HTTP MCP at `/mcp`, exposes health/readiness endpoints, and persists registry,
job, conversation, revision, and artifact metadata in PostgreSQL. PostgreSQL is
also authoritative for companion-stage claims, conversation companion versions,
and the global subscription lease. Redis backs short-lived
coordination, dashboard sessions, and activity events. Neither database is
published on the host.

The router has access to the Docker socket and invokes the Compose project named
by each registered agent. Definitions are loaded from the root phonebook or
registered at runtime through the same validation service. Manifest, Compose,
config, and context paths must remain inside that agent's top-level directory;
symlink and `..` escapes are rejected.

The sibling FastAPI cron service is reachable only on the internal backend
network. The router maps seven typed MCP tools to its bearer-protected
`/internal/v1` API. In the other direction, cron uses a distinct, scoped MCP
token that can call only `list_agents`, `get_agent`, `submit_prompt`,
`get_prompt_status`, and `cancel_prompt`. Cron never uses router REST, Redis,
the Docker socket, or router database tables.
Its `submit_prompt` role rejects non-empty companion bindings, so scheduled
turns cannot stage or attach companion data.

Cron owns schedules, immutable revisions, executions, response queue records,
leases, and acknowledgement tombstones in separate PostgreSQL tables and a
separate Alembic version chain. A schedule occurrence is durably recorded
before MCP submission. Its idempotency key derives from the schedule generation
UUID and scheduled UTC instant, allowing a restart to retry an ambiguous submit
without creating a second turn. Stored router job IDs are polled through MCP;
only successful terminal jobs create response records.

## Job lifecycle

1. A caller may first stream a file/safe archive or queue a public HTTPS Git
   mirror into ephemeral staging. Git imports resolve the requested ref (or
   remote `HEAD`) to one commit before becoming ready.
2. The caller submits a prompt, optionally selecting a `model` and
   `reasoning_effort` and binding up to 20 ready, single-use stage IDs to safe
   logical names.
3. The router resolves each selector from caller input, then the agent's
   explicit Codex configuration, then Codex/account inheritance. It stores the
   resulting nullable execution profile on the conversation and job.
4. Under stage-row and conversation locking, the router copies/reflinks verified
   sources into the conversation tree, persists the job and pending companion
   versions, commits, and only then returns the accepted job with its profile
   and an echo of the additions. Idempotency replay is resolved before
   validating or claiming any newly supplied stages.
5. The scheduler waits for every predecessor turn, then acquires the
   PostgreSQL-backed deployment-wide Codex
   subscription lease and a per-conversation lock.
6. It selects the latest eligible companion version for each name, materializes
   an editable working copy, atomically switches the stable link, repairs prior
   links/copies, and then materializes config/context. Preparation failure ends
   the job before Codex and leaves the pending version retryable by a later turn.
7. Agent Compose starts a short-lived container using the prebuilt image.
8. A new conversation runs `codex exec --json -`; the router records the
   `thread.started` ID. A continuation runs `codex exec resume --json ID -` with
   the exact recorded ID and isolated sessions mount. Non-null model/reasoning
   selectors are applied as one-run CLI overrides on both commands.
9. JSONL progress and final output update the durable job. Files beneath the
   artifact directory are hashed, registered, and exposed as MCP resources.
10. The job container is removed; conversation data and editable companion
    working copies remain for continuation.

Pollers must tolerate skipping transient states. The durable terminal states are
`succeeded`, `failed`, `cancelled`, `interrupted`, and `expired`.

The built-in repository critic is an ordinary instance of this lifecycle. It
reviews one bound repository companion as a complete snapshot, copies it into
job-scoped workspace scratch, runs image-baked Python/JavaScript/Go coverage
adapters, and writes bounded report, coverage, provenance, and log artifacts.
It adds no synchronous review route or cron path. Its immutable agent config is
the sole checked-in command-network opt-in; the root-owned managed Codex policy
supplies an exact six-host dependency/probe allowlist plus local/private
destination denial while other agents remain command-network-off.

## Storage boundaries

- PostgreSQL: authoritative application metadata and state.
- Cron-owned PostgreSQL tables: authoritative schedule, execution, response,
  and response-lease state; there are no cross-service foreign keys.
- Redis: transient coordination, dashboard sessions, and activity events.
- `.runtime/conversations/<key>/workspace`: caller/agent working files.
- `.runtime/conversations/<key>/inputs/objects`: immutable accepted companion
  sources; these are outside the workspace so an earlier active turn cannot see
  a later queued addition.
- `.runtime/conversations/<key>/workspace/.remoteagent/companions`: editable
  versioned working copies.
- `.runtime/conversations/<key>/workspace/companions`: stable atomic name links
  exposed as `/workspace/companions/<name>`.
- `.runtime/conversations/<key>/sessions`: Codex rollout data for exact resume.
- `.runtime/conversations/<key>/artifacts`: publishable output files.
- `.runtime/conversations/<key>/control`: router-owned effective config/context.
- `remoteagent-codex-auth`: shared file-backed Codex credentials.
- `remoteagent-common-skills`: common read-only skills mounted by every agent.
- `.runtime/companion-staging`: expiring unclaimed upload/import state; excluded
  from backups and reconciled after restart.

The router bind-mounts `control/AGENTS.md` at `/workspace/AGENTS.md` and
`control/config.toml` at `$CODEX_HOME/config.toml`, both read-only. This prevents
a prompt from changing the effective revision used by its own turn. The global
PostgreSQL subscription lease separately limits execution to the configured
ChatGPT subscription capacity.

Companion activation is sequence-bounded. A version introduced by a later
queued turn is invisible to an earlier running turn. Once predecessors are
terminal, the latest eligible version per name becomes active even when its
introducing job was cancelled or failed. Same-name replacement leaves the
previous link usable until the new copy is complete, then marks older versions
superseded and reclaims their source/working bytes while retaining metadata.
Agent edits survive later turns until such a replacement.

For turns with active companions, the router constructs an effective prompt by
prepending a versioned block of name, stable path, kind, version, digest, and Git
commit metadata plus the `/workspace/artifacts` output rule. PostgreSQL retains
the caller's raw prompt unchanged; runtime metadata records visible companion
IDs and preamble version, and token accounting observes the effective prompt.
With no active companions, the prompt is unchanged byte-for-byte.

PostgreSQL also stores the conversation's nullable model/reasoning profile and
copies it onto every job for auditability. At creation, caller-supplied values
take precedence over the mounted agent configuration. A continuation may omit
the selectors or repeat the exact stored values; a different explicit value
conflicts because a model change requires a new conversation. `null`
deliberately records that
the current agent revision and ultimately Codex/account defaults remain
authoritative. RemoteAgent does not resolve or snapshot the name of that
inherited default, so a null field may follow a later agent revision.

Cron schedules support fresh or persistent conversation modes. Every execution
uses its snapshotted schedule revision. Agent, model, reasoning-effort, or mode
changes clear future persistent continuity; prompt and timing changes preserve
it. A still-active execution causes later occurrences to be counted as skipped,
not queued. Startup, resume, and schedule changes choose the next future cron
boundary and never catch up missed occurrences.

## Path contract

`.env` stores absolute `REMOTEAGENT_REPO_ROOT` and
`REMOTEAGENT_STATE_ROOT`. The router mounts each at the identical path inside
its container. This is intentional: child bind mounts are interpreted by the
host Docker daemon, so translating those paths inside the router would make
agent startup fail or mount the wrong directory.
