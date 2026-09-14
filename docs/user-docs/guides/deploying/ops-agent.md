# Trinity Ops Agent

The Trinity Ops Agent is a Claude Code agent for operating a Trinity instance — health checks, log tailing, restarts, updates, rollbacks, diagnostics, and agent management — all from a single `.env` pointed at any server.

> 📺 **Watch:** [I Built a DevOps Agent That Deploys Other Agents](https://youtu.be/8RozanPd14Y) *(Apr 2026)* · [all videos](../../videos.md)

## When to Use

Use the ops agent instead of raw Docker commands for day-to-day Trinity operations. It codifies production runbook knowledge into repeatable skills that improve over time as new versions are published.

Use raw Docker only for one-off debugging or when the ops agent itself is unavailable.

## Getting It

Clone the public ops repo and configure your `.env`:

```bash
git clone https://github.com/abilityai/trinity-ops-public
cd trinity-ops-public
cp .env.example .env
# Edit .env with your instance's connection details (see Configuration below)
```

Then open the directory in Claude Code. The repo ships with its own `.claude/skills/` so all the ops commands shown below work out of the box.

## Configuration

The ops agent connects to a Trinity instance via a `.env` file in its workspace.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SSH_HOST` | Remote only | — | Server IP or hostname. Leave empty for localhost. |
| `SSH_USER` | Remote only | `ubuntu` | SSH username |
| `SSH_KEY` | Remote only | `~/.ssh/id_rsa` | Path to private key |
| `SSH_PASSWORD` | Optional | — | Password fallback if no key |
| `SSH_PORT` | Optional | `22` | SSH port |
| `TRINITY_PATH` | Optional | `~/trinity` (`.env.example` ships `/home/ubuntu/trinity`) | Trinity install directory on the server |
| `COMPOSE_FILE` | Optional | `docker-compose.prod.yml` | The compose file the instance runs on |
| `ADMIN_PASSWORD` | Required | — | Trinity admin password for API calls |
| `MCP_API_KEY` | Optional | — | MCP API key for agent queries |
| `ANTHROPIC_API_KEY` | Optional | — | Passed through for agent containers |

**Local mode:** Leave `SSH_HOST` empty. All commands run directly against the local Docker daemon.

**Remote mode:** Set `SSH_HOST`. Commands are forwarded over SSH — no agent container is needed on the remote server.

**Hosted (pull-only) installs:** the ops agent's `/update` and `update.sh` rebuild the platform images from source. On an instance installed with `./scripts/deploy/start.sh --hosted` (prebuilt GHCR images, including the DigitalOcean 1-Click), upgrade with `start.sh --hosted` instead — see [Upgrading → Hosted installs](upgrading.md#hosted-pull-only-installs). Health checks, logs, restarts and backups work the same on either install.

## Day-to-Day Operations

### Health check

```bash
./scripts/status.sh
```

Checks all six Trinity services: backend (`8000`), frontend (`80`), MCP server (`8080`), scheduler (`8001`), Redis, and Vector (`8686`). Reports container status, HTTP endpoint responses, and the current git version.

### View and diagnose logs

```bash
# Backend
./scripts/run.sh "sudo docker logs trinity-backend --tail 100"

# Scheduler
./scripts/run.sh "sudo docker logs trinity-scheduler --tail 50"

# Specific agent
./scripts/run.sh "sudo docker logs agent-myagent --tail 50"

# Errors only
./scripts/run.sh "sudo docker logs trinity-backend 2>&1 | grep -i 'error\|exception\|traceback'"
```

### Restart services

```bash
./scripts/restart.sh
```

Restarts all platform services using `docker compose restart` — not `down/up`, which would orphan running agent containers by removing the `trinity-agent-network`.

### Update Trinity

```bash
./scripts/update.sh
```

Pulls the latest Trinity code, rebuilds platform images (`backend`, `frontend`, `mcp-server`, `scheduler`), restarts, and verifies health — including a check that the running `version` matches the image it runs in, so a stale platform image is called out rather than missed. Does **not** rebuild the agent base image — that image changes rarely and rebuilding it forces every agent to be re-deployed; the script tells you when the pulled range touched it, and `/rebuild-agent` rolls a rebuilt image out to agents.

### Backup database

```bash
./scripts/backup.sh
```

Takes an on-demand copy into `~/backups/` on the host: for SQLite an online backup (`sqlite3 .backup`, verified with `PRAGMA quick_check` — never a raw `cp` of a live database), for the bundled PostgreSQL container a `pg_dump -Fc` archive. An external PostgreSQL is not handled — use your provider's tooling. Run before any update or destructive change. Trinity also backs itself up nightly at 03:30 UTC into `backups/` under its data directory; this script is for the extra copy you want right now. See [Backup and Restore](backup-and-restore.md).

### Agent management

```bash
# List running agents
./scripts/run.sh "sudo docker ps -a --format '{{.Names}}' | grep agent-"

# Start / stop an agent
./scripts/run.sh "sudo docker start agent-myagent"
./scripts/run.sh "sudo docker stop agent-myagent"

# Open a shell inside an agent
./scripts/run.sh "sudo docker exec -it agent-myagent bash"
```

### SSH tunnel (remote instances)

```bash
./scripts/tunnel.sh
```

Opens SSH port-forwarding so you can access the Trinity UI (`http://localhost`) and API locally while the instance runs on a remote server.

## Rollback

If an update breaks the instance:

```bash
# Revert to the previous commit
./scripts/run.sh "cd ~/trinity && git checkout HEAD~1"

# Rebuild platform services from the reverted code
./scripts/run.sh "cd ~/trinity && docker compose -f docker-compose.prod.yml build --no-cache backend frontend mcp-server scheduler"

# Restart
./scripts/restart.sh
```

## Skills

Open the repo in Claude Code and use the slash commands it ships with:

| Skill | What it does |
|-------|-------------|
| `/status` | Health check — backend, scheduler, containers, version |
| `/logs <service> [lines] [errors]` | View logs for any service or agent |
| `/restart [service\|all]` | Restart services with health verification |
| `/update` | Pull latest, rebuild containers, restart, verify (source-built installs) |
| `/agents [list\|start\|stop\|logs\|exec]` | Manage agent containers |
| `/rebuild-agent <name\|--all>` | Rebuild agent containers from the latest base image |
| `/diagnose` | Full error scan — logs, restarts, disk, DB integrity |
| `/telemetry` | CPU, memory, disk, container resource stats |
| `/rollback [commit] [backup]` | Roll back to a previous commit, optionally restoring a DB backup |
| `/cleanup [--execute]` | Prune Docker images, build cache, old backups |
| `/migrate-to-postgres` | Migrate the database from SQLite to PostgreSQL — validate in parallel, then cut over; one-line rollback |
| `/provision [provider]` | Step-by-step provisioning for Hetzner, GCP, AWS, DigitalOcean or localhost |
| `/sync-ops-knowledge` | Review recent Trinity changes and update the agent's own instructions |

## Minimum Server Requirements

| Resource | Minimum (ops agent) | Recommended (ops agent) |
|----------|---------|---------|
| CPU | 1 vCPU | 2 vCPU |
| RAM | 2 GB | 4 GB |
| Disk | 20 GB | 50 GB |
| OS | Ubuntu 22.04+ | Ubuntu 24.04 |

These are the ops repo's figures for the smallest instance it can manage. Trinity's own floor for a working fleet is **8 GB RAM** (agent containers and platform services contend below that) — see [Single-Server Deployment](single-server.md#prerequisites). Supported providers: Hetzner Cloud, GCP, AWS, DigitalOcean, any Linux VM, localhost.

## See Also

**Trinity docs:**
- [Upgrading Trinity](upgrading.md)
- [Backup and Restore](backup-and-restore.md)
- [Monitoring](monitoring.md)
- [Abilities Marketplace](../../automation/abilities-marketplace.md) — `trinity` plugin (`connect`, `onboard`, `sync`) for managing agents on the deployed instance

**External references:**
- [abilityai/trinity-ops-public](https://github.com/abilityai/trinity-ops-public) — Source, changelog, contributing guide
