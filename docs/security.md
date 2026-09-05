# Security

Security requirements and their current implementation status are indexed in
the [requirements baseline](requirements.md). Container-specific controls and
review-only Compose expectations are detailed in the
[Docker runtime contract](docker-runtime.md).

## V1 threat boundary

RemoteAgent is for a trusted internal network and trusted agent definitions. The
operator-facing router bearer authorizes every agent; there are no per-user or
per-agent ACLs. Anyone holding it can run any enabled agent, consume subscription
capacity, modify allowed agent context/config and schedules, and retrieve
published artifacts and cron responses. The same bearer can stage companion
data and read all stage/conversation companion metadata; companions have no
per-user or per-conversation ACL beyond possession of that shared credential.

The cron process is the sole narrower caller. Its independent scoped MCP token
can call only `list_agents`, `get_agent`, `submit_prompt`, `get_prompt_status`,
and `cancel_prompt`. It is rejected from router REST, dashboard, metrics,
resources, administrative tools, and cron-management tools. A second token
authenticates router calls to the private cron API. These service tokens are not
operator or client credentials. The cron role cannot call companion staging or
listing tools, and `submit_prompt` rejects a non-empty companion binding from
that role.

HTTP does not protect a bearer token in transit. Restrict the host firewall and
network path now, then add TLS at a reverse proxy before any untrusted hop.

## Docker socket

The router must create short-lived agent containers and therefore has the Docker
socket mounted. A read-only bind flag does not restrict Docker API operations:
socket access is root-equivalent. Do not expose router debug surfaces, register
unreviewed Compose projects, or run this stack on a multi-tenant host. A future
hardening step should place an allowlisted Docker-socket proxy between the
router and daemon.

## Credentials and secrets

- `.runtime/secrets/*` must be owner-readable only; validation and doctor reject
  group/world-readable modes.
- Reference containers run in production mode; startup and deployment
  validation reject empty/default or reused router/cron bearer values.
- Codex `auth.json` lives in a Docker volume and must be treated as a password.
- Secrets are never accepted as ordinary command-line values, printed in logs,
  committed, copied into images, or included in backups.
- API-key mode reads from a restricted file through stdin. Do not put API keys
  in `.env` or a job-wide environment.
- Rotate the router token with `remotectl token rotate router --yes`; it
  immediately invalidates every configured external caller. Rotate both
  internal service tokens with `remotectl token rotate cron --yes`, which
  synchronously recreates router and cron.

Every agent can use the shared Codex credential while its turn runs. Therefore
only trusted, reviewed images and base contexts may be registered. The common
skills volume is read-only in runners to prevent one agent poisoning another.

The cron container is non-root, drops all capabilities, uses a read-only root
and bounded tmpfs/CPU/memory/PID resources, and joins only the internal backend
network. It receives no Docker socket, repository/state binds, Redis configuration,
Codex credentials, or agent mounts. Its database account is shared at the
deployment level, but its application and migrations own only `cron_*` tables
and `cron_alembic_version`; no foreign keys cross into router tables.

## Container isolation

Agent services run as the host-mapped non-root UID/GID, drop Linux capabilities,
set `no-new-privileges`, use a read-only root filesystem and bounded tmpfs, and
receive only their conversation mounts plus shared auth/skills. They do not get
the Docker socket. Resource limits bound memory, CPU, and process count.

Codex local tools run inside an additional Bubblewrap sandbox. On the supported
Linux/Docker deployment, the runner service deliberately sets both
`seccomp=unconfined` and `apparmor=unconfined`: Docker's default seccomp policy
blocks creation of the nested user namespace, while its AppArmor policy blocks
Bubblewrap's mount propagation setup. `no-new-privileges`, all-capability drop,
the non-root user, read-only container root, resource limits, and isolated
mounts remain in force. Using only one of the two unconfined settings does not
let Bubblewrap start on the reference host.

The runner image bakes a root-owned, mode-0444
`/etc/codex/requirements.toml`. Its managed policy cannot be weakened by an
agent config revision: command tools cannot read `$CODEX_HOME/*auth*`, full
filesystem access is rejected, approval is fixed to `never`, and only
`read-only`/`workspace-write` modes are accepted. It also disables config-
defined MCP servers, user hooks, plugins/apps, and browser/computer integrations
because those processes do not share the local command sandbox. The bundled
local code-mode host is enabled as a narrow exception: Codex 0.153.2 model
metadata can select code-mode tools even while the optional `code_mode` feature
is false, and disabling the host makes those selected tools fail closed. The
host evaluates orchestration JavaScript in sandbox-enabled V8 with imports and
Node APIs unavailable; every nested OS tool call returns to Codex and retains
the same managed approval, filesystem, and network enforcement. No remote
code-mode endpoint is configured. The ChatGPT backend URL is pinned. Router and
host validation reject custom model providers/base URLs, notification commands,
arbitrary path-bearing instruction/skill fields, named permission profiles, and
extra writable roots. Changing those controls requires a reviewed image/router
release, not an agent revision.

The managed image also contains a command-network proxy policy, but networking
remains off unless an immutable agent revision explicitly combines
`sandbox_mode="workspace-write"` with
`[sandbox_workspace_write].network_access=true`. The policy accepts public
network access only to the exact hostnames `example.com`, `pypi.org`,
`files.pythonhosted.org`, `registry.npmjs.org`, `proxy.golang.org`, and
`sum.golang.org`, while denying local binding, upstream-proxy escape,
non-loopback proxy exposure, and arbitrary Unix sockets. The repository critic
is the only checked-in opt-in; the template and joke agent remain offline.

For Codex 0.153.2, the root requirements enable the `network_proxy` feature and
provide constraints but intentionally omit `experimental_network.enabled`.
Although the published activation model says sandbox networking remains the
access gate, the built-image app-server probe observes that managed
`enabled=true` exposes an allowlisted host even when the command policy says
`network_access=false`. The router therefore repeats the validated immutable
revision Boolean on every `codex exec` invocation, forcing `false` unless both
the revision and effective sandbox are `workspace-write` with networking
enabled. Proxy runtime state uses
`XDG_RUNTIME_DIR=/tmp/remoteagent-codex-runtime`, created mode 0700 inside the
existing per-container `/tmp` tmpfs; no new mount is introduced.

This is a hostname policy, not an HTTPS-only firewall. Codex 0.153.2 does not
let the deployment restrict scheme, port, HTTP method/body, calling process,
lockfile state, lifecycle scripts, or transfer size through this table. The
critic configures package managers for credential-free HTTPS and validates
declared dependency locations, but those are agent-enforced behaviors. A public
HTTP endpoint or alternate port on an admitted hostname may still be reachable,
and the allowlist cannot prevent source exfiltration through an admitted
service. Strict protocol enforcement requires a separate host or L7 egress
control that this release does not add. See the
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
and [internet-access risk guidance](https://learn.chatgpt.com/docs/cloud/internet-access).

The external bearer already authorizes registering/revising trusted agent
definitions, so the safe config shape is not hard-coded to one agent ID. A
bearer holder can create another network-enabled revision. Review configuration
changes and treat a repository submitted to the critic as trusted executable
code for that sandboxed turn. Repository text is nevertheless evidence, never
authorization to change critic policy. Dependency restoration occurs only in a
bounded disposable copy; image-baked tools, lock preference, credential
clearing, hook suppression, provenance, and cleanup reduce accidental exposure
but do not form a hostile-code security boundary.

Any bearer holder may choose a statically valid model and reasoning effort when
starting a conversation, which can change latency and shared subscription/API
capacity consumption. These selectors cannot change providers, endpoints,
credentials, permissions, or the sandbox. The router does not publish the
authenticated model catalog or preflight account/model compatibility; an
unavailable selection fails through the normal asynchronous job path without
exposing credential or catalog contents.

`scripts/remotectl doctor` runs a network-off dummy-credential probe through the
same nested sandbox and fails if the managed file becomes readable or the
workspace becomes unwritable. It never mounts or prints the real credential.
`scripts/remotectl smoke network` uses pinned app-server `command/exec` to
separately verify default-agent denial to
the otherwise allowlisted `example.com`, the critic's positive HTTPS result for
that probe host, denial to a reachable unlisted public host, and negative
loopback/private-service/Unix-socket behavior without invoking a model. The
source contract test pins all six admitted hostnames. The probe does not
exercise plain HTTP or alternate ports/methods on an admitted host,
link-local/metadata routing, DNS rebinding, upstream-proxy bypass, or every
possible socket path, and must not be read as evidence for those untested cases.

These controls materially narrow local file access, but the Compose settings
relax two outer-kernel filters so the inner sandbox can exist, and Codex still
has outbound model/web traffic. Treat prompts and agent definitions as trusted
in V1. Use a tailored host AppArmor profile plus narrow seccomp policy,
network allowlisting, and an isolated worker host/VM before accepting hostile
code. Re-test the managed policy whenever Codex CLI, Docker, or the host kernel
is upgraded.

## Companion acquisition and data

Companion inputs remain inside the existing trusted-internal-data boundary.
Their bytes are stored in plaintext on the host, are readable by the router and
the selected conversation's agent turns, and are included in conversation
backups after acceptance. RemoteAgent adds no malware scanning, quarantine,
content disarm, per-user ACL, or encryption at rest. Treat uploaded files and
public repositories as untrusted data even though the callers and deployment
are trusted; do not use companions for secrets.

Uploads stream into owner-only temporary files, hash incrementally, enforce the
configured byte quota, and become visible only after atomic finalization. Safe
archive extraction accepts `.zip`, `.tar`, `.tar.gz`, and `.tgz` while rejecting
absolute/traversing/control-character paths, duplicate normalized paths,
symbolic and hard links, devices, special permission bits, excessive file
counts, and excessive expanded bytes. Staging never runs uploaded executables,
package managers, hooks, or repository code.

Git import accepts only credential-free HTTPS URLs without userinfo, query, or
fragment data. Every pinned A/AAAA result must be globally routable; loopback,
private, link-local, reserved, or otherwise non-global targets are rejected.
Redirects and credential prompting are disabled. The subprocess clears inherited
credential helpers, proxy/config state, and interactive prompts and restricts
protocols and DNS resolution using Git's documented
[`http.curloptResolve`, redirect, and protocol controls](https://git-scm.com/docs/git-config).
Imports create a full bare mirror, pin one selected commit, validate its tree,
and materialize an independent writable checkout without fetching submodule or
Git LFS content.

Ephemeral unclaimed staging is outside the backup boundary and expires after 24
hours by default. After prompt acceptance, immutable source and editable working
copies follow conversation retention/deletion. A version introduced by a later
turn remains outside the shared workspace until its sequence boundary, avoiding
future-data exposure to an earlier active turn. Atomic links prevent partial
same-name replacements. Companion content is not exposed by a download API and
is never published automatically; agents must copy intentional return values to
`/workspace/artifacts`, where the artifact controls apply.

## Artifact safety

Only regular files beneath the conversation artifact directory are registered.
The router rejects absolute paths, traversal, symlink escapes, excessive file
counts, and files above the configured size. Artifacts inherit bearer-token
authorization and should be treated as potentially untrusted downloads.
