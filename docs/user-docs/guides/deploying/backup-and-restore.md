# Backup and Restore

Trinity backs up its own database; this page covers what that gives you, the manual copy you take before something risky, and how to restore.

## When to Run

- **Nothing, day to day** — the automatic job below runs nightly and before every schema migration.
- **Before an upgrade or anything unusually risky** — take a manual on-demand copy you can name (see [Procedure](#procedure)).
- **After a data loss** — restore from the newest good artifact (see [Recovery](#recovery-restore)).
- **Once per `.env` change** — copy `.env` somewhere safe; no database backup covers it.

## Pre-flight

- [ ] Know where your data directory is: the `trinity_trinity-data` volume (dev compose) or `TRINITY_DATA_PATH` (default `./trinity-data` in the checkout; `/opt/trinity/trinity-data` on a DigitalOcean Droplet) for prod and hosted installs. Inside the backend container it is always `/data`.
- [ ] Know your backend: SQLite (`trinity.db`) unless `DATABASE_URL` points at PostgreSQL.
- [ ] For a restore: enough free disk for the artifact plus the live database, and a maintenance window — both writers (backend and scheduler) are stopped.

## Automatic Backups (Built In)

| What | Value |
|---|---|
| Schedule | Daily at **03:30 UTC** (before the platform's nightly maintenance jobs) |
| Where | `/data/backups/` inside the backend container — the `trinity-data` volume (dev) or `${TRINITY_DATA_PATH}/backups` (prod and hosted) |
| SQLite | `trinity-backup-YYYYMMDD.db` — a consistent online copy via SQLite's backup API (never a raw file copy) |
| PostgreSQL | `trinity-backup-YYYYMMDD.dump` — `pg_dump -Fc` (custom format, restorable with `pg_restore`) |
| Pre-migration | `pre-migration-YYYYMMDD-HHMMSS.db` — an extra safety copy taken automatically at boot when a schema migration is about to run (SQLite) |
| Retention | `backup_retention_days` (default **14**, bounds 1–3650) — set via `PUT /api/settings/ops/config` (the Retention tab in Settings does not list it). The newest **3** artifacts are always kept, regardless of age. Like every retention window, it is stored per install and does not change on upgrade. |
| Default | **Enabled.** Disable with `DB_BACKUP_ENABLED=false` in `.env` — this turns off both the nightly job and the boot-time pre-migration copy |
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
(rsync/cron of the `backups/` directory, or your infrastructure's disk snapshots —
on a DigitalOcean Droplet, take Droplet snapshots as well).

### Tuning

```bash
# .env (forwarded by the dev, prod and hosted compose files)
DB_BACKUP_ENABLED=true      # false disables backups entirely (nightly + boot pre-migration)
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
| `.env` file | **Yes — manually** | Host filesystem | **Not in git.** Losing it means losing `CREDENTIAL_ENCRYPTION_KEY` (all encrypted credentials become unrecoverable) and `AGENT_AUTH_SECRET` (every agent's in-container token). A database backup alone does not cover this. |
| Backup artifacts (off-host) | Recommended | Copy of `/data/backups/` | Same-disk artifacts do not survive disk loss — ship them off-host for DR. |
| Agent code | Not separately | Git repositories | Each agent's code lives in a git repo — already versioned there. |
| Agent runtime data | Optional | Agent workspace volumes | Use the per-agent data export (`POST /api/agents/{name}/data/export`). |
| Redis data | Not separately | Named volume `trinity_redis-data` | Ephemeral: JWT tokens, capacity counters. All regenerated on next start. |
| Platform config | Not separately | Git repo | `docker-compose*.yml`, `config/`, `scripts/` — all in version control. |

### Back up `.env` (manual, do this once per change)

```bash
cp .env ~/backups/trinity-env-$(date +%Y%m%d).bak
chmod 600 ~/backups/trinity-env-$(date +%Y%m%d).bak
```

Store it in a secure location (password manager, encrypted storage). Never
commit it to git.

---

## Procedure

### Manual On-Demand Backup

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

> **Production / hosted note:** on a server the database
> lives at `${TRINITY_DATA_PATH}/trinity.db` (a bind-mount directory). Mount
> that path instead of the named volume:
> ```bash
> docker run --rm -v /srv/trinity-data:/data -v ~/backups:/backup \
>   alpine sh -c "apk add -q sqlite && \
>     sqlite3 /data/trinity.db \".backup '/backup/trinity-$(date +%Y%m%d-%H%M%S).db'\""
> ```

The volume name prefix `trinity_` comes from the Docker Compose project name
(the directory name, lowercased). If you cloned Trinity into a differently-named directory,
the prefix differs — check with `docker volume ls | grep trinity`.

**PostgreSQL** (`DATABASE_URL` set):

```bash
# Bundled dev container
docker exec trinity-postgres pg_dump -U trinity -Fc trinity \
  > ~/backups/trinity-pg-$(date +%Y%m%d-%H%M%S).dump
# Managed/external PostgreSQL: use your provider's snapshot tooling,
# or pg_dump -Fc against the host with your connection string
```

### Pulling an artifact to your workstation (GCP-hosted instances)

Two scripts in the repo serve one specific topology — a Trinity instance on a **GCP VM** managed from a workstation with `gcloud` and a `deploy.config` (copy `deploy.config.example` and fill in `GCP_PROJECT`, `GCP_ZONE`, `GCP_INSTANCE`, `REMOTE_DIR`). They are not general-purpose backup tools:

- `./scripts/deploy/backup-database.sh [backup_dir]` — **does not create a backup.** It finds the newest automatic artifact (`trinity-backup-*.db` or `.dump`) under `~/trinity-data/backups` on the VM, downloads it with `gcloud compute scp` into `./backups/` (or the directory you pass), and verifies it locally (`PRAGMA quick_check` for SQLite if `sqlite3` is installed, the `PGDMP` magic bytes for PostgreSQL). If no artifact exists it points you at the backup status block.
- `./scripts/deploy/restore-database.sh <backup_file>` — SQLite only. Asks for confirmation, uploads the file to the VM, stops `backend` and `scheduler` with `docker compose -f docker-compose.prod.yml`, removes stale `-wal`/`-shm`/`-journal` sidecars, copies the file over `~/trinity-data/trinity.db`, starts both services again, and waits ten seconds.

For any other host, use the commands in this page directly, or the [Ops Agent](ops-agent.md)'s `backup.sh` (online backup to `~/backups/` on the instance, SQLite or bundled PostgreSQL).

---

## Verify

```bash
sqlite3 ~/backups/trinity-YYYYMMDD-HHMMSS.db "PRAGMA quick_check;"   # expect: ok
```

(The automatic job verifies every artifact — `PRAGMA quick_check` for SQLite,
the `PGDMP` archive magic for PostgreSQL — before it is kept.)

---

## Recovery (Restore)

### SQLite

**1. Stop both database writers** — backend AND scheduler:

```bash
# Development
docker compose stop backend scheduler

# Production
docker compose -f docker-compose.prod.yml stop backend scheduler

# Hosted
docker compose -f docker-compose.hosted.yml stop backend scheduler
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
the same `rm -f` step. On a bind-mount install, run the `rm -f` and `cp` directly
against `${TRINITY_DATA_PATH}/`.)

**3. Restart services:**

```bash
# Development
docker compose start backend scheduler

# Production
docker compose -f docker-compose.prod.yml start backend scheduler

# Hosted
docker compose -f docker-compose.hosted.yml start backend scheduler
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
- Encrypted channel bot tokens (Slack, Telegram, WhatsApp) and the encrypted platform credentials (API keys, GitHub PAT, Slack app secrets) — decryptable only with `CREDENTIAL_ENCRYPTION_KEY` from your `.env`
- Audit log
- User accounts and sharing configuration
- Retention windows and the install-provenance record

**Not in the database:**
- Your `.env` (incl. `CREDENTIAL_ENCRYPTION_KEY` and `AGENT_AUTH_SECRET`) — back it up manually, see above
- Agent source code (in git)
- Agent workspace volumes (use the per-agent data export)
- Runtime secrets held by Redis (ephemeral — regenerate on restart)
- Container logs (in Vector's log files under the `trinity-logs` volume)
- Platform images (rebuild from source, or re-pull on a hosted install)

---

## See Also

- [Upgrading](upgrading.md) — Upgrade procedure that includes a pre-upgrade backup step
- [Monitoring](monitoring.md) — Health checks and recovery patterns
- [Ops Agent](ops-agent.md) — `backup.sh`, `/rollback` with optional DB restore, `/migrate-to-postgres`
