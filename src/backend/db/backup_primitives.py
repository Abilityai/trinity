"""Database-backup primitives (#2216) — stdlib-only leaf.

The copy/verify/prune mechanics shared by the daily backup job
(``services/db_backup_service.py``) and the boot-time pre-migration hook
(called from ``database.init_database()``).

IMPORT-GRAPH RULE (load-bearing): this module must NOT import ``database``
or anything under ``services/`` — ``maybe_backup_before_migrations`` runs
inside ``init_database()``, which executes at import time (the
``db/migration_lock.py`` dependency-light precedent). Stdlib only, plus a
lazy in-function import of ``db.migrations.migration_health`` (itself
stdlib + ``utils.helpers``).

Why ``sqlite3.Connection.backup()`` and never ``cp`` (D1, #2216):

- One-shot (default ``pages=-1``) runs a single ``sqlite3_backup_step(-1)``
  inside one read transaction ⇒ a consistent snapshot as of backup start,
  standalone ``.db``, no ``-wal``/``-journal`` sidecars.
- The platform DB runs SQLite's default **DELETE (rollback-journal) mode**
  — verified against a live instance (``PRAGMA journal_mode`` → ``delete``;
  nothing in the codebase ever sets ``journal_mode``). In DELETE mode the
  one-shot copy holds a read lock for the copy duration, so a concurrent
  writer waits up to its 30s busy timeout — acceptable at the 03:30 UTC
  off-peak window, and a pathological multi-GB copy that exceeds it fails
  a concurrent write *loudly* rather than corrupting anything. (Under a
  hypothetical future WAL mode the same call never blocks writers.)
- Incremental (``pages=N``) was rejected: it releases the lock between
  steps but restarts from scratch whenever another connection writes —
  under sustained writes that livelocks, worse than a bounded hold.
- A copy exceeding ``_COPY_DURATION_WARN_SECONDS`` logs a WARNING naming
  the approaching 30s busy-timeout wall (the tripwire for the lock-hold
  cost in both directions — our hold and sqlite's internal 0.25s retry).

THREAD-AFFINITY CONTRACT (learnings 2026-07-20): ``sqlite_backup_to`` is a
pure *synchronous* function that opens AND closes both sqlite3 connections
inside itself. The service calls ``await asyncio.to_thread(...)``; no
connection object ever crosses a thread.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Fixed floor: prune NEVER deletes the newest this-many artifacts, regardless
# of age or window. Deliberately a constant and NOT a knob (#1644 rationale: a
# control that must explain which way is safe is the wrong control) — a
# `backup_retention_days=1` slip must still leave recovery points. Pinned ≥ 2
# by tests/unit/test_2216_backup_primitives.py; lowering below that is a
# reviewed decision, not a one-line diff.
BACKUP_MIN_KEEP = 3

# DELETE-mode lock-hold tripwire: past this, log a WARNING naming the 30s
# busy-timeout wall a concurrent writer would hit (D1, #2216).
_COPY_DURATION_WARN_SECONDS = 20.0

# Crash-window sweep age for orphaned `*.tmp.*` files (SIGKILL mid-copy skips
# `finally`; an unswept orphan is unbounded disk growth in a dir the
# pattern-scoped prune deliberately won't touch).
TMP_MAX_AGE_SECONDS = 24 * 3600

# Prune scans ONLY these patterns — never arbitrary files in the backup dir.
ARTIFACT_PATTERNS = (
    "trinity-backup-*.db",
    "trinity-backup-*.dump",
    "pre-migration-*.db",
)


class BackupVerificationError(RuntimeError):
    """The produced copy failed integrity verification — it must never be
    renamed into the artifact namespace."""


# --------------------------------------------------------------------------
# Naming / paths
# --------------------------------------------------------------------------

def default_backup_dir(db_path: str) -> str:
    """``<db dir>/backups`` — lands at ``/data/backups/`` in production
    (``TRINITY_DB_PATH=/data/trinity.db`` in both compose files) and beside
    the scratch DB in tests. Also correct for PostgreSQL installs: ``/data``
    is still the platform bind mount there."""
    return str(Path(db_path).resolve().parent / "backups")


def utc_day_key(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%d")


def sqlite_artifact_name(day_key: str) -> str:
    return f"trinity-backup-{day_key}.db"


def pg_artifact_name(day_key: str) -> str:
    return f"trinity-backup-{day_key}.dump"


def pre_migration_artifact_name(now: Optional[datetime] = None) -> str:
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"pre-migration-{ts}.db"


def tmp_path_for(final_path: str) -> str:
    """Pid-suffixed tmp beside the final artifact. Two concurrent writers get
    distinct tmps; the atomic ``os.replace`` makes the residual same-name race
    last-writer-wins on one intact file, never a torn artifact."""
    return f"{final_path}.tmp.{os.getpid()}"


def ensure_backup_dir(backup_dir: str) -> None:
    """Create the backup dir 0700 (the artifact is the full DB)."""
    Path(backup_dir).mkdir(mode=0o700, parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# SQLite copy + verify
# --------------------------------------------------------------------------

def sqlite_backup_to(src_path: str, dest_path: str) -> Dict[str, Any]:
    """Consistent online copy of ``src_path`` into ``dest_path``. SYNCHRONOUS
    — call via ``asyncio.to_thread``; both connections are opened and closed
    inside this function (thread-affinity contract, module docstring)."""
    start = time.monotonic()
    src = sqlite3.connect(src_path, timeout=30.0)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            # pages=-1 (default): one sqlite3_backup_step(-1) — a single
            # consistent read transaction. See module docstring for the
            # DELETE-mode lock-hold semantics.
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    duration = time.monotonic() - start
    if duration >= _COPY_DURATION_WARN_SECONDS:
        logger.warning(
            "[DBBackup] SQLite copy took %.1fs — approaching the 30s busy "
            "timeout a concurrent writer would hit in DELETE journal mode. "
            "Remedies: migrate to WAL (separate decision), PostgreSQL (#1278).",
            duration,
        )
    size = os.path.getsize(dest_path)
    return {"duration_seconds": duration, "size_bytes": size}


def verify_sqlite_backup(path: str) -> None:
    """Integrity-check a produced copy BEFORE it is renamed into the artifact
    namespace. Raises :class:`BackupVerificationError` on any failure — a
    backup of a corrupt source must never evict a healthy artifact (D4)."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupVerificationError(f"cannot open copy: {exc}") from exc
    try:
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as exc:
            raise BackupVerificationError(f"quick_check failed: {exc}") from exc
        if row is None or row[0] != "ok":
            raise BackupVerificationError(
                f"quick_check reported {row[0] if row else 'no result'!r}"
            )
        count = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        if count <= 0:
            raise BackupVerificationError("copy has an empty sqlite_master")
    finally:
        conn.close()


def sqlite_source_bytes(db_path: str) -> int:
    """Source size for the free-space preflight: main file + any sidecars."""
    total = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            total += os.path.getsize(db_path + suffix)
        except OSError:
            continue
    return total


# --------------------------------------------------------------------------
# Prune + tmp sweep
# --------------------------------------------------------------------------

def _artifact_files(backup_dir: str) -> List[Path]:
    root = Path(backup_dir)
    if not root.is_dir():
        return []
    out: List[Path] = []
    for entry in root.iterdir():
        if not entry.is_file():
            continue
        if any(fnmatch.fnmatch(entry.name, pat) for pat in ARTIFACT_PATTERNS):
            out.append(entry)
    return out


def prune_backups(
    backup_dir: str,
    retention_days: int,
    *,
    min_keep: int = BACKUP_MIN_KEEP,
    now: Optional[float] = None,
) -> List[str]:
    """Delete artifacts older than ``retention_days``, NEVER touching the
    newest ``min_keep`` regardless of age, NEVER touching non-artifact
    patterns, and never deleting inside the window regardless of disk
    pressure (no prune-to-make-room, D4).

    Runs on EVERY scheduled attempt — success, failure or space-skip — so a
    lowered window frees space even while backups are skipping (the Catch-22
    the prune-only-on-success draft had). Safety is carried entirely by the
    bounds above: a failed copy never entered the artifact namespace, so it
    cannot displace anything.

    Returns the deleted file names (identifiers only — logged by name).
    """
    if retention_days <= 0:
        # Defensive: the validated knob can never be ≤ 0 (bounds 1–3650); a
        # direct DB write that smuggles one in must not mean "delete all".
        logger.warning(
            "[DBBackup] prune skipped: non-positive retention_days=%r",
            retention_days,
        )
        return []
    now_ts = now if now is not None else time.time()
    cutoff = now_ts - retention_days * 86400

    files = _artifact_files(backup_dir)
    # Newest first by mtime; the head `min_keep` are untouchable.
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    deleted: List[str] = []
    for candidate in files[max(min_keep, 0):]:
        try:
            if candidate.stat().st_mtime >= cutoff:
                continue  # inside the window — kept regardless of pressure
            candidate.unlink()
            deleted.append(candidate.name)
        except FileNotFoundError:
            continue  # a concurrent prune won the race — fine
        except OSError as exc:
            logger.warning("[DBBackup] prune could not delete %s: %s",
                           candidate.name, exc)
    if deleted:
        logger.info("[DBBackup] pruned %d artifact(s): %s",
                    len(deleted), ", ".join(sorted(deleted)))
    return deleted


def sweep_stale_tmps(
    backup_dir: str,
    *,
    max_age_seconds: float = TMP_MAX_AGE_SECONDS,
    now: Optional[float] = None,
) -> List[str]:
    """Reclaim ``*.tmp.*`` orphans older than the crash window (BKUP-009)."""
    root = Path(backup_dir)
    if not root.is_dir():
        return []
    now_ts = now if now is not None else time.time()
    removed: List[str] = []
    for entry in root.iterdir():
        if not entry.is_file() or ".tmp." not in entry.name:
            continue
        try:
            if now_ts - entry.stat().st_mtime <= max_age_seconds:
                continue  # fresh — may belong to a live run
            entry.unlink()
            removed.append(entry.name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("[DBBackup] tmp sweep could not delete %s: %s",
                           entry.name, exc)
    if removed:
        logger.info("[DBBackup] swept %d stale tmp file(s): %s",
                    len(removed), ", ".join(sorted(removed)))
    return removed


# --------------------------------------------------------------------------
# Boot-time pre-migration backup (BKUP-012)
# --------------------------------------------------------------------------

def _write_boot_status_row(cursor, conn, status: str, detail: str) -> None:
    """Best-effort durable status for the boot hook. Hand-rolled SQL by
    necessity — this runs inside ``init_database()`` before the ``db`` facade
    exists (circular-import trap). Kept to the two status keys; the service
    module owns the full key set (drift risk noted there, D6)."""
    try:
        from utils.helpers import utc_now_iso
        now = utc_now_iso()
        cursor.execute(
            "INSERT OR REPLACE INTO system_settings (key, value, updated_at) "
            "VALUES (?, ?, ?)",
            ("db_backup_last_status", status, now),
        )
        cursor.execute(
            "INSERT OR REPLACE INTO system_settings (key, value, updated_at) "
            "VALUES (?, ?, ?)",
            ("db_backup_last_error", detail[:2000], now),
        )
        conn.commit()
    except Exception:
        # On the corrupt-DB path even this write fails — that's fine, the
        # ERROR log already happened and the migration runner is about to
        # surface the real failure.
        logger.debug("[DBBackup] boot status row write failed (best-effort)")


def maybe_backup_before_migrations(
    cursor,
    conn,
    *,
    db_path: str,
    backup_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Pre-migration safety copy (SQLite arm only, BKUP-012).

    Called from ``init_database()`` inside the ``migration_lock`` window,
    BEFORE the first ``run_all_migrations`` pass. A schema migration is the
    one moment the platform *knows* is risky — the exact "before major
    changes" the retired manual guidance pointed at.

    CONTRACT — fail-open, never crash-loops boot (#1638 seed shape:
    ``init_database`` runs at import). Two skip classes are DISTINCT
    branches, not one blanket except:

    - fresh install (``schema_migrations`` absent/empty) → silent skip at
      INFO — a first boot must not emit a scary ERROR;
    - corrupt DB file (``sqlite3.DatabaseError``) → ERROR + best-effort
      status row, then RETURN — the subsequent ``run_all_migrations`` on the
      same connection raises the SAME exception fingerprint as before this
      feature existed (the incident's uvicorn-dies-at-import signature is
      pinned unchanged by test).

    Returns a summary dict when a backup was taken, else ``None``.
    """
    resolved_dir = backup_dir or default_backup_dir(db_path)
    tmp: Optional[str] = None
    try:
        # Gate 1: migrations actually pending? (cheap — one SELECT over
        # schema_migrations). Fresh installs raise "no such table" here.
        from db.migrations import migration_health
        try:
            applied, _expected, first_pending = migration_health(cursor)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                logger.info(
                    "[DBBackup] pre-migration backup skipped: fresh install "
                    "(no schema_migrations yet — nothing worth copying)"
                )
                return None
            raise
        if first_pending is None:
            return None  # nothing pending — the common no-op boot, silent
        if applied == 0:
            # schema_migrations exists but empty ⇒ effectively fresh
            # (init_schema hasn't recorded anything) — nothing worth copying.
            logger.info(
                "[DBBackup] pre-migration backup skipped: empty "
                "schema_migrations (fresh install)"
            )
            return None

        # Gate 2: real file with real content (a fresh SQLite file is 0 bytes
        # or a single 4 KiB header page).
        try:
            src_size = os.path.getsize(db_path)
        except OSError:
            logger.info("[DBBackup] pre-migration backup skipped: DB file "
                        "not found at %s", db_path)
            return None
        if src_size < 4096:
            logger.info("[DBBackup] pre-migration backup skipped: DB file "
                        "trivial (%d bytes)", src_size)
            return None

        ensure_backup_dir(resolved_dir)
        final = str(Path(resolved_dir) / pre_migration_artifact_name())
        tmp = tmp_path_for(final)
        stats = sqlite_backup_to(db_path, tmp)
        verify_sqlite_backup(tmp)
        os.replace(tmp, final)
        tmp = None
        logger.info(
            "[DBBackup] pre-migration backup taken before %r: %s (%d bytes, "
            "%.2fs)",
            first_pending, final, stats["size_bytes"], stats["duration_seconds"],
        )
        _write_boot_status_row(cursor, conn, "ok", "")
        # The service module reads these two extra keys too; write them here
        # so the boot path leaves a complete "last run" picture.
        try:
            from utils.helpers import utc_now_iso
            now = utc_now_iso()
            for key, value in (
                ("db_backup_last_success_at", now),
                ("db_backup_last_path", final),
                ("db_backup_last_size_bytes", str(stats["size_bytes"])),
                ("db_backup_last_trigger", "boot_pre_migration"),
            ):
                cursor.execute(
                    "INSERT OR REPLACE INTO system_settings "
                    "(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )
            conn.commit()
        except Exception:
            logger.debug("[DBBackup] boot status detail write failed "
                         "(best-effort)")
        return {"status": "ok", "path": final, **stats}

    except sqlite3.DatabaseError as exc:
        # Corrupt DB (e.g. the incident's overwritten header): loud, durable
        # if possible, and NEVER propagated — the migration runner right after
        # us fails with the unchanged incident fingerprint.
        logger.error(
            "[DBBackup] pre-migration backup FAILED (%s: %s). Boot continues; "
            "healthy artifacts in %s are untouched.",
            type(exc).__name__, exc, resolved_dir,
        )
        _write_boot_status_row(cursor, conn, "failed", f"{type(exc).__name__}: {exc}")
        return None
    except Exception as exc:
        logger.error(
            "[DBBackup] pre-migration backup FAILED (%s: %s). Boot continues.",
            type(exc).__name__, exc,
        )
        _write_boot_status_row(cursor, conn, "failed", f"{type(exc).__name__}: {exc}")
        return None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
