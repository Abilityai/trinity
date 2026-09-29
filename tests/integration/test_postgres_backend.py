"""
PostgreSQL backend integration suite (#300).

Exercises the real ``DatabaseManager`` facade — i.e. the converted SQLAlchemy
Core db/*.py modules — directly against a live PostgreSQL server, proving the
configurable backend is operational end-to-end (not just on SQLite).

Runs only when a PostgreSQL server is reachable via ``TEST_POSTGRES_URL``
(default ``postgresql://trinity:trinity@localhost:5432/trinity``); otherwise the
whole module is skipped, so SQLite-only environments stay green. This is the
suite the #300 CI dual-backend gate will run against a postgres:16 service.

Isolation: the schema is created once on the public schema; every test starts
from a TRUNCATE … RESTART IDENTITY CASCADE of all tables.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from db.write_params import ChatMessageFields, ExecutionResult

pytestmark = pytest.mark.integration

pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

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
    """Override tests/conftest.py's autouse cleanup_after_test, which depends on
    api_client (a live backend). This suite talks only to PostgreSQL, no API."""
    yield


def _load_schema():
    spec = importlib.util.spec_from_file_location("_pg_schema_it", _BACKEND / "db/schema.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def manager():
    """A DatabaseManager bound to PostgreSQL with a freshly-created schema."""
    # Route the whole backend at PostgreSQL for this module.
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = PG_URL
    os.environ.pop("ADMIN_PASSWORD", None)  # skip bcrypt admin bootstrap
    os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
    os.environ.setdefault("REDIS_PASSWORD", "test")
    os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
    os.environ.setdefault("SECRET_KEY", "pg-it")

    admin = create_engine(PG_URL)
    with admin.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    admin.dispose()

    import db.engine as engine_mod
    engine_mod.dispose_engines()

    from database import DatabaseManager
    db = DatabaseManager()  # init_database → init_schema_postgres
    yield db

    engine_mod.dispose_engines()
    if prev is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = prev


@pytest.fixture
def db(manager):
    """Truncate every table so each test starts clean."""
    schema = _load_schema()
    import db.engine as engine_mod
    tables = ", ".join(f'"{t}"' for t in schema.TABLES.keys())
    with engine_mod.get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return manager


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _mk_user(db, username="u@example.com", role="user"):
    from db_models import UserCreate
    return db.create_user(UserCreate(
        username=username, password="hash", role=role,
        auth0_sub=None, name="U", picture=None, email=username,
    ))


def _mk_agent(db, name="agent-x", owner="owner@example.com"):
    _mk_user(db, owner, role="creator")
    db.register_agent_owner(name, owner)
    return name


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #

def test_users_crud(db):
    u = _mk_user(db, "alice@example.com")
    assert u["id"] is not None
    assert db.get_user_by_username("alice@example.com")["email"] == "alice@example.com"
    assert db.get_user_by_id(u["id"])["username"] == "alice@example.com"
    assert db.get_user_by_email("alice@example.com")["id"] == u["id"]
    assert db.get_user_by_username("ghost@example.com") is None
    assert db.update_user_role("alice@example.com", "operator")["role"] == "operator"
    assert any(r["username"] == "alice@example.com" for r in db.list_users())


# --------------------------------------------------------------------------- #
# agent ownership + soft-delete + cascade purge
# --------------------------------------------------------------------------- #

def test_agent_ownership_soft_delete_and_purge(db):
    name = _mk_agent(db, "doomed")
    assert db.get_agent_owner("doomed") is not None
    # tag + share child rows to prove cascade
    db.add_agent_tag("doomed", "prod")
    assert db.delete_agent_ownership("doomed") is True       # soft delete
    assert any(a["agent_name"] == "doomed" for a in db.list_soft_deleted_agents())
    assert db.purge_agent_ownership("doomed") is True        # hard purge + cascade
    assert db.get_agent_owner("doomed") is None
    assert db.get_agent_tags("doomed") == []                 # child cascaded


# --------------------------------------------------------------------------- #
# tags — on_conflict_do_nothing / set-replace
# --------------------------------------------------------------------------- #

def test_tags_upsert_paths(db):
    _mk_agent(db, "tagged")
    db.add_agent_tag("tagged", "a")
    db.add_agent_tag("tagged", "a")  # duplicate → on_conflict_do_nothing, no error
    assert sorted(db.get_agent_tags("tagged")) == ["a"]
    db.set_agent_tags("tagged", ["x", "y", "z"])
    assert sorted(db.get_agent_tags("tagged")) == ["x", "y", "z"]
    assert "tagged" in db.get_agents_by_tag("x")
    db.remove_agent_tag("tagged", "x")
    assert sorted(db.get_agent_tags("tagged")) == ["y", "z"]


# --------------------------------------------------------------------------- #
# settings — on_conflict_do_update
# --------------------------------------------------------------------------- #

def test_settings_upsert(db):
    db.set_setting("feature_flag", "on")
    assert db.get_setting("feature_flag").value == "on"
    db.set_setting("feature_flag", "off")  # conflict → update
    assert db.get_setting("feature_flag").value == "off"
    assert db.get_setting("missing") is None


# --------------------------------------------------------------------------- #
# mcp keys
# --------------------------------------------------------------------------- #

def test_mcp_keys_crud(db):
    from db_models import McpApiKeyCreate
    _mk_user(db, "keyowner@example.com", role="creator")
    created = db.create_mcp_api_key("keyowner@example.com", McpApiKeyCreate(name="k1", description="d"))
    key_id = created["id"] if isinstance(created, dict) else created.id
    assert key_id
    listed = db.list_mcp_api_keys("keyowner@example.com")
    assert any((r["id"] if isinstance(r, dict) else r.id) == key_id for r in listed)
    assert db.delete_mcp_api_key(key_id, "keyowner@example.com") in (True, None)


# --------------------------------------------------------------------------- #
# schedules + executions
# --------------------------------------------------------------------------- #

def test_schedules_and_executions(db):
    from db_models import ScheduleCreate
    _mk_agent(db, "sched-agent", owner="sowner@example.com")
    sched = db.create_schedule("sched-agent", "sowner@example.com", ScheduleCreate(
        name="daily", cron_expression="0 9 * * *", message="do it",
    ))
    sid = sched["id"] if isinstance(sched, dict) else sched.id
    assert sid
    assert any((s["id"] if isinstance(s, dict) else s.id) == sid
               for s in db.list_agent_schedules("sched-agent"))

    ex = db.create_schedule_execution(sid, "sched-agent", "do it", triggered_by="schedule")
    eid = ex["id"] if isinstance(ex, dict) else ex.id
    assert eid
    db.update_execution_status(eid, "success", result=ExecutionResult(response="done", cost=0.01))
    got = db.get_execution(eid)
    status = got["status"] if isinstance(got, dict) else got.status
    assert status == "success"
    assert len(db.get_schedule_executions(sid)) >= 1

    db.set_schedule_enabled(sid, False)
    db.delete_schedule(sid, "sowner@example.com")


# --------------------------------------------------------------------------- #
# chat — multi-statement transaction (message insert + session bump)
# --------------------------------------------------------------------------- #

def test_chat_session_and_messages(db):
    u = _mk_user(db, "chatter@example.com")
    _mk_agent(db, "chat-agent", owner="chatowner@example.com")
    session = db.get_or_create_chat_session("chat-agent", u["id"], "chatter@example.com")
    sid = session.id if hasattr(session, "id") else session["id"]
    db.add_chat_message(sid, "chat-agent", u["id"], "chatter@example.com", "user", "hello")
    db.add_chat_message(sid, "chat-agent", u["id"], "chatter@example.com", "assistant", "hi", fields=ChatMessageFields(cost=0.02))
    msgs = db.get_chat_messages(sid)
    assert len(msgs) == 2


# --------------------------------------------------------------------------- #
# sync state list (read path over a converted module)
# --------------------------------------------------------------------------- #

def test_sync_state_empty_read(db):
    assert db.list_sync_states() == [] or isinstance(db.list_sync_states(), list)
    assert db.get_sync_state("nope") is None


# --------------------------------------------------------------------------- #
# Regression guards for dialect bugs found by the live PG e2e (#300)
# --------------------------------------------------------------------------- #

def test_system_views_owner_join(db):
    """system_views.owner_id (TEXT) JOIN users.id (INTEGER): SQLite coerces,
    PostgreSQL needs a cast (was 'operator does not exist: text = integer')."""
    from db_models import SystemViewCreate

    u = _mk_user(db, "viewowner@example.com")
    view = db.create_system_view(str(u["id"]), SystemViewCreate(name="v1", filter_tags=["x"]))
    vid = view.id if hasattr(view, "id") else view["id"]
    assert vid
    rows = db.list_user_system_views(str(u["id"]))
    assert any((r.id if hasattr(r, "id") else r["id"]) == vid for r in rows)
    # owner_email comes from the JOIN that previously failed on PG
    match = next(r for r in rows if (r.id if hasattr(r, "id") else r["id"]) == vid)
    owner_email = match.owner_email if hasattr(match, "owner_email") else match["owner_email"]
    assert owner_email == "viewowner@example.com"


def test_email_whitelist_added_by_join(db):
    """email_whitelist.added_by (TEXT) JOIN users.id (INTEGER) — same class."""
    _mk_user(db, "wladmin@example.com", role="admin")
    db.add_to_whitelist("invitee@example.com", "wladmin@example.com", "manual", default_role="user")
    rows = db.list_whitelist()
    row = next(r for r in rows if (r["email"] if isinstance(r, dict) else r.email) == "invitee@example.com")
    uname = row["added_by_username"] if isinstance(row, dict) else row.added_by_username
    assert uname == "wladmin@example.com"  # resolved via the cast JOIN


def test_idempotency_claim_replay(db):
    """claim() does INSERT then (on conflict) SELECT in one transaction. On PG
    the conflict aborts the transaction unless the INSERT is in a SAVEPOINT
    (was InFailedSqlTransaction → silent fail-open, no dedup)."""
    scope, key = "agent:pgtest", "k-1"
    first = db.idempotency_claim(scope, key)
    assert first["state"] == "new"
    db.idempotency_complete(scope, key, "exec-123", {"ok": True})
    # Second claim must read the surviving completed row (the SELECT-after-conflict
    # path that aborted the transaction before the savepoint fix).
    second = db.idempotency_claim(scope, key)
    assert second["state"] == "completed"
    assert second["execution_id"] == "exec-123"
    assert second["snapshot"] == {"ok": True}
