# Feature: Database Migration Runner Safety (#1160 / #1183)

## Overview
Two correctness guarantees for the SQLite migration runner that executes on every backend boot: (1) a cross-process `flock` serialises the whole suite so concurrent uvicorn workers + the scheduler container can't race it, and (2) the two table-rebuild migrations use an explicit-transaction rename-swap (`_atomic_rebuild`) so a crash mid-rebuild rolls back cleanly instead of losing rows between an autocommitted `DROP` and the re-insert. A failed migration is named on its traceback and surfaced to operators via the `/health` 503 payload (`first_pending`).

This doc is the end-to-end vertical-slice companion to Architectural Invariant #3 in `docs/memory/architecture.md` ("Schema in `db/schema.py`, Migrations in `db/migrations.py`" → "Runner safety (#1160)" / "Backend split (#1183)") — read that for the policy; read this for the call path. There is **no frontend layer**: the operator-facing surface is the `/health` 503, not UI.

## User Story
As a platform operator, I want the migration suite to be safe under concurrent boot and crash, so a multi-worker restart or an OOM mid-migration never corrupts the schema or silently drops access-control rows, and a stuck migration is named in the health check so the 503 is actionable.

## Entry Points
- **Boot**: `src/backend/database.py:144` — `init_database()`, called once per process at backend startup (lifespan). SQLite branch only; the PostgreSQL branch (`db.engine.is_sqlite()` false) delegates to Alembic via `db/alembic_runner.upgrade_to_head()` and never runs this code.
- **Operator surface**: `GET /health` (`src/backend/main.py:1116`) — returns 503 with a `migrations` block (`applied`, `expected`, `first_pending`) when the suite is incomplete. Unauthenticated, top-level (no `/api/` prefix).

## Backend Layer

### Cross-process lock — `src/backend/db/migration_lock.py` (NEW)
- `migration_lock(db_path)` (`migration_lock.py:18`) — `@contextmanager` holding an exclusive OS `flock` (`fcntl.flock(fd, LOCK_EX)`) on a sidecar file `{db_path}.migrate.lock`.
- Stdlib-only, zero import-time side effects by design — so it can be unit-tested in a spawned subprocess without dragging in `database.py` (whose module-level `db = DatabaseManager()` would re-run `init_database()` on import).
- Chosen over a `BEGIN IMMEDIATE` DB lock because the flock is **not** released by the suite's many intra-run `conn.commit()` calls, and the kernel releases it on process death (kill -9 / OOM) — a crashed holder never leaves a stale lock.
- `LOCK_EX` blocks with **no timeout** by design: the holder only runs the fast suite and the kernel frees the lock on death, so the only way to wait forever is a genuinely-hung migration (which would block boot regardless).
- **Fails open**: if the lock file can't be created/locked (e.g. `EACCES` on a pre-#874 root-owned path), it logs a warning and proceeds unlocked — no worse than the prior status quo. Assumes the `/data` dir is a local-FS bind mount (flock is unreliable on NFS).

### Locked boot sequence — `src/backend/database.py:168`
Inside `with migration_lock(DB_PATH):` (covers **both** migration passes AND `init_schema`, so `init_schema`'s `CREATE TABLE IF NOT EXISTS` can't race a concurrent worker mid-rebuild and recreate an empty table over its data):
1. `run_all_migrations(cursor, conn)` (`database.py:177`) — first pass; upgrades an existing DB, skips migrations whose target table doesn't exist yet (fresh install).
2. `init_schema(cursor, conn)` (`database.py:180`) — creates all current tables + indexes from `db/schema.py`.
3. `run_all_migrations(cursor, conn)` (`database.py:187`) — second pass; now records the fresh-install skips (tables exist after step 2) so the health count stays accurate. Re-running is a no-op because every migration guards with `PRAGMA table_info` before mutating.
4. `_ensure_admin_user(cursor, conn)`.

### Migration runner — `src/backend/db/migrations.py`
- `run_all_migrations(cursor, conn)` (`migrations.py:133`) — creates the `schema_migrations` tracking table, reads applied names, iterates `MIGRATIONS` (`migrations.py:2447`, an ordered `[(name, fn)]` list of 71 entries), skips already-applied, runs each, then `INSERT OR IGNORE INTO schema_migrations`. `OperationalError` containing `"no such table"` is swallowed (fresh-install skip); every other failure is **re-raised** so the backend refuses to start with an inconsistent schema. Before re-raising, `e.add_note(f"Trinity migration that failed: {name!r}")` (`migrations.py:178`, `:182`) names the migration on the exception itself while preserving its original type — so the crash-loop traceback identifies the culprit.
- `_atomic_rebuild(cursor, conn, table, create_new_sql, copy_sql, *, indexes=())` (`migrations.py:100`) — the core fix. Atomic rename-swap inside an explicit transaction:
  - `conn.commit()` first to clear any pending implicit transaction before `BEGIN`.
  - `BEGIN` → `DROP TABLE IF EXISTS {table}_new` (clears an orphan from a prior crashed attempt) → `create_new_sql` (`{table}_new`) → `copy_sql` (server-side `INSERT INTO {table}_new ... SELECT ... FROM {table}` — rows never live in a Python list across a commit) → `DROP TABLE {table}` → `ALTER TABLE {table}_new RENAME TO {table}` → re-run `indexes` → `conn.commit()`.
  - Any exception → `conn.rollback()` + re-raise. Under Python's legacy sqlite3 isolation an *implicit* DDL statement autocommits, so the old code's `DROP TABLE` committed before the re-INSERT; an *explicit* `BEGIN` holds the drop + re-create in one unit of work, so the old table is never gone until the new one commits.
- Consumers of `_atomic_rebuild`:
  - `_migrate_agent_sharing_table` (`migrations.py:200`, migration `"agent_sharing"`) — rebuilds `agent_sharing` from `shared_with_id` → `shared_with_email`; the copy `JOIN`s `users`, `lower()`s the email, and drops NULL/empty-email rows (preserving the old row-by-row Python `if share[2]` filter). No indexes recreated (`init_schema`'s index pass owns them).
  - `_migrate_agent_skills_table` (`migrations.py:515`, migration `"agent_skills"`) — rebuilds `agent_skills` from `skill_id` → `skill_name` (straight column copy); recreates `idx_agent_skills_agent` + `idx_agent_skills_skill`.
- `migration_health(cursor)` (`migrations.py:186`) — returns `(applied, expected, first_pending)`: count recorded in `schema_migrations`, `len(MIGRATIONS)`, and the first registered migration not yet recorded (`None` when all applied). Drives the `/health` 503 body.

### Health surface — `src/backend/main.py:1116`
`health_check()`:
- PostgreSQL (`not is_sqlite()`) short-circuits to `{"status": "healthy"}` — the migration-completeness gate is SQLite-only (no `schema_migrations` table; Alembic owns PG).
- SQLite: calls `migration_health(conn.cursor())`. If reading `schema_migrations` raises, treat as incomplete (`applied = 0`, `first_pending = MIGRATIONS[0][0]`).
- `applied < expected` → `JSONResponse(status_code=503, content={"status": "unhealthy", "timestamp", "migrations": {applied, expected, first_pending}})`. Otherwise `{"status": "healthy"}`.

## Data Layer
### Tables touched
- `schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)` — the idempotency tracking table; `INSERT OR IGNORE` makes re-application a no-op and is the atomic record under concurrent workers.
- `agent_sharing` — rebuilt by `_migrate_agent_sharing_table` (`shared_with_id` → `shared_with_email`, lowercased, NULL/empty dropped).
- `agent_skills` — rebuilt by `_migrate_agent_skills_table` (`skill_id` → `skill_name`); indexes `idx_agent_skills_agent`, `idx_agent_skills_skill` recreated.
- Each rebuild transiently creates and drops `{table}_new` inside the atomic transaction (never visible/committed on a crash mid-rebuild).

### Filesystem
- Sidecar lock file `{db_path}.migrate.lock` under the `/data` bind mount — created/opened (`0o644`), `flock`ed, never deleted (a 0-byte sentinel; the kernel releases the lock on close/death).

## Side Effects
- No WebSocket broadcasts, no audit-log writes, no Redis state — this is a boot-time infra subsystem. The only externally-observable signals are:
  - `/health` flips to 503 with `migrations.first_pending` while any migration is unapplied (drives load-balancer / orchestrator health gates and operator paging).
  - On failure, an `ERROR` log line (`Migration FAILED (%s)`) plus the `add_note` traceback name in the crash-loop output.

## Error Handling
| Case | Behavior |
|------|----------|
| Concurrent boot (N workers + scheduler) | flock serialises; exactly one process runs each rebuild, the rest skip via `schema_migrations`. None crash, data intact. |
| Lock file uncreatable/unlockable (`EACCES`, etc.) | Fail-open: warn + proceed unlocked (prior behavior). |
| Crash mid-rebuild (OOM/kill -9 between DROP and re-insert) | Explicit transaction rolls back; old table + all rows survive; no orphaned `{table}_new` committed. Replay completes cleanly. |
| Migration targets a not-yet-existing table (fresh install) | `OperationalError "no such table"` swallowed; `init_schema` creates it; second pass records the skip. |
| Any other migration exception | `e.add_note("Trinity migration that failed: '<name>'")`, original type preserved, re-raised → backend refuses to start (inconsistent schema). |
| Incomplete suite at runtime | `GET /health` → **503** `{status: "unhealthy", migrations: {applied, expected, first_pending}}`. |
| `schema_migrations` unreadable in `/health` | Treated as incomplete: `applied=0`, `first_pending=MIGRATIONS[0][0]` → 503. |

## Testing
Pure unit tests, no backend/Docker — `tests/unit/test_1160_migration_atomicity.py` (391 lines). Uses `multiprocessing` (spawn) for cross-process cases because flock is per-open-file-description; modules are `importlib`-loaded directly to avoid `database.py`'s import-time `init_database()`.

### Prerequisites
- `pytest -m unit tests/unit/test_1160_migration_atomicity.py` (no services required).

### Test Steps (behaviors under test)
1. **Rebuild happy path** — `test_agent_sharing_rebuild_migrates_and_preserves_behavior`, `test_agent_skills_rebuild_migrates_and_recreates_indexes`: seed old schema, run the migrate fn; assert new column present/old absent, NULL/empty email rows dropped + lowercased, skills indexes recreated.
2. **Idempotent** — `test_rebuild_is_noop_on_already_migrated_schema`: second run does nothing, row count unchanged.
3. **Crash atomicity** — `test_crash_during_rebuild_rolls_back_no_data_loss` (parametrized to crash after `DROP TABLE agent_sharing` and at the `RENAME`): a `_CrashAfter` cursor proxy raises at a precise statement; assert the old table + all 4 rows survive (data-loss window closed).
4. **Replay** — `test_replay_after_partial_apply_completes_cleanly`: crash after DROP, assert no orphaned `agent_sharing_new` committed, then replay completes with zero data loss.
5. **Failure naming** — `test_run_all_migrations_names_failing_migration`: a `_boom` migration raises; assert the original type is preserved and `"explosive_migration"` rides `exc.__notes__`.
6. **Health surfacing** — `test_migration_health_names_first_pending`: with 3 registered migrations and an empty tracking table, assert `(0, 3, "m_a")`; record `m_a` → `(1, 3, "m_b")`; record all → `(3, 3, None)`.
7. **Cross-process lock** — `test_migration_lock_serialises_across_processes`: 4 spawned processes each hold the lock 0.3s and log ENTER/EXIT; assert strict `["ENTER","EXIT"]*4` nesting (no overlap).
8. **Concurrent boot rebuild** — `test_concurrent_boot_rebuild_is_safe`: 5 processes boot at once against a DB needing the rebuild; the flock lets exactly one run it, the rest skip via `schema_migrations`; none crash, data intact, `schema_migrations` has the rebuild recorded exactly once.

## Related Flows
- Architectural Invariant #3 in `docs/memory/architecture.md` — migration policy, dual-track SQLite/PostgreSQL (#1183), enterprise separate runner.
- Enterprise tables migrate via a separate runner (`enterprise/backend/_migrations.py`, tracked in `enterprise_schema_migrations`) — out of scope for this doc; never touches the OSS `schema_migrations`.
- [cleanup-service.md](cleanup-service.md) — runs the retention/soft-delete sweeps over tables created by these migrations.
