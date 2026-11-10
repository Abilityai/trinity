"""An agent's own model for turns that name none (ent#1202).

`agent_ownership.default_model` is a nullable per-agent model consulted by ONE
resolver (`services/agent_model_service.py`) ahead of the platform default:

    explicit > public-channel override > agent's own (runnable) > platform default

NULL changes nothing. A stored value this instance cannot run is skipped by
name and the turn runs on the platform default. A row pre-created by the
scheduler gets the resolved model stamped, but only into a blank.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

DEFAULT = "claude-sonnet-4-6"
OWN = "claude-opus-5"


def _settings(default=DEFAULT):
    return SimpleNamespace(get_platform_default_model=lambda: default)


def _db(stored=None, exc=None):
    def _get(name):
        if exc:
            raise exc
        return stored
    return SimpleNamespace(get_default_model_raw=_get)


# --------------------------------------------------------------------------- #
# The predicate: catalog ids and Claude Code aliases only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model,runtime,reason", [
    ("claude-opus-5", "claude-code", None),
    ("claude-sonnet-4-6", "claude-code", None),
    ("opus", "claude-code", None),
    ("sonnet[1m]", "claude-code", None),
    ("", "claude-code", None),
    (None, "claude-code", None),
    ("claude-opus-9", "claude-code", "not_in_catalog"),
    ("gibberish", "claude-code", "not_in_catalog"),
    ("--dangerously-skip", "claude-code", "not_in_catalog"),
    ("gemini-3-pro", "claude-code", "runtime_mismatch"),
    ("gpt-5.1-codex", "claude-code", "runtime_mismatch"),
    ("claude-opus-5", "codex", "runtime_mismatch"),
    ("gpt-5.1-codex", "codex", None),
])
def test_unrunnable_reason(model, runtime, reason):
    from services.model_catalog import unrunnable_reason
    assert unrunnable_reason(model, runtime) == reason


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #
def _resolve(**kw):
    from services.agent_model_service import resolve_default_model
    kw.setdefault("runtime", "claude-code")
    kw.setdefault("settings", _settings())
    return resolve_default_model("agent-1202", **kw)


def test_null_column_changes_nothing():
    r = _resolve(db_ops=_db(None))
    assert (r.model, r.source, r.stored, r.refused_reason) == (DEFAULT, "platform_default", None, None)


def test_a_runnable_own_model_wins_over_the_platform_default():
    r = _resolve(db_ops=_db(OWN))
    assert (r.model, r.source, r.refused_reason) == (OWN, "agent", None)


def test_an_unrunnable_own_model_is_skipped_by_name(caplog):
    r = _resolve(db_ops=_db("claude-opus-9"))
    assert (r.model, r.source, r.stored, r.refused_reason) == (
        DEFAULT, "platform_default", "claude-opus-9", "not_in_catalog")
    assert "claude-opus-9" in caplog.text and "not_in_catalog" in caplog.text


@pytest.mark.parametrize("runtime", ["codex", "gemini-cli"])
def test_a_non_claude_agent_runs_on_its_own_runtime_model(runtime):
    r = _resolve(runtime=runtime, db_ops=_db(OWN))
    assert (r.model, r.source) == (None, "runtime")


def test_a_failed_read_degrades_to_the_platform_default():
    r = _resolve(db_ops=_db(exc=RuntimeError("db locked")))
    assert (r.model, r.source) == (DEFAULT, "platform_default")


def test_strict_reraises_a_failed_read():
    with pytest.raises(RuntimeError):
        _resolve(db_ops=_db(exc=RuntimeError("db locked")), strict=True)


# --------------------------------------------------------------------------- #
# execute_task: the AC2 line (`model_used`) and the pre-created-row stamp
# --------------------------------------------------------------------------- #
@contextmanager
def _wire(stored):
    from services.task_execution_service import TaskExecutionService

    seams = TaskExecutionService.execute_task.__globals__
    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution_timeout.return_value = 900
    mock_db.get_execution.return_value = MagicMock(id="exec-1202", status="running")
    mock_db.update_execution_status.return_value = True
    mock_db.get_default_model_raw.return_value = stored
    mock_db.create_task_execution.return_value = MagicMock(id="exec-new")
    capacity = MagicMock()
    capacity.acquire = AsyncMock(return_value=MagicMock(state="admitted"))
    capacity.release = AsyncMock()
    circuit = MagicMock()
    circuit.allow_request.return_value = True
    payloads: list[dict] = []

    async def _post(agent_name, endpoint, payload, **kwargs):
        payloads.append(dict(payload))
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"response": "ok", "metadata": {}}
        resp.text = "ok"
        return resp

    import config
    with (
        patch.object(config, "DISPATCH_ASYNC", False),
        patch.dict(seams, {
            "db": mock_db,
            "settings_service": _settings(),
            "_resolve_agent_runtime": lambda name: "claude-code",
            "get_capacity_manager": lambda: capacity,
            "activity_service": MagicMock(track_activity=AsyncMock(return_value="a"),
                                          complete_activity=AsyncMock()),
            "CircuitState": lambda *a, **k: circuit,
            "agent_post_with_retry": _post,
            "dispatch_breaker_active": lambda *a, **k: False,
            "_record_dispatch_terminal": AsyncMock(),
            "platform_audit_service": MagicMock(log=AsyncMock()),
        }),
    ):
        yield SimpleNamespace(db=mock_db, payloads=payloads)


def _execute(**kw):
    from services.task_execution_service import TaskExecutionService
    kw.setdefault("agent_name", "agent-1202")
    kw.setdefault("message", "wake")
    kw.setdefault("triggered_by", "reminder")
    return asyncio.run(TaskExecutionService().execute_task(**kw))


def test_a_wake_with_no_model_runs_and_stamps_the_agents_own_model():
    """No pre-created row (ask-ending / caller wakes): the row it creates and
    the agent call both carry the agent's own model."""
    with _wire(OWN) as w:
        _execute()
    fields = w.db.create_task_execution.call_args.kwargs["fields"]
    assert fields.model_used == OWN
    assert w.payloads and w.payloads[0].get("model") == OWN


def test_a_pre_created_row_gets_the_resolved_model_stamped():
    with _wire(OWN) as w:
        _execute(execution_id="exec-1202")
    w.db.stamp_model_used_if_null.assert_called_once_with("exec-1202", OWN)
    assert w.payloads[0].get("model") == OWN


def test_an_explicit_model_is_never_restamped():
    with _wire(OWN) as w:
        _execute(execution_id="exec-1202", model="haiku")
    w.db.stamp_model_used_if_null.assert_not_called()
    assert w.payloads[0].get("model") == "haiku"


# --------------------------------------------------------------------------- #
# DB layer on a real (tmp) database
# --------------------------------------------------------------------------- #
@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-1202.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.engine import get_engine
    from db.tables import metadata, agent_ownership, schedule_executions
    metadata.create_all(get_engine(), tables=[agent_ownership, schedule_executions])
    from sqlalchemy import insert
    with get_engine().begin() as conn:
        for name in ("live-a", "live-b"):
            conn.execute(insert(agent_ownership).values(agent_name=name, owner_id=1))
        conn.execute(insert(agent_ownership).values(
            agent_name="gone", owner_id=1, deleted_at="2026-01-01T00:00:00Z", default_model=OWN))
    return get_engine()


def test_set_and_get_default_model_round_trip(tmp_db):
    from db.agents import AgentOperations
    ops = AgentOperations.__new__(AgentOperations)
    assert ops.get_default_model_raw("live-a") is None
    assert ops.set_default_model("live-a", "  claude-opus-9  ") is True
    assert ops.get_default_model_raw("live-a") == "claude-opus-9"  # stored as written
    assert ops.set_default_model("live-b", "x" * 500) is True
    assert len(ops.get_default_model_raw("live-b")) == 128
    assert ops.get_default_models_raw(["live-a", "gone", "nope"]) == {"live-a": "claude-opus-9"}
    assert ops.set_default_model("live-a", None) is True
    assert ops.get_default_model_raw("live-a") is None
    assert ops.set_default_model("missing", OWN) is False


def test_set_default_model_if_lands_only_on_the_value_read(tmp_db):
    """Two writers that read the same old value: exactly one changes the row."""
    from db.agents import AgentOperations
    ops = AgentOperations.__new__(AgentOperations)
    assert ops.set_default_model_if("live-a", None, OWN) is True        # NULL matched
    assert ops.set_default_model_if("live-a", None, OWN) is False       # already changed
    assert ops.get_default_model_raw("live-a") == OWN
    assert ops.set_default_model_if("live-a", "stale", "other") is False
    assert ops.set_default_model_if("live-a", OWN, None) is True        # back to NULL
    assert ops.get_default_model_raw("live-a") is None


def test_stamp_model_used_if_null_fills_only_a_blank(tmp_db):
    from sqlalchemy import insert, select
    from db.tables import schedule_executions
    from db.schedules import ScheduleOperations
    with tmp_db.begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id="blank", schedule_id="s", agent_name="live-a", status="running",
            started_at="2026-11-09T00:00:00Z", message="m", triggered_by="reminder"))
        conn.execute(insert(schedule_executions).values(
            id="pinned", schedule_id="s", agent_name="live-a", status="running",
            started_at="2026-11-09T00:00:00Z", message="m", triggered_by="reminder",
            model_used="haiku"))
    ops = ScheduleOperations.__new__(ScheduleOperations)
    assert ops.stamp_model_used_if_null("blank", OWN) is True
    assert ops.stamp_model_used_if_null("pinned", OWN) is False
    with tmp_db.connect() as conn:
        got = dict(conn.execute(select(schedule_executions.c.id, schedule_executions.c.model_used)).all())
    assert got == {"blank": OWN, "pinned": "haiku"}


def test_sqlite_migration_adds_the_nullable_column(tmp_path):
    from db.migrations import MIGRATIONS, _migrate_agent_ownership_default_model
    assert ("agent_ownership_default_model", _migrate_agent_ownership_default_model) in MIGRATIONS
    conn = sqlite3.connect(tmp_path / "old.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE agent_ownership (agent_name TEXT PRIMARY KEY, public_channel_model TEXT)")
    cur.execute("INSERT INTO agent_ownership VALUES ('a', NULL)")
    _migrate_agent_ownership_default_model(cur, conn)
    _migrate_agent_ownership_default_model(cur, conn)  # idempotent
    cols = {r[1]: r for r in cur.execute("PRAGMA table_info(agent_ownership)")}
    assert "default_model" in cols and cols["default_model"][3] == 0  # nullable
    assert cur.execute("SELECT default_model FROM agent_ownership").fetchone() == (None,)


def test_alembic_revision_chains_on_the_previous_head():
    import importlib.util
    path = _BACKEND / "migrations" / "versions" / "0123_agent_ownership_default_model.py"
    spec = importlib.util.spec_from_file_location("rev0123", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0123_agent_ownership_default_model"
    assert mod.down_revision == "0122_channel_client_blocks"


# --------------------------------------------------------------------------- #
# The other rungs use the same resolver
# --------------------------------------------------------------------------- #
def test_a2a_precreate_stamps_the_agents_own_model(monkeypatch):
    from routers import a2a
    created = {}

    def _create(**kw):
        created["model"] = kw["fields"].model_used
        return SimpleNamespace(id="e")

    monkeypatch.setattr(a2a, "db", SimpleNamespace(
        create_task_execution=_create, get_agent_subscription_id=lambda n: None,
        get_default_model_raw=lambda n: OWN))
    monkeypatch.setattr(a2a, "settings_service", _settings(), raising=False)
    import services.docker_service as ds
    monkeypatch.setattr(ds, "get_agent_runtime", lambda n: "claude-code")
    assert a2a._mint_a2a_execution("agent-1202", "hi", None) == "e"
    assert created["model"] == OWN


def test_a2a_precreate_stamps_nothing_claude_on_a_non_claude_agent(monkeypatch):
    from routers import a2a
    created = {}

    def _create(**kw):
        created["model"] = kw["fields"].model_used
        return SimpleNamespace(id="e")

    monkeypatch.setattr(a2a, "db", SimpleNamespace(
        create_task_execution=_create, get_agent_subscription_id=lambda n: None,
        get_default_model_raw=lambda n: None))
    monkeypatch.setattr(a2a, "settings_service", _settings(), raising=False)
    import services.docker_service as ds
    monkeypatch.setattr(ds, "get_agent_runtime", lambda n: "codex")
    assert a2a._mint_a2a_execution("agent-1202", "hi", None) == "e"
    assert created["model"] is None


def test_portal_turn_with_no_pick_and_no_override_runs_the_agents_own_model(monkeypatch):
    import database
    import services.docker_service as ds
    from services import settings_service
    from client_portal import service as svc
    monkeypatch.setattr(database.db, "get_public_channel_model", lambda a: None, raising=False)
    monkeypatch.setattr(database.db, "get_default_model_raw", lambda a: OWN, raising=False)
    monkeypatch.setattr(ds, "get_agent_runtime", lambda n: "claude-code")
    monkeypatch.setattr(settings_service, "get_platform_default_model", lambda: DEFAULT)
    assert svc.resolve_turn_model("agent-1202", None) == OWN
    # An explicit public-channel override still wins.
    monkeypatch.setattr(database.db, "get_public_channel_model", lambda a: "haiku", raising=False)
    assert svc.resolve_turn_model("agent-1202", None) == "haiku"


def test_portal_label_names_the_agents_own_model():
    from client_portal import service as svc
    ctx = svc.ModelContext(options=svc.workspace_model_options(), platform_default=DEFAULT,
                           platform_label="Sonnet")
    own = svc._card_model_default({"public_channel_model": None, "default_model": OWN}, ctx, "claude-code")
    assert (own.model, own.source) == (OWN, "own")
    refused = svc._card_model_default({"default_model": "claude-opus-9"}, ctx, "claude-code")
    assert (refused.model, refused.source) == (DEFAULT, "platform")


# --------------------------------------------------------------------------- #
# GET /api/agents/{name}/model — the honest read-back (AC1)
# --------------------------------------------------------------------------- #
def test_get_model_reports_what_a_backend_turn_runs_on(monkeypatch):
    from routers import chat
    from services.agent_model_service import ResolvedModel

    monkeypatch.setattr(chat, "get_agent_container", lambda n: SimpleNamespace(status="running"))

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, timeout=None):
            resp = MagicMock()
            resp.raise_for_status = lambda: None
            resp.json = lambda: {"model": "claude-opus-5"}
            return resp

    monkeypatch.setattr(chat, "agent_httpx_client", lambda n: _Client())
    monkeypatch.setattr(chat, "resolve_default_model", lambda n: ResolvedModel(
        DEFAULT, "platform_default", "claude-opus-9", "not_in_catalog"))
    body = asyncio.run(chat.get_agent_model(name="agent-1202", current_user=None))
    assert body == {
        "model": "claude-opus-5",                    # the container's in-memory value
        "default_model": "claude-opus-9",
        "default_model_source": "platform_default",
        "default_model_refused": "not_in_catalog",
        "backend_turn_model": DEFAULT,
    }
