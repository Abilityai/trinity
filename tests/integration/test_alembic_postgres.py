"""Alembic migration lifecycle on PostgreSQL (#1183).

Proves the Alembic adoption for the Postgres backend: a fresh DB is built by
``alembic upgrade head`` (baseline reuses the head DDL), the run is idempotent,
``downgrade base`` tears down cleanly, the pre-Alembic stamp path adopts an
existing schema without rebuilding, and the Alembic-built schema matches what
the legacy ``init_schema_postgres`` produced.

Runs only when a PostgreSQL server is reachable via ``TEST_POSTGRES_URL``
(default ``postgresql://trinity:trinity@localhost:5432/trinity``); otherwise the
module is skipped so SQLite-only environments stay green.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")
from sqlalchemy import create_engine, inspect, text  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

PG_URL = os.getenv("TEST_POSTGRES_URL", "postgresql://trinity:trinity@localhost:5432/trinity")


def _pg_reachable(url: str) -> bool:
    try:
        eng = create_engine(url)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


if not _pg_reachable(PG_URL):
    pytest.skip(
        f"no PostgreSQL reachable at {PG_URL} (set TEST_POSTGRES_URL to run)",
        allow_module_level=True,
    )


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override tests/conftest.py's api_client-dependent autouse cleanup —
    this suite talks only to PostgreSQL."""
    yield


@pytest.fixture
def pg(monkeypatch):
    """Point the app's engine resolution at the test PG and reset the schema.

    Yields the runner + alembic config + engine, each test starting from an
    empty public schema.
    """
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    import db.engine as engine_mod
    engine_mod.dispose_engines()

    import db.alembic_runner as runner
    eng = engine_mod.get_engine()
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    yield runner, eng
    engine_mod.dispose_engines()


def _table_names(eng):
    return {t for t in inspect(eng).get_table_names() if t != "alembic_version"}


def test_fresh_upgrade_builds_full_schema(pg):
    runner, eng = pg
    runner.upgrade_to_head()
    insp = inspect(eng)
    assert insp.has_table("alembic_version")
    names = _table_names(eng)
    # core OSS tables present
    for t in ("users", "agent_ownership", "agent_loops", "operator_queue"):
        assert t in names, f"{t} missing"
    # a migration-era column (added via the bespoke runner on sqlite) is in the
    # baseline because schema.py TABLES is at head
    cols = {c["name"] for c in insp.get_columns("operator_queue")}
    assert "cleared_at" in cols


def test_upgrade_is_idempotent(pg):
    runner, eng = pg
    runner.upgrade_to_head()
    n1 = len(_table_names(eng))
    runner.upgrade_to_head()  # no-op
    assert len(_table_names(eng)) == n1


def test_downgrade_base_tears_down(pg):
    runner, eng = pg
    from alembic import command
    runner.upgrade_to_head()
    command.downgrade(runner._config(), "base")
    assert _table_names(eng) == set()
    with eng.connect() as c:
        rows = c.execute(text("SELECT count(*) FROM alembic_version")).scalar()
    assert rows == 0  # at base, no version


def test_pre_alembic_db_is_stamped_not_rebuilt(pg):
    """A DB already at head schema (built by the old init_schema_postgres) must
    be stamped at the baseline, not rebuilt — and end up at head."""
    runner, eng = pg
    from db.schema import init_schema_postgres
    init_schema_postgres(eng)
    assert not inspect(eng).has_table("alembic_version")
    runner.upgrade_to_head()
    with eng.connect() as c:
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert ver == "0001_baseline"
    assert "users" in _table_names(eng)


def test_alembic_schema_matches_legacy_build(pg):
    """The Alembic baseline must produce the same table set as the legacy
    init_schema_postgres fresh build (the baseline reuses its DDL)."""
    runner, eng = pg
    from db.schema import init_schema_postgres
    init_schema_postgres(eng)
    legacy = _table_names(eng)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    runner.upgrade_to_head()
    alembic_built = _table_names(eng)
    assert alembic_built == legacy


def test_concurrent_upgrade_does_not_deadlock(pg):
    """Two workers booting at once against a fresh DB must not deadlock (#1425).

    Pre-fix, both entered ``command.upgrade()`` concurrently and PostgreSQL
    raised ``DeadlockDetected`` inside a revision; the ``pg_advisory_lock`` in
    ``upgrade_to_head`` now serialises them. A barrier maximises contention;
    each worker uses its own session (separate ``engine.connect()``), exactly
    like two uvicorn workers. Both must finish cleanly and the schema end at
    head.
    """
    import threading

    runner, eng = pg
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _boot():
        try:
            barrier.wait(timeout=10)
            runner.upgrade_to_head()
        except BaseException as e:  # capture, assert on the main thread
            errors.append(e)

    threads = [threading.Thread(target=_boot) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not any(t.is_alive() for t in threads), "a worker hung (possible deadlock)"
    assert not errors, f"concurrent upgrade raised: {errors!r}"
    assert inspect(eng).has_table("alembic_version")
    with eng.connect() as c:
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert ver is not None
