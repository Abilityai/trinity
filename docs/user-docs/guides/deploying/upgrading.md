# Upgrading Trinity

Apply code changes safely to a running Trinity instance. The procedure below keeps agent containers running throughout the upgrade — only the platform services are restarted.

## When to Run This

Run this procedure before or after every `git pull` that includes changes to platform code (backend, frontend, MCP server, scheduler). Skip it for documentation-only changes.

---

## Pre-flight Checklist

Before touching anything:

- [ ] **Back up the database** (Step 1 below — do this first, always)
- [ ] Confirm at least 2 GB disk free: `df -h /`
- [ ] Note the current Git SHA: `git rev-parse HEAD`
- [ ] Check that no critical agent tasks are running: `docker ps --filter "label=trinity.platform=agent"`
- [ ] Confirm Docker is running: `docker info >/dev/null 2>&1`

---

## Procedure

### Step 1: Back Up the Database

The database is the critical state. Back it up before every upgrade.

```bash
# Development (named volume)
docker run --rm \
  -v trinity_trinity-data:/data \
  -v ~/backups:/backup \
  alpine cp /data/trinity.db /backup/trinity-$(date +%Y%m%d-%H%M%S).db
```

> On a production server using `docker-compose.prod.yml`, the database lives in a bind-mount directory (e.g., `/srv/trinity-data/`), not in the named volume. Adjust accordingly:
> ```bash
> cp /srv/trinity-data/trinity.db ~/backups/trinity-$(date +%Y%m%d-%H%M%S).db
> ```

Verify the backup is readable:

```bash
sqlite3 ~/backups/trinity-<timestamp>.db ".tables"
# Expected: a list of table names, no errors
```

> **PostgreSQL deployments** (instances running with `DATABASE_URL` set): back up with `pg_dump` instead of copying `trinity.db` — see [Backup and Restore](backup-and-restore.md). PostgreSQL schema migrations run automatically on backend boot, same as SQLite.

### Step 2: Pull Latest Changes

```bash
git pull origin main
```

Review what changed:

```bash
git log --oneline -10
git diff HEAD~1 HEAD --stat
```

If `docker/base-image/Dockerfile` appears in the diff, see [Step 5: Base Image Upgrade](#step-5-base-image-upgrade-if-needed) below.

### Step 3: Rebuild Platform Services

When updating Trinity code, rebuild the platform images only:

```bash
docker compose build --no-cache backend frontend mcp-server scheduler
```

The `trinity-agent-base` image is **not** rebuilt by this command. It changes much less often, and rebuilding it forces every agent to be re-deployed. Rebuild it only when `docker/base-image/Dockerfile` itself changes, via `./scripts/deploy/build-base-image.sh`.

For production:

```bash
docker compose -f docker-compose.prod.yml build --no-cache backend frontend mcp-server scheduler
```

### Step 4: Restart Platform Services

> **Use `docker compose restart`, not `down/up`.** `docker compose down` removes the `trinity-agent-network`, which orphans every running agent container — they keep running but lose their network and have to be removed and recreated. `restart` preserves both the agents and the network. The only times to use `down` are: (1) intentional full teardown, (2) recovering from a corrupted compose state.

```bash
# Development
docker compose restart backend frontend mcp-server scheduler

# Production
docker compose -f docker-compose.prod.yml restart backend frontend mcp-server scheduler
```

Services restart in parallel. The backend typically takes 10–20 seconds to become healthy.

### Step 5: Verify

Run the six-probe verification list:

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

All six probes must pass before you declare the upgrade complete.

**Confirm the new version is live.** After the probes pass, check that the backend is actually running the build you just deployed:

```bash
curl -s http://localhost:8000/api/version
# Expected: {"version":"0.6.0","git_commit_short":"<sha>","git_branch":"...","build_date":"..."}
```

The `git_commit_short`, `git_branch`, `git_commit_subject`, and `build_date` fields come from build-time provenance baked into the image. If they read `"unknown"`, the image was built without the deploy script's build args — rebuild with `scripts/deploy/start.sh` to populate them. The same metadata is visible in the UI via the version chip in the navigation bar (click it for the **Build Info** dialog) and in **Settings**.

**Note:** JWT tokens are invalidated when the backend restarts. Users with active web UI sessions will need to log in again. MCP clients (Claude Code) will need to reconnect — run `/mcp` in your Claude Code session or restart the client.

---

## Step 5: Base Image Upgrade (if needed)

Rebuild the base image only when `docker/base-image/Dockerfile` changes:

```bash
./scripts/deploy/build-base-image.sh
```

After the base image is rebuilt, existing agent containers continue using the old image until they are individually stopped and recreated. There is no automatic roll-forward — agents pick up the new base image the next time they are (re)created.

---

## Rollback

If something goes wrong after the upgrade:

### 1. Stop platform services

```bash
# Development
docker compose stop backend frontend mcp-server scheduler

# Production
docker compose -f docker-compose.prod.yml stop backend frontend mcp-server scheduler
```

### 2. Restore the database backup

```bash
# Development (named volume)
docker run --rm \
  -v trinity_trinity-data:/data \
  -v ~/backups:/backup \
  alpine cp /backup/trinity-<timestamp>.db /data/trinity.db

# Production (bind mount — adjust path)
cp ~/backups/trinity-<timestamp>.db /srv/trinity-data/trinity.db
```

### 3. Check out the previous version

```bash
git checkout <previous-sha>
# or
git checkout <previous-tag>
```

### 4. Rebuild and restart

```bash
docker compose build --no-cache backend frontend mcp-server scheduler
docker compose restart backend frontend mcp-server scheduler
```

### 5. Run the six-probe verification to confirm rollback succeeded.

---

## See Also

- [Backup and Restore](backup-and-restore.md) — Detailed backup procedures
- [Monitoring](monitoring.md) — Six-probe health check and recovery patterns
- [Single-Server Deployment](single-server.md) — Initial setup reference
