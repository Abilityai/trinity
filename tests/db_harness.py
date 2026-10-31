"""
Backend-agnostic database test harness (#300).

Replaces the per-file bespoke pattern (a hand-rolled ``tmp_db`` fixture +
``_make_db_schema`` partial DDL + raw ``sqlite3.connect`` seed helpers) that
hardwired unit tests to SQLite. Tests built on this harness run on **either**
backend: SQLite always, and PostgreSQL when ``TEST_POSTGRES_URL`` is set.

Why this exists
---------------
The old fixtures wrote seed rows with raw ``sqlite3`` and built a partial
schema by hand. That (a) could not target PostgreSQL and (b) silently drifted
from the real schema. This harness instead:

* builds the schema from the canonical Core table metadata
  (``db.tables.metadata.create_all``) — one source of truth, dialect-correct
  on both backends, and crucially WITHOUT importing ``database`` /
  ``services`` / ``config`` (so lightweight unit tests keep their minimal
  import surface and don't acquire a ``REDIS_URL`` requirement);
* seeds via the active SQLAlchemy engine, so writes land in whatever backend
  ``db_backend`` selected;
* gives each test a clean database (a fresh temp file on SQLite; a
  drop-and-recreate of the ``public`` schema on PostgreSQL).

Usage
-----
    from db_harness import db_backend, seed_schedule, seed_execution

    def test_something(db_backend):           # runs once per available backend
        seed_schedule("sid-1")
        seed_execution("sid-1", duration_ms=100)
        ...

Backends that legitimately only make sense on SQLite (e.g. tests of the
SQLite migration runner itself) should depend on ``sqlite_only_backend``
instead of ``db_backend``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

SQLITE = "sqlite"
POSTGRES = "postgres"


def available_backends() -> list[str]:
    """SQLite always; PostgreSQL only when TEST_POSTGRES_URL is set."""
    backends = [SQLITE]
    if os.getenv("TEST_POSTGRES_URL"):
        backends.append(POSTGRES)
    return backends


def _reset_postgres() -> None:
    """Drop + recreate the public schema for a clean per-test slate."""
    from db.engine import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


def bootstrap_schema() -> None:
    """Build the full production schema on the active engine.

    Uses the real schema builders — ``db.schema.init_schema`` (cursor-based
    raw DDL) on SQLite and ``init_schema_postgres`` on PostgreSQL — so the
    test schema is a faithful reproduction of production, INCLUDING UNIQUE
    constraints and append-only triggers. (``metadata.create_all`` was an
    earlier approach but dropped UNIQUE constraints that ``ON CONFLICT`` and
    production upserts rely on.) Still import-light: ``db.schema`` pulls in
    no ``services``/``config`` chain.
    """
    from db.engine import get_engine, is_sqlite

    engine = get_engine()
    if is_sqlite():
        from db import schema as sch

        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            sch.init_schema(cur, raw)
            raw.commit()
        finally:
            raw.close()
    else:
        from db.schema import init_schema_postgres

        init_schema_postgres(engine)


# #4096: one schema build per worker process, copied per test. `init_schema`
# is pure DDL (no seed rows, no timestamps), so the copy is content-identical
# to a fresh build — and it is what made `db_backend` cost ~330ms per test,
# 36% of the unit suite's phase time. Module-global rather than a session
# fixture so `_activate_backend` stays a plain function; xdist gives every
# worker its own process AND its own `tmp_path_factory` basetemp, so the
# template is never shared across workers.
_SQLITE_TEMPLATE: Path | None = None


def sqlite_template(tmp_path_factory) -> Path:
    """The per-process SQLite schema template, built on first use."""
    global _SQLITE_TEMPLATE
    if _SQLITE_TEMPLATE is not None and _SQLITE_TEMPLATE.exists():
        return _SQLITE_TEMPLATE

    from db.engine import dispose_engines

    template = Path(tmp_path_factory.mktemp("db-template")) / "trinity.db"
    saved = {k: os.environ.get(k) for k in ("TRINITY_DB_PATH", "DATABASE_URL")}
    os.environ["TRINITY_DB_PATH"] = str(template)
    os.environ.pop("DATABASE_URL", None)
    try:
        dispose_engines()
        bootstrap_schema()
    finally:
        # Close the engine so the file is complete before anyone copies it,
        # then put the environment back exactly as it was.
        dispose_engines()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    _SQLITE_TEMPLATE = template
    return template


def _activate_backend(backend: str, tmp_path, monkeypatch, tmp_path_factory=None) -> None:
    """Point the engine at `backend` and give the test a fresh schema.

    With `tmp_path_factory` (the two fixtures below pass it) the SQLite schema
    is a copy of the per-process template; a direct caller that omits it
    (test_ent695_apply_engine builds a second world mid-test) gets the full
    `init_schema` build, same result, ~300ms slower.
    """
    from db.engine import dispose_engines

    if backend == SQLITE:
        db_path = tmp_path / "trinity.db"
        monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
        monkeypatch.delenv("DATABASE_URL", raising=False)
        dispose_engines()
        if tmp_path_factory is None:
            bootstrap_schema()
        else:
            shutil.copyfile(sqlite_template(tmp_path_factory), db_path)
    elif backend == POSTGRES:
        monkeypatch.setenv("DATABASE_URL", os.environ["TEST_POSTGRES_URL"])
        dispose_engines()
        _reset_postgres()
        bootstrap_schema()
    else:  # pragma: no cover - guard
        raise ValueError(f"unknown backend {backend!r}")


@pytest.fixture(params=available_backends())
def db_backend(request, tmp_path, monkeypatch, tmp_path_factory):
    """Parametrized DB backend.

    Yields the backend name (``"sqlite"`` / ``"postgres"``) after pointing the
    engine at it and giving the test a fresh schema (a copy of the per-process
    template on SQLite, #4096). Disposes cached engines on teardown so the
    next test/param starts clean.
    """
    from db.engine import dispose_engines

    _activate_backend(request.param, tmp_path, monkeypatch, tmp_path_factory)
    try:
        yield request.param
    finally:
        dispose_engines()


@pytest.fixture
def sqlite_only_backend(tmp_path, monkeypatch, tmp_path_factory):
    """SQLite-only variant for tests that are inherently SQLite-specific
    (e.g. the SQLite migration runner). Mirrors ``db_backend`` but never
    parametrizes onto PostgreSQL."""
    from db.engine import dispose_engines

    _activate_backend(SQLITE, tmp_path, monkeypatch, tmp_path_factory)
    try:
        yield SQLITE
    finally:
        dispose_engines()


# ----------------------------------------------------------------------
# Engine-based seed helpers (write to the active backend, not raw sqlite3).
# Cover the common scheduling/ownership tables; tests needing other tables
# seed them the same way via ``get_engine()``.
# ----------------------------------------------------------------------

def _engine():
    from db.engine import get_engine

    return get_engine()


def run(sql: str, **binds):
    """Execute a write statement on the active engine (named :binds)."""
    with _engine().begin() as conn:
        conn.execute(text(sql), binds)


def scalar(sql: str, **binds):
    """Return the first column of the first row (or None) — named :binds."""
    with _engine().connect() as conn:
        return conn.execute(text(sql), binds).scalar()


def count(table: str, where: str = "1=1", **binds) -> int:
    """COUNT(*) helper that works on both backends (named :binds in `where`)."""
    return scalar(f"SELECT COUNT(*) FROM {table} WHERE {where}", **binds) or 0


def _translate_qmarks(sql: str, params):
    """Translate sqlite-style ?-placeholders + a param sequence to named binds.

    Passes dict params (already named) and empty params through untouched.
    """
    if params is None:
        params = ()
    if isinstance(params, dict):
        return sql, params
    binds = {f"p{i}": v for i, v in enumerate(params)}
    out = sql
    for i in range(len(params)):
        out = out.replace("?", f":p{i}", 1)
    return out, binds


class _Cursor:
    """sqlite3.Cursor-like view over engine results (materialised per execute)."""

    def __init__(self):
        self._rows = []
        self._i = 0

    def execute(self, sql: str, params=()):
        from sqlalchemy import text

        sql2, binds = _translate_qmarks(sql, params)
        with _engine().begin() as conn:  # autocommit on exit (writes persist)
            res = conn.execute(text(sql2), binds)
            self._rows = list(res.fetchall()) if res.returns_rows else []
        self._i = 0
        return self

    def fetchone(self):
        if self._i < len(self._rows):
            row = self._rows[self._i]
            self._i += 1
            return row
        return None

    def fetchall(self):
        rows = self._rows[self._i:]
        self._i = len(self._rows)
        return rows


class EngineConn:
    """sqlite3.Connection-like shim over the active engine (#300).

    Lets the "returned-conn" test fixtures (which yield a raw sqlite3
    connection for direct verification reads / legacy-row seeding) run on
    either backend. Accepts ?-style SQL + a param sequence, autocommits each
    statement, and returns SQLAlchemy ``Row`` objects (which support both
    positional ``row[0]`` and key ``row["col"]`` access — matching how the
    tests consume sqlite3 rows). ``.commit()``/``.close()`` are no-ops
    (writes already autocommit); usable as a context manager.
    """

    def execute(self, sql: str, params=()):
        return _Cursor().execute(sql, params)

    def cursor(self):
        return _Cursor()

    def commit(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def engine_conn() -> EngineConn:
    """Return a sqlite3.Connection-like shim bound to the active engine."""
    return EngineConn()


def seed_user(user_id: int = 1, username: str = "owner", role: str = "user") -> int:
    """Insert a users row (idempotent on username). Returns the id."""
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, role, created_at, updated_at) "
                "VALUES (:id, :u, :r, :n, :n)"
            ),
            {"id": user_id, "u": username, "r": role, "n": "2026-01-01T00:00:00Z"},
        )
    return user_id


def seed_agent(agent_name: str = "agent-1", owner_id: int = 1) -> None:
    """Insert an agent_ownership row."""
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
                "VALUES (:a, :o, :n)"
            ),
            {"a": agent_name, "o": owner_id, "n": "2026-01-01T00:00:00Z"},
        )


def seed_schedule(
    sid: str,
    agent_name: str = "agent-1",
    owner_id: int = 1,
    deleted_at: str | None = None,
) -> None:
    """Insert an agent_schedules row via the active engine."""
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_schedules "
                "(id, agent_name, name, cron_expression, message, enabled, "
                " timezone, owner_id, created_at, updated_at, deleted_at) "
                "VALUES (:id, :a, 'sched', '0 0 * * *', 'hi', 1, 'UTC', "
                " :o, :n, :n, :del)"
            ),
            {"id": sid, "a": agent_name, "o": owner_id,
             "n": "2026-01-01T00:00:00Z", "del": deleted_at},
        )


def seed_execution(
    sid: str,
    agent_name: str = "agent-1",
    *,
    exec_id: str | None = None,
    started_at: str = "2026-01-01T00:00:00.000000Z",
    status: str = "success",
    duration_ms: int | None = 1000,
    cost: float | None = 0.01,
    tool_calls=None,
    triggered_by: str = "schedule",
) -> str:
    """Insert a schedule_executions row via the active engine. Returns its id."""
    import json

    if exec_id is None:
        exec_id = f"e-{sid}-{started_at[-10:]}-{status}-{duration_ms}"
    if isinstance(tool_calls, list):
        tool_calls = json.dumps(tool_calls)
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schedule_executions "
                "(id, schedule_id, agent_name, status, started_at, duration_ms, "
                " cost, tool_calls, triggered_by, message) "
                "VALUES (:id, :sid, :a, :st, :sa, :du, :co, :tc, :tb, '')"
            ),
            {"id": exec_id, "sid": sid, "a": agent_name, "st": status,
             "sa": started_at, "du": duration_ms, "co": cost,
             "tc": tool_calls, "tb": triggered_by},
        )
    return exec_id
