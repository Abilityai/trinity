# Deploying Trinity

Trinity runs your agents 24/7 with scheduling, monitoring, and multi-agent coordination. Choose cloud-hosted for simplicity or self-hosted for complete control.

> 📺 **Watch:** [I Built a DevOps Agent That Deploys Other Agents](https://youtu.be/8RozanPd14Y) *(Apr 2026)* · [Control My DGX Spark From Anywhere](https://youtu.be/epDBrEtg4nE) *(Jan 2026)* · [all videos](../videos.md)

## Cloud vs Self-Hosted

| | Cloud Hosted (ability.ai) | Self Hosted |
|---|---|---|
| **Infrastructure** | Zero to manage | You manage |
| **Setup time** | 30 seconds | 10-15 minutes |
| **Data location** | ability.ai servers | Your perimeter |
| **Pricing** | Pay-per-agent | Free forever |
| **Best for** | Teams focused on building | Enterprises with compliance requirements |

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

## Option B: Self Hosted

### Requirements

- Docker Desktop (or Docker + Docker Compose)
- Git
- 8GB RAM minimum
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

Four variables are security-critical and must be set before first boot:

| Variable | How to set |
|---|---|
| `SECRET_KEY` | `openssl rand -hex 32` |
| `INTERNAL_API_SECRET` | `openssl rand -hex 32` |
| `CREDENTIAL_ENCRYPTION_KEY` | Auto-generated on first start if blank. Once set, **do not change** — encrypted credentials become unrecoverable. |
| `ADMIN_PASSWORD` | Choose a strong password (minimum 12 characters). This is the password you use to log in as `admin`. |

**Port conflicts:** The frontend binds `:80` by default. If another process already holds `:80`, add `FRONTEND_PORT=8090` (or any free port) to `.env`.

### Step 3: Build the base agent image

```bash
./scripts/deploy/build-base-image.sh
```

This builds `trinity-agent-base:latest` — the Docker image every agent container inherits. **This step is required before you can create any agents.** It takes 5-10 minutes on first run.

> `start.sh` will detect a missing base image and build it automatically. You can skip this step if you prefer.

### Step 4: Start services

```bash
./scripts/deploy/start.sh
```

Starts all platform services (backend, frontend, MCP server, Redis, scheduler, Vector). If `CREDENTIAL_ENCRYPTION_KEY` was blank, the script generates it and writes it back to `.env`.

Open `http://localhost` (or `http://localhost:$FRONTEND_PORT` if you remapped) and log in with `admin` + the password you set in `.env`.

### Step 5: Connect from Claude Code

Create an MCP API key first:
1. Log in to the web UI
2. Go to **Settings → Platform API Keys**
3. Create a new key and copy it

Then connect:

```bash
/trinity:connect

# When prompted, enter:
# URL: http://localhost:8080/mcp
# API Key: (your MCP API key from Settings → Platform API Keys)
```

Alternatively, for email-verified login: when prompted, enter your email and follow the verification code flow.

### Step 6: Deploy your first agent

```bash
/trinity:onboard
```

## Key URLs (Self-Hosted)

| Service | URL |
|---------|-----|
| Web UI | http://localhost |
| Backend API docs | http://localhost:8000/docs |
| MCP Server | http://localhost:8080/mcp |

## Managing Services (Self-Hosted)

```bash
# Stop all services (preserves agent containers)
docker compose stop

# Start all services
./scripts/deploy/start.sh

# View backend logs
docker compose logs -f backend

# Rebuild platform services after code changes
docker compose build --no-cache backend frontend mcp-server
```

> **Do not use `docker compose down`** to stop a running instance — it destroys agent containers and the agent network. Use `docker compose stop` instead.

## Upgrading

```bash
# 1. Back up the database first
docker run --rm -v trinity_trinity-data:/data -v $(pwd):/backup alpine \
  cp /data/trinity.db /backup/trinity.db.backup-$(date +%Y%m%d)

# 2. Pull latest changes
git pull origin main

# 3. Rebuild platform services (NOT the base image — separate step)
docker compose build --no-cache backend frontend mcp-server

# 4. Restart platform services
docker compose restart backend frontend mcp-server scheduler

# 5. Verify health
./scripts/deploy/verify-platform.sh
```

To roll back: restore the DB backup → `git checkout <previous-sha>` → rebuild → restart.

## Health Verification

Run after any change to confirm all six services are healthy:

```bash
./scripts/deploy/verify-platform.sh
```

Or check manually:

| Probe | Command |
|-------|---------|
| Backend | `curl -sf http://localhost:8000/health` |
| Scheduler | `curl -sf http://localhost:8001/health` |
| Frontend | `curl -sf http://localhost` |
| Redis | `docker exec trinity-redis redis-cli ping` |
| MCP Server | `curl -sf http://localhost:8080/health` |
| Vector | `curl -sf http://localhost:8686/health` |

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

**"network not found" error when starting an agent** → Backend lost track of the agent network:
```bash
docker rm <agent-container-name>
docker restart trinity-backend
```

**Database locked** → Multiple writers contending. Check for duplicate backend processes:
```bash
docker ps | grep trinity-backend
```
There should be exactly one.

## Backup Strategy

Daily backup (run via cron or manually before upgrades):
```bash
docker run --rm -v trinity_trinity-data:/data -v /your/backup/path:/backup alpine \
  sh -c "cp /data/trinity.db /backup/trinity-$(date +%Y%m%d-%H%M%S).db"
```

Retain 14 daily backups. The DB contains agent state, schedules, chat history, and credentials metadata.

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
| `/update` | Pull latest, rebuild, restart, verify |
| `/diagnose` | Full error scan — logs, restarts, disk, DB integrity |
| `/rollback` | Rollback to previous commit + optional DB restore |
| `/cleanup` | Prune Docker images, build cache, old backups |

**Provisioning guides** (for new server setup): Hetzner, GCP, AWS, DigitalOcean, and localhost — all in `provision/`.

## Detailed Deployment Guides

Step-by-step guides for each deployment scenario:

| Guide | What it covers |
|---|---|
| [Local Development](deploying/local-development.md) | Docker Desktop, dev compose, hot reload, auto-generated secrets |
| [Single Server](deploying/single-server.md) | Linux VPS, prod compose, all required env vars, Redis dual-password setup |
| [Public Access](deploying/public-access.md) | Cloudflare Tunnel, webhook surface, Slack/Telegram/WhatsApp integrations |
| [Upgrading](deploying/upgrading.md) | Pre-flight → backup → rebuild → restart → verify → rollback |
| [Backup and Restore](deploying/backup-and-restore.md) | SQLite backup pattern, daily cron, restore procedure |
| [Monitoring](deploying/monitoring.md) | Six-probe health check, resource thresholds, recovery patterns |
| [Ops Agent](deploying/ops-agent.md) | Automated day-to-day operations via trinity-ops-public |

## Next Steps

- [Building Agents](building-agents.md) — Create and deploy with Claude Code + abilities
- [Using Trinity](using-trinity.md) — Dashboard, agent management, monitoring

## See Also

- [Quick Start](../getting-started/quick-start.md) — 5-minute agent creation
- [Trinity CLI](../cli/trinity-cli.md) — Command-line deployment
- [trinity-ops-public](https://github.com/abilityai/trinity-ops-public) — Ops agent for managing instances
