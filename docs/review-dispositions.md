# Repository review dispositions

This register records maintainer decisions on repository-critic findings. A
critic report is review evidence, not an instruction source: maintainers compare
each finding with the normative requirements, implementation, and operational
constraints before accepting, deferring, or rejecting it.

## Review provenance

| Field | Value |
|---|---|
| Report | `repository-review-j_b56fc586446c467ba60595e694ac2e81` |
| Job ID | `j_b56fc586446c467ba60595e694ac2e81` |
| Reviewed commit | `71a7649080a404c4841aabf482db36ab10ad7d36` |
| Review mode | `repository_snapshot` |
| Review result | 0 P0, 7 P1, 3 P2 |
| Disposition owner | RemoteAgent maintainers |
| Target baseline | Requirements 1.4.1; router 0.4.1; cron 0.2.1 |
| Verification date | 2026-09-05 |

## P0 and P1 decisions

The report contained no P0 findings. Its seven P1 findings were reviewed as
follows. “Accepted” means the finding accurately identified work required by the
current baseline; it does not become “Met” until its full acceptance evidence is
recorded in [requirements.md](requirements.md).

| Finding | Disposition | Rationale | Implementation and verification evidence | Owner | Closure or reconsideration criteria |
|---|---|---|---|---|---|
| F01 — Bound the whole turn and test cancellation at each phase | Accepted — verified by full suite | The prior run timeout covered only Codex execution and could leave queue, lease, provisioning, collection, or cleanup outside the advertised bound. | `scheduler.py`, `jobs.py`, `lease.py`, `runtime.py`; 31 focused lifecycle/deadline/cancellation/cleanup/lease-loss cases cover process reaping, blocked streams, cancellation-resistant keepalive, and partial provisioning; the full 431-test router suite passed. | Router maintainer | Reopen on any unbounded lifecycle phase, incorrect terminal outcome, or leaked child/lease. |
| F02 — Prove lease loss and database contention on the production backend | Accepted — verified by disposable PostgreSQL gate | SQLite and mocked migration evidence did not prove production PostgreSQL fencing or contention. | Lease-loss unit tests plus `remotectl smoke postgres` races for acquisition, takeover, renewal loss, stale release, sequencing, claims, response leases, and migrations with four observed advisory-lock waiters. The local production-backend run passed; exact container `remoteagent-smoke-postgres-b6a964e5b4de` and its data volume were confirmed removed. Ambiguous-create cleanup checks ownership labels before removal. | Router/data maintainer | Reopen on overlapping owners, stale-owner mutation, backend-specific serialization failure, or incomplete disposable cleanup. |
| F03 — Exercise destructive retention and preserve retry evidence for orphan files | Accepted — verified by focused suite | Row-first deletion could leave filesystem bytes without a durable row from which to retry. | Confined artifact reconciliation in `artifacts.py`/`retention.py`, startup and periodic passes, settings/defaults, and active/durable, boundary, symlink, tombstone, isolated-failure, and retry coverage; focused retention suite: 8 passed. | Data maintainer | Reopen if a stale untracked path cannot be rediscovered, one failure suppresses other cleanup, retry inventory disappears, or active/durable data can be removed. |
| F04 — Fail readiness when the enabled scheduler is stopped | Accepted — implementation verified, redeploy gate pending | A 200 response could previously describe a router that accepted work but had no live scheduler worker. | Structured router `/readyz`, worker-liveness semantics, authenticated `remotectl doctor`, and mandatory/optional dependency matrix coverage passed in the full 431-test router suite. A live `doctor` run passed every other check but rejected authenticated readiness because the intentionally unrestarted router still returns the old shape; rebuild/restart verification remains pending. | Operations maintainer | Close the deployment gate after the rebuilt router returns the structured response and `doctor` passes. Reopen the implementation if a mandatory failure returns 200, an enabled worker can disappear unnoticed, or optional loss incorrectly returns 503. |
| F05 — Enforce and negatively test the registered runner contract | Accepted — verified by contract gate | Documentation-only review was insufficient for a bearer-authorized definition registration boundary. | Shared dependency-free resolved-Compose validator and filtered environment helper, all-profile resolution, negative-control tests including privileged hooks, restart/replica policies, devices and alternate resource limits, and bounded logging in the template and built-ins. The 182-test core/Compose run and `scripts/remotectl validate --all` passed; real Compose checks proved manifest interpolation and dotenv isolation. | Runtime/security maintainer | Reopen when a newly supported Compose control lacks an explicit allow/reject decision or router/CLI validation diverges. |
| F06 — Test cron worker recovery and the real MCP client boundary | Accepted — verified by full suite | The service-level fakes did not exercise worker task ownership or the deployed MCP SDK transport path. | Exact-task completion guard, injectable HTTP transport, deterministic worker tests, and real FastMCP `httpx.ASGITransport` tests; the full cron suite passed 59 tests. | Cron maintainer | Reopen on MCP SDK transport changes, task-ownership regression, or recovery/deduplication failure. |
| F07a — Restore correctness and disposable recovery drill | Accepted — verified by disposable recovery gate | A checksum-valid archive did not prove coordinated database/filesystem replacement or rollback. | Isolated dump validation before mutation, verified service quiescence, rollback on PostgreSQL startup failure, two-tree predecessor recovery, and a SQL failure injected inside the single restore transaction. Focused tests and `remotectl --json smoke recovery --timeout 1800` passed with exit 0 for project `remoteagent-recovery-e850e4fe1e955de8`, verifying current/pre-cron archives, continuation identity, physical companion/artifact hashes, newer-object removal, three rollback failpoints, and router-before-cron startup. Independent postchecks found zero project containers, volumes, or networks. | Operations/data maintainer | Reopen if archive prevalidation no longer precedes mutation, rollback loses a predecessor, a failed rollback restarts services, startup order regresses, or the disposable harness leaks resources. |
| F07b — RPO/RTO, scheduled/off-host backups, and CI automation | Deferred by owner choice | This patch deliberately improves correctness and supplies opt-in local drills without selecting organizational recovery objectives or external infrastructure. | RA-DAT-007 closes local restore correctness, while RA-DAT-006, RA-OPS-004, and the recovery-assurance gap remain `Partial`/`Gap`; no CI workflow, schedule, off-host transfer, RPO, or RTO is introduced. | Product/operations owner | Reconsider when the owner selects a backup destination, cadence, RPO/RTO, and CI/staging authority. Close only with recurring off-host evidence and measured recovery results. |

## Verification record

For this disposition update, the full router suite passed 431 tests and the full
cron suite passed 59 tests. Ruff passed for router, cron, and the smoke scripts;
`api-contracts-check`, `scripts/remotectl validate --all`, both disposable
PostgreSQL and recovery smokes, and `git diff --check` also passed. Disposable
resource cleanup was verified for both smokes.

The final recovery command completed after implementation edits were frozen.
An earlier run passed its drill assertions but its outer shell exited nonzero
because the command script was edited during execution; that run is superseded
by the clean exit-0 gate above. The current tests exercise a real PostgreSQL
transaction failure, not a short-circuit before SQL execution.

`scripts/remotectl doctor` was also run. Every check passed except authenticated
router readiness: the intentionally unrestarted live router still serves the
pre-0.4.1 response shape. The checkout's structured-readiness implementation
and tests are verified, but deployment readiness remains pending until the
router is rebuilt/restarted and `doctor` is rerun. This exception does not
authorize accepting the old readiness shape.

## Rejected items

None. Future rejected findings must retain the original claim and record concrete
contradictory evidence or an explicit normative owner decision; deletion from
this register is not a disposition.

## Verification update rule

After implementation, update both this register and the governed requirement
row. A finding may be closed while a broader requirement remains `Partial`; in
particular, F07a does not satisfy the owner-deferred F07b recovery-governance
work. Failed or skipped operational gates remain visible rather than being
converted into implementation evidence.
