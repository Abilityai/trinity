"""
SQLAlchemy engine factory — the configurable database backend seam (#300).

`DATABASE_URL` selects the backend at startup:

    postgresql://trinity:secret@postgres:5432/trinity
    sqlite:////data/trinity.db                 (derived from TRINITY_DB_PATH when unset)

Every compose file defaults DATABASE_URL to the bundled PostgreSQL, and the
images refuse SQLite (#4175). SQLite remains for the unit tier until it is retired.

All 44 db modules route through this engine via SQLAlchemy Core. The raw
sqlite3 context manager in `db/connection.py` remains for sqlite-specific
maintenance paths only (PRAGMA migrations, WAL checkpoint, VACUUM, the /health
migration gate — all gated to `is_sqlite()` at their call sites). The canary
snapshot reader was migrated onto this engine seam in #1540, so it now honors
DATABASE_URL like every other read path.
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


SQLITE_GUIDE = "docs/migrations/SQLITE_TO_POSTGRES.md"
_MIGRATE_COMMAND = "docker compose <your -f files> run --rm backend python -m db.sqlite_to_postgres"
_STAY_ON_SQLITE = (
    "To stay on SQLite for this release only, set both DATABASE_URL= (empty) and "
    "TRINITY_ALLOW_SQLITE=1 in .env."
)


def postgres_required() -> bool:
    """True inside a Trinity image (the Dockerfiles set this, #4175).

    Keyed on an image-only variable because `init_database()` runs at import
    across the SQLite unit tier, which keeps working until that tier is retired.
    """
    return os.getenv("TRINITY_REQUIRE_POSTGRES") == "1"


def sqlite_db_path() -> str:
    return os.getenv("TRINITY_DB_PATH", "/data/trinity.db")


def sqlite_file_has_data() -> bool:
    try:
        return os.path.getsize(sqlite_db_path()) > 0
    except OSError:
        return False


def refuse_sqlite_backend() -> None:
    """Refuse SQLite inside an image, before anything touches the file (#4175)."""
    if not postgres_required() or os.getenv("TRINITY_ALLOW_SQLITE") == "1":
        return
    if sqlite_file_has_data():
        raise RuntimeError(
            f"Refusing to start: this install has a SQLite database at {sqlite_db_path()}, and "
            f"SQLite reached end-of-support on 2026-09-01. Move it to PostgreSQL: {_MIGRATE_COMMAND} "
            f"(see {SQLITE_GUIDE}). {_STAY_ON_SQLITE}"
        )
    raise RuntimeError(
        "Refusing to start: DATABASE_URL is empty, which selects SQLite, and SQLite reached "
        "end-of-support on 2026-09-01. Remove the empty DATABASE_URL line from .env to use the "
        f"bundled PostgreSQL, or set a postgresql:// URL (see {SQLITE_GUIDE})."
    )


def refuse_empty_postgres_beside_sqlite() -> None:
    """Refuse to build an empty PostgreSQL next to an install's SQLite data (#4175).

    Compose's default URL means the backend sees a valid postgresql:// URL on an
    upgraded SQLite install, so this is the state that loses data silently: a new
    empty database, a seeded admin, and every later boot passes.
    """
    raise RuntimeError(
        f"Refusing to start: PostgreSQL has no users and this install has a SQLite database at "
        f"{sqlite_db_path()}. Copy it across: {_MIGRATE_COMMAND} (see {SQLITE_GUIDE}). To start "
        f"fresh instead, move that file aside. {_STAY_ON_SQLITE}"
    )


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
