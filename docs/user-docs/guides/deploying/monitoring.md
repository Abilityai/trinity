# Monitoring

## When to Check

- After every upgrade or restart
- When an agent stops responding
- When the platform feels slow or unresponsive
- As a daily practice on production instances

---

## Pre-flight

- [ ] Docker is running: `docker info >/dev/null 2>&1`
- [ ] You know the last change made (upgrade, config edit, new agent)
- [ ] You know which compose file the stack runs on: none (dev), `-f docker-compose.prod.yml`, or `-f docker-compose.hosted.yml` — every `docker compose` command below needs that flag on a server install

---

## Procedure

### Step 1: Run the Six-Probe Health Check

Run all six probes. All must pass:

| Probe | Command | Expected |
|---|---|---|
| Backend | `curl -s http://localhost:8000/health` | `{"status":"healthy",...}` |
| Scheduler | `curl -s http://localhost:8001/health` | `{"status":"healthy","active_schedules":N}` |
| Frontend | `curl -s -o /dev/null -w '%{http_code}' http://localhost` | `200` |
| Redis | `docker exec trinity-redis redis-cli ping` | `PONG` |
| MCP Server | `curl -s http://localhost:8080/health` | HTTP 200 |
| Vector | `docker exec trinity-vector wget -q -O - http://localhost:8686/health` | Non-empty response |

Run as a block:

```bash
# 1. Backend
curl -s http://localhost:8000/health
# Expected: {"status":"healthy",...}

# 2. Scheduler
curl -s http://localhost:8001/health
# Expected: {"status":"healthy","active_schedules":N}

# 3. Frontend (HTTP 200)
curl -s -o /dev/null -w '%{http_code}' http://localhost
# Expected: 200

# 4. Redis
docker exec trinity-redis redis-cli ping
# Expected: PONG

# 5. MCP Server
curl -s http://localhost:8080/health
# Expected: 200 OK

# 6. Vector (log aggregation)
docker exec trinity-vector wget -q -O - http://localhost:8686/health
# Expected: non-empty response
```

> The scheduler's port 8001 is not published to the host by any compose file, so run probe 2 from inside its container: `docker exec trinity-scheduler curl -sf http://localhost:8001/health`. `./scripts/deploy/verify-platform.sh` runs the same checks (plus the base-image and `.env` checks) and treats a failed scheduler probe as a warning for this reason. A `503` from the backend with a `migrations` block is a pending or failed schema migration, not a dead backend.

### Step 2: Check Resource Thresholds

| Metric | Warning | Critical | Action |
|---|---|---|---|
| Backend `/health` | — | not 200 | Restart `trinity-backend` |
| Scheduler `/health` | — | not 200 | Restart `trinity-scheduler` |
| Agent context usage | >75% | >90% | Reset agent context or restart agent container |
| Host CPU | >80% | >95% | Investigate runaway processes |
| Host memory | >85% | >95% | Check container memory limits |
| Disk free | <20% | <5% | Prune Docker, archive logs |
| Error rate (per hour) | >10 | >50 | Inspect `platform.json` log |
| Container restarts | any | repeated | `docker logs <container>` |
| `trinity.db` size | >1 GB | >5 GB | Archive old data |
| Vector log size | >5 GB | >10 GB | Trigger archival rotation |

Check disk and Docker space:

```bash
df -h /
docker system df
```

#### Container log rotation

Docker's default JSON log driver has **no size limit**, and an unbounded container log will eventually fill the Docker data root and wedge the whole fleet at once. Trinity therefore caps every container it controls — platform services and agent containers alike — at 10 MB per file, 3 files, by default.

| Variable | Applies to | Default |
|---|---|---|
| `CONTAINER_LOG_MAX_SIZE` / `CONTAINER_LOG_MAX_FILE` | Platform services (compose) | `10m` / `3` |
| `AGENT_LOG_MAX_SIZE` / `AGENT_LOG_MAX_FILE` | Agent containers | `10m` / `3` |

Both are validated in both directions: a malformed value **and** an absurd-but-well-formed one (over 1 GB, or more than 10 files) fall back to the bounded default and log a warning. There is no way to configure an unbounded log.

These apply at container creation, so platform services adopt a change on the next `docker compose up`, and existing agents on **recreate** — not on a plain restart.

This only shortens the history available to `docker logs`. Vector's aggregate at `/data/logs` is the primary queryable record and keeps its own `LOG_RETENTION_DAYS`; live log streaming in the UI is unaffected by rotation.

Check `trinity.db` size:

```bash
# Development (named volume)
docker run --rm -v trinity_trinity-data:/data alpine ls -lh /data/trinity.db

# Production / hosted (bind mount)
ls -lh /srv/trinity-data/trinity.db
```

#### Backups and retention

The database backs itself up nightly at 03:30 UTC and before every migration. Check it without a shell — the block reports the last run's status, the age of the newest success, artifact count and bytes:

```bash
curl -s -H "Authorization: Bearer <token>" http://localhost:8000/api/settings/retention | jq .backup
```

A failed or skipped backup raises an item in **Operations → Needs Response**; a "backups are stale" alarm repeats weekly while the newest success is older than three days. The same endpoint reports every retention window in force — each is an explicit per-install setting (written at the values already in effect the first time a release boots without them), so an upgrade never changes what your instance keeps. Windows, the blast-radius guard and admin approval of large prunes: [Operations → Monitoring](../../operations/monitoring.md). Backup details: [Backup and Restore](backup-and-restore.md).

### Step 3: Check Container Status

```bash
# All platform services (add -f docker-compose.prod.yml / -f docker-compose.hosted.yml on a server)
docker compose ps

# Agent containers only
docker ps --filter "label=trinity.platform=agent"

# Look for unexpected restart counts
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"
```

A `Restarting` status or `(N)` restart count next to `Up` indicates a crash loop. See [Recovery Patterns](#recovery-patterns) below.

Agent containers are created with Docker's `unless-stopped` restart policy, so Docker now counts their restarts: the **"restarting frequently"** health issue and its operator alert fire fleet-wide for the first time, and an agent in Docker's `restarting` state shows as **stopped** in the roster while dockerd retries it. An agent you stopped on purpose is not restarted — `unless-stopped` honours a manual stop across a host reboot.

---

## Verify

All six probes return expected output, no warning or critical thresholds exceeded, no containers in crash loops, backup status `ok` with a recent success.

---

## Viewing Logs

### Structured logs (via Vector)

```bash
# Platform logs (backend, scheduler, MCP server)
docker exec trinity-vector sh -c "tail -50 /data/logs/platform.json" | jq .

# Agent logs
docker exec trinity-vector sh -c "tail -50 /data/logs/agents.json" | jq .

# Filter for errors
docker exec trinity-vector sh -c "cat /data/logs/platform.json" | jq 'select(.level == "ERROR")'
```

On Docker Desktop, `start.sh` switches Vector to an on-disk log source and the local files land at `/data/logs/local-*.json` instead.

### Container logs (Docker directly)

```bash
docker compose logs -f backend
docker compose logs -f frontend
docker compose logs -f scheduler
docker logs trinity-backend --tail 100
```

---

## Fleet Health API

The fleet health endpoint returns per-agent health data:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=your-admin-password' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token') or '')")

curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/ops/fleet/health | jq .
```

This endpoint is admin-only and returns context usage, health check status, and recent activity for every agent.

`GET /api/version` reports the running build (`version`, `git_commit_short`, `build_date`), the edition and its registered features, and `install_source` — how the instance was provisioned.

---

## Recovery Patterns

### Backend not responding

```bash
docker compose restart backend
# Wait ~15 seconds
curl -s http://localhost:8000/health
```

### Scheduler not running schedules

```bash
docker exec trinity-scheduler curl -sf http://localhost:8001/health
docker compose restart scheduler
```

### Agent network not found / agents stuck in `Restarting`

This happens when `docker compose down` was used instead of `docker compose stop` (or `./scripts/deploy/stop.sh`). The `trinity-agent-network` was removed; a recreated network gets a new id, so every existing agent container fails to start against it — and because agents carry the `unless-stopped` policy, dockerd keeps retrying each one in a backoff loop while the roster shows them as stopped.

```bash
# 1. Recreate the missing network while leaving running containers intact
docker compose up -d
# or for production / hosted:
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.hosted.yml up -d

# 2. Remove each stale agent container — its workspace volume is untouched
docker rm -f <agent-container-name>

# 3. Start the agents again from the UI (each agent's Start toggle), or restart the whole fleet with POST /api/ops/fleet/restart
```

### Agent context >90%

Start a fresh conversation: in the Workspace, press **New chat** for that agent — the new thread begins with a clean context window (the agent's **Chat** tab is stateless and starts fresh on every message anyway).

Or restart the agent container directly:

```bash
docker restart <agent-container-name>
```

### Database locked (`SQLITE_BUSY` in backend logs)

Check for duplicate backend processes:

```bash
docker ps | grep trinity-backend
# Expected: exactly one line
```

If there is only one backend container, restart it:

```bash
docker compose restart backend
```

### MCP clients disconnected after restart

JWT tokens are invalidated when the backend restarts. Users need to log in again. Claude Code MCP clients need to reconnect — run `/mcp` in your Claude Code session or restart the client.

### Redis authentication errors

If Redis passwords changed after the `redis-data` volume was already populated, the backend cannot connect. See `docs/migrations/REDIS_AUTH.md` for the step-by-step upgrade path.

### `start.sh` refuses to start: two databases

`start.sh` checks that the file set you are starting can see the database this checkout already holds — the dev volume versus the prod/hosted bind-mount directory — and refuses a silent switch in either direction, printing the copy command. If both stores exist it warns which one the stack will use. Bring the stack up with the file set it was installed with; see [Single Server → Which compose files go together](single-server.md#which-compose-files-go-together).

### Disk full — Docker cleanup

```bash
# Remove unused images, containers, networks (safe to run)
docker system prune -f

# Remove dangling images only
docker image prune -f

# Check size recovered
docker system df
```

---

## Automation

For automated monitoring, alerting, and runbook execution, see the [Ops Agent guide](ops-agent.md) and the [trinity-ops-public](https://github.com/abilityai/trinity-ops-public) repository.

---

## See Also

- [Upgrading](upgrading.md) — Upgrade procedure with verification steps
- [Backup and Restore](backup-and-restore.md) — Automatic backups, manual copies, restore
- [Ops Agent](ops-agent.md) — Automated health checks and incident response
- [Operations → Monitoring](../../operations/monitoring.md) — Health checks, cleanup and retention as seen from the UI and API
