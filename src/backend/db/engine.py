"""
SQLAlchemy engine factory — the configurable database backend seam (#300).

`DATABASE_URL` selects the backend at startup:

    sqlite:////data/trinity.db                 (default; derived from TRINITY_DB_PATH)
    postgresql://trinity:secret@postgres:5432/trinity

SQLite stays the zero-config default — nothing changes without DATABASE_URL.

All 44 db modules route through this engine via SQLAlchemy Core. The raw
sqlite3 context manager in `db/connection.py` remains for sqlite-specific
maintenance paths (PRAGMA migrations, WAL checkpoint, VACUUM, the /health
migration gate — all gated to `is_sqlite()` at their call sites) and for the
default-off canary snapshot reader (PG support is a #300 follow-up).
"""

import os
import threading
from typing import Dict

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool

# Engines are cached per resolved URL (not a single global) so that test
# suites pointing TRINITY_DB_PATH/DATABASE_URL at different throwaway files
# each get their own engine instead of sharing the first one created.
_engines: Dict[str, Engine] = {}
_lock = threading.Lock()


def resolve_database_url() -> str:
    """Active DATABASE_URL, defaulting to SQLite derived from TRINITY_DB_PATH."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    db_path = os.getenv("TRINITY_DB_PATH", "/data/trinity.db")
    # Three leading slashes + an absolute path == four total: sqlite:////abs
    return f"sqlite:///{db_path}"


def is_sqlite(url: str | None = None) -> bool:
    return make_url(url or resolve_database_url()).get_backend_name() == "sqlite"


def _build_engine(url: str) -> Engine:
    # hide_parameters=True: keep bound values OUT of DBAPIError.__str__ (the
    # `[parameters: [...]]` tail) so a statement error's traceback can't leak
    # user content — chat message bodies, emails, credentials — into logs when
    # a caller logs it with exc_info=True / logger.exception (#1444). The masking
    # this platform does at the app layer is defeated if SQLAlchemy re-prints the
    # values in an error; this closes that class engine-wide. Load-bearing for
    # the fail-loud persistence path in routers/chat.py — do not remove.
    if make_url(url).get_backend_name() == "sqlite":
        # NullPool: one connection per checkout, opened and closed on demand —
        # matches the legacy connection.py semantics exactly (no shared
        # cross-thread sqlite handle). check_same_thread is disabled because
        # the pool may check a connection in on a different thread than it was
        # checked out on under a threaded uvicorn worker.
        return create_engine(
            url,
            poolclass=NullPool,
            connect_args={"timeout": 30.0, "check_same_thread": False},
            future=True,
            hide_parameters=True,
        )
    # Server-based backends (PostgreSQL): real connection pooling.
    return create_engine(
        url,
        pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
        pool_pre_ping=True,
        future=True,
        hide_parameters=True,
    )


def get_engine() -> Engine:
    """Process-wide engine for the active DATABASE_URL (cached per URL)."""
    url = resolve_database_url()
    engine = _engines.get(url)
    if engine is None:
        with _lock:
            engine = _engines.get(url)
            if engine is None:
                engine = _build_engine(url)
                _engines[url] = engine
    return engine


def make_insert(table):
    """Dialect-appropriate ``insert()`` supporting ``.on_conflict_*`` (#300).

    Both the sqlite and postgresql dialect Insert constructs expose the same
    ``.on_conflict_do_update(index_elements=..., set_=...)`` /
    ``.on_conflict_do_nothing(index_elements=...)`` API, so this is the single
    portable replacement for ``INSERT OR REPLACE`` / ``INSERT OR IGNORE``.
    The dialect is chosen from the active engine, so the returned statement is
    always valid for the backend it will execute against.
    """
    if is_sqlite():
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        from sqlalchemy.dialects.postgresql import insert as _insert
    return _insert(table)


def dispose_engines() -> None:
    """Dispose and forget every cached engine (test teardown / reconfigure)."""
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()
