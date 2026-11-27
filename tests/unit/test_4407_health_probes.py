"""`/health` gates on the PostgreSQL schema and `/health/ready` probes the
dependencies (#4407).

`GET /health` returned `healthy` at once on PostgreSQL, the default database,
and never looked at the schema, Redis, Docker or the background loops.

The schema checks below run against a real SQLAlchemy engine (an in-memory
database carrying a real `alembic_version` table) and the real revision files
this tree ships. The two endpoint functions are taken out of `main.py` and
executed, not read as text: importing `main` boots the whole application.
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import sqlalchemy  # noqa: E402
from sqlalchemy.exc import OperationalError, ProgrammingError  # noqa: E402

from services import health_service as hs  # noqa: E402

pytestmark = pytest.mark.unit


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def clean_state():
    hs.reset_for_tests()
    yield
    hs.reset_for_tests()


@pytest.fixture
def engine(monkeypatch):
    """A real engine `health_service` reads through `db.engine.get_engine`."""
    import db.engine as db_engine

    eng = sqlalchemy.create_engine(
        "sqlite://", poolclass=sqlalchemy.pool.StaticPool,
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db_engine, "get_engine", lambda: eng)
    yield eng
    eng.dispose()


def _stamp(engine, *revisions: str) -> None:
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL)"))
        conn.execute(sqlalchemy.text("DELETE FROM alembic_version"))
        for revision in revisions:
            conn.execute(
                sqlalchemy.text("INSERT INTO alembic_version (version_num) VALUES (:r)"),
                {"r": revision},
            )


def _shipped_heads() -> list:
    heads = hs._alembic_script_heads()
    assert len(heads) == 1, f"the tree ships {len(heads)} Alembic heads: {heads}"
    return heads


def _a_revision_behind_head() -> str:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(_BACKEND / "migrations"))
    head = script.get_revision(script.get_current_head())
    down = head.down_revision
    return down if isinstance(down, str) else down[0]


# ---------------------------------------------------------------------------
# The PostgreSQL schema gate
# ---------------------------------------------------------------------------

def test_a_database_at_the_shipped_head_is_at_head(engine):
    _stamp(engine, *_shipped_heads())

    assert hs.postgres_schema_state() == {"state": hs.AT_HEAD}


def test_a_pending_revision_is_behind_and_names_both_sides(engine):
    behind = _a_revision_behind_head()
    _stamp(engine, behind)

    state = hs.postgres_schema_state()

    assert state == {"state": hs.BEHIND, "current": [behind], "head": _shipped_heads()}


def test_a_database_never_stamped_is_behind(engine):
    state = hs.postgres_schema_state()

    assert state["state"] == hs.BEHIND
    assert state["current"] == []


def test_a_revision_this_image_does_not_know_is_behind(engine):
    """A database left ahead by a rolled-back image is not this image's head."""
    _stamp(engine, "9999_from_a_newer_image")

    assert hs.postgres_schema_state()["state"] == hs.BEHIND


@pytest.mark.parametrize("fault", [
    OperationalError("SELECT 1", {}, Exception("connection refused")),
    ConnectionRefusedError("refused"),
])
def test_a_database_that_cannot_be_asked_is_unknown_not_behind(monkeypatch, fault):
    def _down():
        raise fault
    monkeypatch.setattr(hs, "_alembic_database_heads", _down)

    assert hs.postgres_schema_state() == {"state": hs.UNKNOWN}


def test_a_database_that_answered_with_an_error_is_behind(monkeypatch):
    def _broken():
        raise ProgrammingError("SELECT", {}, Exception("permission denied"))
    monkeypatch.setattr(hs, "_alembic_database_heads", _broken)

    assert hs.postgres_schema_state()["state"] == hs.BEHIND


def test_once_at_head_the_database_is_not_asked_again(engine, monkeypatch):
    _stamp(engine, *_shipped_heads())
    assert hs.postgres_schema_state()["state"] == hs.AT_HEAD

    asked = MagicMock(side_effect=AssertionError("asked again"))
    monkeypatch.setattr(hs, "_alembic_database_heads", asked)

    assert hs.postgres_schema_state() == {"state": hs.AT_HEAD}
    asked.assert_not_called()


def test_behind_is_never_remembered(engine):
    _stamp(engine, _a_revision_behind_head())
    assert hs.postgres_schema_state()["state"] == hs.BEHIND

    _stamp(engine, *_shipped_heads())  # the upgrade lands

    assert hs.postgres_schema_state()["state"] == hs.AT_HEAD


# ---------------------------------------------------------------------------
# Dependency probes
# ---------------------------------------------------------------------------

def test_a_reachable_database_is_ok(engine):
    assert hs.check_database() == hs.OK


def test_an_unreachable_database_is_reported(monkeypatch):
    import db.engine as db_engine

    def _down():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))
    monkeypatch.setattr(db_engine, "get_engine", _down)

    assert hs.check_database() == hs.UNREACHABLE


def test_an_unreachable_redis_is_reported(monkeypatch):
    """A real client against a port nothing listens on."""
    import redis

    monkeypatch.setattr(
        hs, "_redis_client",
        redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=1, socket_timeout=1),
    )

    assert hs.check_redis() == hs.UNREACHABLE


def test_a_redis_that_answers_is_ok(monkeypatch):
    monkeypatch.setattr(hs, "_redis_client", MagicMock(ping=MagicMock(return_value=True)))

    assert hs.check_redis() == hs.OK


def test_docker_probe_states(monkeypatch):
    from services import docker_service

    monkeypatch.setattr(docker_service, "docker_client", MagicMock(), raising=False)
    assert hs.check_docker() == hs.OK

    monkeypatch.setattr(docker_service, "docker_client", None, raising=False)
    assert hs.check_docker() == hs.UNAVAILABLE

    monkeypatch.setattr(
        docker_service, "docker_client",
        MagicMock(ping=MagicMock(side_effect=OSError("no socket"))), raising=False,
    )
    assert hs.check_docker() == hs.UNREACHABLE


# ---------------------------------------------------------------------------
# Loop ticks
# ---------------------------------------------------------------------------

@pytest.fixture
def ticks(monkeypatch):
    """The loop-health module `health_service` reads, resolved the way it
    resolves it (at call time): another test module can re-import the service
    package, and a module object held from collection would then be a copy
    nobody reads."""
    from services import loop_health as current

    monkeypatch.setattr(current, "_ticks", {})
    monkeypatch.setattr(current, "_last_tick_at", {})
    return current


def test_a_completed_cycle_is_reported_with_its_time_and_age(ticks, monkeypatch):
    monkeypatch.setattr(ticks.time, "time", lambda: 1_800_000_000.0)
    ticks.record_tick("monitoring", 0.25)

    reported = hs.loop_ticks(now=1_800_000_042.0)

    assert reported == {"monitoring": {
        "last_tick_at": "2027-01-15T08:00:00.000000Z",
        "age_seconds": 42.0,
        "last_ms": 250.0,
        "ticks": 1,
    }}


def test_the_tick_context_manager_stamps_the_time(ticks):
    import time as _time

    before = _time.time() - 5
    with ticks.tick("heartbeat_watch"):
        pass

    assert ticks.last_tick_times()["heartbeat_watch"] > before


def test_the_telemetry_snapshot_keeps_its_shape(ticks):
    ticks.record_tick("monitoring", 0.01)

    assert set(ticks.tick_snapshot()["monitoring"]) == {
        "ticks", "slow_ticks", "last_ms", "max_ms",
    }


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

def _probes(monkeypatch, *, database=hs.OK, redis=hs.OK, docker=hs.OK, schema=hs.AT_HEAD):
    monkeypatch.setattr(hs, "check_database", lambda: database)
    monkeypatch.setattr(hs, "check_redis", lambda: redis)
    monkeypatch.setattr(hs, "check_docker", lambda: docker)
    monkeypatch.setattr(hs, "schema_state", lambda: {"state": schema})
    monkeypatch.setattr(hs, "loop_ticks", lambda: {"monitoring": {"age_seconds": 3.0}})


def test_everything_up_is_ready(monkeypatch):
    _probes(monkeypatch)

    assert _await(hs.readiness()) == {
        "ready": True,
        "checks": {"database": "ok", "redis": "ok", "docker": "ok", "schema": "at_head"},
        "loops": {"monitoring": {"age_seconds": 3.0}},
    }


@pytest.mark.parametrize("down", [
    {"database": hs.UNREACHABLE}, {"redis": hs.UNREACHABLE},
    {"docker": hs.UNREACHABLE}, {"docker": hs.UNAVAILABLE}, {"schema": hs.BEHIND},
])
def test_one_dependency_down_is_not_ready_and_is_named(monkeypatch, down):
    _probes(monkeypatch, **down)

    result = _await(hs.readiness())

    assert result["ready"] is False
    (name, state), = down.items()
    assert result["checks"][name] == state
    assert [k for k, v in result["checks"].items() if v not in ("ok", "at_head")] == [name]


def test_a_schema_that_could_not_be_verified_does_not_block_readiness(monkeypatch):
    """The database probe already says whether the database is reachable."""
    _probes(monkeypatch, schema=hs.UNKNOWN)

    assert _await(hs.readiness())["ready"] is True


def test_a_probe_that_hangs_is_cut_off_and_reported(monkeypatch):
    import time as _time

    _probes(monkeypatch)
    monkeypatch.setattr(hs, "PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(hs, "check_docker", lambda: _time.sleep(0.5) or hs.OK)

    result = _await(hs.readiness())

    assert result["checks"]["docker"] == hs.TIMEOUT
    assert result["ready"] is False


def test_a_probe_that_raises_does_not_break_the_answer(monkeypatch):
    _probes(monkeypatch)
    monkeypatch.setattr(hs, "check_redis", MagicMock(side_effect=RuntimeError("boom")))

    result = _await(hs.readiness())

    assert result["checks"]["redis"] == hs.TIMEOUT
    assert result["ready"] is False


def test_readiness_is_answered_from_a_short_cache(monkeypatch):
    _probes(monkeypatch)
    probe = MagicMock(return_value=hs.OK)
    monkeypatch.setattr(hs, "check_database", probe)

    async def _twice():
        return await hs.readiness(), await hs.readiness()
    first, second = _await(_twice())

    assert first == second
    assert probe.call_count == 1

    monkeypatch.setattr(hs, "READINESS_CACHE_SECONDS", 0.0)
    _await(hs.readiness())
    assert probe.call_count == 2


def test_no_dependency_error_text_reaches_the_answer(monkeypatch):
    """The endpoint is unauthenticated: a probe answers with a fixed word."""
    import db.engine as db_engine

    secret = "postgresql://trinity:hunter2@db.internal:5432/trinity"

    def _down():
        raise OperationalError("SELECT 1", {}, Exception(f"could not connect to {secret}"))
    monkeypatch.setattr(db_engine, "get_engine", _down)
    monkeypatch.setattr(hs, "check_redis", lambda: hs.OK)
    monkeypatch.setattr(hs, "check_docker", lambda: hs.OK)
    monkeypatch.setattr(hs, "loop_ticks", lambda: {})

    body = json.dumps(_await(hs.readiness()))

    assert "hunter2" not in body and "db.internal" not in body
    assert '"database": "unreachable"' in body


# ---------------------------------------------------------------------------
# The endpoints, executed
# ---------------------------------------------------------------------------

def _endpoints() -> dict:
    """Compile `health_check` and `readiness_check` out of main.py and return
    them as callables. The route decorators are dropped; the bodies are the
    shipped ones."""
    tree = ast.parse((_BACKEND / "main.py").read_text())
    wanted = {"health_check": "/health", "readiness_check": "/health/ready"}
    found = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in wanted:
            routes = [
                d.args[0].value for d in node.decorator_list
                if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "get"
            ]
            assert routes == [wanted[node.name]], (node.name, routes)
            node.decorator_list = []
            found.append(node)
    assert {n.name for n in found} == set(wanted)
    module = ast.Module(body=found, type_ignores=[])
    namespace = {
        "asyncio": asyncio, "datetime": datetime, "logger": MagicMock(),
        "utc_now_iso": lambda: "2026-11-27T00:00:00Z",
    }
    exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), namespace)
    return namespace


def _on_postgres(monkeypatch):
    import db.engine as db_engine

    monkeypatch.setattr(db_engine, "is_sqlite", lambda *a, **k: False)


def test_health_is_503_on_postgres_with_a_pending_revision(engine, monkeypatch):
    _on_postgres(monkeypatch)
    behind = _a_revision_behind_head()
    _stamp(engine, behind)

    response = _await(_endpoints()["health_check"]())

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["status"] == "unhealthy"
    assert body["migrations"] == {"current": [behind], "head": _shipped_heads()}


def test_health_is_healthy_on_postgres_at_head(engine, monkeypatch):
    _on_postgres(monkeypatch)
    _stamp(engine, *_shipped_heads())

    body = _await(_endpoints()["health_check"]())

    assert body == {"status": "healthy", "timestamp": "2026-11-27T00:00:00Z"}


def test_health_stays_healthy_through_a_database_blip(monkeypatch):
    _on_postgres(monkeypatch)

    def _down():
        raise OperationalError("SELECT 1", {}, Exception("connection reset"))
    monkeypatch.setattr(hs, "_alembic_database_heads", _down)

    body = _await(_endpoints()["health_check"]())

    assert body["status"] == "healthy"
    assert body["schema"] == "unverified"


def test_health_probes_no_dependency(engine, monkeypatch):
    """Liveness stays cheap: Redis and Docker are readiness's business."""
    _on_postgres(monkeypatch)
    _stamp(engine, *_shipped_heads())
    for probe in ("check_redis", "check_docker", "check_database"):
        monkeypatch.setattr(hs, probe, MagicMock(side_effect=AssertionError(probe)))

    assert _await(_endpoints()["health_check"]())["status"] == "healthy"


def test_ready_endpoint_is_200_when_ready(monkeypatch):
    _probes(monkeypatch)

    response = _await(_endpoints()["readiness_check"]())

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "ready",
        "timestamp": "2026-11-27T00:00:00Z",
        "checks": {"database": "ok", "redis": "ok", "docker": "ok", "schema": "at_head"},
        "loops": {"monitoring": {"age_seconds": 3.0}},
    }


def test_ready_endpoint_is_503_with_redis_unreachable(monkeypatch):
    _probes(monkeypatch, redis=hs.UNREACHABLE)

    response = _await(_endpoints()["readiness_check"]())

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["status"] == "not_ready"
    assert body["checks"]["redis"] == "unreachable"
