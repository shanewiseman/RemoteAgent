# Deployment

Read the [Docker runtime and Compose contract](docker-runtime.md) before changing
images, mounts, networks, services, dependency health checks, or host Docker
settings. Production acceptance status is tracked in the
[requirements baseline](requirements.md).

## Host prerequisites

- A dedicated Linux server on a trusted internal network.
- Docker Engine with the Compose v2 plugin and permission to use its socket.
- Bash, `curl`, `git`, `tar`, and Python 3 for local validation/smoke tooling.
- Direct host execution of `scripts/remotectl` requires Python 3.11 or newer
  (`tomllib`). The container administration wrapper below uses the router image's
  Python 3.12 instead, without installing Python on the host.
- Outbound HTTPS from agent containers to OpenAI authentication/model services,
  from the router to intended public Git hosts when Git companions are used,
  and from the repository critic to `example.com`, `pypi.org`,
  `files.pythonhosted.org`, `registry.npmjs.org`, `proxy.golang.org`, and
  `sum.golang.org` when project dependency restoration is required.
- Resolver access that returns all A/AAAA candidates for Git-host validation and
  supports the router's per-address Git DNS pinning.
- Enough disk for PostgreSQL, images, conversation workspaces, and artifacts.
- A Linux kernel/Docker runtime that permits nested Bubblewrap when the runner's
  reviewed `seccomp=unconfined` and `apparmor=unconfined` options are applied.

Do not run the router on a shared, untrusted Docker host. Access to the Docker
socket is effectively root access to the server.

Run `scripts/remotectl doctor` after every Docker, kernel, AppArmor, or Codex CLI
upgrade. Its inner-sandbox probe uses a dummy credential and no network; a
failure is a deployment blocker, not an optional warning.

Conversation workspaces remain host bind mounts without a general workspace
quota. Companion acquisition separately enforces default per-item, accepted
source, and deployment staging quotas, but editable working copies, artifacts,
sessions, and backups can still grow. Put `.runtime` on a dedicated
quota/monitored filesystem and alert well before exhaustion. Doctor's free-space
check is a preflight signal, not storage enforcement.

Dockerfile package downloads use the builder's normal bridge network. On a
dedicated Linux builder where host firewall rules block bridge egress, set
`REMOTEAGENT_BUILD_NETWORK=host` in `.env` for builds; this grants Dockerfile
`RUN` steps host-network access and should not be enabled on a shared builder.

## First deployment

```sh
git clone YOUR_INTERNAL_REMOTE RemoteAgent
cd RemoteAgent
scripts/remotectl init
scripts/remotectl build all
scripts/remotectl skills sync
scripts/remotectl auth login --method chatgpt
scripts/remotectl start
scripts/remotectl doctor
```

`init` is idempotent. It copies `.env.example` only when `.env` is absent,
records absolute host paths and the Docker socket group, creates restricted
runtime directories (including owner-only companion staging), and creates
independent 256-bit router, database, scoped
cron MCP, and cron internal-API secrets. It merges the server's hostname, FQDN,
and interface addresses into the MCP Host and Origin allowlists and always
retains `router:8080` in the Host allowlist for the backend cron client. It
never prints or overwrites an existing secret.

The generated `.env` carries these standard companion capacity defaults (bytes
are decimal-free integer counts):

```dotenv
REMOTEAGENT_COMPANION_MAX_OBJECT_BYTES=104857600
REMOTEAGENT_COMPANION_MAX_FILES_PER_ITEM=20000
REMOTEAGENT_COMPANION_MAX_PER_TURN=20
REMOTEAGENT_COMPANION_MAX_ACTIVE_NAMES=200
REMOTEAGENT_COMPANION_MAX_CONVERSATION_BYTES=1073741824
REMOTEAGENT_COMPANION_STAGING_MAX_BYTES=5368709120
REMOTEAGENT_COMPANION_STAGE_TTL_SECONDS=86400
REMOTEAGENT_COMPANION_GIT_WORKERS=2
REMOTEAGENT_COMPANION_GIT_TIMEOUT_SECONDS=300
```

Tune these together with filesystem capacity and recreate the router. Reducing
a limit does not rewrite already accepted conversations; it constrains new
staging/binding admission and reconciliation. The static REST/MCP contract caps
bindings at 20, so `REMOTEAGENT_COMPANION_MAX_PER_TURN` may only lower that
deployment limit.

On a headless host the default login executes `codex login --device-auth` in the
auth helper. Device-code login may first need to be enabled in ChatGPT security
or workspace settings. As an alternative, authenticate on a trusted workstation
and import its restricted `auth.json`:

```sh
chmod 600 /secure/path/auth.json
scripts/remotectl auth import --file /secure/path/auth.json
```

### Administration using a prebuilt image

For a host with an older Python installation, use `scripts/remotectl-container`
in place of `scripts/remotectl`. It runs a disposable administration container
from the router image, with its Python 3.12, Docker CLI, Compose, and shell tools.
The host only needs Bash, Docker, and ordinary core utilities. The router image
must already be built or loaded; the wrapper never pulls an image implicitly.

After loading the release images and copying the reviewed source checkout:

```sh
REMOTEAGENT_ADMIN_IMAGE=remoteagent/router:RELEASE \
  scripts/remotectl-container init --non-interactive
# Set REMOTEAGENT_VERSION=RELEASE in the generated .env to match every image.
# Set REMOTEAGENT_BIND_ADDRESS to the intended host interface before starting.
scripts/remotectl-container validate --all
scripts/remotectl-container skills sync
scripts/remotectl-container auth login --method chatgpt
scripts/remotectl-container start
scripts/remotectl-container doctor
```

`REMOTEAGENT_ADMIN_IMAGE` selects an explicit helper image. Otherwise the wrapper
reads only the literal `REMOTEAGENT_VERSION` from `.env` (or `--env-file`) and
selects `remoteagent/router:<version>`, defaulting to `local`. It does not source
the environment file into the host shell. The normal CLI reads that
operator-owned file inside the helper. Alternate environment files must remain
inside the repository tree.

The helper runs with the invoking user's UID/GID and the Docker socket's group.
It mounts the repository read-write at the same absolute path, mounts the chosen
Unix Docker socket, and uses the host network so health checks reach the selected
host bind address. Both its Docker client and the child Compose commands target
that socket; a remote Docker context is not used. These are administrative,
root-equivalent Docker permissions, scoped operationally to the requested CLI
command. The helper has a read-only root and writable temporary filesystem;
its execution does not install packages or change host Docker settings.
Administration scratch lives under `.runtime/admin-tmp` on the same-path mount
so disposable recovery deployments remain visible to the host Docker daemon.

An interactive terminal is preserved for device login; through SSH, allocate a
terminal with `ssh -t`. Piped input is passed without terminal processing. For
credential import, use a protected regular file inside the repository mount;
the normal import command requires file size and permission checks and does not
accept a pipe as its `--file`. Never put credential contents in command arguments.
Backup/restore paths must likewise be
inside the mounted repository. The default `.runtime/backups` path meets that
requirement. A source checkout's `.git` directory must be present for the CLI's
Git-based upgrade commands.

### Codex startup caches and skills

The agent image runs its build-time Codex feature probe as the final non-root
agent user. This matters on a fresh installation: Docker populates an empty
auth volume from the image's `$CODEX_HOME`, including any caches created during
the build. A successful credential import or `auth status` check does not prove
that an agent can start; finish installation with a live first turn and a
continuation using the selected release images.

`$CODEX_HOME/skills` is a writable directory for Codex's bundled `.system`
skills. Individual common skills link into the read-only shared skills volume.
The entrypoint automatically replaces the old whole-directory symlink only
when its target exactly matches the configured shared skills path. It preserves
unrelated custom skills and fails on conflicting paths instead of overwriting
them. Rebuild the base and every agent image to apply this change.

An existing auth volume created by an older image can retain root-owned
`$CODEX_HOME/tmp/arg0` caches and fail startup with `Permission denied` even after
an image rebuild. Quiesce jobs and auth helpers, inspect the affected paths,
and use a reviewed one-off administration container to restore ownership of
only the auth volume's `$CODEX_HOME/tmp` subtree to the deployment UID/GID.
Confirm that the subtree is a real directory and do not follow symlinks during
the ownership repair. Preserve credentials, sessions, and the rest of the
volume; rebuilding images does not migrate existing volume contents. This is
an application-volume repair and requires no host Docker or kernel changes.

For release acceptance, check both an initially empty auth volume and a volume
with the legacy skills symlink. Verify that a non-root runner can create its
Codex cache and bundled `.system` skills, discover a common skill, complete a
real turn, and resume that conversation. Keep the shared skills mount read-only
throughout these checks.

## Network placement

The router binds `0.0.0.0:8080` by default. Cron, PostgreSQL, and Redis stay on
an internal Compose network and publish no host ports. Restrict port 8080 with the host firewall to known MCP
callers. For TLS, place a reverse proxy on the edge, forward to
`127.0.0.1:8080`, set `REMOTEAGENT_BIND_ADDRESS=127.0.0.1`, and change
`REMOTEAGENT_DASHBOARD_ALLOW_HTTP=false` after secure-cookie forwarding works.

For a specific internal interface, set for example
`REMOTEAGENT_BIND_ADDRESS=192.168.20.111` and `REMOTEAGENT_PORT=8080`.
Administration checks follow that address. Wildcard IPv4/IPv6 binds use their
corresponding loopback address for checks; specific IPv6 addresses are bracketed
when constructing HTTP URLs.

MCP transport rejects unlisted HTTP `Host` values with status 421 to prevent
DNS rebinding. If callers use a DNS alias, load balancer, reverse-proxy name, or
a port different from the initialized server address, append its exact
`host:port` to the JSON list in `REMOTEAGENT_MCP_ALLOWED_HOSTS`. Browser-based
MCP clients also need their exact scheme/host/port in
`REMOTEAGENT_MCP_ALLOWED_ORIGINS`. Recreate the router after editing `.env`;
never use `*` as a catch-all host.

Public Git companion import is not a generic outbound fetch proxy. The router
accepts credential-free HTTPS only, resolves and validates every address as
globally routable, pins Git to those addresses, disables redirects and
interactive credentials, and clears inherited proxy/config state. Permit only
the DNS and outbound HTTPS required by deployment policy. An upstream egress
proxy or firewall is still recommended defense in depth before broadening the
trusted internal boundary.

The repository critic is a distinct command-network opt-in. Its Codex sandbox
uses the root-owned managed proxy, whose allowlist contains exactly
`example.com`, `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`,
`proxy.golang.org`, and `sum.golang.org`; other checked-in agents remain
network-off. Ensure public DNS and bridge egress work from the runner, then use
`scripts/remotectl smoke network` to execute the exact pinned Codex app-server
policy without a model and verify default-agent denial to the otherwise admitted
`example.com`, critic HTTPS success to that probe host, unlisted-public-host
denial, and critic loopback/private-service/Unix-socket denial.
DNS rebinding and reachable link-local/metadata fixtures are not exercised by
that probe. The proxy filters hostnames but cannot restrict scheme, port, or
HTTP method for an admitted host. If those guarantees are required, add a
separately reviewed host or L7 egress control before enabling the critic.

## Updating

The management CLI deliberately does not modify Git state:

```sh
git fetch --tags
git switch --detach SIGNED_RELEASE_TAG
scripts/remotectl init --non-interactive
scripts/remotectl upgrade check
scripts/remotectl upgrade apply --yes
```

The initialization pass after switching releases is required when upgrading
from 0.1.x. It preserves existing secrets and settings, creates only the missing
cron service tokens, and merges `router:8080` into the MCP Host allowlist.
Without it, 0.2 validation intentionally fails rather than starting services
with absent or default internal credentials.

Upgrade refuses a dirty checkout, stops cron before checking router idleness,
and keeps both services quiesced while creating the consistent backup and
rebuilding immutable images. It recreates the router before cron. If the
upgrade fails, an exit guard attempts to restore the prior services in the same
order. Keep the old checkout/images and generated backup
until health, discovery, and a manual smoke test have passed.

The 0.3 companion migration is additive. It adds `companion_stages` and
`conversation_companions`, while accepted data uses the existing complete
conversation-tree backup/deletion boundary. Unclaimed
`.runtime/companion-staging` data is ephemeral and excluded from backups. After
upgrade, verify a file upload, asynchronous Git import/poll, first-turn binding,
later-turn edit persistence, and same-name replacement before removing the
backup. Remember that companion bytes are plaintext and accessible to every
holder of the shared router bearer through the supported metadata/control
surface; this release adds no per-user ACL or encryption at rest.

Release 0.4.0 has no database migration and keeps `/api/v1` wire shapes. It adds
one fail-closed structured agent-config exception and bakes a managed network
policy into every runner image. Rebuild every agent image, verify the default
network-off doctor probe, run `scripts/remotectl smoke network`, and run both
live smoke agents before accepting the release. Phonebook synchronization
creates a missing `repository-critic` but does not overwrite an existing
durable revision.

Release 0.4.1 also has no database migration and preserves the public REST/MCP
schemas and `/api/v1`. It makes the four-hour job timeout an absolute
submission-through-collection deadline, adds truthful structured readiness and
confined artifact reconciliation, enforces the resolved per-agent Compose
contract, hardens cron worker/transport verification, and makes database plus
filesystem restore rollback-coherent. Run both local disposable backend smokes
before deploying this patch. Backup scheduling, off-host transfer, CI, and
owner-approved RPO/RTO remain explicitly deferred in
[the review-disposition register](review-dispositions.md).

For immediate rollback, drain critic jobs and publish a new immutable critic
revision without `network_access=true`; then disable the managed proxy in the
image policy and rebuild every agent. For a full 0.3 downgrade, before switching
to the 0.3 checkout use the 0.4 API to replace the durable critic definition in
one new revision with both `enabled=false` and a `config_toml` that omits the
entire `[sandbox_workspace_write]` table. Verify it appears disabled with
`GET /api/v1/agents?include_disabled=true`, then drain all remaining critic
jobs and delete critic conversations that must not remain resumable before
checking out 0.3. Editing the phonebook or merely removing
`network_access=true` from checked-in files is insufficient: phonebook
synchronization does not overwrite the durable current revision, and 0.3 cannot
validate the nested table. Retain a 0.4 compatibility build instead when critic
conversations must remain resumable.

The conversation model-profile schema change remains additive. Its migration adds
nullable `model` and `reasoning_effort` snapshots to conversations and jobs and
leaves pre-upgrade rows `null`, preserving their dynamic agent-config/Codex
inheritance behavior. It does not rewrite conversation workspaces, Codex session
files, or thread IDs. After upgrading, verify an existing continuation as well
as a new conversation with explicit selectors before removing the backup.

Release 0.2.0 also creates cron-owned tables and its independent
`cron_alembic_version` record. There is no router-table backfill and no
cross-service foreign key. Verify `remotectl migrate status all`, the protected
cron readiness check in `remotectl doctor`, one deterministic scheduled run,
and response lease/acknowledgement before removing the backup.

## Version pinning

The runtime Dockerfiles pin Python, Node, Docker CLI/Compose, Codex CLI, and
critic coverage executors. Update those pins through normal review, rebuild all
agent images, and run deterministic tests before deployment. Agent executables
must be ready when instantiated. The critic's narrower exception installs only
target-project dependencies into disposable workspace scratch and records the
resolved set; it never mutates the image.
