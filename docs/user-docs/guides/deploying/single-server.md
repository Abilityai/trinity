# Single-Server Deployment

Run Trinity on a Linux VPS or dedicated server with a stable URL. Two install methods share one `.env` contract, one installer and one set of day-two procedures:

- **Option A — prebuilt images** (`./scripts/deploy/start.sh --hosted`): every platform image and the agent base image are pulled from GHCR. A fresh VM is serving in about two minutes. **This is the path you want on a server.**
- **Option B — build from source** (`docker compose -f docker-compose.prod.yml`): the server compiles its own images, including the ~1.9 GB agent base image (5–10 minutes). Use it when you carry local patches or the enterprise overlay.

Both use the production compose shape: no hot-reload, health checks and `unless-stopped` restart policies on every service, Redis off the agent network. A third path, the [DigitalOcean Marketplace 1-Click](#digitalocean-marketplace-1-click), is Option A baked into a Droplet image.

## Prerequisites

- Linux server (Ubuntu 22.04 LTS or later recommended), **8 GB RAM minimum** — below that the agent containers and the platform services contend and turns start failing under load
- Docker Engine 24+ and the Docker Compose plugin (`docker compose` — no hyphen)
- A domain or subdomain pointing to your server's IP (e.g., `trinity.your-domain.com`), or a plan for private access — see [TLS on a bare VM](#tls-on-a-bare-vm)
- `openssl` on the server for secret generation
- Outbound HTTPS access from the server (image pulls from `ghcr.io`, Anthropic API calls)

## Which compose files go together

Trinity ships **three complete stacks**, not one base file plus overlays. `docker-compose.prod.yml` and `docker-compose.hosted.yml` are standalone: each restates the hardening (`security_opt`, `group_add`, `cap_drop`) because nothing else supplies it.

| Install | Command | Where `/data` lives |
|---|---|---|
| Dev (source build, localhost) | `./scripts/deploy/start.sh` — i.e. `docker compose up -d` (auto-merges `docker-compose.override.yml` if present) | named volume `trinity-data` |
| Production (source build) | `docker compose -f docker-compose.prod.yml up -d` (+ `-f docker-compose.prod.enterprise.yml` with the enterprise submodule) | bind mount `${TRINITY_DATA_PATH:-./trinity-data}` |
| Hosted (prebuilt GHCR images) | `./scripts/deploy/start.sh --hosted` — day-two: `docker compose -f docker-compose.hosted.yml …` | bind mount `${TRINITY_DATA_PATH:-./trinity-data}` |

The remaining files are narrow overlays and never a third stack: `docker-compose.override.example.yml` (the Docker Desktop Vector log source — `start.sh` copies it to `docker-compose.override.yml` on Docker Desktop and appends it by name under `--hosted`), `docker-compose.gitea.yml` (dev-only: a local Gitea for git-sync testing, layered on the dev file), `docker-compose.prod.enterprise.yml` (layers the private enterprise submodule onto prod), and `docker-compose.sibling.yml` (a Redis-only stack for integration tests).

**Never stack the dev file under prod or hosted** (`-f docker-compose.yml -f docker-compose.prod.yml`). Compose concatenates list-type keys, so the combination either fails validation on the duplicated hardening entries (Compose ≥ 2.24) or, on older versions, silently gives the frontend two host mappings for one port and it never joins its network.

**Never run a bare `docker compose up -d` on a production host.** It loads the dev file, whose `/data` is the named volume, and boots a healthy-looking backend on an empty database while the real one sits untouched in `TRINITY_DATA_PATH`. `start.sh` refuses this crossing in both directions and prints the file set the host was installed with (`quickstart.sh` is an alias for `start.sh`, so it inherits the refusal). If both stores already exist — the state a wrong-file start leaves behind — `start.sh` warns which one it is about to use rather than staying silent.

## Option A: Prebuilt images (recommended)

```bash
git clone https://github.com/abilityai/trinity.git && cd trinity
cp .env.example .env
# Set ADMIN_PASSWORD, ANTHROPIC_API_KEY, FRONTEND_URL / PUBLIC_CHAT_URL, and an email provider (see the .env sections below)

# Pin the release you want, in .env — `latest` moves on every Trinity release,
# so an unpinned install turns your next re-run into an unscheduled upgrade.
echo 'TRINITY_IMAGE_TAG=v0.9.0' >> .env

./scripts/deploy/start.sh --hosted --unattended
```

What `--hosted` changes, and nothing else:

- **Compose file.** `docker-compose.hosted.yml` — `docker-compose.prod.yml` with every `build:` block replaced by a GHCR `image:` reference. Same ports, volumes, networks, `.env` keys and security posture; a CI guard fails the build if the two files ever disagree.
- **Image tag.** `TRINITY_IMAGE_TAG` selects the image set (default `latest`). Precedence: a value in the shell or CI → `.env` → `latest`. Put it in `.env`: that is where the pin survives a reboot and whoever runs the next upgrade. Each release publishes `v0.9.0`, `0.9.0`, `0.9`, `latest` and `sha-<short>` for one digest, so either version spelling works. A pre-release tag (for example `v0.9.5-rc1`) publishes its own tags but never moves `latest`.
- **Agent base image.** The backend creates agent containers from the local tag `trinity-agent-base:latest`, which is not a compose service. `start.sh --hosted` pulls `ghcr.io/abilityai/trinity-agent-base:<tag>` and tags it locally before bringing the stack up. A bare `docker compose -f docker-compose.hosted.yml up -d` starts a platform that cannot create a single agent and does not fail until the first agent-create.
- **No fallback to building.** A failed pull is fatal and the message names the likely causes: the tag is not published (check the release list), the GHCR package is private (`denied` / `unauthorized` — a publishing fault, report it), or no route to `ghcr.io`. Drop `--hosted` to build from source instead.
- **Tunnel.** A non-empty `TUNNEL_TOKEN` in `.env` starts the `cloudflared` service (`--profile tunnel`) and persists `COMPOSE_PROFILES=tunnel` to `.env`, so later bare `docker compose -f docker-compose.hosted.yml stop` / `logs` commands act on the tunnel container too. See [Public Access](public-access.md).
- **Build provenance.** Not stamped from your checkout — `GET /api/version` reports the commit the release workflow built.

Everything else — secret generation, the `ADMIN_PASSWORD` contract, `DOCKER_GID` detection, the data-directory ownership fix, the 180-second serving poll and the next-steps card — is the same `start.sh` behaviour described in [Local Development → Start Services](local-development.md#4-start-services).

**Pull-only means it does not build — not that it needs nothing on disk.** The hosted compose mounts `./config/agent-templates`, `./config/process-templates`, `./config/process-docs`, `./config/manifests`, `./config/vector.yaml` and `./config/otel-collector.yaml` from the checkout, and `/data` is the `${TRINITY_DATA_PATH:-./trinity-data}` bind mount. Handed to a managed host or a compose catalogue without the repository, Docker creates each of those as an empty directory: the template catalog is empty and the first-run starter fleet finds no manifest, with no error at install time. Keep the checkout beside the compose file.

**Converting an existing source install in place is not a drop-in.** The dev stack keeps `/data` in the named volume `trinity-data`; hosted (like prod) binds `${TRINITY_DATA_PATH:-./trinity-data}`. `start.sh --hosted` detects a dev volume with no bind-mount database, refuses, and prints the copy command (`docker compose stop`, then copy the volume contents into the directory, then re-run). Set `TRINITY_DATA_PATH` to a new directory to deliberately start fresh.

Day-two commands on a hosted install (note the explicit `-f` — hosted opts out of compose's default file merge):

```bash
docker compose -f docker-compose.hosted.yml ps
docker compose -f docker-compose.hosted.yml logs -f backend
./scripts/deploy/stop.sh                       # runs `stop`, never `down`; detects the hosted file itself
./scripts/deploy/start.sh --hosted             # start again — and the upgrade command (see Upgrading)
```

Upgrading a hosted install is a re-run of `start.sh --hosted` with a new `TRINITY_IMAGE_TAG`, never a bare `docker compose pull` (which skips the agent base image and leaves agents on the old runtime) — see [Upgrading → Hosted installs](upgrading.md#hosted-pull-only-installs).

## Option B: Build from source

### 1. Clone the Repository

```bash
git clone https://github.com/abilityai/trinity.git
cd trinity
```

### 2. Configure `.env`

```bash
cp .env.example .env
```

`start.sh` has no production-compose mode: without `--hosted` it starts the **dev** stack. A source-built production install is therefore brought up with `docker compose -f docker-compose.prod.yml` directly, and you generate the secrets yourself. Every variable in the tables below is forwarded by `docker-compose.prod.yml` (and by the hosted file — the two agree by construction).

#### Security-critical (must be set before first boot)

| Variable | How to generate | Notes |
|---|---|---|
| `SECRET_KEY` | `openssl rand -hex 32` | JWT signing key. Never reuse across instances. |
| `ADMIN_PASSWORD` | Choose a strong password | Minimum 12 characters. Drives both `admin` login and the MCP server's legacy auth path. **Required** — `docker-compose.prod.yml` refuses to render if unset. |
| `ADMIN_USERNAME` | Optional, default `admin` | The admin account's username. Forwarded by all three compose files. |
| `CREDENTIAL_ENCRYPTION_KEY` | `openssl rand -hex 32` | Encrypts OAuth tokens, channel bot tokens, subscription credentials and the credential-bearing platform settings. **If lost, all encrypted credentials become unrecoverable.** |
| `INTERNAL_API_SECRET` | `openssl rand -hex 32` | Authenticates scheduler-to-backend calls. Set it explicitly — do not rely on the `SECRET_KEY` fallback. |
| `AGENT_AUTH_SECRET` | `openssl rand -hex 32` | Master secret the backend derives each agent's in-container auth token from. **Never rotate** — every running agent's token stops working until the agent is recreated. |
| `REDIS_PASSWORD` | `openssl rand -hex 24` | Admin/`default` ACL user. Used for recovery and ad-hoc ops. |
| `REDIS_BACKEND_PASSWORD` | `openssl rand -hex 24` | Runtime ACL user for `backend` and `scheduler` containers. Embedded in `REDIS_URL` at compose render time. **Required** — compose refuses to render without it. |
| `DOCKER_GID` | GID of `/var/run/docker.sock` as a container sees it | Compose falls back to `999` (the Debian/Ubuntu `docker` group). Set it if your host's socket group differs (`stat -c '%g' /var/run/docker.sock`); `start.sh` detects it for hosted installs. |

Generate the hex secrets at once:

```bash
echo "SECRET_KEY=$(openssl rand -hex 32)"
echo "CREDENTIAL_ENCRYPTION_KEY=$(openssl rand -hex 32)"
echo "INTERNAL_API_SECRET=$(openssl rand -hex 32)"
echo "AGENT_AUTH_SECRET=$(openssl rand -hex 32)"
echo "REDIS_PASSWORD=$(openssl rand -hex 24)"
echo "REDIS_BACKEND_PASSWORD=$(openssl rand -hex 24)"
```

Paste the output into `.env`.

#### Redis security note

Trinity uses two separate Redis passwords by design. `REDIS_BACKEND_PASSWORD` is the runtime credential embedded in `REDIS_URL` for the `backend` and `scheduler` containers. Even if a platform container were compromised and this password leaked, it does **not** grant access to destructive Redis commands (`FLUSHALL`, `CONFIG`, `SHUTDOWN`, etc.) — those require `REDIS_PASSWORD`. See `docs/migrations/REDIS_AUTH.md` for details on the ACL design.

#### Required for agent functionality

| Variable | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | Required for agents to run Claude. Can be left blank and configured in Settings after login (or connect a Claude subscription there). |
| `GITHUB_PAT` | Required to clone private GitHub template repos. |

#### Required for production access

| Variable | Notes |
|---|---|
| `FRONTEND_URL` | Your public-facing domain (e.g., `https://trinity.your-domain.com`). Used for OAuth redirect callbacks and email verification links. |
| `PUBLIC_CHAT_URL` | The externally reachable URL for public chat links and webhooks. Often the same as `FRONTEND_URL`. Leave blank if all users access via VPN. |
| `FRONTEND_PORT` | Host port the web UI binds (default `80`). Honoured by the prod and hosted compose files as well as dev. |
| `TRINITY_DATA_PATH` | Bind-mount directory for `/data` (see [Data path](#data-path)). |

#### Email authentication

Email login is enabled by default. Set at least one email provider:

| Variable | Notes |
|---|---|
| `EMAIL_PROVIDER` | `resend` (recommended), `sendgrid`, `smtp`, or `console` (logs codes — dev only). `.env.example` ships `console`; the compose default when the line is absent is `resend`. |
| `RESEND_API_KEY` | Required when `EMAIL_PROVIDER=resend`. Get from [resend.com](https://resend.com/api-keys). |
| `SENDGRID_API_KEY` | Required when `EMAIL_PROVIDER=sendgrid`. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | Required when `EMAIL_PROVIDER=smtp`. |
| `SMTP_FROM` | From address for verification emails (e.g., `noreply@your-domain.com`). |

#### Optional integrations

| Variable | Notes |
|---|---|
| `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` / `SLACK_SIGNING_SECRET` | Slack OAuth and channel adapter. The client id/secret are read by the prod and hosted files only. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google Workspace OAuth. |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub OAuth (prod and hosted only). |
| `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` | Notion OAuth (prod and hosted only). |
| `GEMINI_API_KEY` | Platform image generation, voice chat, voice transcription. |
| `TUNNEL_TOKEN` | Cloudflare Tunnel token — see [Public Access](public-access.md). |

#### Data path

The prod and hosted compose files use a bind-mount directory for `trinity.db` instead of a named Docker volume:

```
TRINITY_DATA_PATH=./trinity-data
```

The default `./trinity-data` is relative to the repo root. Use an absolute path on a server for clarity:

```
TRINITY_DATA_PATH=/srv/trinity-data
```

Create the directory before starting, owned by UID 1000 (the backend and scheduler run as a non-root user and cannot create `trinity.db` in a root-owned directory; `start.sh` does this step for hosted installs):

```bash
mkdir -p /srv/trinity-data && sudo chown -R 1000:1000 /srv/trinity-data
```

The automatic database backups land in `backups/` under this directory — see [Backup and Restore](backup-and-restore.md).

#### Database backend

Trinity stores platform state in **SQLite** by default, in `trinity.db` under your `TRINITY_DATA_PATH` bind mount (`/data/trinity.db` inside the container). SQLite works with zero configuration — but **PostgreSQL is the recommended backend for production**, and **SQLite support ends September 1, 2026** (after that date it stops receiving schema migrations and fixes).

To run on PostgreSQL, set one variable in `.env`:

```
DATABASE_URL=postgresql://trinity:your-postgres-password@your-db-host:5432/trinity
```

Both the backend and the scheduler pick it up (`DB_POOL_SIZE` and `DB_MAX_OVERFLOW` tune the pool; both default sensibly). Notes for the prod and hosted compose:

- Neither ships a **bundled PostgreSQL service** — point `DATABASE_URL` at an operator-managed instance (a managed cloud database or your own PostgreSQL server). The bundled `--profile postgres` container exists only in the dev compose.
- Selection is non-sticky and non-destructive: comment `DATABASE_URL` out and the next restart is back on SQLite.
- A fresh PostgreSQL database is initialized automatically on first boot (Alembic-managed migrations).
- **Migrating an existing SQLite instance?** Use the Trinity Ops Agent's `/migrate-to-postgres` skill ([ops-agent guide](ops-agent.md)) — a validate-then-cutover flow that never writes to your SQLite file, so rollback is one line. New-instance setup details: `docs/POSTGRESQL_SETUP.md` in the repo.
- On PostgreSQL, the automatic backups are `pg_dump` archives, and a manual backup is `pg_dump` too — never a copy of `trinity.db`. See [Backup and Restore](backup-and-restore.md).

On every backend boot, a versioned migration runner brings the schema up to date (the bespoke SQLite runner or Alembic for PostgreSQL). The runner is crash-safe and concurrency-safe: a cross-process lock serialises it so multiple workers and the scheduler cannot race each other, and table rebuilds run inside a transaction that rolls back cleanly on a mid-migration crash. If a migration is still pending or has failed, the backend's `/health` endpoint returns `503` with a `migrations` block (`applied`, `expected`, `first_pending`) naming the stuck migration — so a 503 from `curl http://localhost:8000/health` during an upgrade is actionable, not opaque. Before any migration runs, the backend takes a `pre-migration-<timestamp>.db` copy (SQLite) into `backups/`.

### 3. Build the Base Agent Image

```bash
./scripts/deploy/build-base-image.sh
```

This builds `trinity-agent-base:latest` — the image every agent container inherits. Required before you can create any agents. Takes 5–10 minutes on first build.

### 4. Build and Start Platform Services

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

This starts: `backend`, `frontend`, `redis`, `mcp-server`, `scheduler`, `vector`, and `otel-collector`, plus a one-shot `trinity-logs-init` container that prepares the log directory and exits.

The `cloudflared` tunnel service is **not started** by default — it requires an explicit `--profile tunnel` flag. See [Public Access](public-access.md).

## First Login

Open your domain (or `http://your-server-ip`, or `http://your-server-ip:$FRONTEND_PORT`) in a browser. Log in with:
- **Username:** `admin` (or `ADMIN_USERNAME` if you changed it)
- **Password:** the `ADMIN_PASSWORD` you set in `.env` (re-applied on every backend start — change it there, not in the UI)

There is no setup wizard on a server install: setting `ADMIN_PASSWORD` (mandatory in both server compose files) provisions the admin account during startup, and the unauthenticated first-run form refuses to run once a usable admin exists. What you see instead is the Dashboard with a fresh install's starter fleet and a **Finish setup** card offering to bind a sign-in email (so you can log in with email + password) — see [First-Time Setup](../../getting-started/setup.md).

After login, go to **Settings → Access → Email Whitelist** to allow team members to log in via email verification.

## Connect from Claude Code

Create an MCP API key:
1. Log in to the web UI
2. Go to **Settings → MCP Keys**
3. Create a new key and copy it

Then connect from your Claude Code session:

```bash
/trinity:connect
# URL: https://trinity.your-domain.com/mcp   (the production frontend proxies /mcp to the MCP server)
#      or http://your-server:8080/mcp        (the MCP server's own port, where it is reachable)
# API Key: (your MCP API key)
```

The `/mcp` proxy on the production nginx means an install that exposes only ports 80/443 — a tunnel, a reverse proxy, the DigitalOcean 1-Click — still serves MCP at the same hostname as the web UI. **Settings → MCP Keys → MCP Server URL** sets the URL the UI shows to users; leave it empty to derive it from the hostname.

## Restart vs. Down

> **Use `docker compose restart`, not `down/up`.** `docker compose down` removes the `trinity-agent-network`, which orphans every running agent container — they keep running but lose their network and have to be removed and recreated. `restart` preserves both the agents and the network. The only times to use `down` are: (1) intentional full teardown, (2) recovering from a corrupted compose state.

```bash
# Correct way to restart platform services
docker compose -f docker-compose.prod.yml restart backend frontend mcp-server scheduler
# Hosted installs
docker compose -f docker-compose.hosted.yml restart backend frontend mcp-server scheduler

# Full stop (agents will need to be restarted/recreated)
docker compose -f docker-compose.prod.yml down
```

Since agent containers are created with Docker's `unless-stopped` restart policy, a `down` no longer leaves them merely orphaned: dockerd retries each one in a backoff loop against a network that no longer exists, and the roster shows them as stopped while it churns. Recovery: `docker compose -f <file> up -d` (recreates the network), then `docker rm -f` each stale agent container and start it again from the UI or **Operations → Restart All** — the workspace volume holds the agent's data and is untouched by a container removal.

`./scripts/deploy/stop.sh` runs `stop`, never `down`, and reads the running stack's compose label to pick `docker-compose.hosted.yml` when needed. It does **not** detect a source-built production stack — on one of those, use the explicit `docker compose -f docker-compose.prod.yml stop`.

### Host reboots

Every platform service carries `restart: unless-stopped`, and so does every agent container Trinity creates, so a host reboot brings the whole fleet back. An agent you stopped on purpose (Stop, quarantine, emergency stop) stays stopped — that is the difference between `unless-stopped` and `always`. Agent containers created before this policy shipped keep Docker's default (`no`) until they are recreated; the one-shot sweep is in [Upgrading → Agent restart policy](upgrading.md#agent-restart-policy-one-time-after-upgrading).

## Verify Service Health

After starting, verify all services are healthy:

```bash
# Backend
curl -s http://localhost:8000/health

# Scheduler
curl -s http://localhost:8001/health

# Frontend
curl -s -o /dev/null -w '%{http_code}' http://localhost

# Redis
docker exec trinity-redis redis-cli ping

# MCP Server
curl -s http://localhost:8080/health

# Vector
docker exec trinity-vector wget -q -O - http://localhost:8686/health
```

The scheduler's port 8001 is not published to the host by either server compose file — probe it from inside the container instead: `docker exec trinity-scheduler curl -sf http://localhost:8001/health`. See [monitoring.md](monitoring.md) for the full monitoring guide.

## TLS on a bare VM

Trinity serves plain HTTP and terminates TLS **outside** the application. There is no HTTPS listener in any compose file and no auto-certificate step, so pick one of these before putting an instance on a public address:

| Path | What it gives you | When to use it |
|---|---|---|
| **Tunnel** (Cloudflare Tunnel — set `TUNNEL_TOKEN` in `.env`) | HTTPS at a real hostname, no inbound ports open at all | The default for a public instance. Nothing to renew. |
| **Private network** (Tailscale / WireGuard / VPC) | Encrypted transport, instance not on the public internet | HTTP over a WireGuard tunnel is encrypted — this is a finished posture, not a compromise. |
| **Reverse proxy you run** (Caddy / nginx + Let's Encrypt) | HTTPS at your own domain | You already operate a proxy, or you need a domain the tunnel can't serve. |

Plain HTTP on a public IPv4 with none of the above is the one combination to avoid: credentials and JWTs cross the network in the clear. The DigitalOcean 1-Click below is the deliberate exception — it ships its own Caddy with a short-lived certificate for the Droplet's IP. Tunnel setup: [Public Access](public-access.md).

## DigitalOcean Marketplace 1-Click

The Trinity 1-Click is a Droplet image with Docker, Caddy, ufw and a pinned Trinity release already pulled — Option A baked into a snapshot, so first boot pulls nothing.

### Sizing

| Use case | RAM | vCPU | Boot disk |
|---|---|---|---|
| Minimum — platform plus 1–2 agents | 4 GB | 2 | 50 GB |
| **Recommended** — a working fleet | **8 GB** | **4** | **80 GB** |
| Larger fleets | 16 GB+ | 8+ | 160 GB+ |

The baked images occupy a significant share of the disk before any agent exists; agent workspaces grow from there. Disk can be increased on a running Droplet, never decreased.

### What first boot does

First boot runs once per Droplet, about ninety seconds, with no input from you:

1. **Admin password** — generated (24 characters) and written to `/etc/trinity/admin-credentials`, or taken from a file you supplied through user-data (below).
2. **`.env`** — written at `/opt/trinity/.env` with `ADMIN_PASSWORD`, `FRONTEND_PORT=8081` (the web UI moves off `:80` so Caddy can own 80/443), `TRINITY_IMAGE_TAG=<the baked release>`, `TRINITY_INSTALL_SOURCE=do-marketplace` (the install-provenance marker) and `FRONTEND_URL=https://<droplet-ip>`. `start.sh` generates the remaining secrets as on any install.
3. **Firewall** — ufw allows 22, 80 and 443 only, and a `DOCKER-USER` rule set drops internet traffic to every port the compose file publishes (8000, 8080, 8081, 8686, the OTel ports). Docker publishes past ufw, so this rule set is what actually closes those ports; a systemd unit re-applies it on every boot.
4. **Caddy** — a Caddyfile for `https://<droplet-ip>` with a Let's Encrypt short-lived IP certificate (about six days, renewed automatically), reverse-proxying to the web UI on `127.0.0.1:8081`; `http://` redirects to `https://`. First boot then verifies the certificate was actually issued and records the result — the login banner prints `http://` if it was not.
5. **Trinity** — `./scripts/deploy/start.sh --hosted --unattended` from `/opt/trinity`.

### Get your admin password

The password is printed in the login banner. How you reach it depends on the authentication you chose when creating the Droplet:

- **Password auth** — open **Droplet → Console** in the DigitalOcean control panel and log in as `root`. No SSH client needed.
- **SSH-key auth** — DigitalOcean leaves the root account locked, so the Console cannot accept a login. Connect with your key: `ssh root@<droplet-ip>`.

Either way the banner prints the Trinity password, the URL to open, and whether HTTPS came up. Re-read it any time with `cat /etc/trinity/admin-credentials`. The banner never echoes a password you chose yourself.

To choose the password yourself, paste this into **Additional Options → Startup scripts** when creating the Droplet:

```yaml
#cloud-config
write_files:
  - path: /etc/trinity/admin-password
    permissions: '0600'
    content: "your-password-here"
```

It must be `#cloud-config` with `write_files`, not a shell script — a shell script runs after first boot has already generated a password and started Trinity. The file is shredded once the password has been read.

### Sign in and harden

Open `https://<droplet-ip>` and sign in as `admin`. The certificate is a real Let's Encrypt certificate for the IP address, so there is no browser warning. To replace the generated password, edit `ADMIN_PASSWORD` in `/opt/trinity/.env` and restart the backend (`docker compose -f docker-compose.hosted.yml restart backend`) — the backend re-applies that value on every start, and there is no change-password form in the UI.

Because the install recorded `do-marketplace` as its provenance, the Dashboard shows a **Secure this instance** card to admins until it is dismissed. It is a two-step upgrade prompt, not a breakage warning:

1. **Give it a real name** — point a domain's A record at the Droplet and set it as the **Public URL** in **Settings → General** (the card's **Add a domain** button takes you there). Trinity then hands out the name instead of the IP; whatever terminates TLS in front of it can pick up the name and use an ordinary long-lived certificate. Setting the domain advances the card to step two.
2. **Serve it without exposing it** — with that domain on Cloudflare, a Cloudflare Tunnel lets the server stop listening on the public internet while inbound integrations (Telegram, WhatsApp, VoIP, public agent links, webhook triggers) keep working. Set `TUNNEL_TOKEN` in `/opt/trinity/.env` and re-run `start.sh --hosted` — see [Public Access](public-access.md). This step is optional; dismissing it ends the guide.

The card describes only what the instance *advertises* (the configured Public URL); nothing inspects a certificate or opens a socket. Dismissal is per browser and per step. Provenance itself is recorded once at first boot and cannot be edited afterwards — the marker in `.env` is never re-read, and the API refuses to write or clear it — so the card never appears on an install that was not provisioned from a marketplace image.

Add a model credential (**Settings → Integrations**), then create your first agent — the seeded starter fleet is already running.

### Managing the Droplet

Over SSH or in the Droplet Console:

```bash
cd /opt/trinity

docker compose -f docker-compose.hosted.yml ps          # status
./scripts/deploy/stop.sh                                # stop (never `down`)
./scripts/deploy/start.sh --hosted                      # start
docker compose -f docker-compose.hosted.yml restart     # restart
docker compose -f docker-compose.hosted.yml logs -f backend
```

**Updating.** Pin the release you want in `.env`, check out the matching tag so the compose files and mounted config move with the images, and re-run the installer:

```bash
cd /opt/trinity
echo 'TRINITY_IMAGE_TAG=v0.9.1' >> .env
sudo git fetch --tags && sudo git checkout v0.9.1
sudo ./scripts/deploy/start.sh --hosted
```

**Backups.** Nightly and pre-migration database backups land in `/opt/trinity/trinity-data/backups/` (the data directory is `./trinity-data` relative to the checkout). They are on the same disk as the database, so they protect against corruption and mistakes, not against losing the Droplet — take Droplet snapshots as well. See [Backup and Restore](backup-and-restore.md).

**Support.** GitHub Issues on `abilityai/trinity`, label `do-marketplace`. DigitalOcean does not build or support Trinity.

## `.env` reference

Every key in `.env.example`, with the compose files that forward it. **A key a compose file does not forward does nothing on that install.** Legend: `dev` = `docker-compose.yml`, `prod` = `docker-compose.prod.yml`, `hosted` = `docker-compose.hosted.yml`. Keys marked "commented" ship commented out in `.env.example` and take effect only when you uncomment them.

### Core, security, admin

| Key | Forwarded by | What it does |
|---|---|---|
| `SECRET_KEY` | dev · prod · hosted | JWT signing key. Generated by `start.sh` if blank. |
| `CREDENTIAL_ENCRYPTION_KEY` | dev · prod · hosted | Encrypts credentials at rest. Generated if blank; never change once set. |
| `CREDENTIAL_ENCRYPTION_KEY_SECONDARY` | dev · prod · hosted | Decrypt-only fallback used only during key rotation (`scripts/deploy/rotate-credential-key.py`). Leave empty normally. |
| `INTERNAL_API_SECRET` | dev · prod · hosted | Scheduler-to-backend and internal-route secret. Generated if blank. |
| `AGENT_AUTH_SECRET` | dev · prod · hosted | Master for per-agent in-container auth tokens. Generated if blank; never rotate. |
| `ADMIN_USERNAME` | dev · prod · hosted | Admin account username (default `admin`). |
| `ADMIN_PASSWORD` | dev · prod · hosted | Admin password; also the MCP server's legacy password auth. Mandatory in prod/hosted. |
| `ANTHROPIC_API_KEY` | dev · prod · hosted | Platform-wide Claude API key for agents (or set it in Settings). |
| `PUBLIC_ACCESS_REQUESTS_ENABLED` | dev · prod · hosted | `true` lets anyone reaching the backend add their own email to the login whitelist (`POST /api/access/request`). Default `false`. |
| `DOCKER_GID` | dev · prod · hosted | Group of the Docker socket inside the backend container (default `999`; `start.sh` detects it). |

### Outbound intake and telemetry

| Key | Forwarded by | What it does |
|---|---|---|
| `OPERATOR_INTAKE_ENABLED` | dev · prod · hosted | `false` disables the once-per-install, opt-in operator contact submission. |
| `OPERATOR_INTAKE_URL` | dev · prod · hosted | Endpoint for that submission (override to self-host). |
| `DO_NOT_TRACK` | dev · prod · hosted | Any value other than `0`/empty/`false` disables the operator intake and telemetry sharing. |
| `TELEMETRY_SHARING_ENABLED` | dev · prod · hosted | Hard kill switch for opt-in usage sharing (`false` → the Settings consent toggle refuses). |
| `TELEMETRY_SHARING_URL` | dev · prod · hosted | Receiver endpoint (override to self-host). |
| `TELEMETRY_SHARING_INTERVAL_HOURS` | dev · prod · hosted | Sharing heartbeat cadence (default 24). |
| `TELEMETRY_SHARING_BACKFILL_DEFAULT_DAYS` | dev · prod · hosted | Default backfill window at consent (default 30). |
| `TEMPLATE_REGISTRY_ENABLED` | dev · prod · hosted | `false` stops fetching the vendor template registry (air-gap). |
| `TEMPLATE_REGISTRY_URL` | dev · prod · hosted | Registry document URL (HTTPS only, no redirects). |
| `VITE_BUG_REPORTING_ENABLED` | prod build only | Baked into the production frontend image at build time; `false` removes the in-app bug/feedback widget. Published hosted images carry the default; the dev Vite server reads `src/frontend/.env` instead. |
| `VITE_BUG_INTAKE_URL` | prod build only | Bug-report endpoint baked into the production frontend (repointing also needs a CSP change in the frontend sources). |

### Email login

| Key | Forwarded by | What it does |
|---|---|---|
| `EMAIL_PROVIDER` | dev · prod · hosted | `console`, `smtp`, `sendgrid`, or `resend` (compose default `resend` when absent). |
| `RESEND_API_KEY` | dev · prod · hosted | Resend API key. |
| `SENDGRID_API_KEY` | dev · prod · hosted | SendGrid API key. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | dev · prod · hosted | SMTP transport. |
| `SMTP_FROM` | dev · prod · hosted | From address for verification emails. |
| `EXTRA_CORS_ORIGINS` | dev · prod · hosted | Extra allowed browser origins, comma-separated. |

### OAuth providers and GitHub

| Key | Forwarded by | What it does |
|---|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | dev · prod · hosted | Google Workspace OAuth. |
| `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` | prod · hosted | Slack OAuth app. |
| `SLACK_SIGNING_SECRET` | dev · prod · hosted | Verifies Slack webhook requests. |
| `SLACK_SOCKET_CONNECTION_COUNT` | dev · prod · hosted | Concurrent Slack Socket Mode connections (1–10, default 2). |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | prod · hosted | GitHub OAuth app. |
| `GITHUB_PAT` | dev · prod · hosted | Platform PAT for cloning private template repos. |
| `TRINITY_GIT_BASE_URL` / `TRINITY_GIT_API_BASE` (commented) | gitea overlay only | Self-hosted git (GitHub Enterprise, Gitea); active only under `-f docker-compose.gitea.yml`. |
| `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` | prod · hosted | Notion OAuth app. |

### Models, image generation, voice, telephony

| Key | Forwarded by | What it does |
|---|---|---|
| `GEMINI_API_KEY` | dev · prod · hosted | Platform image generation; also required for voice and VoIP. |
| `GOOGLE_API_KEY` | dev · prod · hosted | Injected into Gemini-runtime agents; platform fallback for `GEMINI_API_KEY`. |
| `ELEVENLABS_API_KEY` | dev · prod · hosted | Outbound voice replies on channels; empty = off. |
| `ELEVENLABS_MODEL_ID` | dev · prod · hosted | ElevenLabs model (default `eleven_multilingual_v2`). |
| `TTS_MAX_CHARS` | dev · prod · hosted | Replies longer than this are delivered as text instead of synthesized. |
| `GEMINI_TEXT_MODEL` / `GEMINI_TRANSCRIPTION_MODEL` (commented) | dev · prod · hosted | Override the built-in Gemini model ids. Keep commented unless overriding — an empty value shadows the default. |
| `VOICE_ENABLED` | dev · prod · hosted | Voice chat platform-wide (default on). |
| `VOICE_MODEL` (commented) | dev · prod · hosted | Gemini Live model override. Keep commented unless overriding. |
| `WORKSPACE_VOICE_MAX_DURATION` | dev · prod · hosted | Max length of one Workspace voice call, seconds (default 1800). |
| `WORKSPACE_ENABLED` | dev · prod · hosted | Voice Workspace canvas beta flag. |
| `VOIP_ENABLED` | dev · prod · hosted | Outbound phone calls via Twilio (default off; also needs a per-agent binding). |
| `A2A_OUTBOUND_ENABLED` | dev · prod · hosted | Let agents task external A2A agents (default off; also needs registered endpoints). |
| `VOIP_MAX_CALL_DURATION` / `VOIP_DEFAULT_DAILY_CALL_CAP` / `VOIP_CALL_RATE_LIMIT` / `VOIP_CALL_RATE_WINDOW` / `VOIP_TICKET_TTL_SECONDS` / `VOIP_INTENT_TTL_SECONDS` | dev · prod · hosted | Telephony spend and abuse controls. |

### Rate limits and caps

| Key | Forwarded by | What it does |
|---|---|---|
| `REPORT_RATE_LIMIT` | dev · prod · hosted | Structured reports an agent may create per 60 s. |
| `PORTAL_CHAT_BURST_LIMIT` / `PORTAL_CHAT_HOURLY_LIMIT` | dev · prod · hosted | Workspace chat sends per (email, agent). |
| `PORTAL_UPLOAD_BURST_LIMIT` / `PORTAL_UPLOAD_HOURLY_LIMIT` | dev · prod · hosted | Workspace uploads per email. |
| `PORTAL_FILE_BURST_LIMIT` / `PORTAL_FILE_HOURLY_LIMIT` / `PORTAL_FILE_DELETE_BURST_LIMIT` | dev · prod · hosted | Workspace file downloads and deletes per email. |
| `PORTAL_TITLE_MODEL` / `PORTAL_TITLE_TIMEOUT_SECONDS` | dev · prod · hosted | Model and timeout used to name a Workspace thread. |
| `SKILLS_RECONCILE_MAX_REMOVALS` / `SKILLS_FLEET_INJECT_CONCURRENCY` | dev · prod · hosted | Skills-library reconcile safety cap and fleet re-inject parallelism. |
| `TRINITY_DEFAULT_SKILL_SOURCE` / `TRINITY_DEFAULT_SKILL_SOURCE_REF` (commented) | dev · prod · hosted | Bundled community skills source seeded on fresh installs; set the URL to `""` to disable the seed. |
| `WEBHOOK_RATE_LIMIT` / `WEBHOOK_IP_RATE_LIMIT` / `WEBHOOK_MAX_BODY_BYTES` | dev · prod · hosted | Public webhook trigger limits. |
| `REMINDER_MESSAGE_MAX_CHARS` / `REMINDER_MIN_DELAY_SECONDS` / `REMINDER_MAX_DELAY_SECONDS` / `MAX_PENDING_REMINDERS_PER_AGENT` / `MAX_REMINDERS_PER_AGENT_PER_DAY` / `REMINDER_RATE_LIMIT` | dev · prod · hosted | Agent self-reminder caps. |
| `OPERATOR_QUEUE_MAX_PENDING_PER_AGENT` / `OPERATOR_QUEUE_CREATE_RATE_LIMIT` / `OPERATOR_QUEUE_CREATE_RATE_WINDOW` / `OPERATOR_QUEUE_FLEET_CREATE_RATE_LIMIT` / `OPERATOR_QUEUE_MAX_SCAN_PER_CYCLE` / `OPERATOR_QUEUE_MAX_FILE_BYTES` / `OPERATOR_QUEUE_TITLE_MAX` / `OPERATOR_QUEUE_QUESTION_MAX` / `OPERATOR_QUEUE_CONTEXT_MAX_BYTES` / `OPERATOR_QUEUE_OPTIONS_MAX_BYTES` / `OPERATOR_QUEUE_ID_MAX` / `OPERATOR_QUEUE_EXECUTION_ID_MAX` / `OPERATOR_QUEUE_EMAIL_MAX` / `OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS` / `OPERATOR_ALERT_MAX_PENDING_PER_TYPE` | dev · prod · hosted | Operator-queue ingestion caps (bound a runaway agent). |

### Install identity, URLs, ports, data

| Key | Forwarded by | What it does |
|---|---|---|
| `TRINITY_INSTALL_SOURCE` | dev · prod · hosted | Install-provenance marker (`do-marketplace`, `vultr-marketplace`, `script`); read once at first boot and recorded permanently. Leave empty unless you build a marketplace image. |
| `BACKEND_URL` | dev · prod · hosted | Backend base URL used to build OAuth callback URLs (default `http://localhost:8000`). |
| `FRONTEND_PORT` | dev · prod · hosted | Host port for the web UI (default 80). |
| `FRONTEND_URL` | dev · prod · hosted | Public UI URL for email links and OAuth callbacks. |
| `PUBLIC_CHAT_URL` | dev · prod · hosted | External base URL for public chat links and webhooks. |
| `TRINITY_IMAGE_TAG` | hosted | Which published image set to pull (default `latest`). Ignored by source builds. |
| `TUNNEL_TOKEN` | prod · hosted | Cloudflare Tunnel token; the `cloudflared` service is profile-gated. |
| `SSH_HOST` | dev · prod · hosted | Host advertised for agent SSH access (auto-detected from `FRONTEND_URL` when empty). |
| `TRINITY_DATA_PATH` | prod · hosted | Bind-mount directory for `/data` (default `./trinity-data`). Dev uses a named volume. |
| `HOST_TEMPLATES_PATH` | prod · hosted | Host path of the agent-template directory when compose runs outside the repo root. |
| `TRINITY_INSTANCE_NAME` | dev · prod · hosted | Label naming this instance in outbound alerts. |
| `TRINITY_DEFAULT_SYSTEM_MANIFEST` | dev · prod · hosted | Path to your own first-run starter-fleet manifest, or `disabled` to skip seeding. |
| `TRINITY_MANIFESTS_DIR` | dev · prod · hosted | Directory the "Install a system" catalog reads (bind-mount it too). |

### Redis

| Key | Forwarded by | What it does |
|---|---|---|
| `REDIS_PASSWORD` | dev · prod · hosted (also the sibling test stack) | Redis admin (`default`) user password. |
| `REDIS_BACKEND_PASSWORD` | dev · prod · hosted (also the sibling test stack) | Runtime password for the `backend`/`scheduler` users; compose builds `REDIS_URL` from it. |

### Database

| Key | Forwarded by | What it does |
|---|---|---|
| `DATABASE_URL` (commented) | dev · prod · hosted | `postgresql://…` switches the backend and scheduler to PostgreSQL; unset = SQLite. |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` (commented) | dev · prod · hosted | PostgreSQL connection pool (defaults 10 / 20). |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` (commented) | dev only (`--profile postgres`) | Credentials of the bundled dev PostgreSQL container. |

### Logs, backups, retention

| Key | Forwarded by | What it does |
|---|---|---|
| `LOG_RETENTION_DAYS` | dev · prod · hosted | Days of raw Vector logs kept before archival/deletion (default 5). |
| `LOG_ARCHIVE_ENABLED` | dev · prod · hosted | Compress logs to `/data/archives` instead of deleting (default true). |
| `LOG_CLEANUP_HOUR` | dev · prod · hosted | UTC hour of the daily log cleanup (default 3). |
| `DB_BACKUP_ENABLED` | dev · prod · hosted | `false` disables the nightly backup and the boot pre-migration copy. |
| `DB_BACKUP_HOUR` / `DB_BACKUP_MINUTE` | dev · prod · hosted | Nightly backup time, UTC (default 03:30). |
| `DB_BACKUP_PG_DUMP_TIMEOUT_SECONDS` | dev · prod · hosted | Wall-clock budget for `pg_dump` (default 1800). |
| `CONTAINER_LOG_MAX_SIZE` / `CONTAINER_LOG_MAX_FILE` | dev · prod · hosted | Docker log rotation for platform services (default `10m` × 3). |
| `AGENT_LOG_MAX_SIZE` / `AGENT_LOG_MAX_FILE` | dev · prod · hosted | Docker log rotation for agent containers (applied on recreate). |
| `AGENT_TMP_SIZE` | dev · prod · hosted | Size of each agent's RAM-backed `/tmp` (default `512m`; applied on recreate). |
| `AGENT_IDLE_FINALIZE_S` | dev · prod · hosted | Seconds of stdout silence before a headless turn may finalize early (default 300; applied on recreate). |

### MCP server

| Key | Forwarded by | What it does |
|---|---|---|
| `MCP_AGENT_CHAT_PULL_ENABLED` | dev · prod · hosted | Experimental: route agent-to-agent chat through the async task path. |
| `MCP_INLINE_AUTH_ENABLED` | dev · prod · hosted | Keyless email-code sign-in over MCP (default off; expose the MCP port over TLS only when on). |
| `MCP_INLINE_AUTH_TIMEOUT_MS` | dev · prod · hosted | Relay timeout for the inline-auth path. |
| `MCP_CHAT_TIMEOUT_MS` / `MCP_RECOVERY_TIMEOUT_MS` | dev · prod · hosted | Sync-chat ceiling and the post-abort execution lookup budget. |
| `MCP_A2A_TIMEOUT_MS` | dev · prod · hosted | Outbound A2A fetch timeout. |
| `ASK_TRINITY_ENDPOINT` (commented) | dev · prod · hosted | Endpoint behind the `ask_trinity` docs-Q&A tool. |

### Execution, dispatch, reliability

| Key | Forwarded by | What it does |
|---|---|---|
| `PULL_MODE_PILOT_AGENTS` / `MAX_REDELIVERY` | dev · prod · hosted | Pull-mode pilot agents (empty = off) and the poison-task redelivery cap. |
| `DISPATCH_ASYNC` | dev · prod · hosted | Fire-and-forget dispatch of schedule/webhook turns (default false). |
| `BACKEND_AGENT_CALL_LIMIT` / `BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S` | dev · prod · hosted | Concurrent outbound agent calls per backend worker and the queue wait ceiling. |
| `DISPATCH_BREAKER_ENABLED` | dev · prod · hosted | Global gate for the per-agent dispatch circuit breaker (default false). |
| `SUBSCRIPTION_SWEEP_CONCURRENCY` | dev · prod · hosted | Parallel probes in the subscription-headroom sweep. |
| `REDELIVERY_GOVERNOR_ENABLED` / `REDELIVERY_FLEET_LIMIT` / `REDELIVERY_FLEET_WINDOW_SECONDS` / `REDELIVERY_AGENT_LIMIT` / `REDELIVERY_AGENT_WINDOW_SECONDS` / `CORRELATED_FAILURE_THRESHOLD` / `CORRELATED_FAILURE_WINDOW_SECONDS` / `CORRELATED_PAUSE_TTL_SECONDS` / `REDELIVERY_PAUSE_RETRY_AFTER_SECONDS` | dev · prod · hosted | Re-delivery governor (default off) and its caps. |

### Observability

| Key | Forwarded by | What it does |
|---|---|---|
| `OTEL_ENABLED` | dev · prod · hosted | Claude Code metrics export from agents (default 1). |
| `OTEL_COLLECTOR_ENDPOINT` | dev · prod · hosted | Collector endpoint (default the bundled `trinity-otel-collector`). |
| `OTEL_METRICS_EXPORTER` / `OTEL_LOGS_EXPORTER` / `OTEL_EXPORTER_OTLP_PROTOCOL` / `OTEL_METRIC_EXPORT_INTERVAL` | dev · prod · hosted | Exporter settings. |
| `TELEMETRY_CONTAINER_STATS_TTL` / `TELEMETRY_DOCKER_POOL_SIZE` | dev · prod · hosted | Container-stats cache freshness and Docker fetch parallelism. |
| `CANARY_ENABLED` / `CANARY_SLACK_WEBHOOK_URL` | dev · prod · hosted | Continuous invariant watcher (staging/dev) and its Slack webhook. |

## See Also

- [Public Access](public-access.md) — Cloudflare Tunnel for webhooks, public chat and `/mcp`
- [Upgrading](upgrading.md) — How to update Trinity safely, source and hosted
- [Backup and Restore](backup-and-restore.md) — Protecting your database
- [Monitoring](monitoring.md) — Health checks and recovery patterns
- [Ops Agent](ops-agent.md) — Automated day-to-day operations
- [First-Time Setup](../../getting-started/setup.md) — What a fresh install contains
