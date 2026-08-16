"""
Automatic Database Backup Service (#2216).

Daily APScheduler job producing on-disk recovery points for BOTH backends
(SQLite via stdlib ``sqlite3.Connection.backup()``, PostgreSQL via a
``pg_dump -Fc`` subprocess), cloned from the ``db_vacuum_service`` shape.
Default **ON** — every install gets recovery points with zero setup.

Runs at 03:30 UTC by default: before the 04:15 audit prune and 04:30 VACUUM
(a backup's purpose is recovery points, so capture-more-data ordering wins)
and after the 03:00 log archival (different volume).

Scope, stated honestly: artifacts land under ``<db dir>/backups`` —
``/data/backups/`` in production — on the SAME disk as the source. This
protects against the #2216 incident class (a stray write corrupting the DB
file, a fat-fingered delete, a bad migration), NOT against disk loss.
Off-site is a follow-up (the ``archive_storage.ArchiveStorage`` ABC is the
ready seam); the status block carries ``scope: "same-disk"`` so the boundary
is machine-readable, not just prose.

Double-fire (``--workers 2``) story — two independent fail-safe guards:

- **Day-keyed idempotence** (works with Redis down): today's artifact
  already exists → skip with INFO. The artifact name IS the day key.
- **Redis SETNX lease** ``db_backup:running`` (shared fail-open breaker
  client): duplicate-I/O *suppression*, NEVER a correctness boundary.
  Correctness is carried entirely by pid-suffixed tmps + atomic
  ``os.replace`` + day-keyed names + pattern-scoped prune; the worst
  concurrent-duplicate outcome is one clean, loud ENOSPC on a tmp while
  the sibling completes.

Configuration (env vars — forwarded by BOTH compose files, else inert, the
#1486/#1488 class):

- ``DB_BACKUP_ENABLED`` (default ``true``)
- ``DB_BACKUP_HOUR`` / ``DB_BACKUP_MINUTE`` (default 03:30 UTC; malformed
  values fall back with a WARNING instead of crashing at import — an
  improvement over the bare ``int()`` in the db_vacuum template)
- ``DB_BACKUP_PG_DUMP_TIMEOUT_SECONDS`` (default 1800)

Retention: ``backup_retention_days`` (ops settings, default 14, bounds
1–3650 — ``0`` is INVALID here; see :func:`effective_backup_retention_days`
for the deliberately inverted coercion) + the fixed
``backup_primitives.BACKUP_MIN_KEEP`` floor.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db.connection as db_connection
from db import backup_primitives as bp

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int, low: int, high: int) -> int:
    """Fail-safe env parse (#1871 shape): malformed AND out-of-range values
    fall back to the bounded default; an explicitly-set rejected value logs a
    WARNING (a silently-ignored knob is the #1039 inert-by-obscurity class)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not an integer — using default %d", name, raw, default
        )
        return default
    if not (low <= value <= high):
        logger.warning(
            "%s=%r out of range [%d, %d] — using default %d",
            name, raw, low, high, default,
        )
        return default
    return value


DB_BACKUP_ENABLED = os.getenv("DB_BACKUP_ENABLED", "true").lower() == "true"
DB_BACKUP_HOUR = _int_env("DB_BACKUP_HOUR", 3, 0, 23)
DB_BACKUP_MINUTE = _int_env("DB_BACKUP_MINUTE", 30, 0, 59)

# pg_dump wall-clock budget. `asyncio.wait_for` cancels only the *await*,
# never the child — the timeout path explicitly kill()s and wait()s (reaps)
# the subprocess (the class utils/registered_run.py exists for agent-side).
PG_DUMP_TIMEOUT_SECONDS = _int_env(
    "DB_BACKUP_PG_DUMP_TIMEOUT_SECONDS", 1800, 60, 86400
)

# Lease TTL is DERIVED from the slowest guarded operation (learnings
# 2026-07-31 lease-sizing): a pg_dump may legitimately run up to
# PG_DUMP_TIMEOUT_SECONDS, so the lease must outlive it plus headroom.
# Retuning the dump timeout visibly re-opens this math — keep them linked.
_LEASE_TTL_SECONDS = PG_DUMP_TIMEOUT_SECONDS + 300
_LEASE_KEY = "db_backup:running"

# --- Retention knob (ops settings model, D4) ------------------------------
RETENTION_KEY = "backup_retention_days"
DEFAULT_RETENTION_DAYS = 14
_RETENTION_MIN, _RETENTION_MAX = 1, 3650

# Free-space preflight headroom (skip + WARNING + alarm when short — never
# die, and NEVER prune-to-make-room).
_PREFLIGHT_HEADROOM = 1.2

# Staleness re-alarm (BKUP-013b): "does not fail silently" must hold over
# TIME, not just at the edge — a sustained failure/chronic-skip state
# re-surfaces instead of costing exactly one dismissible alert.
_STALENESS_ALARM_DAYS = 3       # ≈ 3 missed dailies
_STALENESS_REALARM_DAYS = 7     # while stale, re-alarm at most weekly

# Alarm plumbing: platform-created directly via db.create_operator_queue_item
# (bypasses the #1632 agent-ingestion caps by construction). The sentinel host
# is uncreatable as an agent (sanitize_agent_name strips the leading '_') and
# is excluded from canary L-03's orphan scan — parity-tested against
# canary/snapshot.py (_PLATFORM_ALARM_SENTINELS). The id prefix is registered
# in operator_queue_service._RESERVED_ID_PREFIXES so an agent cannot
# pre-create (and via on_conflict_do_nothing, silence) the backup alarm.
ALARM_AGENT_NAME = "_db-backup"
ALARM_ID_PREFIX = "db-backup-"

_STDERR_CAP = 2000

# Durable status keys (system_settings — in-process state is invisible to the
# other uvicorn worker, ent#236 lesson). The boot hook in
# db/backup_primitives.py writes a subset of these with hand-rolled SQL (it
# runs before the db facade exists) — keep the names in sync with it.
STATUS_KEYS = (
    "db_backup_last_status",        # ok | failed | skipped_no_space | disabled
    "db_backup_last_success_at",
    "db_backup_last_error",
    "db_backup_last_path",
    "db_backup_last_size_bytes",
    "db_backup_last_duration_ms",
    "db_backup_last_trigger",       # scheduled | boot_pre_migration
)


class BackupRunError(RuntimeError):
    """A backup attempt failed in a way the run loop reports as `failed`."""


def effective_backup_retention_days() -> int:
    """THE one reader for ``backup_retention_days`` (D4 — one reader, one
    number). The generic ops readers coerce garbage → ``0`` → "sweep
    disabled", which is safe for row retention and catastrophic here
    (keep-forever = the #1871 disk-fill trap). This reader inverts: any
    unparseable or out-of-bounds stored value → **default 14 + WARNING**.

    ``GET /api/settings/retention`` excludes the key from its generic
    ``windows`` map and reports it only through this helper (the ``backup``
    block); the boot retention log special-cases it the same way — pinned by
    tests/unit/test_2216_backup_observability.py.
    """
    from database import db

    raw = db.get_setting_value(RETENTION_KEY, None)
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "[DBBackup] %s=%r unparseable — using default %d (inverted "
            "fail-safe: 0/garbage must not mean keep-forever)",
            RETENTION_KEY, raw, DEFAULT_RETENTION_DAYS,
        )
        return DEFAULT_RETENTION_DAYS
    if not (_RETENTION_MIN <= value <= _RETENTION_MAX):
        logger.warning(
            "[DBBackup] %s=%r out of bounds [%d, %d] — using default %d",
            RETENTION_KEY, raw, _RETENTION_MIN, _RETENTION_MAX,
            DEFAULT_RETENTION_DAYS,
        )
        return DEFAULT_RETENTION_DAYS
    return value


def _pg_conninfo_and_password(url_str: str) -> Tuple[str, Optional[str]]:
    """The operator's ``DATABASE_URL`` with exactly TWO rewrites (D2):

    1. SQLAlchemy driver suffix normalized away
       (``postgresql+psycopg2://`` → ``postgresql://`` — libpq rejects it);
    2. password stripped — it travels ONLY via subprocess-env ``PGPASSWORD``
       (argv is world-readable through /proc), never logged.

    Everything else — including query params like ``sslmode=require``,
    ``sslrootcert``, ``options`` — passes through untouched: re-parsing into
    ``-h/-p/-U`` flags would silently drop the SSL params managed PG (RDS,
    Cloud SQL) mandates, and backups would permanently fail exactly for
    external-PG installs.

    ``render_as_string`` edge cases handled deliberately:
    - ``hide_password=False`` (the default renders ``***`` — which libpq
      would then try as the literal password);
    - ``URL.set(password=None)`` KEEPS the old value (None means "unchanged"
      in `.set`), so the strip uses ``_replace(password=None)``.
    """
    from sqlalchemy.engine import make_url

    url = make_url(url_str)
    password = url.password  # raw (unescaped) — exactly what PGPASSWORD needs
    normalized = url.set(drivername="postgresql")
    stripped = normalized._replace(password=None)
    conninfo = stripped.render_as_string(hide_password=False)
    return conninfo, password


class DBBackupService:
    """Daily database backup for both backends (#2216)."""

    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not DB_BACKUP_ENABLED:
            logger.info("DB backup disabled (DB_BACKUP_ENABLED=false)")
            return
        # BOTH backends — unlike db_vacuum there is no is_sqlite() no-op arm.
        self.scheduler.add_job(
            self.run_backup,
            CronTrigger(hour=DB_BACKUP_HOUR, minute=DB_BACKUP_MINUTE),
            id="db_backup",
            name="Daily database backup",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        self.scheduler.start()
        logger.info(
            "DB backup scheduler started: daily at %02d:%02d UTC → %s",
            DB_BACKUP_HOUR, DB_BACKUP_MINUTE, self.backup_dir(),
        )

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("DB backup scheduler stopped")

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def backup_dir() -> str:
        # Read DB_PATH live (module attribute) — tests monkeypatch
        # db.connection.DB_PATH, and /data is the platform bind mount on
        # PostgreSQL installs too (TRINITY_DB_PATH still defaults there).
        return bp.default_backup_dir(db_connection.DB_PATH)

    @staticmethod
    def _sqlite_backup_and_verify(src: str, tmp: str) -> Dict[str, Any]:
        """Sync worker for ``asyncio.to_thread`` — the sqlite connections are
        opened AND closed inside (thread-affinity contract, see
        db/backup_primitives.py docstring)."""
        stats = bp.sqlite_backup_to(src, tmp)
        bp.verify_sqlite_backup(tmp)
        return stats

    # -- lease (duplicate-I/O suppression, never a correctness boundary) ---

    @staticmethod
    def _acquire_lease() -> Tuple[str, Optional[str]]:
        """→ ("acquired", token) | ("held", None) | ("unavailable", None).

        Fail-open: Redis down degrades to the day-key guard alone — worst
        case is one duplicate nightly I/O burst, atomic-replace-safe.
        """
        from redis_breaker_util import get_breaker_redis

        client = get_breaker_redis()
        if client is None:
            return "unavailable", None
        token = secrets.token_hex(16)
        try:
            ok = client.set(_LEASE_KEY, token, nx=True, ex=_LEASE_TTL_SECONDS)
        except Exception as exc:
            logger.warning("[DBBackup] lease acquire failed-open (%s)", exc)
            return "unavailable", None
        return ("acquired", token) if ok else ("held", None)

    @staticmethod
    def _release_lease(token: str) -> None:
        """Own-token compare-and-delete, attempted unconditionally (learnings
        2026-07-31: an ownership-checked release runs regardless of detected
        loss — it is foreign-safe by construction)."""
        from redis_breaker_util import get_breaker_redis, lock_token_matches

        client = get_breaker_redis()
        if client is None:
            return
        try:
            current = client.get(_LEASE_KEY)
            if lock_token_matches(current, token):
                client.delete(_LEASE_KEY)
        except Exception as exc:
            logger.warning("[DBBackup] lease release failed-open (%s)", exc)

    # -- status + alarms ---------------------------------------------------

    async def _record_outcome(
        self,
        status: str,
        *,
        trigger: str,
        error: str = "",
        path: Optional[str] = None,
        size_bytes: Optional[int] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        """Write durable status; fire the edge alarm on the success→failure
        transition (prior status read BEFORE the new one is written).

        Edge semantics (BKUP-013a): fire iff the new status is a failure kind
        (``failed`` / ``skipped_no_space``) AND the prior status was ``ok``
        or absent — exactly once per failure episode, re-armed only by an
        intervening success. Continued failure is the staleness re-alarm's
        job, not the edge's.
        """
        from database import db
        from utils.helpers import utc_now_iso

        now = utc_now_iso()

        def _write() -> Optional[str]:
            prior = db.get_setting_value("db_backup_last_status", None)
            db.set_setting("db_backup_last_status", status)
            db.set_setting("db_backup_last_error", (error or "")[:_STDERR_CAP])
            db.set_setting("db_backup_last_trigger", trigger)
            if db.get_setting_value("db_backup_first_attempt_at", None) is None:
                # Staleness reference for installs that have never succeeded —
                # without it a from-day-one failure would never re-alarm.
                db.set_setting("db_backup_first_attempt_at", now)
            if status == "ok":
                db.set_setting("db_backup_last_success_at", now)
                if path is not None:
                    db.set_setting("db_backup_last_path", path)
                if size_bytes is not None:
                    db.set_setting("db_backup_last_size_bytes", str(size_bytes))
                if duration_ms is not None:
                    db.set_setting("db_backup_last_duration_ms", str(duration_ms))
            return prior

        try:
            prior = await asyncio.to_thread(_write)
        except Exception:
            logger.exception("[DBBackup] could not persist status")
            return

        if status in ("failed", "skipped_no_space") and prior in (None, "ok"):
            self._emit_alarm(
                title=(
                    "Database backup skipped: not enough disk space"
                    if status == "skipped_no_space"
                    else "Database backup failed"
                ),
                question=(
                    f"The nightly database backup reported '{status}': "
                    f"{(error or 'no detail')[:500]} — the platform has no "
                    f"fresh recovery point until this is resolved. Backups "
                    f"land in {self.backup_dir()}."
                ),
                context={
                    "alert_type": "db_backup_failure",
                    "status": status,
                    "trigger": trigger,
                    "backup_dir": self.backup_dir(),
                },
            )

    def _emit_alarm(self, *, title: str, question: str, context: Dict) -> None:
        """Direct platform create on the sentinel host. Context carries
        status/paths/sizes only — never row data (canary G-04 rule)."""
        from database import db
        from utils.helpers import utc_now_iso

        now = utc_now_iso()
        item = {
            "id": f"{ALARM_ID_PREFIX}{now}",
            "type": "alert",
            "status": "pending",
            "priority": "high",
            "title": title,
            "question": question,
            "context": context,
            "created_at": now,
            # Must stay NULL: mark_operator_queue_expired flips any pending
            # row past expires_at to expired fleet-wide every 5s.
            "expires_at": None,
        }
        try:
            db.create_operator_queue_item(ALARM_AGENT_NAME, item)
            logger.warning("[DBBackup] operator alarm emitted: %s", title)
        except Exception:
            logger.exception("[DBBackup] failed to emit operator alarm")

    async def _staleness_check(self) -> None:
        """BKUP-013b: while the last success is older than 3 days, re-alarm at
        most once per 7 days. Never fires while fresh; never raises."""
        from database import db
        from utils.helpers import utc_now_iso

        def _read() -> Dict[str, Optional[str]]:
            return {
                "last_success_at": db.get_setting_value(
                    "db_backup_last_success_at", None),
                "first_attempt_at": db.get_setting_value(
                    "db_backup_first_attempt_at", None),
                "last_staleness_alarm_at": db.get_setting_value(
                    "db_backup_last_staleness_alarm_at", None),
            }

        try:
            state = await asyncio.to_thread(_read)
            reference = state["last_success_at"] or state["first_attempt_at"]
            if not reference:
                return
            now = datetime.now(timezone.utc)
            age_days = (now - _parse_iso(reference)).total_seconds() / 86400.0
            if age_days <= _STALENESS_ALARM_DAYS:
                return
            last_alarm = state["last_staleness_alarm_at"]
            if last_alarm is not None:
                alarm_age = (now - _parse_iso(last_alarm)).total_seconds() / 86400.0
                if alarm_age < _STALENESS_REALARM_DAYS:
                    return
            await asyncio.to_thread(
                db.set_setting, "db_backup_last_staleness_alarm_at",
                utc_now_iso(),
            )
            self._emit_alarm(
                title="Database backups are stale",
                question=(
                    f"The newest successful database backup is "
                    f"{age_days:.1f} days old (threshold "
                    f"{_STALENESS_ALARM_DAYS}d). Silence over time is also a "
                    f"failure — check db_backup_last_error and the backend "
                    f"logs. Backups land in {self.backup_dir()}."
                ),
                context={
                    "alert_type": "db_backup_stale",
                    "last_success_age_days": round(age_days, 1),
                    "backup_dir": self.backup_dir(),
                },
            )
        except Exception:
            logger.exception("[DBBackup] staleness check failed")

    # -- source-size estimate for the preflight ----------------------------

    async def _estimate_source_bytes(self, sqlite_backend: bool) -> int:
        if sqlite_backend:
            return await asyncio.to_thread(
                bp.sqlite_source_bytes, db_connection.DB_PATH
            )

        def _pg_size() -> int:
            from sqlalchemy import text
            from db.engine import get_engine
            with get_engine().connect() as c:
                # -Fc compresses, so this OVER-estimates — the safe direction.
                return int(
                    c.execute(
                        text("SELECT pg_database_size(current_database())")
                    ).scalar() or 0
                )

        try:
            return await asyncio.to_thread(_pg_size)
        except Exception as exc:
            logger.warning(
                "[DBBackup] could not estimate PG database size (%s) — "
                "proceeding; an actual ENOSPC fails loudly on its own", exc
            )
            return 0

    # -- PG arm ------------------------------------------------------------

    async def _pg_dump_to(
        self, conninfo: str, password: Optional[str], tmp_path: str
    ) -> Dict[str, Any]:
        start = time.monotonic()
        env = dict(os.environ)
        if password:
            # Env-only — never argv (world-readable /proc), never logged.
            env["PGPASSWORD"] = password
        # else: leave the environment untouched — a URL with no password means
        # peer/trust auth or an operator-provided PGPASSWORD already in env.
        try:
            proc = await asyncio.create_subprocess_exec(
                "pg_dump", "-Fc", "-d", conninfo, "-f", tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            raise BackupRunError(
                "pg_dump binary not found — this backend image predates "
                "#2216. Rebuild it (docker compose build backend); "
                "postgresql-client-17 is baked into docker/backend/Dockerfile."
            )
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(), PG_DUMP_TIMEOUT_SECONDS
            )
        except TimeoutError:
            # wait_for cancels the AWAIT, never the child: kill explicitly,
            # then reap (no zombie, no pinned PG connection). The partial tmp
            # is unlinked by the caller's finally.
            proc.kill()
            await proc.wait()
            raise BackupRunError(
                f"pg_dump timed out after {PG_DUMP_TIMEOUT_SECONDS}s "
                f"(child killed and reaped)"
            )
        if proc.returncode != 0:
            detail = (stderr or b"").decode("utf-8", "replace")[:_STDERR_CAP]
            raise BackupRunError(f"pg_dump exited {proc.returncode}: {detail}")

        def _verify() -> int:
            size = os.path.getsize(tmp_path)
            if size <= 0:
                raise BackupRunError("pg_dump produced an empty file")
            with open(tmp_path, "rb") as fh:
                if fh.read(5) != b"PGDMP":
                    raise BackupRunError(
                        "pg_dump output is missing the PGDMP magic bytes"
                    )
            return size

        size = await asyncio.to_thread(_verify)
        return {
            "duration_seconds": time.monotonic() - start,
            "size_bytes": size,
        }

    # -- the run -----------------------------------------------------------

    async def run_backup(self, *, trigger: str = "scheduled") -> Dict[str, Any]:
        """One backup attempt. Returns a summary dict for tests/manual use.

        Ordering (D1/D4): tmp sweep → day-key check → preflight → copy/dump →
        verify → atomic replace → status/alarm; then — in the tail, on EVERY
        attempt (success, failure or space-skip) — prune + staleness check.
        A lease-skip returns before the tail: the sibling holding the lease
        runs the identical tail itself.
        """
        if not DB_BACKUP_ENABLED:
            return {"status": "disabled"}

        started = time.monotonic()
        backup_dir = self.backup_dir()

        lease_state, token = self._acquire_lease()
        if lease_state == "held":
            logger.info("[DBBackup] skipped: another worker holds the lease")
            return {"status": "skipped_lease"}

        summary: Dict[str, Any] = {"status": "failed"}
        try:
            try:
                await asyncio.to_thread(bp.ensure_backup_dir, backup_dir)
                await asyncio.to_thread(bp.sweep_stale_tmps, backup_dir)

                from db.engine import is_sqlite
                sqlite_backend = is_sqlite()
                day = bp.utc_day_key()
                final_name = (
                    bp.sqlite_artifact_name(day) if sqlite_backend
                    else bp.pg_artifact_name(day)
                )
                final = str(Path(backup_dir) / final_name)

                # Day-keyed idempotence: the artifact name IS the day key.
                if os.path.exists(final):
                    logger.info(
                        "[DBBackup] skipped: today's artifact already exists "
                        "(%s)", final_name
                    )
                    summary = {"status": "skipped_exists", "path": final}
                    return summary

                # Free-space preflight (BKUP-008). Never prune-to-make-room.
                source_bytes = await self._estimate_source_bytes(sqlite_backend)
                free = shutil.disk_usage(backup_dir).free
                needed = int(source_bytes * _PREFLIGHT_HEADROOM)
                if source_bytes and free < needed:
                    msg = (
                        f"free space {free} B < required {needed} B "
                        f"(1.2x source {source_bytes} B) in {backup_dir}"
                    )
                    logger.warning("[DBBackup] skipped_no_space: %s", msg)
                    await self._record_outcome(
                        "skipped_no_space", trigger=trigger, error=msg
                    )
                    summary = {"status": "skipped_no_space", "error": msg}
                    return summary

                tmp: Optional[str] = bp.tmp_path_for(final)
                try:
                    if sqlite_backend:
                        stats = await asyncio.to_thread(
                            self._sqlite_backup_and_verify,
                            db_connection.DB_PATH, tmp,
                        )
                    else:
                        from db.engine import resolve_database_url
                        conninfo, password = _pg_conninfo_and_password(
                            resolve_database_url()
                        )
                        stats = await self._pg_dump_to(conninfo, password, tmp)
                    os.replace(tmp, final)
                    tmp = None
                finally:
                    if tmp is not None:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass

                duration_ms = int(stats["duration_seconds"] * 1000)
                await self._record_outcome(
                    "ok", trigger=trigger, path=final,
                    size_bytes=stats["size_bytes"], duration_ms=duration_ms,
                )
                logger.info(
                    "[DBBackup] backup complete: %s (%d bytes, %d ms)",
                    final, stats["size_bytes"], duration_ms,
                )
                summary = {"status": "ok", "path": final, **stats}
                return summary

            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                logger.error("[DBBackup] backup FAILED: %s", msg)
                await self._record_outcome("failed", trigger=trigger, error=msg)
                summary = {"status": "failed", "error": msg}
                return summary
        finally:
            # Tail: prune on EVERY attempt (the floor carries the safety,
            # D4) + the staleness re-alarm. Fail-open — the outcome above is
            # already recorded.
            try:
                days = await asyncio.to_thread(effective_backup_retention_days)
                await asyncio.to_thread(bp.prune_backups, backup_dir, days)
            except Exception:
                logger.exception("[DBBackup] prune tail failed")
            try:
                await self._staleness_check()
            except Exception:
                logger.exception("[DBBackup] staleness tail failed")
            if token:
                self._release_lease(token)
            elapsed = time.monotonic() - started
            if elapsed > 0.8 * _LEASE_TTL_SECONDS:
                logger.warning(
                    "[DBBackup] run took %.0fs — over 80%% of the %ds lease "
                    "TTL (derived from DB_BACKUP_PG_DUMP_TIMEOUT_SECONDS). "
                    "A run that outlives its lease degrades to the day-key "
                    "guard alone; consider raising the timeout knob.",
                    elapsed, _LEASE_TTL_SECONDS,
                )


def _parse_iso(value: str) -> datetime:
    """ISO-Z tolerant parse → aware UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def build_backup_status_block() -> Dict[str, Any]:
    """The ``backup`` block for ``GET /api/settings/retention`` (BKUP-014).

    Blocking reads (settings + dir listing) run via ``asyncio.to_thread`` —
    the same discipline D1 applies to the copy itself. Note this shares the
    default executor with Starlette's sync-route pool (acceptable; a bounded
    dir listing occupies one slot for milliseconds).
    """
    from database import db

    backup_dir = DBBackupService.backup_dir()

    def _read_status() -> Dict[str, Optional[str]]:
        out = {k: db.get_setting_value(k, None) for k in STATUS_KEYS}
        out["retention_days"] = effective_backup_retention_days()
        return out

    def _listing() -> Dict[str, Any]:
        files = []
        root = Path(backup_dir)
        if root.is_dir():
            import fnmatch
            for entry in root.iterdir():
                if entry.is_file() and any(
                    fnmatch.fnmatch(entry.name, pat)
                    for pat in bp.ARTIFACT_PATTERNS
                ):
                    stat = entry.stat()
                    files.append((entry.name, stat.st_size, stat.st_mtime))
        files.sort(key=lambda f: f[2], reverse=True)
        newest = files[0] if files else None
        return {
            "count": len(files),
            "total_bytes": sum(f[1] for f in files),
            "newest": newest[0] if newest else None,
            "newest_age_seconds": (
                int(time.time() - newest[2]) if newest else None
            ),
        }

    status = await asyncio.to_thread(_read_status)
    artifacts = await asyncio.to_thread(_listing)

    last_success_at = status.get("db_backup_last_success_at")
    age_days: Optional[float] = None
    if last_success_at:
        try:
            age_days = round(
                (datetime.now(timezone.utc) - _parse_iso(last_success_at))
                .total_seconds() / 86400.0,
                2,
            )
        except ValueError:
            age_days = None

    def _int_or_none(value: Optional[str]) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "enabled": DB_BACKUP_ENABLED,
        "schedule_utc": f"{DB_BACKUP_HOUR:02d}:{DB_BACKUP_MINUTE:02d}",
        "backup_dir": backup_dir,
        # Same-disk boundary, machine-readable — the docs state it in prose;
        # a future UI must not have to parse prose (BKUP-003).
        "scope": "same-disk",
        "retention_days": status["retention_days"],
        "min_keep": bp.BACKUP_MIN_KEEP,
        "last_status": status.get("db_backup_last_status"),
        "last_success_at": last_success_at,
        "last_success_age_days": age_days,
        "stale": bool(age_days is not None and age_days > _STALENESS_ALARM_DAYS),
        "last_error": status.get("db_backup_last_error") or None,
        "last_path": status.get("db_backup_last_path"),
        "last_size_bytes": _int_or_none(status.get("db_backup_last_size_bytes")),
        "last_duration_ms": _int_or_none(
            status.get("db_backup_last_duration_ms")),
        "last_trigger": status.get("db_backup_last_trigger"),
        "artifacts": artifacts,
    }


db_backup_service = DBBackupService()
