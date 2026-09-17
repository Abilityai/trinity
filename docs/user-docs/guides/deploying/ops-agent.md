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

If you have the [abilities](https://github.com/abilityai/abilities) `trinity` plugin installed, `/trinity:deploy` does this for you: it walks through deploying Trinity (cloud, remote SSH server, or localhost) or connecting to an existing instance, then clones this repo into a per-instance directory with `.env` already filled in.

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
| `COMPOSE_FILE` | Optional | `docker-compose.prod.yml` | The compose file the instance runs on (`docker-compose.hosted.yml` on a hosted install). It also decides whether `/update` builds images or pulls them |
| `TRINITY_BRANCH` | Optional | `main` | The branch `/update` pulls |
| `FRONTEND_PORT` / `BACKEND_PORT` / `MCP_PORT` / `SCHEDULER_PORT` | Optional | `80` / `8000` / `8080` / `8001` | Service ports on the instance, as the scripts probe them |
| `ADMIN_PASSWORD` | Required | — | Trinity admin password for API calls |
| `MCP_API_KEY` | Optional | — | MCP API key for agent queries |
| `ANTHROPIC_API_KEY` | Optional | — | Passed through for agent containers |
| `TUNNEL_FRONTEND` / `TUNNEL_BACKEND` / `TUNNEL_MCP` | Optional | `13000` / `18000` / `18080` | Local ports `tunnel.sh` forwards to on a remote instance |

**Local mode:** Leave `SSH_HOST` empty. All commands run directly against the local Docker daemon.

**Remote mode:** Set `SSH_HOST`. Commands are forwarded over SSH — no agent container is needed on the remote server.

**Hosted (pull-only) installs:** on an instance installed with `./scripts/deploy/start.sh --hosted` (prebuilt GHCR images, including both DigitalOcean paths), set `COMPOSE_FILE=docker-compose.hosted.yml`. `/update` and `update.sh` then switch to hosted mode: instead of building, they run `start.sh --hosted --unattended` on the server, which pulls the platform images and the agent base image. The release comes from `TRINITY_IMAGE_TAG` in the **server's** `.env`, so pin it there — see [Upgrading → Hosted installs](upgrading.md#hosted-pull-only-installs). Health checks, logs, restarts and backups work the same on either install.

**A DigitalOcean Droplet** created by the [installer script](digitalocean.md) or the 1-Click needs `SSH_USER=root`, `TRINITY_PATH=/opt/trinity`, `COMPOSE_FILE=docker-compose.hosted.yml` and `FRONTEND_PORT=8081` (Caddy owns ports 80 and 443 there). The backend port is not reachable from outside the Droplet, so the scripts call it on the server itself over SSH, or you reach it through `tunnel.sh`.

## Day-to-Day Operations

### Health check

```bash
./scripts/status.sh
```

Lists every `trinity-*` and `agent-*` container with its status, then probes the backend (`/health` on `BACKEND_PORT`), the frontend (HTTP code on `FRONTEND_PORT`), Redis (`redis-cli ping`) and the scheduler (its Docker health status), and prints the checkout's current git commit. It does not probe the MCP server or Vector — use the six-probe list in [Monitoring](monitoring.md) for those.

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

In order, the script:

1. **Checks `CREDENTIAL_ENCRYPTION_KEY`.** It stops before touching anything if the key is empty in the server's `.env`, because the backend refuses to start without it once credentials are stored encrypted.
2. **Backs up the database** with `backup.sh`, and aborts if the backup fails.
3. **Pulls** the latest code from `TRINITY_BRANCH` (default `main`).
4. **Updates the containers.** On a source-built install it rebuilds the platform images (`backend`, `frontend`, `mcp-server`, `scheduler`) and runs `docker compose up -d`. On a hosted install it runs `start.sh --hosted --unattended`.
5. **Sweeps agent restart policies.** Agent containers created by older releases are moved to `unless-stopped`, so they come back after a host reboot. A stopped agent stays stopped.
6. **Verifies.** It checks backend and scheduler health, and whether the running `version` matches the image it runs in, so a stale platform image is called out rather than missed. It also warns if the PostgreSQL migration history has more than one head, which stops schema migrations from applying.

It does **not** rebuild the agent base image on a source-built install. That image changes rarely, and rebuilding it forces every agent to be re-deployed. The script tells you when the pulled range touched it, or on a hosted install that a new one was pulled. Agents adopt a new base image on a cold stop and start, and `/rebuild-agent` rolls it out to agents.

> **Never `docker compose down`.** Agent containers restart automatically (`unless-stopped`), and `down` recreates the agent network, which sends every agent into a restart loop. Stop the stack with `docker compose stop` (upstream's `scripts/deploy/stop.sh`) instead.

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

Opens SSH port-forwarding so you can reach the Trinity UI and API locally while the instance runs on a remote server — the UI at `http://localhost:13000` by default (`TUNNEL_FRONTEND`), the API at `http://localhost:18000` (`TUNNEL_BACKEND`). With `SSH_HOST` empty it prints the direct local address instead.

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

On a **hosted** install there is nothing to rebuild. Set `TRINITY_IMAGE_TAG` in the server's `.env` back to the previous release and re-run `start.sh --hosted`, which also pulls that release's agent base image.

**Rolling back to a release before v0.9.5 needs the pre-update database.** v0.9.5 encrypts the credentials entered in Settings and deletes their plaintext copies, and older releases read only the plaintext. Restore the backup taken before the update (the `/update` backup, or the platform's own `pre-migration-*.db` copy), or re-enter those credentials in Settings after the rollback. `/rollback` takes the backup file as its second argument.

## Skills

Open the repo in Claude Code and use the slash commands it ships with:

| Skill | What it does |
|-------|-------------|
| `/status` | Health check — backend, scheduler, containers, version, and whether the install is source-built or hosted |
| `/logs <service> [lines] [errors]` | View logs for any service or agent |
| `/restart [service\|all]` | Restart services with health verification |
| `/update [--wait\|--force]` | Back up the DB, pull latest, rebuild containers (or run `start.sh --hosted` on a hosted install), restart, verify; `--wait` waits for running executions, `--force` skips that check |
| `/agents [list\|start\|stop\|logs\|exec\|policy\|dump]` | Manage agent containers; `policy` lists each agent's restart policy and restart count, `dump <name>` writes a stuck agent server's thread stacks to its log without restarting it |
| `/rebuild-agent <name\|--all>` | Rebuild agent containers from the latest base image |
| `/diagnose` | Full error scan — logs, restarts, disk, DB integrity |
| `/telemetry` | CPU, memory, disk, container resource stats |
| `/rollback [commit] [backup]` | Roll back to a previous commit, optionally restoring a DB backup |
| `/cleanup [--execute]` | Prune Docker images, build cache, old backups |
| `/migrate-to-postgres` | Migrate the database from SQLite to PostgreSQL — validate in parallel, then cut over; one-line rollback |
| `/provision [provider]` | Step-by-step provisioning for Hetzner, GCP, AWS, DigitalOcean or localhost; for DigitalOcean it offers the one-command installer first |
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
