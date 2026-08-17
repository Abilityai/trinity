# Running Trinity on PostgreSQL (instead of SQLite)

> **Status: experimental, opt-in (#300).** SQLite remains the zero-config
> default and its behavior is unchanged. PostgreSQL is selected entirely by a
> single environment variable (`DATABASE_URL`). Nothing about the SQLite path
> changes unless you explicitly set it.

This guide covers standing up a **new** Trinity instance backed by PostgreSQL.
It does **not** cover migrating existing SQLite data into PostgreSQL — there is
no ETL tool yet (see [Limitations](#limitations--not-yet-supported)). Enabling
PostgreSQL today gives you a **fresh, empty** database.

---

## 1. How the backend is selected

The backend is chosen at process startup from one environment variable,
resolved in `src/backend/db/engine.py:resolve_database_url()`:

| `DATABASE_URL` value | Backend used |
|----------------------|--------------|
| *unset* | **SQLite** at `TRINITY_DB_PATH` (default `/data/trinity.db`) |
| *empty string* (`DATABASE_URL=`) | **SQLite** (empty is treated as unset) |
| `sqlite:////data/trinity.db` | **SQLite** (explicit) |
| `postgresql://user:pass@host:5432/dbname` | **PostgreSQL** |

Key properties (all verified live):

- **SQLite is the default.** With no `DATABASE_URL`, or an empty one, you get
  SQLite — no Postgres container is even started (it is gated behind a compose
  profile, see below).
- **The flag is the *only* selector and it is not sticky.** Set it →
  PostgreSQL; comment it out → SQLite, on the very next restart. Switching is
  non-destructive (the two stores are independent files/volumes).
- **Both the backend and the standalone scheduler read the same
  `DATABASE_URL`** (`docker-compose.yml` passes it to both services), so the
  scheduler talks to whichever database the backend uses.

Internally, every `db/` module runs on **SQLAlchemy Core**, so the same code
generates dialect-correct SQL for either backend. There is no separate
PostgreSQL code path to maintain.

---

## 2. Prerequisites

- A Trinity checkout that includes #300 (the configurable-backend work).
- Docker + Docker Compose (the bundled `postgres:16-alpine` service is the
  easiest path; an external managed Postgres also works — just point
  `DATABASE_URL` at it).
- The backend/scheduler images already include the driver (`psycopg2-binary`)
  and `sqlalchemy`. No image changes needed.

---

## 3. Setup — bundled PostgreSQL container (recommended)

### 3.1 Configure `.env`

Add (or uncomment) these lines in your `.env`:

```bash
# --- PostgreSQL backend (#300) ---
POSTGRES_DB=trinity
POSTGRES_USER=trinity
POSTGRES_PASSWORD=<choose-a-strong-password>     # do NOT use the 'trinity' default in production

# Point Trinity at it. Host = the compose service name 'postgres'.
DATABASE_URL=postgresql://trinity:<choose-a-strong-password>@postgres:5432/trinity
```

Notes:
- The host in `DATABASE_URL` must be **`postgres`** (the compose service /
  Docker-DNS name), not `localhost`, because the backend reaches it over the
  Docker network.
- `POSTGRES_PASSWORD` and the password embedded in `DATABASE_URL` must match.
- The Postgres container lives on `trinity-platform-network` only — **agents
  can never reach it** (network topology, Issue #589). This is by design.

### 3.2 Start the stack with the `postgres` profile

The Postgres service is gated behind a compose **profile**, so it only starts
when you ask for it:

```bash
# Start the database first (or include it in the same up command)
docker compose --profile postgres up -d postgres

# Then the rest of the platform. Because DATABASE_URL is set in .env,
# backend + scheduler will connect to Postgres automatically.
docker compose --profile postgres up -d
```

If you use `./scripts/deploy/start.sh`, set `DATABASE_URL` in `.env` first; the
script reads `.env` and the profile must still be supplied
(`COMPOSE_PROFILES=postgres ./scripts/deploy/start.sh` or add `postgres` to
`COMPOSE_PROFILES` in your environment).

### 3.3 What happens on first (cold) start

On an **empty** Postgres database, the backend bootstraps everything
automatically (`init_database()` → `db/alembic_runner.upgrade_to_head()`,
i.e. `alembic upgrade head`, #1183):

1. **All tables are created** by the Alembic `0001_baseline` revision (~61
   tables) plus the PL/pgSQL append-only audit-log triggers. The baseline
   reuses the exact head DDL the legacy `init_schema_postgres` emitted, so the
   result is identical; subsequent schema changes ship as new Alembic
   revisions (an existing PG DB is migrated in place rather than rebuilt). A
   database that predates Alembic is stamped at the baseline on first start,
   not rebuilt. *(SQLite keeps its separate bespoke `db/migrations.py` runner —
   the two coexist during the Postgres transition.)*
2. **The admin user is seeded** with the password from `ADMIN_PASSWORD`.
3. The instance is in **first-run setup** state (`setup_completed=false`) —
   exactly like a fresh SQLite instance. You complete the normal first-launch
   setup flow (admin password via the setup token printed at startup, or the
   web UI) before login works.

> **This is not Postgres-specific** — any brand-new Trinity DB (SQLite too)
> starts un-set-up. If you see `{"detail":"setup_required"}` on login, that is
> the expected first-run gate, not an error.

### 3.4 Verify

```bash
# Backend resolved Postgres?
docker exec trinity-backend python -c \
  "import db.engine as e; print('sqlite:', e.is_sqlite(), '|', e.resolve_database_url())"
# -> sqlite: False | postgresql://trinity:...@postgres:5432/trinity

# Health (on Postgres, /health returns healthy without the SQLite-only
# schema_migrations gate):
curl -s http://localhost:8000/health      # {"status":"healthy",...}

# Tables built?
docker exec trinity-postgres psql -U trinity -d trinity -tAc \
  "select count(*) from information_schema.tables where table_schema='public'"
# -> ~61

# Admin + append-only triggers present?
docker exec trinity-postgres psql -U trinity -d trinity -tAc \
  "select username, role from users where username='admin'"
docker exec trinity-postgres psql -U trinity -d trinity -tAc \
  "select tgname from pg_trigger where tgname like 'audit_log%'"
# -> audit_log_no_update / audit_log_no_delete
```

---

## 4. Setup — external / managed PostgreSQL

If you run Postgres elsewhere (RDS, Cloud SQL, a separate VM):

1. Create an empty database and a role that owns it.
2. Do **not** enable the `postgres` compose profile (you don't need the bundled
   container).
3. Set `DATABASE_URL` to your external instance:
   ```bash
   DATABASE_URL=postgresql://<user>:<pass>@<host>:5432/<db>
   ```
4. Ensure the backend and scheduler containers can reach that host/port over
   the network.
5. Start the stack normally (`docker compose up -d`). Cold-start bootstrap
   (§3.3) runs against the external DB on first boot.

---

## 5. Connection pooling tunables

For PostgreSQL the engine uses a real connection pool with `pool_pre_ping`
(stale-connection detection). Tune via environment variables (read in
`db/engine.py`):

| Env var | Default | Meaning |
|---------|---------|---------|
| `DB_POOL_SIZE` | `10` | Persistent pooled connections |
| `DB_MAX_OVERFLOW` | `20` | Extra connections opened under burst load |

(SQLite uses `NullPool` and ignores these.)

---

## 6. Operational notes

- **Admin password is re-seeded from `ADMIN_PASSWORD` on every boot.** The
  bootstrap re-applies it idempotently, so the admin always logs in with the
  current `.env` value. Changing the admin password permanently means changing
  `ADMIN_PASSWORD` (same behavior as SQLite).
- **Idempotent restarts.** Re-booting against a populated Postgres does not
  duplicate tables, the admin user, or settings — schema creation is
  `IF NOT EXISTS` and triggers use `CREATE OR REPLACE`. Verified: data survives
  `docker compose restart backend scheduler`.
- **Scheduler.** The standalone `trinity-scheduler` reads the same
  `DATABASE_URL`. It reads schedules from Postgres, registers cron jobs, fires
  them, and writes `schedule_executions` rows back to Postgres. Verified
  end-to-end (a cron-fired schedule produced a `success` row with the agent's
  real response).
- **`/health` differences.** On SQLite, `/health` includes a
  `schema_migrations` gate (the SQLite-only bespoke runner). On PostgreSQL that
  gate is skipped — Postgres schema is owned by Alembic, whose state lives in
  the `alembic_version` table (#1183). Both return `{"status":"healthy"}` when up.
- **Canary harness is backend-agnostic (#1540).** Every SQL-tier collector in
  `src/backend/canary/` reads the configured backend through the
  `get_engine()`/`DATABASE_URL` seam (`src/backend/canary/` has zero `sqlite3`
  imports), so the CANARY-001 invariant harness works the same on PostgreSQL as
  on SQLite. It is safe — and intended — to keep `CANARY_ENABLED=1` on
  PostgreSQL; do **not** re-derive the retired SQLite-only constraint. Run-state
  is observable via `GET /api/canary/status` (admin) and the `canary_enabled`
  flag on `GET /api/settings/feature-flags` (#2217).
- **Backups.** SQLite = copy `~/trinity-data/trinity.db` (see
  `scripts/deploy/backup-database.sh`). PostgreSQL = `pg_dump`:
  ```bash
  docker exec trinity-postgres pg_dump -U trinity trinity > trinity-pg-backup.sql
  ```

---

## 7. Rollback to SQLite

Switching back is a one-line change + restart (non-destructive — your Postgres
volume is untouched and can be re-enabled later):

```bash
# In .env, comment out the selector:
# DATABASE_URL=postgresql://trinity:...@postgres:5432/trinity

# Restart without the postgres profile:
docker compose up -d
```

The backend resolves SQLite again (`sqlite:////data/trinity.db`) and the
Postgres container is not started. Verified: the flag toggles cleanly in both
directions.

---

## 8. End-to-end verification checklist

These are the scenarios validated for #300 (run them after any significant
change to the DB layer):

- [ ] **Cold-start**: empty Postgres → tables + admin + triggers built, healthy.
- [ ] **Durability**: write data → `restart backend scheduler` → data survives.
- [ ] **Idempotent reboot**: second boot → no duplicate admin, table count
      stable, no schema errors.
- [ ] **Scheduler fire**: agent running + autonomy on + enabled schedule →
      `schedule_executions` row reaches `success` with a real response.
- [ ] **Default still SQLite**: with `DATABASE_URL` unset, no Postgres
      container, backend resolves `sqlite:///…`, full CRUD works.

---

## 9. Limitations / not-yet-supported

- **No SQLite → PostgreSQL data migration.** Enabling Postgres gives a fresh
  empty DB. Moving an existing SQLite deployment's data into Postgres requires
  an ETL tool that does not exist yet (planned — see Next Steps).
- **Experimental.** PostgreSQL is not yet the recommended production default.
  Promote deliberately after the dual-backend CI gate lands.
- **CI currently tests SQLite only.** The PostgreSQL dialect path is validated
  by the verification checklist above and targeted probes, not (yet) by an
  automated dual-backend CI matrix. New SQL must stay dialect-portable (see
  Troubleshooting) until that gate exists.

---

## 10. Troubleshooting & dialect gotchas

PostgreSQL is stricter than SQLite. The classes of bug it surfaces (all fixed
in the #300 work — listed so new code avoids reintroducing them):

| Symptom (PostgreSQL) | Cause | Fix pattern |
|----------------------|-------|-------------|
| `DatatypeMismatch: column is integer but expression is boolean` | Python `bool` written to an INTEGER column | Coerce `bool → int` (done centrally via a TypeDecorator in `db/tables.py`) |
| `UndefinedFunction: operator does not exist: text = integer` | Joining a TEXT column to an INTEGER column | `cast(col, Text)` on the integer side of the JOIN |
| `argument of CASE/WHEN must be type boolean` | A bare integer used as a boolean (`CASE WHEN 1 …`) | Use a real predicate (`CASE WHEN 1=1 …`) |
| `InFailedSqlTransaction: current transaction is aborted` | An error earlier in the transaction (e.g. an INSERT conflict) poisons the rest | Wrap the conflict-prone statement in a SAVEPOINT (`conn.begin_nested()`) |

| Symptom | Likely cause |
|---------|--------------|
| `{"detail":"setup_required"}` on login | First-run setup not completed — expected on any fresh DB (§3.3) |
| `could not translate host name "postgres"` | Backend not on the platform network, or using an external DB without a reachable host |
| Backend logs `REDIS_URL must include credentials` | Unrelated to Postgres — set a credentialed `REDIS_URL` (see `docs/migrations/REDIS_AUTH.md`) |

---

## See also

- `docs/DEPLOYMENT.md` — general deployment
- `docs/migrations/REDIS_AUTH.md` — Redis credentials (required regardless of DB)
- `src/backend/db/engine.py` — the backend selector
- Issue #300 — configurable database backend
