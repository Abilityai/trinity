# Feature Flow: Automatic Database Backups (#2216)

> **Status**: ✅ Implemented (2026-08-16) · **Issue**: #2216 (epic #1258) · **Requirements**: [infrastructure.md §8.2a](../requirements/infrastructure.md)

## Overview

Every install gets on-disk recovery points for the platform database with zero operator setup, on both backends: a daily in-process job (03:30 UTC) plus a boot-time pre-migration copy (SQLite). Retention is bounded in both directions (a window ceiling and a fixed keep-floor), failures are operator-visible over time (edge + staleness alarms), status is readable without shell access, and restore is documented and exercised by test.

**Why it exists.** `scripts/deploy/backup-database.sh` shipped but nothing invoked it — and it was a workstation-side GCP pull doing a naive live `cp`. A real instance's SQLite header was overwritten by an HTTP response body; every worker died at import with `file is not a database`; the file was `sqlite3 .recover`-able (~1.37M rows) but there was **no backup**, so the instance was rebuilt empty. No code hardening addresses "an ordinary human slip becomes total data loss" — only recovery points do.

**Scope, stated honestly.** Artifacts land on the **same disk** as the source (`/data/backups/`). This protects against the incident class (stray write, fat-fingered delete, bad migration), NOT disk loss. `scope: "same-disk"` is a field on the status block so no future UI has to parse prose. Off-site is a flagged follow-up (the `archive_storage.ArchiveStorage` ABC is the ready seam).

## User Story

As a self-hosting operator, when something corrupts or deletes the platform DB, I have a verified, recent copy sitting beside it that I can restore in minutes — without ever having set up cron, and I find out promptly when the copy stops being made.

## Entry Points

| Trigger | Where | Notes |
|---|---|---|
| Daily cron `03:30 UTC` | `services/db_backup_service.py::DBBackupService.run_backup` (APScheduler, backend lifespan) | `DB_BACKUP_HOUR`/`DB_BACKUP_MINUTE` env; `misfire_grace_time=3600` |
| Backend boot with **pending SQLite migrations** | `database.init_database()` → `db/backup_primitives.maybe_backup_before_migrations` | Inside the `migration_lock` window, BEFORE the first `run_all_migrations` pass |
| Admin read | `GET /api/settings/retention` → `backup` block | `assert_admin`; no new endpoint |
| Admin write (retention) | `PUT /api/settings/ops/config {"backup_retention_days": N}` | Validated 1–3650 (`0` invalid); generic `PUT /api/settings/{key}` 422-blocks; `/ops/reset` skips |

## Frontend Layer

None (v1). The status block is consumable by the existing Settings retention panel; no UI change ships with #2216.

## Backend Layer

### Leaf: `db/backup_primitives.py` (stdlib-only)

**Import-graph rule (load-bearing):** must not import `database` or `services/*` — `maybe_backup_before_migrations` runs inside `init_database()`, which executes at import (the `db/migration_lock.py` dependency-light precedent).

| Function | Contract |
|---|---|
| `sqlite_backup_to(src, dest)` | **Synchronous.** Opens AND closes both `sqlite3` connections inside itself (thread-affinity contract, learnings 2026-07-20) — the service calls it via `asyncio.to_thread`. One-shot `Connection.backup()` (`pages=-1`): a single read transaction ⇒ a consistent snapshot as of start, standalone `.db`, no sidecars. Logs a WARNING past `_COPY_DURATION_WARN_SECONDS=20` naming the 30s busy-timeout wall. |
| `verify_sqlite_backup(path)` | Read-only open → `PRAGMA quick_check == ok` → `COUNT(*) FROM sqlite_master > 0`; raises `BackupVerificationError` — a corrupt copy must never enter the artifact namespace. |
| `prune_backups(dir, retention_days, min_keep=BACKUP_MIN_KEEP)` | Newest `min_keep` (**3**, fixed constant) untouchable regardless of age; nothing inside the window is deleted regardless of pressure; only `ARTIFACT_PATTERNS` (`trinity-backup-*.db`, `trinity-backup-*.dump`, `pre-migration-*.db`); non-positive window → no-op + WARNING. |
| `sweep_stale_tmps(dir)` | Reclaims `*.tmp.*` older than 24h (the SIGKILL-mid-copy crash window). |
| `maybe_backup_before_migrations(cursor, conn, *, db_path)` | Gates: `migration_health` reports a pending migration; not a fresh install (no/empty `schema_migrations` → INFO skip); DB file ≥ 4 KiB. Copies to `pre-migration-YYYYMMDD-HHMMSS.db` via tmp → verify → `os.replace`; writes status rows with hand-rolled SQL (the `db` facade doesn't exist yet). **Fail-open**: `sqlite3.DatabaseError` (corrupt DB) → ERROR + best-effort `failed` status row + return; any other exception → ERROR + return. The subsequent `run_all_migrations` raises the byte-identical incident fingerprint (pinned by test). |

**Journal mode.** The platform DB runs SQLite's default **DELETE** mode — verified against a live dev instance (`PRAGMA journal_mode` → `delete`; nothing in the codebase sets `journal_mode`, and the per-cycle `PRAGMA wal_checkpoint(TRUNCATE)` is a no-op today). `Connection.backup()` is correct in both modes; in DELETE mode the one-shot copy holds a read lock for the copy duration (a concurrent writer waits up to its 30s busy timeout — acceptable at 03:30 UTC; the duration WARNING is the tripwire). Incremental `pages=N` was rejected (restarts from scratch on every foreign write → livelock under sustained writes).

### Service: `services/db_backup_service.py`

`DBBackupService` (singleton `db_backup_service`), started/stopped in `main.py` lifespan beside `db_vacuum_service` (each in its own try/except). Unlike db_vacuum there is **no** `is_sqlite()` no-op arm — both backends are served.

**`run_backup(trigger="scheduled")` ordering:**

```
acquire lease (SETNX db_backup:running, fail-open) ── held → return skipped_lease
 ├─ ensure_backup_dir(0700) → sweep_stale_tmps
 ├─ day-key check: <dir>/trinity-backup-YYYYMMDD.{db|dump} exists → skipped_exists
 ├─ free-space preflight: disk free ≥ 1.2× source (SQLite: file+sidecars; PG: pg_database_size)
 │     short → status skipped_no_space + WARNING + edge alarm (NEVER prune-to-make-room)
 ├─ SQLite: to_thread(sqlite_backup_to + verify)   |  PG: pg_dump -Fc subprocess + PGDMP/size verify
 │     tmp = <final>.tmp.<pid>; success → os.replace(tmp, final); finally: unlink tmp
 ├─ status ok (+path/size/duration) | failed (+error) → edge alarm on ok→failure transition
 └─ TAIL, on EVERY attempt (success / failed / space-skip):
       prune_backups(dir, effective_backup_retention_days()) → staleness check → release lease
       (>80% of lease TTL elapsed → WARNING)
```

**PG arm** (`_pg_conninfo_and_password` + `_pg_dump_to`): the operator's `DATABASE_URL` reaches `pg_dump -d` with exactly two rewrites — driver suffix normalized (`postgresql+psycopg2://` → `postgresql://`, libpq rejects the former) and password stripped (via `URL._replace(password=None)` — `.set(password=None)` KEEPS the old value; `render_as_string(hide_password=False)` — the default renders `***`, which libpq would try literally). Query params (`sslmode`, `sslrootcert`, `options`) pass through (percent-encoded values, which libpq decodes) — re-parsing into `-h/-p/-U` flags would silently drop what managed PG mandates. Password → subprocess-env `PGPASSWORD` only, never argv, never logged; a URL without a password leaves the env untouched. Timeout (`DB_BACKUP_PG_DUMP_TIMEOUT_SECONDS`, 1800) → `proc.kill()` → `await proc.wait()` (reap) → tmp unlinked in the caller's `finally`. Missing binary → `BackupRunError` naming the image rebuild. `postgresql-client-17` is baked into `docker/backend/Dockerfile` (major-pinned, #1823 rationale).

**Lease** (`_LEASE_KEY = db_backup:running`, `_LEASE_TTL_SECONDS = PG_DUMP_TIMEOUT_SECONDS + 300`, comment-linked): duplicate-I/O **suppression, never a correctness boundary** — correctness is pid-suffixed tmps + atomic `os.replace` + day-keyed names + pattern-scoped prune. Fail-open (Redis down → day-key guard alone; worst case one duplicate nightly I/O burst, atomic-replace-safe). Release is own-token compare-and-delete (`redis_breaker_util.lock_token_matches`), attempted unconditionally.

**Retention reader** — `effective_backup_retention_days()` is THE one reader. The generic ops readers coerce garbage → `0` → "sweep disabled" (safe for row retention, catastrophic here = keep-forever, #1871 class); this one coerces unparseable/out-of-bounds → **default 14 + WARNING**. `GET /api/settings/retention` excludes the key from the generic `windows` map and reports it only via this helper in the `backup` block; `cleanup_service.log_effective_retention_windows` special-cases it through the same helper.

**Status** (durable `system_settings`, ent#236 lesson): `db_backup_last_status` (`ok|failed|skipped_no_space` — a disabled job writes nothing; the status block's `enabled` field carries that), `db_backup_last_success_at`, `db_backup_last_error`, `db_backup_last_path`, `db_backup_last_size_bytes`, `db_backup_last_duration_ms`, `db_backup_last_trigger` (`scheduled|boot_pre_migration`), plus `db_backup_first_attempt_at` (staleness reference for never-succeeded installs) and `db_backup_last_staleness_alarm_at`.

**Alarms** — direct `db.create_operator_queue_item` on sentinel `_db-backup` (platform-create path, bypasses the #1632 agent-ingestion caps), id `db-backup-<utc_now_iso()>`, `type=alert`, `priority=high`, `expires_at=None`. (a) **Edge**: fires iff the new status is `failed`/`skipped_no_space` AND the prior status was `ok`/absent — once per episode, re-armed by an intervening success. (b) **Staleness**: last success (or first attempt) older than `_STALENESS_ALARM_DAYS=3` → alarm, re-fired at most every `_STALENESS_REALARM_DAYS=7`. Context carries status/paths/sizes only — never row data (canary G-04). Registration duties: `db-backup-` in `operator_queue_service._RESERVED_ID_PREFIXES`; `_db-backup` in `canary/snapshot._PLATFORM_ALARM_SENTINELS` (the L-03 `operator_queue` predicate is `NOT IN (<sentinels>)`, generalized from the single `_retention-guard` literal) — service↔canary parity pinned by test.

**Config** (env, forwarded in **both** compose files + `.env.example` — the #1486/#1488 inert-knob class): `DB_BACKUP_ENABLED` (true — one reader, `bp.backup_enabled_from_env()`, shared by the daily job AND the boot hook: `false` disables both, since only the job's tail ever prunes a boot copy), `DB_BACKUP_HOUR` (3), `DB_BACKUP_MINUTE` (30), `DB_BACKUP_PG_DUMP_TIMEOUT_SECONDS` (1800). Malformed/out-of-range → default + WARNING (`_int_env`, #1871 shape). Retention: `backup_retention_days` in `OPS_SETTINGS_DEFAULTS` (14) / `OPS_SETTINGS_VALIDATION` (`int`, 1, 3650) / `RETENTION_OPS_KEYS`; NOT in `COMMUNITY_FRESH_INSTALL_SEED`.

### Boot hook wiring (`database.py`)

```python
with migration_lock(DB_PATH):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        maybe_backup_before_migrations(cursor, conn, db_path=DB_PATH)   # #2216, fail-open
        run_all_migrations(cursor, conn)
        ...
```

PG is deliberately excluded from the boot hook in v1 (Alembic DDL is transactional; a boot-path subprocess adds import-time latency; the daily `pg_dump` covers PG recovery points).

## Data Layer

- **No schema change** — status rides `system_settings`; config rides ops settings + env. No `db/migrations.py` entry, no Alembic revision.
- Filesystem: `<db dir>/backups/` (= `/data/backups/`, created 0700). Artifacts: `trinity-backup-YYYYMMDD.db`, `trinity-backup-YYYYMMDD.dump`, `pre-migration-YYYYMMDD-HHMMSS.db`; in-flight tmps `<final>.tmp.<pid>`.
- Redis: `db_backup:running` (SETNX lease, TTL 2100s by default). Not `agent:*`-named, so the #1560 name-keyed registry doesn't apply.

## Side Effects

- Nightly disk write of one artifact (≈ DB size for SQLite; compressed for PG); bounded by `backup_retention_days` × daily + `BACKUP_MIN_KEEP` floor + the pre-migration artifacts (rare).
- Operator-queue rows on the sentinel host on failure/staleness edges.
- In DELETE journal mode: a read lock held for the copy duration at 03:30 UTC.

## Error Handling

| Error | Handler | Recovery |
|---|---|---|
| Copy fails / corrupt source | verify-before-replace; tmp unlinked in `finally` | `failed` + edge alarm; healthy artifacts untouched; prune still runs |
| pg_dump non-zero / timeout | rc check / `wait_for` → kill → wait | `failed` + alarm; tmp unlinked |
| pg_dump binary absent (un-rebuilt image) | `FileNotFoundError` caught | `failed` + alarm naming the rebuild |
| Disk short | preflight | `skipped_no_space` + WARNING + alarm; prune still runs; never prune-to-make-room |
| Redis down | lease fail-open | day-key guard alone; worst case one duplicate run |
| Boot hook exception (any) | blanket catch → `logger.error` | boot continues; incident fingerprint of the migration runner unchanged |
| Malformed env hour/minute / garbage retention row | `_int_env` / `effective_backup_retention_days` | default + WARNING |
| SIGKILL mid-copy | aged `*.tmp.*` sweep next run | orphan reclaimed |
| `/data/backups` unwritable | `mkdir`/write raises | `failed` + alarm |

## Security Considerations

- **No new attack surface** — no new endpoint, no new auth path, no WS, no MCP tool. Status rides the existing admin-only `GET /api/settings/retention` (`assert_admin`, which rejects agent principals since #1890); the only write path is the validated `PUT /api/settings/ops/config`.
- **No user-controlled input reaches the job.** Artifact names are server-generated from a UTC day key, so there is no traversal surface; the retention knob passes ops-config validation (422 on garbage) before it can reach the prune.
- **The DB password never leaves the process except as `PGPASSWORD` on the subprocess env** — never in argv (world-readable via `/proc`), never logged. `create_subprocess_exec` (no shell) means the conninfo URI is never word-split or interpolated. An unparseable `DATABASE_URL` raises `BackupRunError from None` so neither the ERROR log nor the durable status echoes the URL — the exception chain is the leak path a bare `raise` would keep.
- **Artifacts are the full database** — dir 0700, files 0600, same bind mount as `trinity.db` itself, so no new exposure class. They carry the AES-256-GCM envelopes (Invariant #12) but **not** `CREDENTIAL_ENCRYPTION_KEY`, which lives in `.env`: an artifact alone decrypts nothing. Backing up `.env` stays a documented operator step.
- **An agent cannot silence the platform's own failure alarm.** `db-backup-` is registered in `operator_queue_service._RESERVED_ID_PREFIXES` — without it an agent could pre-create the id and the ingestion path's `on_conflict_do_nothing` would swallow the real alert; the `_db-backup` sentinel host is uncreatable because `sanitize_agent_name` strips the leading `_`, and it is excluded from canary L-03's orphan scan with service↔canary parity pinned by test.
- **Alarm context carries status, paths and sizes only** — never row data or DB contents (the canary G-04 rule).
- **Prune is pattern-scoped** to the three artifact globs among the direct children of the backup dir, so a misconfigured path can never turn the sweep into an arbitrary-file deleter; deletions are logged by name only.

## Testing

| File | Covers | Count |
|---|---|---|
| `tests/unit/test_2216_backup_primitives.py` | copy/verify/thread-affinity/duration WARNING; prune floor (merciless); tmp sweep; naming | 22 |
| `tests/unit/test_2216_boot_pre_migration_backup.py` | skip branches (no-pending / fresh-SILENT / empty), artifact-before-mutation, corrupt-DB fail-open + **unchanged incident fingerprint**, no eviction, no propagation, tmp unlink | 9 |
| `tests/unit/test_2216_backup_restore_roundtrip.py` | AC#5 incident replay + pre-migration artifact restore | 2 |
| `tests/unit/test_2216_db_backup_service.py` | day-key, lease, preflight, prune-on-every-attempt, status, edge + staleness alarms, env parse, inverted reader, PG conninfo/PGPASSWORD/timeout kill+reap/missing binary | 38 |
| `tests/unit/test_2216_backup_observability.py` | retention GET backup block + windows exclusion + one-number rendering, write-path guards, reserved prefix, sentinel parity + L-03 predicate | 14 |

Run: `pytest tests/unit/test_2216_*.py -q` → 85 passed (no backend). Neighbor pins reconciled: `test_retention_floor.py`, `test_297_ops_settings_validation.py`, `test_1771a_retention_edges.py`.

**Local execution proof** (scripts/docs are invisible to verify-local): scratch DB → `sqlite_backup_to` → corrupt the source header with the incident bytes → restore the artifact → row equality — recorded in the #2216 PR.

## Related Flows

- [cleanup-service.md](cleanup-service.md) — the retention model + #1644 blast-radius guard the backup key deliberately does NOT route through (its floor is structural, not an ack gate)
- [database-migration-runner.md](database-migration-runner.md) — the `migration_lock` window the boot hook lives in
- `docs/user-docs/guides/deploying/backup-and-restore.md` — operator procedure (restore incl. stale-sidecar removal, PG `pg_restore -Fc`)

## Follow-ups (flagged, not filed)

Off-site destinations via `ArchiveStorage`; PG boot-time backup; compression; manual backup-now endpoint; WAL migration; the pre-existing unforwarded `DB_VACUUM_*`/`AUDIT_RETENTION_*` env knobs.
