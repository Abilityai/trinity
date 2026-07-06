# Backup and Restore

## When to Run This

- **Before every upgrade** — always take a backup before pulling new code
- **Daily on production instances** — automate with cron (see [Automation](#automation) below)
- **Before any destructive operation** — deleting agents, bulk changes, schema migrations

---

## What to Back Up

| Component | Back up? | Where it lives | Notes |
|---|---|---|---|
| `trinity.db` (SQLite) | **Yes — primary target** | Named volume `trinity_trinity-data` (dev) or bind-mount at `TRINITY_DATA_PATH` (prod) | Agents, schedules, chat history, credentials metadata, audit log |
| PostgreSQL database | **Yes — if `DATABASE_URL` is set** | Bundled `trinity-postgres` container (dev) or your managed PostgreSQL (prod) | Same contents as `trinity.db`; back up with `pg_dump` (see below) instead of a file copy |
| `.env` file | **Yes** | Host filesystem | **Not in git.** Losing it means losing `CREDENTIAL_ENCRYPTION_KEY`, which makes all encrypted credentials unrecoverable. |
| Agent code | Not separately | Git repositories | Each agent's code lives in a git repo — already versioned there. |
| Redis data | Not separately | Named volume `trinity_redis-data` | Ephemeral: JWT tokens, capacity counters. All regenerated on next start. |
| Platform config | Not separately | Git repo | `docker-compose.yml`, `config/`, `scripts/` — all in version control. |

---

## Pre-flight

- [ ] Confirm backup destination has enough space: `df -h ~/backups`
- [ ] Confirm Docker is running: `docker info >/dev/null 2>&1`

---

## Procedure

### Step 1: Back Up the Database

Run this on the host with services running — it does not require stopping anything:

```bash
# Backup (run on the host, with services running)
docker run --rm \
  -v trinity_trinity-data:/data \
  -v ~/backups:/backup \
  alpine cp /data/trinity.db /backup/trinity-$(date +%Y%m%d-%H%M%S).db
```

> **Production note:** On a server using `docker-compose.prod.yml`, the database lives at `${TRINITY_DATA_PATH}/trinity.db` (a bind-mount directory), not in the named volume. Adjust accordingly:
> ```bash
> cp /srv/trinity-data/trinity.db ~/backups/trinity-$(date +%Y%m%d-%H%M%S).db
> ```

The volume name prefix `trinity_` comes from the Docker Compose project name (the directory name). If you cloned Trinity into a differently-named directory, the prefix will differ — check with `docker volume ls | grep trinity`.

> **PostgreSQL deployments:** if your instance runs with `DATABASE_URL` set, the state lives in PostgreSQL, not `trinity.db`. Back it up with `pg_dump` instead:
> ```bash
> # Bundled dev container
> docker exec trinity-postgres pg_dump -U trinity trinity > ~/backups/trinity-pg-$(date +%Y%m%d-%H%M%S).sql
> # Managed/external PostgreSQL: use your provider's snapshot tooling or pg_dump against the host
> ```
> Restore with `psql -U trinity trinity < backup.sql` into an empty database (services stopped first).

### Step 2: Back Up `.env`

```bash
cp .env ~/backups/trinity-env-$(date +%Y%m%d).bak
chmod 600 ~/backups/trinity-env-$(date +%Y%m%d).bak
```

Store this in a secure location (password manager, encrypted storage). Never commit it to git.

---

## Verify

```bash
sqlite3 ~/backups/trinity-YYYYMMDD-HHMMSS.db ".tables"
```

Expected: a list of table names with no errors. If `sqlite3` is not installed locally:

```bash
docker run --rm \
  -v ~/backups:/backup \
  alpine sh -c "apk add -q sqlite && sqlite3 /backup/trinity-YYYYMMDD-HHMMSS.db '.tables'"
```

---

## Restore Procedure

Stop the backend and scheduler first to release the SQLite write lock:

```bash
# Development
docker compose stop backend scheduler

# Production
docker compose -f docker-compose.prod.yml stop backend scheduler
```

Copy the backup into the volume (or bind-mount path):

```bash
# Restore (services stopped first)
docker run --rm \
  -v trinity_trinity-data:/data \
  -v ~/backups:/backup \
  alpine cp /backup/trinity-YYYYMMDD-HHMMSS.db /data/trinity.db
```

Restart services:

```bash
# Development
docker compose start backend scheduler

# Production
docker compose -f docker-compose.prod.yml start backend scheduler
```

Verify health:

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8001/health
```

---

## Automation

Add a cron job to back up the database daily and retain 14 days of history:

```bash
crontab -e
```

Add this line (adjust paths as needed):

```cron
0 3 * * * docker run --rm -v trinity_trinity-data:/data -v ~/backups:/backup alpine cp /data/trinity.db /backup/trinity-$(date +\%Y\%m\%d-\%H\%M\%S).db && find ~/backups -name "trinity-*.db" -mtime +14 -delete
```

This runs at 03:00 daily, creates a timestamped backup, and deletes backups older than 14 days.

Verify the cron job is working after the first scheduled run:

```bash
ls -lh ~/backups/trinity-*.db
```

---

## What Is and Is Not in the Database

**In `trinity.db`:**
- All agent metadata (names, ownership, settings)
- All schedules and execution history
- All chat sessions and message history
- Credentials metadata (not plaintext values — those live in agent `.env` files and `.credentials.enc` files)
- Encrypted channel bot tokens (Slack, Telegram, WhatsApp)
- Audit log
- User accounts and sharing configuration

**Not in `trinity.db`:**
- Agent source code (in git)
- Runtime secrets held by Redis (ephemeral — regenerate on restart)
- Container logs (in Vector's log files under the `trinity-logs` volume)
- Platform images (rebuild from source)

---

## See Also

- [Upgrading](upgrading.md) — Upgrade procedure that includes a pre-upgrade backup step
- [Monitoring](monitoring.md) — Health checks and recovery patterns
