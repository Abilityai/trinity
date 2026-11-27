"""What the backend's health endpoints report (#4407).

Two questions, kept apart:

* **Liveness** (`GET /health`): is this process up with a complete schema?
  It must stay cheap and must not fail because a dependency blinked. The only
  thing that turns it unhealthy is a schema that is known to be incomplete.
* **Readiness** (`GET /health/ready`): can this process do its work right now?
  It probes the database, Redis and Docker and reports each one, plus when each
  background loop last completed a cycle.

Everything here is synchronous and is run off the event loop by the endpoints.
No function raises, and none returns an error string from a dependency: these
endpoints are unauthenticated, so a probe answers with a fixed word and the
detail goes to the log.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parent.parent

# Schema states.
AT_HEAD = "at_head"
BEHIND = "behind"
UNKNOWN = "unknown"

# Probe answers.
OK = "ok"
UNREACHABLE = "unreachable"
UNAVAILABLE = "unavailable"
TIMEOUT = "timeout"

PROBE_TIMEOUT_SECONDS = 3.0
# One readiness answer is reused for this long, so the unauthenticated endpoint
# cannot be turned into a stream of dependency probes.
READINESS_CACHE_SECONDS = 2.0

_lock = threading.Lock()
# A schema seen at head stays at head for the life of the process: revisions
# only change with a deploy, which restarts it. Remembering that keeps liveness
# off the database once the gate has passed.
_schema_confirmed = False
_script_heads: Optional[List[str]] = None
_redis_client = None
_readiness_cache: Optional[tuple] = None  # (monotonic, payload)


def reset_for_tests() -> None:
    global _schema_confirmed, _script_heads, _redis_client, _readiness_cache
    with _lock:
        _schema_confirmed = False
        _script_heads = None
        _redis_client = None
        _readiness_cache = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _alembic_script_heads() -> List[str]:
    """The head revision(s) of the revision files shipped in this image."""
    global _script_heads
    if _script_heads is None:
        from alembic.script import ScriptDirectory

        heads = sorted(ScriptDirectory(str(_BACKEND_DIR / "migrations")).get_heads())
        _script_heads = heads
    return list(_script_heads)


def _alembic_database_heads() -> List[str]:
    """The revision(s) the database says it is at; empty when it was never stamped."""
    from sqlalchemy import inspect, text

    from db.engine import get_engine

    engine = get_engine()
    with engine.connect() as conn:
        if not inspect(conn).has_table("alembic_version"):
            return []
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    return sorted(str(row[0]) for row in rows)


def _is_connection_fault(exc: BaseException) -> bool:
    """A database that could not be asked, as opposed to one that answered."""
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    if isinstance(exc, (OperationalError, InterfaceError, PoolTimeout, OSError)):
        return True
    return isinstance(exc, DBAPIError) and bool(getattr(exc, "connection_invalidated", False))


def postgres_schema_state() -> Dict[str, Any]:
    """Is the PostgreSQL schema at the Alembic head this image ships?

    ``at_head``: the database's revisions equal the shipped heads.
    ``behind``: the database answered and they differ (or it was never stamped).
    ``unknown``: the database could not be asked. That is not evidence of a
    bad schema, so liveness does not fail on it.
    """
    global _schema_confirmed
    if _schema_confirmed:
        return {"state": AT_HEAD}
    try:
        head = _alembic_script_heads()
    except Exception as e:  # noqa: BLE001
        logger.error("health: could not read the Alembic revision files: %s", e)
        return {"state": BEHIND, "current": [], "head": []}
    try:
        current = _alembic_database_heads()
    except Exception as e:  # noqa: BLE001
        if _is_connection_fault(e):
            logger.warning("health: could not ask the database for its revision: %s", e)
            return {"state": UNKNOWN}
        logger.error("health: reading alembic_version failed: %s", e)
        return {"state": BEHIND, "current": [], "head": head}
    if head and current == head:
        _schema_confirmed = True
        return {"state": AT_HEAD}
    return {"state": BEHIND, "current": current, "head": head}


def sqlite_schema_state() -> Dict[str, Any]:
    """The SQLite migration gate, in the same shape (the #1160 check)."""
    from db.connection import get_db_connection
    from db.migrations import MIGRATIONS, migration_health

    try:
        with get_db_connection() as conn:
            applied, expected, first_pending = migration_health(conn.cursor())
    except Exception as e:  # noqa: BLE001
        logger.warning("health: could not query schema_migrations: %s", e)
        applied, expected = 0, len(MIGRATIONS)
        first_pending = MIGRATIONS[0][0] if MIGRATIONS else None
    if applied < expected:
        return {"state": BEHIND, "applied": applied, "expected": expected,
                "first_pending": first_pending}
    return {"state": AT_HEAD}


def schema_state() -> Dict[str, Any]:
    from db.engine import is_sqlite

    return sqlite_schema_state() if is_sqlite() else postgres_schema_state()


# ---------------------------------------------------------------------------
# Dependency probes
# ---------------------------------------------------------------------------

def check_database() -> str:
    try:
        from sqlalchemy import text

        from db.engine import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1")).first()
        return OK
    except Exception as e:  # noqa: BLE001
        logger.warning("health: database probe failed: %s", e)
        return UNREACHABLE


def _redis():
    global _redis_client
    if _redis_client is None:
        import redis

        from config import REDIS_URL

        _redis_client = redis.from_url(
            REDIS_URL, socket_connect_timeout=2, socket_timeout=2,
        )
    return _redis_client


def check_redis() -> str:
    try:
        _redis().ping()
        return OK
    except Exception as e:  # noqa: BLE001
        logger.warning("health: Redis probe failed: %s", type(e).__name__)
        return UNREACHABLE


def check_docker() -> str:
    try:
        from services import docker_service

        client = docker_service.docker_client
        if client is None:
            return UNAVAILABLE
        client.ping()
        return OK
    except Exception as e:  # noqa: BLE001
        logger.warning("health: Docker probe failed: %s", e)
        return UNREACHABLE


def loop_ticks(now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """Each background loop of THIS worker that has completed a cycle: when,
    how long ago, and how long the cycle took. A loop that has never completed
    one is absent; a loop that stopped shows a growing age."""
    from services import loop_health
    from utils.helpers import to_utc_iso
    from datetime import datetime, timezone

    now = time.time() if now is None else now
    last = loop_health.last_tick_times()
    out: Dict[str, Dict[str, Any]] = {}
    for name, entry in loop_health.tick_snapshot().items():
        at = last.get(name)
        out[name] = {
            "last_tick_at": (
                to_utc_iso(datetime.fromtimestamp(at, tz=timezone.utc)) if at else None
            ),
            "age_seconds": round(max(0.0, now - at), 1) if at else None,
            "last_ms": entry.get("last_ms"),
            "ticks": entry.get("ticks"),
        }
    return out


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

async def _probe(fn: Callable[[], Any], on_timeout: Any) -> Any:
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn), PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return on_timeout
    except Exception as e:  # noqa: BLE001 — a probe never breaks the endpoint
        logger.warning("health: probe %s raised: %s", getattr(fn, "__name__", fn), e)
        return on_timeout


async def readiness() -> Dict[str, Any]:
    """Probe every dependency at once, each bounded, and say which is down.

    ``ready`` is False when the database, Redis or Docker is not ``ok`` or the
    schema is known to be behind. Loop ticks are reported and never decide it:
    loops run on different cadences and some only on the worker that holds a
    lease, so "old" has no single meaning here.
    """
    global _readiness_cache
    cached = _readiness_cache
    if cached and (time.monotonic() - cached[0]) < READINESS_CACHE_SECONDS:
        return cached[1]

    database, redis_state, docker_state, schema = await asyncio.gather(
        _probe(check_database, TIMEOUT),
        _probe(check_redis, TIMEOUT),
        _probe(check_docker, TIMEOUT),
        _probe(schema_state, {"state": UNKNOWN}),
    )
    checks = {
        "database": database,
        "redis": redis_state,
        "docker": docker_state,
        "schema": schema.get("state", UNKNOWN),
    }
    ready = (
        database == OK and redis_state == OK and docker_state == OK
        and checks["schema"] != BEHIND
    )
    try:
        loops = loop_ticks()
    except Exception as e:  # noqa: BLE001
        logger.warning("health: could not read loop ticks: %s", e)
        loops = {}
    payload = {"ready": ready, "checks": checks, "loops": loops}
    _readiness_cache = (time.monotonic(), payload)
    return payload
