# API and MCP client guide

RemoteAgent exposes two supported client interfaces:

- a versioned HTTP control API under `/api/v1`;
- a Model Context Protocol (MCP) server at `/mcp` using MCP Streamable HTTP.

The machine-readable HTTP contract is [OpenAPI 3.1](api/openapi.json). The
machine-readable MCP discovery snapshots are [tools/list](api/mcp-tools-list.json)
and [resources/templates/list](api/mcp-resource-templates-list.json).

Runtime MCP discovery is authoritative. The checked-in MCP files are reviewable
snapshots and drift detectors, not a replacement for `initialize`, `tools/list`,
and `resources/templates/list`. OpenAPI is not the MCP contract; it describes
the HTTP control API only.

## Standards references

- [OpenAPI Specification 3.1.1](https://spec.openapis.org/oas/v3.1.1.html)
- [MCP 2025-06-18 Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [MCP 2025-06-18 tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [MCP 2025-06-18 resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)

## Common connection settings

Examples in this document assume:

```sh
export REMOTEAGENT_URL="http://remoteagent.internal:8080"
read -rsp "RemoteAgent bearer token: " REMOTEAGENT_TOKEN
echo
```

The external router bearer has no per-caller or per-agent access control.
Anyone holding it can use every enabled agent, change agent configuration and
schedules, retrieve artifacts and cron responses, and consume Codex capacity.
Use HTTP only on the trusted internal network; use TLS before the connection
crosses an untrusted boundary.

The cron process uses a separate scoped bearer that is limited to
`list_agents`, `get_agent`, `submit_prompt`, `get_prompt_status`, and
`cancel_prompt`. It is rejected from REST, resources, metrics, dashboard routes,
administrative tools, and the cron-management tools themselves.

All versioned HTTP and MCP requests require:

```http
Authorization: Bearer <router-token>
```

Missing or invalid credentials return:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
Content-Type: application/json

{"detail":"missing or invalid bearer token"}
```

## HTTP control API

The checked-in [OpenAPI document](api/openapi.json) is the client-generation
contract for `/api/v1`. A running router serves the same document at
`/openapi.json`; that endpoint is itself bearer-protected.

### Routes

| Method and path | Success | Purpose |
| --- | --- | --- |
| `GET /api/v1/agents` | `200 AgentSummary[]` | Discover enabled agents; set `include_disabled=true` to include all. |
| `GET /api/v1/agents/{agent_id}` | `200 AgentView` | Read the effective definition, TOML, base context, and revision. |
| `POST /api/v1/agents` | `201 AgentView` | Register or explicitly replace an agent definition. |
| `PATCH /api/v1/agents/{agent_id}/configuration` | `200 AgentView` | Append an immutable effective config/context revision. |
| `POST /api/v1/companion-stages/uploads` | `201 CompanionStageView` | Stream one file or safe archive into a ready, single-use stage. |
| `POST /api/v1/companion-stages/git-imports` | `202 CompanionStageView` | Queue a full-history public HTTPS Git import. |
| `GET /api/v1/companion-stages/{stage_id}` | `200 CompanionStageView` | Poll stage state and metadata. |
| `POST /api/v1/jobs` | `202 PromptAccepted` | Queue a prompt and return without waiting for Codex. |
| `GET /api/v1/jobs` | `200 JobView[]` | List recent jobs, optionally filtered by conversation and status. |
| `GET /api/v1/jobs/{job_id}` | `200 JobView` | Poll one job and retrieve its terminal result or error. |
| `POST /api/v1/jobs/{job_id}/cancel` | `200 JobView` | Cancel a queued job or request cancellation of an active job. |
| `GET /api/v1/jobs/{job_id}/artifacts` | `200 ArtifactView[]` | List artifacts produced by a job. |
| `POST /api/v1/conversations/{conversation_key}/archive` | `204` | Archive a conversation after all jobs are terminal. |
| `DELETE /api/v1/conversations/{conversation_key}` | `204` | Delete a conversation and its isolated runtime data. |
| `GET /api/v1/conversations/{conversation_key}/companions` | `200 ConversationCompanionView[]` | List active and pending companions; `include_history=true` includes superseded versions. |
| `GET /api/v1/artifacts` | `200 ArtifactView[]` | List artifacts, optionally filtered by job or conversation. |
| `GET /api/v1/artifacts/{artifact_id}` | `200 ArtifactView` | Read artifact metadata. |
| `GET /api/v1/artifacts/{artifact_id}/content` | `200` or `206` bytes | Download immutable artifact content, with byte-range support. |

`GET /api/v1/jobs` accepts `conversation_key`, a `status` enum value, and a
`limit` from 1 through 1000. `GET /api/v1/artifacts` accepts `job_id` and
`conversation_key`. Artifact lists are currently capped by the service at 1000
records.

### Agent schemas and registration

`AgentSummary` contains:

```json
{
  "id": "joke-agent",
  "name": "Joke Agent",
  "description": "Responds with a prompt-influenced joke.",
  "enabled": true,
  "revision": 1
}
```

`AgentView` adds `compose_file`, `project_name`, `runner_service`,
`dependency_services`, `environment`, `labels`, `metadata`, `config_toml`, and
`base_context`.

Register an agent with:

```http
POST /api/v1/agents
Content-Type: application/json

{
  "definition": {
    "id": "release-notes",
    "name": "Release Notes",
    "description": "Produces release notes from a checked-out project.",
    "compose_file": "release-notes/compose.yaml",
    "runner_service": "agent",
    "dependency_services": [],
    "enabled": true,
    "config_toml": "model_reasoning_effort = \"medium\"\n",
    "base_context": "Write concise, evidence-based release notes.",
    "environment": {},
    "labels": {},
    "metadata": {}
  },
  "replace": false
}
```

The path is interpreted on the RemoteAgent server, not on the caller. It must
resolve inside the agent's top-level directory. The ID must be a lowercase slug
matching `^[a-z0-9][a-z0-9_-]{0,62}$`; the Compose project name is fixed to
`remoteagent-<agent-id>`. The runner and every declared dependency must exist in
the resolved Compose model, and dependency services must have enabled health
checks. Controller-reserved and `DOCKER_*` environment variables are rejected.

Registration is idempotent when the submitted definition is unchanged. A
different definition returns `409` unless `replace` is true. Replacement creates
a new immutable revision only when the effective definition differs.

Agent-managed Codex TOML accepts only the fail-closed scalar settings described
in [Agent authoring](agent-authoring.md). Custom providers, endpoints, hooks,
MCP servers, plugins, path readers, permission profiles, and extra writable roots
are rejected with `422`.

Update config or context without replacing the structural definition:

```http
PATCH /api/v1/agents/release-notes/configuration
Content-Type: application/json

{
  "config_toml": "model_reasoning_effort = \"high\"\n",
  "base_context": null
}
```

An omitted or `null` field preserves its current value. A no-op update does not
create another revision. `base_context` is limited to 1,000,000 characters.

### Asynchronous prompts and conversations

#### Conversation execution profile

`PromptRequest` accepts two optional conversation-level Codex selectors:

| Field | Accepted value | Meaning |
| --- | --- | --- |
| `model` | Lowercase identifier matching `[a-z0-9][a-z0-9._-]{0,127}` | Codex model to request. |
| `reasoning_effort` | `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or `ultra` | Reasoning effort to request from a compatible model. |

For a new conversation, the router resolves each field independently in this
order:

1. the value supplied by the caller;
2. the agent revision's explicit `model` or `model_reasoning_effort` value;
3. `null`, meaning no RemoteAgent override and normal Codex/account inheritance.

The resolved nullable pair is stored on the conversation and snapshotted on
every job. `PromptAccepted` and `JobView` both return `model` and
`reasoning_effort` as required-but-nullable fields. Here, “resolved” means the
explicit RemoteAgent execution profile; `null` does not mean that Codex ran
without a model. It means the router did not discover or override the
model/effort that Codex ultimately inherited.

For a continuation, omit either field to reuse its stored value. A supplied
field may exactly match its stored value; a different non-null value returns
`409`. Start a new conversation to change either selector. Non-null values are
reapplied to both `codex exec` and `codex exec resume`, using `--model` and a
`model_reasoning_effort` one-off configuration override. This follows OpenAI's
documented [Codex CLI one-off override](https://learn.chatgpt.com/docs/config-file/config-advanced#one-off-overrides-from-the-cli)
precedence and prevents a later agent configuration from replacing an explicit
conversation selection.

Validation is intentionally static. This release has no REST endpoint, MCP
tool, or resource for authenticated model-catalog discovery, and the router does
not preflight account availability or model/effort compatibility. A
syntactically valid but unavailable selection can therefore receive `202` and
later end as a `failed` job; clients must poll the normal status endpoint/tool.

Submit a new conversation:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer ${REMOTEAGENT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "joke-agent",
    "prompt": "Tell me a joke about database migrations.",
    "model": "gpt-5.6-terra",
    "reasoning_effort": "low",
    "idempotency_key": "example-20260901-turn-1"
  }' \
  "${REMOTEAGENT_URL}/api/v1/jobs"
```

The `202` response is immediate:

```json
{
  "job_id": "j_f8e1c91b4fbc421191603a1c05c6bba8",
  "conversation_key": "c_c9d88a03a6674affab2b8b68d6776597",
  "status": "queued",
  "model": "gpt-5.6-terra",
  "reasoning_effort": "low",
  "companion_additions": []
}
```

Poll without holding the submission connection open:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer ${REMOTEAGENT_TOKEN}" \
  "${REMOTEAGENT_URL}/api/v1/jobs/j_f8e1c91b4fbc421191603a1c05c6bba8"
```

Possible statuses are:

- non-terminal: `queued`, `provisioning`, `waiting_for_lease`, `running`,
  `collecting`;
- terminal: `succeeded`, `failed`, `cancelled`, `interrupted`, `expired`.

Pollers must tolerate skipped transient states. On `succeeded`, `result` contains
the agent's final text. On an unsuccessful terminal state, inspect `error`.
`usage` may contain non-negative `input_tokens`, `cached_input_tokens`,
`output_tokens`, and `reasoning_output_tokens`; it is `null` when the runtime did
not provide usable totals. Every `JobView` also includes the nullable `model` and
`reasoning_effort` snapshot and its bounded `companion_additions`, including
while the job is non-terminal.

Continue a conversation by sending its opaque key with a new idempotency key:

```json
{
  "agent_id": "joke-agent",
  "prompt": "Now make it about schema drift.",
  "conversation_key": "c_c9d88a03a6674affab2b8b68d6776597",
  "idempotency_key": "example-20260901-turn-2"
}
```

The router serializes turns in one conversation and resumes its exact stored
Codex thread. A conversation key cannot move to another agent. Keys supplied by
callers must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`; otherwise the router
generates a `c_`-prefixed key. Prompts are limited to 2,000,000 characters.

The continuation above omits both profile fields and therefore reuses the
stored `gpt-5.6-terra`/`low` selection. Supplying those same values is also
valid. Supplying a different value for either field returns `409` and does not
queue a job.

Idempotency keys are 1 through 256 characters and are scoped to
`(agent_id, idempotency_key)`. Repeating a key returns the original job,
conversation, status, stored profile, and original companion additions. Replay
lookup happens before companion-stage validation or claiming: newly supplied
stage IDs on a replay remain untouched. For v1 compatibility, the router does
not compare a retry's prompt, conversation key, companions, model, or reasoning
effort with the original request. A client must never reuse a key for different
logical work and must treat the returned original fields as authoritative.

The additive database migration for this contract leaves pre-upgrade
conversation and job profile columns `null`. Those conversations retain their
legacy Codex/config inheritance behavior, and clients that omit the new fields
remain source-compatible. Because `null` is no conversation-level override, a
later agent revision may change an inherited setting; only a non-null stored
selection is reasserted independently of agent revision changes.

Queued cancellation changes the job to `cancelled` immediately. Cancellation of
an active job is cooperative: the returned `JobView` can retain its current
status until the scheduler stops it. Cancelling an already terminal job returns
that job unchanged.

Archival requires all jobs to be terminal and prevents future turns. Deletion
also requires no active jobs and removes the conversation record, workspaces,
sessions, and stored artifact data. Treat deletion as irreversible.

### Conversation companions

A companion is caller-supplied reference data owned by one conversation. The
acquisition flow is intentionally two-phase so large or slow inputs are ready
before prompt acceptance:

1. upload a file/archive or queue a public Git import;
2. poll an asynchronous Git stage until it is `ready`;
3. bind one or more ready stage IDs to safe logical names in a prompt;
4. verify the accepted response echoes every item in `companion_additions`.

Upload one raw body over REST (uploaded bytes are not transported through MCP):

```sh
EXPECTED_SHA256="$(sha256sum ./reference.pdf | awk '{print $1}')"
curl --fail-with-body \
  -H "Authorization: Bearer ${REMOTEAGENT_TOKEN}" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @./reference.pdf \
  "${REMOTEAGENT_URL}/api/v1/companion-stages/uploads?filename=reference.pdf&kind=file&sha256=${EXPECTED_SHA256}"
```

Use `kind=archive` for `.zip`, `.tar`, `.tar.gz`, or `.tgz`. Safe extraction
rejects absolute/traversing/control-character paths, duplicate normalized
paths, links, devices, special permission bits, more than 20,000 files, or more
than the configured expansion limit. The optional lowercase `sha256` parameter
checks the streamed upload before atomic finalization. A successful upload is
immediately `ready` and returns `201 CompanionStageView`.

Queue a credential-free Git import:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer ${REMOTEAGENT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://github.com/example/reference-repo.git",
    "ref": "refs/heads/main"
  }' \
  "${REMOTEAGENT_URL}/api/v1/companion-stages/git-imports"
```

Git imports return `202` in `queued` state. Poll
`GET /api/v1/companion-stages/{stage_id}` through `importing` to `ready` or
`failed`. Omitting `ref` selects the remote `HEAD`; otherwise the requested ref
must resolve to a commit. The ready stage records that immutable commit while
preserving the mirror's full reachable refs/history. Submodule repositories and
Git LFS pointer files may be present, but their referenced content is not
fetched automatically.

`CompanionStageView` contains `id`, `kind`, `status`, bounded
`source_metadata`, optional `size_bytes`, `file_count`, `sha256`, and
`resolved_git_commit`, a bounded `error`, expiry/claim timestamps, and normal
record timestamps. Stage states are `queued`, `importing`, `ready`, `failed`,
`claimed`, and `expired`. Stage IDs match `^cs_[0-9a-f]{32}$`, expire after 24
hours by default, and may be claimed exactly once.

Bind a ready stage while creating or continuing a conversation:

```json
{
  "agent_id": "release-notes",
  "prompt": "Compare the reference repository with the specification.",
  "conversation_key": "c_c9d88a03a6674affab2b8b68d6776597",
  "idempotency_key": "example-20260902-turn-3",
  "companions": [
    {"stage_id": "cs_0123456789abcdef0123456789abcdef", "name": "reference-repo"}
  ]
}
```

Names are one 1–128-character path component matching
`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`. One turn accepts at most 20 unique stage
IDs and names. Prompt acceptance atomically consumes the stages and returns each
accepted item in `companion_additions`, including its ID, stage ID, name,
version, kind, state, stable `/workspace/companions/<name>` path, byte/file
counts, digest, introducing job/sequence, Git commit when applicable, and
timestamps. New clients MUST validate this echo; an older server could otherwise
ignore an unknown request field.

Accepted versions begin `pending`. Once every predecessor turn is terminal, the
router activates the latest accepted version per name whose introducing
sequence is visible to the runnable turn. The first turn can therefore use its
own additions, while an already-running earlier turn cannot see data attached
to a later prompt. A cancelled or failed introducing turn does not detach its
data; the next runnable turn activates it. If preparation fails, the job fails
before Codex starts, the version remains pending with a bounded error, and the
next turn retries activation without exposing a partial copy.

Working copies are editable and persist across turns. Reusing a name performs
an atomic replacement and intentionally discards edits made under the previous
version. The old active version stays visible until the replacement copy is
ready; after the switch, its bytes are reclaimed but metadata remains available
with `include_history=true`. Missing or changed stable links are repaired before
each turn, and a missing working copy is reconstructed from its immutable source
when possible.

When companions are active, the router prepends a versioned instruction block
that lists their stable paths, kinds, versions, digests, and Git commits. The
caller prompt remains stored unchanged, while token accounting uses the
effective prompt including that block. Prompts without companions are passed
through byte-for-byte unchanged. Files intended for caller retrieval still
belong in `/workspace/artifacts`; v1 has no companion-content download,
standalone removal, automatic publishing, private Git, generic URL fetch, or
scheduled companion binding.

Use
`GET /api/v1/conversations/{conversation_key}/companions` for active and pending
metadata. Set `include_history=true` to include superseded versions. The shared
router bearer authorizes all such metadata; there are no per-caller companion
ACLs.

### Artifacts

`ArtifactView` has this shape:

```json
{
  "id": "a_7aef2ddc809e4e45a87286cd37f7b31e",
  "job_id": "j_f8e1c91b4fbc421191603a1c05c6bba8",
  "conversation_key": "c_c9d88a03a6674affab2b8b68d6776597",
  "relative_path": "reports/result.md",
  "media_type": "text/markdown",
  "size_bytes": 4821,
  "sha256": "<64 lowercase hexadecimal characters>",
  "resource_uri": "artifact://a_7aef2ddc809e4e45a87286cd37f7b31e"
}
```

Download all or part of a large artifact over HTTP:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer ${REMOTEAGENT_TOKEN}" \
  -H "Range: bytes=0-1048575" \
  -o result.part \
  "${REMOTEAGENT_URL}/api/v1/artifacts/a_7aef2ddc809e4e45a87286cd37f7b31e/content"
```

Content responses include a SHA-256 `ETag`, `Accept-Ranges: bytes`, attachment
`Content-Disposition`, and `Cache-Control: private, immutable`. A valid range
returns `206`; an unsatisfiable range returns `416`. Malformed range errors and
some file-server range errors can be plain text even though ordinary API domain
errors use the JSON error envelope.

Artifact count, file-size, and retention limits are deployment settings. The
defaults accept up to 1000 changed files per job and 100 MiB per file. Clients
must not assume those defaults on another deployment.

### HTTP errors

Ordinary domain errors use:

```json
{"detail":"human-readable message"}
```

Request parsing and schema validation use FastAPI's `422` validation envelope:

```json
{
  "detail": [
    {
      "loc": ["body", "prompt"],
      "msg": "String should have at least 1 character",
      "type": "string_too_short"
    }
  ]
}
```

| Status | Meaning |
| --- | --- |
| `400` | Malformed artifact range or transport-level request. |
| `401` | Missing or invalid bearer token. |
| `404` | Requested agent, job, conversation, companion stage, artifact, or content was not found. |
| `409` | Registration/conversation state conflicts, or a companion stage is not ready, expired, or already claimed. |
| `410` | Artifact metadata exists but storage is unavailable or violates policy. |
| `413` | A companion item, extracted tree, turn binding, or conversation source-data limit was exceeded. |
| `416` | Artifact byte range is unsatisfiable. |
| `422` | Body, query, TOML, path, Compose, companion name/archive/URL/ref/hash validation failed. |
| `503` | A readiness dependency is unavailable. |
| `507` | Deployment-wide unclaimed companion staging capacity is exhausted. |

Listing artifacts for an unknown job currently returns an empty array rather
than `404`. Clients should first fetch the job when that distinction matters.

## MCP Streamable HTTP

RemoteAgent implements MCP over JSON-RPC 2.0 at the exact `/mcp` path. The
documented interoperability baseline is MCP protocol revision `2025-06-18`.
The transport is stateless and configured for JSON responses:

- each JSON-RPC message is a new HTTP `POST`;
- the server does not issue or require `Mcp-Session-Id`;
- JSON-RPC requests receive `application/json` responses;
- accepted notifications receive HTTP `202` with no body;
- there is no router-specific MCP notification when a background job finishes.

Clients must poll `get_prompt_status`. `GET /mcp` can establish an MCP SSE stream,
but RemoteAgent does not publish job-completion notifications on it and it is not
a substitute for polling.

### Required headers

Initialization requires the bearer, JSON content type, and both Streamable HTTP
response types in `Accept`:

```http
Authorization: Bearer <router-token>
Content-Type: application/json
Accept: application/json, text/event-stream
```

After negotiation, also send:

```http
MCP-Protocol-Version: 2025-06-18
```

The MCP transport separately validates `Host` and, when supplied, `Origin`
against the deployment allowlists.

### Initialize and discover

Initialize with:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer ${REMOTEAGENT_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-06-18",
      "capabilities": {},
      "clientInfo": {"name": "remoteagent-example", "version": "1.0.0"}
    }
  }' \
  "${REMOTEAGENT_URL}/mcp"
```

The result identifies `RemoteAgent Router`, returns the negotiated protocol
version and instructions, and advertises tools and resources. Tool and resource
lists are stable for one router release, with `listChanged=false`; rediscover
after reconnecting to an upgraded server.

Complete initialization with a notification:

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized"
}
```

Then discover tools:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list",
  "params": {}
}
```

Each returned MCP `Tool` includes `inputSchema` and `outputSchema`. The exact
release snapshot is [mcp-tools-list.json](api/mcp-tools-list.json).

Discover resource templates separately:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "resources/templates/list",
  "params": {}
}
```

`resources/list` is empty because RemoteAgent resources use URI templates.

### Tools

This release exposes 20 tools: 13 core agent/job/companion/artifact/conversation
tools plus seven cron schedule/response tools.

| Tool | Arguments | Structured output | Behavior |
| --- | --- | --- | --- |
| `list_agents` | `include_disabled=false` | `{"result": AgentSummary[]}` | Discover agents and current revisions. |
| `get_agent` | `agent_id` | `AgentView` | Read one effective agent definition. |
| `register_agent` | `definition`, `replace=false` | `AgentView` | Validate and register or replace an agent. |
| `update_agent_configuration` | `agent_id`, optional `config_toml`, `base_context` | `AgentView` | Append an immutable config/context revision. |
| `submit_prompt` | `agent_id`, `prompt`, optional `conversation_key`, `idempotency_key`, `companions`, `model`, `reasoning_effort` | `PromptAccepted` | Atomically bind ready stages, queue a turn, and return the profile plus echoed additions. |
| `stage_git_repository` | `url`, optional `ref` | `CompanionStageView` | Queue a credential-free public HTTPS Git import. |
| `get_companion_stage` | `stage_id` | `CompanionStageView` | Poll Git import or inspect upload-stage lifecycle state. |
| `list_conversation_companions` | `conversation_key`, `include_history=false` | `{"result": ConversationCompanionView[]}` | List active/pending metadata and optionally superseded versions. |
| `get_prompt_status` | `job_id` | `JobView` | Poll status and retrieve a terminal result. |
| `cancel_prompt` | `job_id` | `JobView` | Cancel or request cancellation. |
| `list_artifacts` | optional `job_id`, `conversation_key` | `{"result": ArtifactView[]}` | List immutable artifacts and resource URIs. |
| `archive_conversation` | `conversation_key` | `ConversationArchived` | Return `status: "archived"` after archival. |
| `delete_conversation` | `conversation_key` | `ConversationDeleted` | Return `deleted: true` after deletion. |
| `configure_cron_schedule` | `schedule_id`, `cron_expression`, `agent_id`, `prompt`; optional `timezone`, `model`, `reasoning_effort`, `conversation_mode` | `CronScheduleView` | Atomically create, no-op, or append a replacement revision. |
| `list_cron_schedules` | `include_disabled=false` | `{"result": CronScheduleView[]}` | List schedule state and response counters. |
| `get_cron_schedule` | `schedule_id` | `CronScheduleView` | Retrieve one schedule and current execution state. |
| `set_cron_schedule_enabled` | `schedule_id`, `enabled` | `CronScheduleView` | Pause or resume future occurrences. |
| `delete_cron_schedule` | `schedule_id` | `CronScheduleDeleteResult` | Delete now or mark deleting until its active run is terminal. |
| `retrieve_cron_responses` | exactly one of `schedule_id`, `execution_id`; `limit=50` | `CronResponseLease` | Lease separately identified successful responses. |
| `acknowledge_cron_responses` | `lease_id` | `CronResponseAcknowledgement` | Atomically delete only responses from that lease. |

The same limits and behavioral validation described for the HTTP schemas apply
to MCP tools even where a generated input schema cannot express a server-local
path, Compose health check, safe TOML key, or cross-field rule.

The MCP request body remains limited to 4 MiB, so uploaded companion bytes go
through the REST streaming endpoint. MCP provides Git staging, stage polling,
conversation listing, and prompt binding because those operations carry only
bounded JSON metadata.

#### Cron schedule and response behavior

Schedule IDs use the same lowercase slug syntax as agent IDs. Configuration
validates that the selected agent exists and is enabled. An unchanged request
is idempotent; any changed configuration creates an immutable revision, while
an active execution continues with its existing snapshot. The default timezone
is UTC and `conversation_mode` is `fresh`; `persistent` reuses the established
conversation captured when submission is accepted until the agent, model,
reasoning effort, or mode is changed.

Cron expressions have exactly five fields and support standard lists, ranges,
steps, month/weekday names, Sunday `0` or `7`, and conventional day-of-month /
day-of-week OR behavior. Macros, seconds/year fields, hashed/random extensions,
invalid timezones, and impossible schedules are rejected. New, changed,
resumed, and startup-recovered schedules begin at the next future boundary.
There is no missed-run catch-up. A nonexistent DST minute is skipped; only the
first occurrence of a repeated wall-clock minute runs.

Only one execution per schedule may be nonterminal. A later occurrence is
counted as skipped rather than queued. Executions are persisted before MCP
submission and the generation/instant-derived idempotency key is reused after
an ambiguous failure. Cron polls through `get_prompt_status`, requests
cancellation after the configured run deadline, and stores a response only for
`succeeded` jobs.

Cron schedule schemas are unchanged and have no companion field. The scoped
cron role is also rejected if it attempts a non-empty companion binding through
`submit_prompt`; scheduled companions are outside the v1 lifecycle contract.

`retrieve_cron_responses` is a destructive-queue handshake, not a read-only
history endpoint. Schedule retrieval leases successful records FIFO; exact
execution lookup leases at most one. The response supplies a lease ID, expiry,
an array of distinct `CronResponse` objects, and `more_available`. Each response
contains schedule/execution/revision, agent/router-job/conversation identifiers,
scheduled/completed timestamps, model profile, usage, and complete result text.
The default lease is five minutes. Expiry makes unacknowledged rows available
again; acknowledgement physically deletes only rows still belonging to that
lease and is retry-idempotent through a 24-hour tombstone. A stale lease cannot
delete records that have since been re-leased.

The router reaches cron through a separate bearer-protected `/internal/v1` API.
That API is backend-only and deliberately absent from public `/api/v1`; its
checked-in [OpenAPI document](../cron/openapi.json) is an implementation
contract between the two services, not a supported endpoint for external
clients.

### Tool calls and structured content

Call `submit_prompt`:

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "tools/call",
  "params": {
    "name": "submit_prompt",
    "arguments": {
      "agent_id": "joke-agent",
      "prompt": "Tell me a joke about an index that was never used.",
      "model": "gpt-5.6-terra",
      "reasoning_effort": "low",
      "idempotency_key": "mcp-example-turn-1"
    }
  }
}
```

A successful call contains both backwards-compatible text and typed structured
data:

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\n  \"job_id\": \"j_...\",\n  \"conversation_key\": \"c_...\",\n  \"status\": \"queued\",\n  \"model\": \"gpt-5.6-terra\",\n  \"reasoning_effort\": \"low\",\n  \"companion_additions\": []\n}"
      }
    ],
    "structuredContent": {
      "job_id": "j_...",
      "conversation_key": "c_...",
      "status": "queued",
      "model": "gpt-5.6-terra",
      "reasoning_effort": "low",
      "companion_additions": []
    },
    "isError": false
  }
}
```

Queue and poll a Git companion with `stage_git_repository` and
`get_companion_stage` before binding its returned `stage_id`:

```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "tools/call",
  "params": {
    "name": "stage_git_repository",
    "arguments": {
      "url": "https://github.com/example/reference-repo.git",
      "ref": "refs/heads/main"
    }
  }
}
```

Clients should prefer `structuredContent` and validate it against the discovered
`outputSchema`. FastMCP wraps list return values in a `result` property for
structured output. The corresponding text content can contain one text block
per list item, so clients must not infer list structure from `content`.

Poll with another independent HTTP POST:

```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "method": "tools/call",
  "params": {
    "name": "get_prompt_status",
    "arguments": {"job_id": "j_..."}
  }
}
```

After a terminal result, list its artifacts:

```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "method": "tools/call",
  "params": {
    "name": "list_artifacts",
    "arguments": {"job_id": "j_..."}
  }
}
```

Preserve the `conversation_key` from the first call and include it in a later
`submit_prompt` to continue the exact conversation. For each selector, omit it
to reuse the stored value or repeat that exact value. MCP reports a mismatched
continuation as a normal tool result with
`isError: true`, corresponding to the REST `409` domain conflict. An
idempotency-key retry instead returns the original result as described above.

### Resource templates and reads

RemoteAgent exposes two templates:

| Name | URI template | Declared media type | Result |
| --- | --- | --- | --- |
| `agent_configuration` | `agent://{agent_id}/configuration` | `application/json` | Text containing the effective `AgentView` JSON. |
| `agent_artifact` | `artifact://{artifact_id}` | `application/octet-stream` | Base64-encoded MCP blob. |

Read the effective configuration:

```json
{
  "jsonrpc": "2.0",
  "id": 20,
  "method": "resources/read",
  "params": {"uri": "agent://joke-agent/configuration"}
}
```

Read an artifact URI returned by `list_artifacts`:

```json
{
  "jsonrpc": "2.0",
  "id": 21,
  "method": "resources/read",
  "params": {"uri": "artifact://a_7aef2ddc809e4e45a87286cd37f7b31e"}
}
```

MCP artifact reads are limited to 16 MiB and always use
`application/octet-stream`, regardless of the more specific type in
`ArtifactView`. Use the authenticated HTTP content endpoint, including `Range`
when helpful, for larger files.

### MCP errors and limits

MCP has two error layers. First inspect the HTTP status, then parse the JSON-RPC
envelope.

| HTTP status | Transport condition |
| --- | --- |
| `400` | Missing/invalid MCP JSON content type, malformed JSON-RPC, or unsupported protocol version. |
| `401` | Missing or invalid router bearer token. |
| `403` | Supplied `Origin` is not allowlisted. |
| `405` | Session termination is unsupported because the transport is stateless. |
| `406` | The client does not accept the required response type. |
| `413` | MCP request body exceeds 4 MiB. |
| `421` | `Host` is not allowlisted. |

The 4 MiB limit counts encoded UTF-8 bytes. A highly non-ASCII prompt can hit it
before reaching the 2,000,000-character prompt limit.

Invalid tool arguments, unknown tools, and domain failures are returned as an
MCP tool result with `isError: true`, normally with a text detail such as:

```json
{
  "result": {
    "content": [
      {
        "type": "text",
        "text": "Error executing tool get_agent: missing-agent"
      }
    ],
    "isError": true
  }
}
```

Companion tools prefix a machine-readable JSON object with
`REMOTEAGENT_TOOL_ERROR:` inside that error text. Its bounded `code` is one of
`companion_not_found`, `companion_conflict`, `companion_limit_exceeded`,
`companion_invalid`, or `companion_capacity_exhausted`; it also contains a
boolean `retryable` and bounded human `message`. These correspond to REST
`404`, `409`, `413`, `422`, and `507` respectively. Clients should parse this
structured marker rather than matching the prose message.

Malformed protocol messages and unknown JSON-RPC methods use the JSON-RPC
`error` member instead. Resource read failures are also protocol errors rather
than tool results. Clients must not depend on exception text or SDK-specific
numeric codes; use the error layer, operation, and documented domain state.

## Operational surfaces outside the client contract

The following endpoints exist for deployment and operators but are not part of
the versioned application API:

- public liveness: `GET /health` and `GET /healthz`;
- bearer-protected readiness: `GET /readyz`;
- the authenticated dashboard and its read-only projection endpoints under
  `/dashboard`;
- bearer-protected Prometheus exposition at `GET /metrics`;
- interactive `/docs` and `/redoc` pages.

Dashboard JSON, SSE event payloads, HTML, CSS, JavaScript, diagnostic bundles,
and Prometheus metric names may change without an `/api/v1` compatibility
guarantee. Do not build ordinary client applications against
`/dashboard/api/v1`; use the control API or MCP.

## Compatibility rules

- Breaking HTTP request or response changes require a new `/api/vN` version.
- Additive optional fields may be added within v1; clients should ignore unknown
  response fields.
- `model` and `reasoning_effort` are additive, required-but-nullable response
  fields and optional request fields. Older request producers can omit them;
  response consumers following the existing ignore-unknown-fields rule remain
  compatible. Generated clients should preserve `null` as the documented
  inheritance state.
- `companions` is an additive optional prompt field and
  `companion_additions` is an additive response field. A client that depends on
  a binding must first create the stage successfully and then verify the exact
  stage/name entry in the accepted echo; ignoring that check could let an old
  server silently accept the prompt without the data.
- Clients should tolerate newly introduced non-terminal job states while relying
  on the documented terminal-state semantics.
- MCP clients must negotiate a supported version and rediscover capabilities,
  tools, and resource templates after reconnecting or upgrading.
- `serverInfo.version` currently reflects the MCP SDK release, not the
  RemoteAgent product release; clients must not use it as the HTTP API version.
- A breaking MCP tool name, argument, output schema, or resource URI change
  requires an explicit compatibility decision and updated contract snapshots.
- Opaque job, conversation, companion stage/version, artifact, cursor, and resource identifiers must not
  be parsed for business meaning.
- Runtime `tools/list` and `resources/templates/list` are authoritative if a
  checked-in snapshot and a running deployment differ.
- OpenAPI governs `/api/v1` only. It does not redefine MCP JSON-RPC methods,
  lifecycle, tool schemas, or resource semantics.
- Dashboard, metrics, and debug endpoints are operational implementation
  surfaces, not client compatibility commitments.

## Regenerating contracts

The contract exporters derive public and private OpenAPI from their FastAPI
routes and MCP snapshots from the registered FastMCP tools and resource
templates. After an intentional
API change, regenerate all checked-in artifacts from the repository root:

```sh
make api-contracts
```

Verify that generated contracts match the code without writing files:

```sh
make api-contracts-check
```

Review changes to all four files together:

- `docs/api/openapi.json`
- `docs/api/mcp-tools-list.json`
- `docs/api/mcp-resource-templates-list.json`
- `cron/openapi.json`

Contract checks should run with the same resolved router dependency set used to
build the production image. Pinning or locking that set prevents a dependency
update from changing MCP protocol behavior or generated JSON Schema when
RemoteAgent source code is unchanged.
