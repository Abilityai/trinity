"""Alembic runner for the PostgreSQL backend (#1183).

Invoked from ``database.init_database()`` on the non-SQLite path instead of the
old ``init_schema_postgres`` fresh-build. SQLite keeps the legacy bespoke
``db/migrations.py`` runner — the two coexist during the Postgres transition.

Adoption (one-time) handling:
  - fresh PG DB (no tables)           -> ``upgrade head`` runs the baseline +
                                         any later revisions, building the
                                         full schema.
  - pre-Alembic PG DB (built by the
    old ``init_schema_postgres``, no
    ``alembic_version`` table)        -> ``stamp 0001_baseline`` (its schema IS
                                         the baseline), then ``upgrade head``
                                         applies anything added after baseline.
  - already-managed PG DB             -> ``upgrade head`` applies pending
                                         revisions.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from db.engine import get_engine, resolve_database_url

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_BASELINE_REVISION = "0001_baseline"
# Any core OSS table that the baseline creates — used to detect a pre-Alembic
# database that already has the schema but no alembic_version table.
_CORE_TABLE = "users"
# Fixed 64-bit key for the PostgreSQL session advisory lock that serialises
# concurrent upgrade_to_head() across uvicorn workers + the scheduler container
# sharing one database (#1425). The SQLite path uses an OS flock
# (db/migration_lock.py), but that only serialises processes on a shared local
# filesystem — it does NOT cover the Alembic-on-PG path, where two workers both
# entered command.upgrade() and deadlocked inside a revision. pg_advisory_lock
# is cross-connection and cross-host (keyed by the number, not tied to any one
# connection), so it is the correct primitive for the server-based backend.
# Hex spelling of "trinmig"; well within signed-bigint range.
_ADVISORY_LOCK_KEY = 0x7472696E6D6967


@contextmanager
def _pg_migration_lock(engine):
    """Serialise the migration run with a PostgreSQL session advisory lock.

    Holds a dedicated connection for the lock's lifetime so Alembic's own
    connections (checked out separately from the pool) don't affect it — the
    lock is global to the database, keyed by ``_ADVISORY_LOCK_KEY``. Blocks
    with no timeout (like the SQLite flock): the holder only runs the fast
    migration suite, and PostgreSQL releases a session advisory lock
    automatically when the connection closes, so a crashed holder never leaves
    a stale lock. Fails open — if the lock can't be taken (e.g. transient error
    at acquire), the caller proceeds unlocked rather than blocking boot, no
    worse than the pre-#1425 status quo.
    """
    conn = None
    try:
        conn = engine.connect()
        conn.exec_driver_sql("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    except Exception as e:  # pragma: no cover - defensive fail-open
        logger.warning("PG migration advisory lock unavailable (%s); proceeding without it", e)
        if conn is not None:
            conn.close()
            conn = None
    try:
        yield
    finally:
        if conn is not None:
            try:
                conn.exec_driver_sql("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
            except Exception:  # pragma: no cover - close() releases it anyway
                pass
            finally:
                conn.close()


def _config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", resolve_database_url())
    return cfg


def upgrade_to_head() -> None:
    """Bring the active PostgreSQL schema to head via Alembic.

    Serialised across concurrent boots (multi-worker uvicorn + the scheduler
    container) by a PG session advisory lock (#1425) — the SQLite flock in
    db/migration_lock.py does not reach this path, which let two workers race
    command.upgrade() into a deadlock inside a revision.
    """
    engine = get_engine()

    # Only PostgreSQL reaches here in practice (SQLite keeps the bespoke runner),
    # but guard the lock on the dialect so any other server backend degrades to
    # the prior unlocked behaviour rather than issuing PG-only SQL.
    lock = _pg_migration_lock(engine) if engine.dialect.name == "postgresql" else _noop_lock()
    with lock:
        insp = inspect(engine)
        has_version = insp.has_table("alembic_version")
        has_core = insp.has_table(_CORE_TABLE)

        cfg = _config()
        if not has_version and has_core:
            logger.info(
                "Alembic: pre-Alembic PG schema detected (no alembic_version) — "
                "stamping %s before upgrade", _BASELINE_REVISION,
            )
            command.stamp(cfg, _BASELINE_REVISION)

        command.upgrade(cfg, "head")
        logger.info("Alembic: PostgreSQL schema at head")


@contextmanager
def _noop_lock():
    """No-op fallback lock for a non-PostgreSQL server backend."""
    yield
