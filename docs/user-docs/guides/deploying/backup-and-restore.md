# Backup and Restore

## Automatic Backups (Built In)

Trinity backs up its own database — you do not need to set anything up.

| What | Value |
|---|---|
| Schedule | Daily at **03:30 UTC** (before the platform's nightly maintenance jobs) |
| Where | `/data/backups/` inside the backend container — the `trinity-data` volume (dev) or `${TRINITY_DATA_PATH}/backups` (prod) |
| SQLite | `trinity-backup-YYYYMMDD.db` — a consistent online copy via SQLite's backup API (never a raw file copy) |
| PostgreSQL | `trinity-backup-YYYYMMDD.dump` — `pg_dump -Fc` (custom format, restorable with `pg_restore`) |
| Pre-migration | `pre-migration-YYYYMMDD-HHMMSS.db` — an extra safety copy taken automatically at boot when a schema migration is about to run (SQLite) |
| Retention | `backup_retention_days` (default **14**, bounds 1–3650) — set via `PUT /api/settings/ops/config`. The newest **3** artifacts are always kept, regardless of age. |
| Default | **Enabled.** Disable with `DB_BACKUP_ENABLED=false` in `.env` |
| Failure visibility | A failed or skipped backup raises an item in Operations → Needs Response, and a "backups are stale" alarm re-fires weekly while the newest success is older than 3 days |

### Checking backup status (no shell needed)

As an admin:

```bash
curl -s -H "Authorization: Bearer <token>" \
  http://localhost:8000/api/settings/retention | jq .backup
```

Returns the last run's status, the last success time and age, artifact count and
total bytes, retention settings — and `scope: "same-disk"` (see below).

### Scope — what the automatic backups do and do not protect

Artifacts live on the **same disk** as the database. That protects against the
realistic common failures — a stray write corrupting the database file, a
fat-fingered delete, a bad migration — but **not against loss of the disk or
host**. For disaster recovery, ship artifacts off-host on your own schedule
(rsync/cron of `/data/backups/`, or your infrastructure's disk snapshots).

### Tuning

```bash
# .env (all forwarded by both compose files)
DB_BACKUP_ENABLED=true      # false disables backups entirely
DB_BACKUP_HOUR=3            # UTC
DB_BACKUP_MINUTE=30
DB_BACKUP_PG_DUMP_TIMEOUT_SECONDS=1800
```

Retention is deliberately **not** an env var — it is an ops setting
(`backup_retention_days`) so it is validated (1–3650; `0` is rejected —
disabling backups is `DB_BACKUP_ENABLED=false`, never "keep forever").

---

## What to Back Up

| Component | Back up? | Where it lives | Notes |
|---|---|---|---|
| Database | **Automatic** | `/data/backups/` (see above) | Agents, schedules, chat history, credentials metadata, audit log |
| `.env` file | **Yes — manually** | Host filesystem | **Not in git.** Losing it means losing `CREDENTIAL_ENCRYPTION_KEY`, which makes all encrypted credentials unrecoverable. A database backup alone does not cover this. |
| Backup artifacts (off-host) | Recommended | Copy of `/data/backups/` | Same-disk artifacts do not survive disk loss — ship them off-host for DR. |
| Agent code | Not separately | Git repositories | Each agent's code lives in a git repo — already versioned there. |
| Agent runtime data | Optional | Agent workspace volumes | Use the per-agent data export (`POST /api/agents/{name}/data/export`). |
| Redis data | Not separately | Named volume `trinity_redis-data` | Ephemeral: JWT tokens, capacity counters. All regenerated on next start. |
| Platform config | Not separately | Git repo | `docker-compose.yml`, `config/`, `scripts/` — all in version control. |

### Back up `.env` (manual, do this once per change)

```bash
cp .env ~/backups/trinity-env-$(date +%Y%m%d).bak
chmod 600 ~/backups/trinity-env-$(date +%Y%m%d).bak
```

Store it in a secure location (password manager, encrypted storage). Never
commit it to git.

---

## Manual On-Demand Backup

Usually unnecessary — but before something unusually risky you may want a fresh
copy without waiting for 03:30 UTC.

**SQLite** — use the safe online-backup primitive, **never `cp` a live
database** (a raw copy of a file mid-write, ignoring its journal, can produce a
torn or stale backup):

```bash
docker run --rm \
  -v trinity_trinity-data:/data \
  -v ~/backups:/backup \
  alpine sh -c "apk add -q sqlite && \
    sqlite3 /data/trinity.db \".backup '/backup/trinity-$(date +%Y%m%d-%H%M%S).db'\""
```

> **Production note:** on a server using `docker-compose.prod.yml` the database
> lives at `${TRINITY_DATA_PATH}/trinity.db` (a bind-mount directory). Mount
> that path instead of the named volume:
> ```bash
> docker run --rm -v /srv/trinity-data:/data -v ~/backups:/backup \
>   alpine sh -c "apk add -q sqlite && \
>     sqlite3 /data/trinity.db \".backup '/backup/trinity-$(date +%Y%m%d-%H%M%S).db'\""
> ```

The volume name prefix `trinity_` comes from the Docker Compose project name
(the directory name). If you cloned Trinity into a differently-named directory,
the prefix differs — check with `docker volume ls | grep trinity`.

**PostgreSQL** (`DATABASE_URL` set):

```bash
# Bundled dev container
docker exec trinity-postgres pg_dump -U trinity -Fc trinity \
  > ~/backups/trinity-pg-$(date +%Y%m%d-%H%M%S).dump
# Managed/external PostgreSQL: use your provider's snapshot tooling,
# or pg_dump -Fc against the host with your connection string
```

### Verify a backup

```bash
sqlite3 ~/backups/trinity-YYYYMMDD-HHMMSS.db "PRAGMA quick_check;"   # expect: ok
```

(The automatic job verifies every artifact — `PRAGMA quick_check` for SQLite,
the `PGDMP` archive magic for PostgreSQL — before it is kept.)

---

## Restore Procedure

### SQLite

**1. Stop both database writers** — backend AND scheduler:

```bash
# Development
docker compose stop backend scheduler

# Production
docker compose -f docker-compose.prod.yml stop backend scheduler
```

**2. Remove stale journal sidecars beside the target, then copy the artifact
in.** A leftover `-wal`/`-shm`/`-journal` file beside a restored `.db` is a
corruption hazard — SQLite would try to replay a journal that belongs to the
*old* database. Harmless if absent:

```bash
docker run --rm \
  -v trinity_trinity-data:/data \
  -v ~/backups:/backup \
  alpine sh -c "rm -f /data/trinity.db-wal /data/trinity.db-shm /data/trinity.db-journal && \
    cp /backup/trinity-backup-YYYYMMDD.db /data/trinity.db"
```

(To restore from an *automatic* artifact without copying it off-host first, the
source path is `/data/backups/trinity-backup-YYYYMMDD.db` inside the same
volume: `cp /data/backups/trinity-backup-YYYYMMDD.db /data/trinity.db` after
the same `rm -f` step.)

**3. Restart services:**

```bash
# Development
docker compose start backend scheduler

# Production
docker compose -f docker-compose.prod.yml start backend scheduler
```

**4. Verify health:**

```bash
curl -s http://localhost:8000/health
```

### PostgreSQL

Restore a `-Fc` dump with `pg_restore` into an **empty** database, services
stopped:

```bash
docker compose stop backend scheduler
# Recreate an empty DB, then restore into it:
docker exec trinity-postgres psql -U trinity -c "DROP DATABASE IF EXISTS trinity_restore;"
docker exec trinity-postgres psql -U trinity -c "CREATE DATABASE trinity_restore;"
docker exec -i trinity-postgres pg_restore -U trinity -d trinity_restore \
  < ~/backups/trinity-backup-YYYYMMDD.dump
# Point DATABASE_URL at the restored DB (or swap names), then:
docker compose start backend scheduler
```

For managed PostgreSQL, use `pg_restore -d <conninfo>` against your provider.

> **Version note:** the bundled `pg_dump` is v17 and dumps servers up to its
> own major. If your managed PostgreSQL is ever newer than the client shipped
> in the backend image, backups fail loudly (operator alarm) rather than
> silently — upgrade the image, or dump with your provider's tooling.

---

## Automation

**Trinity does this automatically** — the daily job above replaced the crontab
recipe this page used to suggest. There is nothing to install.

- **Disable**: `DB_BACKUP_ENABLED=false` in `.env` (then restart the backend).
- **Ship artifacts off-host** (recommended for DR — same-disk scope above):
  ```cron
  # e.g. nightly rsync of the artifact directory, AFTER the 03:30 UTC run
  0 5 * * * rsync -a /srv/trinity-data/backups/ backup-host:/srv/trinity-backups/
  ```
- **Watch it**: check Operations → Needs Response for backup alarms, or poll
  the status block (`GET /api/settings/retention` → `backup`).

---

## What Is and Is Not in the Database

**In the database (and therefore in an artifact):**
- All agent metadata (names, ownership, settings)
- All schedules and execution history
- All chat sessions and message history
- Credentials metadata (not plaintext values — those live in agent `.env` files and `.credentials.enc` files)
- Encrypted channel bot tokens (Slack, Telegram, WhatsApp) — decryptable only with `CREDENTIAL_ENCRYPTION_KEY` from your `.env`
- Audit log
- User accounts and sharing configuration

**Not in the database:**
- Your `.env` (incl. `CREDENTIAL_ENCRYPTION_KEY`) — back it up manually, see above
- Agent source code (in git)
- Agent workspace volumes (per-agent data export, #1169)
- Runtime secrets held by Redis (ephemeral — regenerate on restart)
- Container logs (in Vector's log files under the `trinity-logs` volume)
- Platform images (rebuild from source)

---

## See Also

- [Upgrading](upgrading.md) — Upgrade procedure that includes a pre-upgrade backup step
- [Monitoring](monitoring.md) — Health checks and recovery patterns
