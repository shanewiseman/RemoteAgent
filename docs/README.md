# RemoteAgent documentation

This directory is the maintained design, client, deployment, and operations
record for RemoteAgent. The documents describe the current implementation;
where a production requirement is not fully met, the requirement is explicitly
marked `Partial`, `Gap`, or `Constraint` rather than implied to exist.

## Canonical documents

| Document | Purpose |
|---|---|
| [Product and system requirements](requirements.md) | Versioned requirements baseline, acceptance criteria, implementation status, traceability, and change-control rules. Start here for any focused modification. |
| [Client API contract](api.md) | Supported REST and MCP workflows, staged conversation companions, model/reasoning selection, authentication, errors, limits, examples, and compatibility policy. |
| [Machine-readable contracts](api/) | OpenAPI 3.1 for public REST plus exact MCP `tools/list` and `resources/templates/list` snapshots using JSON Schema; the private service contract is [cron/openapi.json](../cron/openapi.json). |
| [Docker runtime contract](docker-runtime.md) | Core and per-agent Compose responsibilities, lifecycle commands, storage, networks, security expectations, and failure recovery. |
| [Architecture](architecture.md) | Component boundaries, durable state, companion activation, scheduling, and conversation execution flow. |
| [Deployment](deployment.md) | Initial server installation and production deployment checklist. |
| [Operations](operations.md) | Routine administration, diagnostics, backup, restore, cleanup, and upgrade procedures. |
| [Agent authoring](agent-authoring.md) | How to add an agent project, runner image, context, configuration, and dependency services. |
| [Security](security.md) | Threat model, trust boundaries, credentials, sandboxing, and hardening guidance. |

## Contract authority

- For REST clients, [api/openapi.json](api/openapi.json) is the checked-in
  machine-readable contract.
- For MCP clients, protocol initialization and live `tools/list` and
  `resources/templates/list` discovery are authoritative. The checked-in MCP
  snapshots are reviewable build-time copies of those standard responses.
- The cron service's checked-in OpenAPI contract documents a private
  router-to-service interface; it does not add routes to public `/api/v1`.
- For product behavior and planned changes, requirement IDs in
  [requirements.md](requirements.md) are stable and are never renumbered or
  reused.
- For actual storage and orchestration safety, `compose.yaml`, agent manifests,
  migrations, and executable code remain the final implementation evidence.

Regenerate and verify client contracts after any route, schema, tool, or
resource-template change:

```sh
make api-contracts
make api-contracts-check
```

The router and cron test suites also reject stale checked-in contracts.

## Change workflow

Every behavior change should name the affected requirement IDs, update their
acceptance/evidence cells, update any API or Docker contract it changes, and add
or revise automated verification. Backward-incompatible client changes require
a new API major version or an explicit deprecation and migration plan.
