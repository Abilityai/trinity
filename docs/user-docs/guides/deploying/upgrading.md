# Upgrading Trinity

Apply a new Trinity release to a running instance. The procedure keeps agent containers running throughout — only the platform services are rebuilt (source installs) or re-pulled (hosted installs) and restarted.

## When to Run This

- **Source install** (`docker-compose.yml` or `docker-compose.prod.yml`): after every `git pull` that includes changes to platform code (backend, frontend, MCP server, scheduler). Skip it for documentation-only changes.
- **Hosted install** (`start.sh --hosted`, including the DigitalOcean 1-Click): whenever you move `TRINITY_IMAGE_TAG` to a new release. If you left the tag at `latest`, **every** re-run of `start.sh --hosted` is an upgrade — pin a version on any install you intend to keep.

---

## Pre-flight Checklist

Before touching anything:

- [ ] **Back up the database** (Step 1 below — do this first, always). The platform also takes its own `pre-migration-<timestamp>.db` copy at boot whenever a schema migration is about to run, but an explicit copy you can name is still the one to roll back to.
- [ ] Confirm at least 2 GB disk free: `df -h /`
- [ ] Note the current version: `git rev-parse HEAD` (source) or the `TRINITY_IMAGE_TAG` line in `.env` (hosted)
- [ ] Check that no critical agent tasks are running: `docker ps --filter "label=trinity.platform=agent"`
- [ ] Confirm Docker is running: `docker info >/dev/null 2>&1`
- [ ] Hosted: confirm the release is published — the `v0.9.1` / `0.9.1` / `0.9` tags appear on GHCR for every release tag (a pre-release tag such as `v0.9.5-rc1` publishes its own tags but never moves `latest`)

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

> On a production or hosted server the database lives in a bind-mount directory (`TRINITY_DATA_PATH`, default `./trinity-data` in the checkout — e.g. `/srv/trinity-data/`, or `/opt/trinity/trinity-data/` on a DigitalOcean Droplet), not in the named volume. Adjust accordingly:
> ```bash
> cp /srv/trinity-data/trinity.db ~/backups/trinity-$(date +%Y%m%d-%H%M%S).db
> ```

A plain `cp` of a live database can be torn by a write in flight. For a guaranteed-consistent copy while the platform is running, use the online-backup form in [Backup and Restore → Manual On-Demand Backup](backup-and-restore.md#manual-on-demand-backup), or simply keep the newest automatic artifact from `backups/` in the data directory.

Verify the backup is readable:

```bash
sqlite3 ~/backups/trinity-<timestamp>.db ".tables"
# Expected: a list of table names, no errors
```

> **PostgreSQL deployments** (instances running with `DATABASE_URL` set): back up with `pg_dump` instead of copying `trinity.db` — see [Backup and Restore](backup-and-restore.md). PostgreSQL schema migrations run automatically on backend boot, same as SQLite.

### Step 2: Pull Latest Changes

**Source install:**

```bash
git pull origin main
```

Review what changed:

```bash
git log --oneline -10
git diff HEAD~1 HEAD --stat
```

If `docker/base-image/Dockerfile` appears in the diff, see [Base Image Upgrade](#base-image-upgrade-if-needed) below.

**Hosted install:** pin the new release in `.env` (the last `TRINITY_IMAGE_TAG` line wins) and check out the matching tag, so the compose file and the mounted `config/` move with the images:

```bash
echo 'TRINITY_IMAGE_TAG=v0.9.1' >> .env
git fetch --tags && git checkout v0.9.1
```

`start.sh` reads the pin from `.env`; a value exported in the shell overrides it for that run only.

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

#### Hosted (pull-only) installs

There is nothing to build. One command re-pulls the four platform images **and** the agent base image, retags the base image locally, and brings the stack up:

```bash
./scripts/deploy/start.sh --hosted
```

Do not substitute a bare `docker compose -f docker-compose.hosted.yml pull`: the agent base image is not a compose service, so that pull skips it and leaves every agent on the old runtime. `start.sh --hosted` also performs Step 4 for you — `docker compose up -d` recreates only the containers whose image changed and leaves the agent network in place — so continue at [Step 5](#step-5-verify).

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

> Probe 2 as written needs the scheduler's port 8001 on the host, which no compose file publishes — run it from inside the container instead: `docker exec trinity-scheduler curl -sf http://localhost:8001/health`. If your web UI is remapped, probe 3 uses `http://localhost:$FRONTEND_PORT`. A `503` from probe 1 with a `migrations` block names a pending or failed migration — see [Single Server → Database backend](single-server.md#database-backend).

**Confirm the new version is live.** After the probes pass, check that the backend is actually running the build you just deployed:

```bash
curl -s http://localhost:8000/api/version
# Expected: {"version":"<version>","git_commit_short":"<sha>","git_branch":"...","build_date":"...","install_source":"..."}
```

The `git_commit_short`, `git_branch`, `git_commit_subject`, and `build_date` fields report **the code that is actually running**, not merely what was baked in at build time — so a container running mounted source, or one that drifted from its image, is reported honestly rather than claiming the image's commit. If they read `"unknown"` on a source install, the image was built without the deploy script's build args — rebuild with `scripts/deploy/start.sh` to populate them. On a hosted install they describe the commit the release workflow built, which is what you want.

The response also carries `edition` (`oss` or `enterprise`), `enterprise_features`, and `install_source` (how the instance was provisioned — `unknown` on an ordinary install), so you can confirm after an upgrade that the entitlements you expect are actually registered.

The same metadata is visible in the UI via the version chip in the navigation bar (click it for the **Build Info** dialog) and in **Settings → General → Build Info**.

**Note:** JWT tokens are invalidated when the backend restarts. Users with active web UI sessions will need to log in again. MCP clients (Claude Code) will need to reconnect — run `/mcp` in your Claude Code session or restart the client.

**Retention windows do not change on upgrade.** Every install stores its retention windows (execution logs, health checks, soft-deleted agents, backups, …) as explicit per-install settings, written at the values already in force the first time a release boots without them. A new release therefore never widens or narrows what your instance keeps; change a window deliberately under **Settings → Retention** (see [Monitoring](../../operations/monitoring.md)).

---

## Base Image Upgrade (if needed)

Rebuild the base image only when `docker/base-image/Dockerfile` changes:

```bash
./scripts/deploy/build-base-image.sh
```

On a hosted install there is no separate step — `start.sh --hosted` pulls the release's base image and retags it as `trinity-agent-base:latest` on every run.

**How agents pick it up.** A rebuilt or re-pulled base image is adopted at an agent's next **cold start** — that is, the next time it goes from stopped to running. Trinity compares the running container's image against the current base image and recreates the container when they differ.

What this means in practice:

- A **running** agent is never replaced out from under you. Drift is detected but not acted on until the agent is next started cold.
- **Restart the fleet** (Operations) to roll the new image out: each agent goes through the normal lifecycle, so a drifted agent is recreated and an up-to-date one keeps its container.
- The **system agent** (`trinity-system`) follows the same rule and is deliberately never replaced while running. If its image goes stale you get an operator alert rather than a mid-operation container swap; it adopts the rebuild the next time it is started cold.
- Rebuilding the base image is what makes newly shipped agent-side features available. Skipping it means agents keep running the old runtime, which is usually harmless but occasionally leaves a feature silently inert.

---

## Agent restart policy (one time, after upgrading)

Agent containers are now created with Docker's `unless-stopped` restart policy, so a host reboot brings them back. Agents created **before** that release still carry Docker's default (`no`) and stay `Exited` after a reboot until they are recreated. The create and recreate paths are fixed, so this population can only shrink — but an active fleet heals itself only as agents happen to be recreated. Run this once after upgrading rather than waiting:

```bash
# Census — which agents are still on `no`
for c in $(docker ps -a --format '{{.Names}}' | grep '^agent-'); do
  docker inspect -f '{{.Name}} {{.HostConfig.RestartPolicy.Name}}' "$c"
done

# One-shot fix — changes the policy without starting a stopped agent
docker ps -a --format '{{.Names}}' | grep '^agent-' | while read -r c; do
  [ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c")" = "no" ] &&
    docker update --restart unless-stopped "$c"
done
```

Use `unless-stopped`, never `always`: Trinity stops an agent through Docker's manual stop, which `unless-stopped` honours, so a quarantined or deliberately stopped agent stays down across a reboot. On the dev compose the `backend`, `frontend` and `redis` services adopt the same policy on the next `docker compose up` (which `start.sh` performs). Two consequences worth knowing: "restarting frequently" alerts for agents now fire (they never could before), and a `docker compose down` leaves every agent in a restart loop rather than merely orphaned — see [Monitoring → Recovery Patterns](monitoring.md#recovery-patterns). Full runbook: [`docs/migrations/AGENT_RESTART_POLICY_2026-09.md`](../../../migrations/AGENT_RESTART_POLICY_2026-09.md).

---

## Rollback

If something goes wrong after the upgrade:

### 1. Stop platform services

```bash
# Development
docker compose stop backend frontend mcp-server scheduler

# Production
docker compose -f docker-compose.prod.yml stop backend frontend mcp-server scheduler

# Hosted
docker compose -f docker-compose.hosted.yml stop backend frontend mcp-server scheduler
```

### 2. Restore the database backup

Remove any journal sidecars beside the target first — a leftover `-wal`/`-shm`/`-journal` file belongs to the old database and must not be replayed against the restored one:

```bash
# Development (named volume)
docker run --rm \
  -v trinity_trinity-data:/data \
  -v ~/backups:/backup \
  alpine sh -c "rm -f /data/trinity.db-wal /data/trinity.db-shm /data/trinity.db-journal && \
    cp /backup/trinity-<timestamp>.db /data/trinity.db"

# Production / hosted (bind mount — adjust path)
rm -f /srv/trinity-data/trinity.db-wal /srv/trinity-data/trinity.db-shm /srv/trinity-data/trinity.db-journal
cp ~/backups/trinity-<timestamp>.db /srv/trinity-data/trinity.db
```

### 3. Check out the previous version

```bash
# Source install
git checkout <previous-sha>
# or
git checkout <previous-tag>

# Hosted install — the previous tag, in .env (last line wins) and in the checkout
echo 'TRINITY_IMAGE_TAG=v0.9.0' >> .env
git checkout v0.9.0
```

### 4. Rebuild and restart

```bash
# Source install
docker compose build --no-cache backend frontend mcp-server scheduler
docker compose restart backend frontend mcp-server scheduler

# Hosted install — re-pulls the previous images and starts the stack
./scripts/deploy/start.sh --hosted
```

### 5. Run the six-probe verification to confirm rollback succeeded.

---

## Automation

The [Ops Agent](ops-agent.md)'s `/update` skill runs the source-install procedure end to end (pull, rebuild the four platform images, restart, verify, and flag a stale base image). It builds from source, so on a hosted install use `start.sh --hosted` as above instead.

---

## See Also

- [Backup and Restore](backup-and-restore.md) — Detailed backup procedures
- [Monitoring](monitoring.md) — Six-probe health check and recovery patterns
- [Single-Server Deployment](single-server.md) — Initial setup reference, source and hosted
