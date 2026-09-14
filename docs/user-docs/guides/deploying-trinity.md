# Deploying Trinity

Trinity runs your agents 24/7 with scheduling, monitoring, and multi-agent coordination. Choose cloud-hosted for simplicity or self-hosted for complete control.

> 📺 **Watch:** [I Built a DevOps Agent That Deploys Other Agents](https://youtu.be/8RozanPd14Y) *(Apr 2026)* · [Control My DGX Spark From Anywhere](https://youtu.be/epDBrEtg4nE) *(Jan 2026)* · [all videos](../videos.md)

## Cloud vs Self-Hosted

| | Cloud Hosted (ability.ai) | Self Hosted |
|---|---|---|
| **Infrastructure** | Zero to manage | You manage |
| **Setup time** | 30 seconds | 2 minutes (prebuilt images) to 15 minutes (build from source) |
| **Data location** | ability.ai servers | Your perimeter |
| **Pricing** | Pay-per-agent | Free forever |
| **Best for** | Teams focused on building | Enterprises with compliance requirements |

### Self-hosted install paths

| Path | Command | Best for | Guide |
|---|---|---|---|
| **Local, build from source** | `./scripts/deploy/start.sh` | Your own machine, development, hot reload | [Local Development](deploying/local-development.md) |
| **Server, prebuilt images** | `./scripts/deploy/start.sh --hosted` | Any Linux VM — serving in about two minutes, no on-box builds | [Single Server → Prebuilt images](deploying/single-server.md#option-a-prebuilt-images-recommended) |
| **Server, build from source** | `docker compose -f docker-compose.prod.yml up -d` | Servers that build their own images (custom patches, enterprise overlay) | [Single Server → Build from source](deploying/single-server.md#option-b-build-from-source) |
| **DigitalOcean Marketplace 1-Click** | Create a Droplet from the Trinity image | HTTPS at the Droplet's IP with zero input; upgrade to a domain later | [Single Server → DigitalOcean 1-Click](deploying/single-server.md#digitalocean-marketplace-1-click) |

All four paths share one installer (`scripts/deploy/start.sh`), one `.env` contract, and one set of day-two procedures ([Upgrading](deploying/upgrading.md), [Backup and Restore](deploying/backup-and-restore.md), [Monitoring](deploying/monitoring.md)).

## Option A: Cloud Hosted (ability.ai)

### Step 1: Create an account

Sign up at [ability.ai](https://ability.ai).

### Step 2: Get your MCP connection URL

After signup, go to **Settings > API Keys** and copy your MCP server URL.

### Step 3: Connect from Claude Code

```bash
/trinity:connect
```

The skill asks for your connection URL and saves it to your config.

### Step 4: Deploy your first agent

```bash
/trinity:onboard
```

Done. Your agent is now running on ability.ai.

## Option B: Self Hosted (local, from source)

> **Tip:** the install runs one-shot with `./scripts/deploy/start.sh --unattended` (generates and prints the admin password), or is driven end to end by an AI coding agent via the runbook at [`docs/AGENT_INSTALL_GUIDE.md`](../../AGENT_INSTALL_GUIDE.md). `./quickstart.sh` is an alias for `start.sh` (`--defaults` means `--unattended`).

### Requirements

- Docker Desktop (or Docker Engine + the Docker Compose v2 plugin — `docker compose`, no hyphen)
- Git
- 8 GB RAM
- Modern web browser

### Step 1: Clone the repo

```bash
git clone https://github.com/abilityai/trinity.git
cd trinity
```

### Step 2: Configure `.env`

```bash
cp .env.example .env
```

Set **`ADMIN_PASSWORD`** (12+ characters) — the one required edit. `start.sh` refuses to run while it is blank, or generates one under `--unattended`.

`start.sh` generates everything else that is blank on first run and writes it back to `.env`: `SECRET_KEY`, `INTERNAL_API_SECRET`, `CREDENTIAL_ENCRYPTION_KEY`, `AGENT_AUTH_SECRET`, both Redis passwords, and `DOCKER_GID`. Once generated, **do not change** `CREDENTIAL_ENCRYPTION_KEY` (encrypted credentials become unrecoverable) or `AGENT_AUTH_SECRET` (every running agent's token stops working until it is recreated).

**Port conflicts:** The frontend binds `:80` by default. If another process already holds `:80`, add `FRONTEND_PORT=8090` (or any free port) to `.env`. `start.sh` warns about busy ports before it starts.

### Step 3: Build the base agent image (optional)

```bash
./scripts/deploy/build-base-image.sh
```

This builds `trinity-agent-base:latest` — the Docker image every agent container inherits. It takes 5–10 minutes on first run. **You can skip it:** `start.sh` builds the image when it is missing (or, under `--hosted`, pulls it from GHCR instead).

### Step 4: Start services

```bash
./scripts/deploy/start.sh
```

Starts all platform services (backend, frontend, MCP server, Redis, scheduler, Vector, OTel collector), waits up to 180 seconds for the backend to report healthy, then prints the access URLs and a next-steps card (including the generated admin password under `--unattended`).

Open `http://localhost` (or `http://localhost:$FRONTEND_PORT` if you remapped) and log in with `admin` + the password from `.env`. Because `ADMIN_PASSWORD` was set, the admin account already exists — there is no setup screen. See [First-Time Setup](../getting-started/setup.md) for what a fresh install contains.

### Step 5: Connect from Claude Code

Create an MCP API key first:
1. Log in to the web UI
2. Go to **Settings → MCP Keys**
3. Create a new key and copy it

Then connect:

```bash
/trinity:connect

# When prompted, enter:
# URL: http://localhost:8080/mcp
# API Key: (your MCP API key from Settings → MCP Keys)
```

Alternatively, for email-verified login: when prompted, enter your email and follow the verification code flow.

### Step 6: Deploy your first agent

```bash
/trinity:onboard
```

## Option C: Server with Prebuilt Images

On a server, pull-only is the path you want: every platform image and the agent base image are published to GHCR on each release, so nothing is compiled on the box.

```bash
git clone https://github.com/abilityai/trinity.git && cd trinity
cp .env.example .env                    # set ADMIN_PASSWORD, ANTHROPIC_API_KEY, ...
echo 'TRINITY_IMAGE_TAG=v0.9.0' >> .env  # pin a release; `latest` moves on every release
./scripts/deploy/start.sh --hosted --unattended
```

The repository checkout must stay beside the compose file (it mounts `./config/*`), and upgrades are a re-run of `start.sh --hosted` with a new `TRINITY_IMAGE_TAG` — not a bare `docker compose pull`. Full details, the tunnel and TLS choices, and the "which compose files go together" table: [Single Server → Prebuilt images](deploying/single-server.md#option-a-prebuilt-images-recommended).

## Option D: DigitalOcean Marketplace 1-Click

Create a Droplet from the Trinity image (4 GB RAM minimum, 8 GB recommended). First boot generates the admin password, obtains a Let's Encrypt certificate for the Droplet's own IP, and runs `start.sh --hosted --unattended` from the images baked into the snapshot — Trinity is serving over HTTPS about ninety seconds later with no input from you. The password is printed in the login banner (Droplet → Console, or `ssh root@<droplet-ip>` if you chose SSH-key auth); change it by editing `ADMIN_PASSWORD` in `/opt/trinity/.env` and restarting the backend.

After the first login, a **Secure this instance** card on the Dashboard walks you through adding a real domain and, optionally, a Cloudflare Tunnel. Details: [Single Server → DigitalOcean 1-Click](deploying/single-server.md#digitalocean-marketplace-1-click).

## Key URLs (Self-Hosted)

| Service | Local (dev compose) | Server (prod / hosted compose) |
|---------|-----|-----|
| Web UI | http://localhost | `https://trinity.your-domain.com` |
| Backend API docs | http://localhost:8000/docs | `http://your-server:8000/docs` |
| MCP Server | http://localhost:8080/mcp | `https://trinity.your-domain.com/mcp` — the production frontend proxies `/mcp` to the MCP server, so an install that exposes only 80/443 still serves MCP (port 8080 also works where it is reachable) |

## Managing Services (Self-Hosted)

```bash
# Stop all services (preserves agent containers) — picks the right compose file
./scripts/deploy/stop.sh

# Start all services (add --hosted on a hosted install)
./scripts/deploy/start.sh

# View backend logs (hosted installs: add -f docker-compose.hosted.yml)
docker compose logs -f backend

# Rebuild platform services after code changes (source installs only)
docker compose build --no-cache backend frontend mcp-server scheduler
```

> **Do not use `docker compose down`** to stop a running instance — it destroys the agent network. Agent containers are created with Docker's `unless-stopped` restart policy, so after a `down` every agent sits in a restart loop against a network that no longer exists. Use `./scripts/deploy/stop.sh` (which runs `docker compose stop`) instead. If it already happened: `docker rm -f` the stale agent containers and start them again from the UI — their workspace volumes are untouched.

Hosted installs opt out of compose's default file merge, so every day-two compose command needs `-f docker-compose.hosted.yml`; `stop.sh` reads the running stack's own label and picks the file for you.

## Upgrading

Full procedure with pre-flight, verification and rollback: [Upgrading](deploying/upgrading.md). In short:

**Source install**

```bash
# 1. Back up the database first (the platform also takes a pre-migration copy at boot)
docker run --rm -v trinity_trinity-data:/data -v $(pwd):/backup alpine \
  cp /data/trinity.db /backup/trinity.db.backup-$(date +%Y%m%d)

# 2. Pull latest changes
git pull origin main

# 3. Rebuild platform services (NOT the base image — separate step)
docker compose build --no-cache backend frontend mcp-server scheduler

# 4. Restart platform services
docker compose restart backend frontend mcp-server scheduler

# 5. Verify health
./scripts/deploy/verify-platform.sh
```

**Hosted (pull-only) install**

```bash
echo 'TRINITY_IMAGE_TAG=v0.9.1' >> .env     # the release you want (last line wins)
git fetch --tags && git checkout v0.9.1    # keep compose files + config in step with the images
./scripts/deploy/start.sh --hosted         # re-pulls the platform images AND the agent base image
```

To roll back: restore the DB backup → check out the previous commit or set the previous tag → rebuild (source) or re-run `start.sh --hosted` (hosted) → restart.

## Health Verification

Run after any change to confirm the platform services are healthy:

```bash
./scripts/deploy/verify-platform.sh
```

It checks Docker, the six core containers, the HTTP health endpoints, the base agent image, and that `.env` carries `SECRET_KEY`, `CREDENTIAL_ENCRYPTION_KEY` and `ADMIN_PASSWORD`. Or check manually:

| Probe | Command |
|-------|---------|
| Backend | `curl -sf http://localhost:8000/health` |
| Scheduler | `curl -sf http://localhost:8001/health` |
| Frontend | `curl -sf http://localhost` |
| Redis | `docker exec trinity-redis redis-cli ping` |
| MCP Server | `curl -sf http://localhost:8080/health` |
| Vector | `curl -sf http://localhost:8686/health` |

> The scheduler's port 8001 is not published to the host by any compose file, so probe it from inside its container: `docker exec trinity-scheduler curl -sf http://localhost:8001/health`. `verify-platform.sh` treats a failed scheduler probe as a warning for this reason.

## Resource Thresholds

Monitor these metrics to catch problems before they cascade:

| Metric | Warning | Critical | Action |
|--------|---------|----------|--------|
| Agent context usage | >70% | >90% | Restart the agent |
| Host CPU | >70% | >90% | Scale down active agents |
| Host memory | >80% | >95% | Restart idle agents |
| Disk usage | >70% | >85% | Archive or prune logs |
| Container restarts | >3/hour | >10/hour | Check logs for crash loop |
| DB size | >500 MB | >1 GB | Run log archival |

## Common Recovery Patterns

**Agent stuck at >90% context** → Restart the agent container:
```bash
docker restart <agent-container-name>
```

**"network not found" when starting an agent, or agents looping in `Restarting`** → `docker compose down` removed the agent network. Recreate the network, remove the stale agent containers, and start them again from the UI (their workspace volumes survive):
```bash
docker compose up -d                 # recreates the missing network
docker rm -f <agent-container-name>  # then start the agent from the UI (or POST /api/ops/fleet/restart for the whole fleet)
```

**Database locked** → Multiple writers contending. Check for duplicate backend processes:
```bash
docker ps | grep trinity-backend
```
There should be exactly one.

## Backups

Trinity backs up its own database: nightly at **03:30 UTC** plus a pre-migration copy at boot, under `backups/` in the data directory (`/data/backups/` inside the backend container), verified and retention-pruned. Status without a shell: `GET /api/settings/retention` → `backup`. The artifacts live on the same disk as the database, so ship them off-host for disaster recovery. Everything else — manual on-demand copies, restore, PostgreSQL — is in [Backup and Restore](deploying/backup-and-restore.md).

## Managing a Running Instance (Ops Agent)

Once Trinity is running — locally or on a server — the **[trinity-ops-public](https://github.com/abilityai/trinity-ops-public)** repo gives you a Claude Code ops agent for day-to-day operations.

```bash
git clone https://github.com/abilityai/trinity-ops-public.git
cd trinity-ops-public

cp .env.example .env
# Set SSH_HOST (leave blank if Trinity runs on this machine)

claude  # launch the ops agent
```

| Skill | What it does |
|-------|-------------|
| `/status` | Health check — backend, containers, Redis, version |
| `/logs <service>` | View logs for any service or agent |
| `/restart [service\|all]` | Restart services with health verification |
| `/update` | Pull latest, rebuild, restart, verify (source-built installs; hosted installs upgrade with `start.sh --hosted`) |
| `/diagnose` | Full error scan — logs, restarts, disk, DB integrity |
| `/rollback` | Rollback to previous commit + optional DB restore |
| `/cleanup` | Prune Docker images, build cache, old backups |

**Provisioning guides** (for new server setup): Hetzner, GCP, AWS, DigitalOcean, and localhost — all in `provision/`. The full skill list is in [Ops Agent](deploying/ops-agent.md).

## Detailed Deployment Guides

Step-by-step guides for each deployment scenario:

| Guide | What it covers |
|---|---|
| [Local Development](deploying/local-development.md) | Docker Desktop, dev compose, hot reload, what `start.sh` generates |
| [Single Server](deploying/single-server.md) | Linux VPS: prebuilt images (`--hosted`) or build from source, the DigitalOcean 1-Click, every `.env` key and which compose forwards it, Redis dual-password setup, which compose files go together |
| [Public Access](deploying/public-access.md) | Cloudflare Tunnel, TLS postures, webhook surface, Slack/Telegram/WhatsApp integrations, `/mcp` through the tunnel |
| [Upgrading](deploying/upgrading.md) | Pre-flight → backup → rebuild or re-pull → restart → verify → rollback |
| [Backup and Restore](deploying/backup-and-restore.md) | Automatic nightly backups, manual copies, restore procedure, PostgreSQL |
| [Monitoring](deploying/monitoring.md) | Six-probe health check, resource thresholds, recovery patterns |
| [Ops Agent](deploying/ops-agent.md) | Automated day-to-day operations via trinity-ops-public |

## Next Steps

- [Building Agents](building-agents.md) — Create and deploy with Claude Code + abilities
- [Using Trinity](using-trinity.md) — Dashboard, agent management, monitoring

## See Also

- [Quick Start](../getting-started/quick-start.md) — 5-minute agent creation
- [Trinity CLI](../cli/trinity-cli.md) — Command-line deployment
- [trinity-ops-public](https://github.com/abilityai/trinity-ops-public) — Ops agent for managing instances
