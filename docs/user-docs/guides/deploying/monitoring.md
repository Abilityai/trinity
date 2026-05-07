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

Check `trinity.db` size:

```bash
# Development (named volume)
docker run --rm -v trinity_trinity-data:/data alpine ls -lh /data/trinity.db

# Production (bind mount)
ls -lh /srv/trinity-data/trinity.db
```

### Step 3: Check Container Status

```bash
# All platform services
docker compose ps

# Agent containers only
docker ps --filter "label=trinity.platform=agent"

# Look for unexpected restart counts
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"
```

A `Restarting` status or `(N)` restart count next to `Up` indicates a crash loop. See [Recovery Patterns](#recovery-patterns) below.

---

## Verify

All six probes return expected output, no warning or critical thresholds exceeded, no containers in crash loops.

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
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/ops/fleet/health | jq .
```

This endpoint is admin-only and returns context usage, health check status, and recent activity for every agent.

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
curl -s http://localhost:8001/health
docker compose restart scheduler
```

### Agent network not found (agents can't be started)

This happens when `docker compose down` was used instead of `docker compose stop`. The `trinity-agent-network` was removed.

```bash
# Recreate missing networks while leaving running containers intact
docker compose up -d
# or for production:
docker compose -f docker-compose.prod.yml up -d
```

### Agent context >90%

Reset context via the web UI: navigate to the agent, open the **Session** or **Chat** tab, and use the reset/close option. The next response will start with a clean context window.

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
- [Backup and Restore](backup-and-restore.md) — Database backup before making changes
- [Ops Agent](ops-agent.md) — Automated health checks and incident response
