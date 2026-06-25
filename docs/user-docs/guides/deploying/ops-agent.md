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
| `TRINITY_PATH` | Optional | `~/trinity` | Trinity install directory on the server |
| `ADMIN_PASSWORD` | Optional | — | Trinity admin password for API calls |
| `MCP_API_KEY` | Optional | — | MCP API key for agent queries |

**Local mode:** Leave `SSH_HOST` empty. All commands run directly against the local Docker daemon.

**Remote mode:** Set `SSH_HOST`. Commands are forwarded over SSH — no agent container is needed on the remote server.

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

Pulls the latest Trinity code, rebuilds platform images (`backend`, `frontend`, `mcp-server`, `scheduler`), restarts, and verifies health. Does **not** rebuild the agent base image — that image changes rarely and rebuilding it forces every agent to be re-deployed.

### Backup database

```bash
./scripts/backup.sh
```

Copies the SQLite database to `/tmp/trinity-<timestamp>.db` on the server. Run before any update or destructive change.

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
./scripts/run.sh "cd ~/trinity && docker compose build --no-cache backend frontend mcp-server"

# Restart
./scripts/restart.sh
```

## Minimum Server Requirements

| Resource | Minimum |
|----------|---------|
| CPU | 1 vCPU |
| RAM | 2 GB |
| Disk | 20 GB |
| OS | Ubuntu 22.04+ |

Supported providers: Hetzner Cloud, GCP, AWS, DigitalOcean, any Linux VM, localhost.

## See Also

**Trinity docs:**
- [Upgrading Trinity](upgrading.md)
- [Backup and Restore](backup-and-restore.md)
- [Monitoring](monitoring.md)
- [Abilities Marketplace](../../automation/abilities-marketplace.md) — `trinity` plugin (`connect`, `onboard`, `sync`) for managing agents on the deployed instance

**External references:**
- [abilityai/trinity-ops-public](https://github.com/abilityai/trinity-ops-public) — Source, changelog, contributing guide
