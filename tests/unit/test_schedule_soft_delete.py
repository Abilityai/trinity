"""
Tests for schedule soft-delete + retention purge (Issue #834 Phase 1b).

Same shape as `tests/unit/test_agent_soft_delete.py`: the production
`db.connection.get_db_connection()` is routed to an ephemeral SQLite
via `monkeypatch.setattr` on the module attribute (env-var routing
is too fragile — `DB_PATH` is bound at module-import time).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


# Sibling tests (e.g. `test_execution_retention_prune.py`,
# `test_audit_retention_prune.py`, `test_session_operations.py`) stub
# `sys.modules["db.<sub>"]` with `importlib`-loaded modules bound at
# exec time to *their* tmp-DB-pointing `db.connection`. Those stubs
# are not restored on teardown, so a later import from this test
# would return a stale stub whose `get_db_connection()` points at a
# deleted tmp file — surfacing as `sqlite3.OperationalError: no such
# table: agent_schedules` / `users` under pytest-randomly seeds that
# happen to run those tests first.
#
# Sanctioned `_STUBBED_MODULE_NAMES` + autouse `_restore_sys_modules`
# pattern (precedent: `tests/unit/test_agent_cleanup_parity.py`) —
# snapshots the real modules, defensively pops any stale stubs so
# this file's imports re-resolve fresh, and restores on teardown so
# *we* don't pollute sibling tests either.
_STUBBED_MODULE_NAMES = [
    "db.schedules",
    "db.users",
    "db.agents",
    "db.monitoring",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {n: sys.modules.get(n) for n in _STUBBED_MODULE_NAMES}
    for name in _STUBBED_MODULE_NAMES:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _make_db_schema(conn: sqlite3.Connection) -> None:
    """Minimal schema covering agent_schedules + schedule_executions + users."""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            role TEXT DEFAULT 'user',
            auth0_sub TEXT,
            name TEXT,
            picture TEXT,
            email TEXT,
            created_at TEXT,
            updated_at TEXT,
            last_login TEXT,
            suspended_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE agent_schedules (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            name TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            message TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            timezone TEXT DEFAULT 'UTC',
            description TEXT,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_at TEXT,
            next_run_at TEXT,
            timeout_seconds INTEGER DEFAULT 900,
            allowed_tools TEXT,
            model TEXT,
            max_retries INTEGER DEFAULT 0,
            retry_delay_seconds INTEGER DEFAULT 60,
            validation_enabled INTEGER DEFAULT 0,
            validation_prompt TEXT,
            validation_timeout_seconds INTEGER DEFAULT 120,
            webhook_token TEXT,
            webhook_enabled INTEGER DEFAULT 0,
            deleted_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY,
            schedule_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            message TEXT NOT NULL,
            triggered_by TEXT NOT NULL
        )
        """
    )
    # list_all_enabled_schedules() JOINs agent_ownership and filters
    # ao.deleted_at IS NULL (#834 Phase 1a agent-level soft-delete);
    # the firing-list query needs this table present + an owner row.
    cur.execute(
        """
        CREATE TABLE agent_ownership (
            agent_name TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT
        )
        """
    )
    cur.execute(
        "CREATE INDEX idx_agent_schedules_deleted_at "
        "ON agent_schedules(deleted_at) WHERE deleted_at IS NOT NULL"
    )
    cur.execute(
        "INSERT INTO users(id, username, role) VALUES (1, 'owner', 'user'), (2, 'admin', 'admin')"
    )
    conn.commit()


@pytest.fixture
def tmp_schedule_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    _make_db_schema(conn)
    conn.close()

    # Belt-and-suspenders against sibling test pollution. The autouse
    # `_restore_sys_modules` above pops stale stubs that bind
    # `get_db_connection` to a now-deleted polluter tmp DB. But a stale
    # `db.connection` itself (left by a polluter that replaced the
    # module via plain assignment, vs. our autouse restore) would also
    # break the patched-attribute approach. Defend against both:
    #   1. set `TRINITY_DB_PATH` env so any fresh `db.connection` load
    #      reads OUR tmp file as its module-level DB_PATH;
    #   2. `monkeypatch.delitem` `db.connection` so the import on the
    #      next line *is* a fresh load against the env var above
    #      (auto-restored on teardown — we don't pollute);
    #   3. ALSO `monkeypatch.setattr(connection_mod, "DB_PATH", ...)`
    #      in case the fresh load picked up a different env value due
    #      to ordering surprise.
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.delitem(sys.modules, "db.connection", raising=False)

    try:
        import db.connection as connection_mod
    except ImportError:
        pytest.skip("backend venv required")

    monkeypatch.setattr(connection_mod, "DB_PATH", str(db_path))
    return str(db_path)


@pytest.fixture
def schedule_ops(tmp_schedule_db):
    try:
        from db.schedules import ScheduleOperations
        from db.users import UserOperations
        from db.agents import AgentOperations
    except ImportError:
        pytest.skip("backend venv required")
    user_ops = UserOperations()
    agent_ops = AgentOperations(user_ops)
    return ScheduleOperations(user_ops, agent_ops)


def _seed_schedule(db_path: str, sid: str, agent_name: str = "agent-1",
                   enabled: bool = True, deleted_at: str | None = None,
                   webhook_token: str | None = None):
    conn = sqlite3.connect(db_path)
    # Live owner row so the agent-level JOIN in
    # list_all_enabled_schedules() passes (these tests exercise
    # *schedule* soft-delete; the agent itself stays live).
    conn.execute(
        "INSERT OR IGNORE INTO agent_ownership"
        "(agent_name, owner_id, created_at, deleted_at) "
        "VALUES (?, 1, '2026-01-01T00:00:00Z', NULL)",
        (agent_name,),
    )
    conn.execute(
        """
        INSERT INTO agent_schedules
            (id, agent_name, name, cron_expression, message, enabled,
             owner_id, created_at, updated_at, deleted_at, webhook_token,
             webhook_enabled)
        VALUES (?, ?, 'sched', '0 0 * * *', 'hi', ?, 1,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',
                ?, ?, ?)
        """,
        (sid, agent_name, 1 if enabled else 0, deleted_at, webhook_token,
         1 if webhook_token else 0),
    )
    # Add a sample execution
    conn.execute(
        "INSERT INTO schedule_executions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"exec-{sid}", sid, agent_name, "completed",
         "2026-01-01T00:00:00Z", "hi", "schedule"),
    )
    conn.commit()
    conn.close()


def _count(db_path: str, table: str, where: str = "1=1", params: tuple = ()) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
    conn.close()
    return n


def _deleted_at(db_path: str, sid: str):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT deleted_at FROM agent_schedules WHERE id = ?", (sid,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# -----------------------------------------------------------------------------
# delete_schedule → soft-delete
# -----------------------------------------------------------------------------

def test_delete_schedule_sets_deleted_at(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "sid-1")

    assert schedule_ops.delete_schedule("sid-1", "owner") is True

    # Row still present, deleted_at set
    assert _count(tmp_schedule_db, "agent_schedules", "id=?", ("sid-1",)) == 1
    assert _deleted_at(tmp_schedule_db, "sid-1") is not None
    # Execution row preserved (KEEP-policy via subscription rollup parity)
    assert _count(tmp_schedule_db, "schedule_executions", "schedule_id=?", ("sid-1",)) == 1


def test_delete_schedule_idempotent(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "sid-1", deleted_at="2026-01-01T00:00:00Z")

    # Second delete on already-soft-deleted row → True (idempotent)
    assert schedule_ops.delete_schedule("sid-1", "owner") is True


def test_delete_schedule_rejects_non_owner(tmp_schedule_db, schedule_ops):
    """A non-owner non-admin can't soft-delete someone else's schedule."""
    _seed_schedule(tmp_schedule_db, "sid-1")
    # Create another user
    conn = sqlite3.connect(tmp_schedule_db)
    conn.execute("INSERT INTO users(id, username, role) VALUES (3, 'eve', 'user')")
    conn.commit()
    conn.close()

    assert schedule_ops.delete_schedule("sid-1", "eve") is False
    assert _deleted_at(tmp_schedule_db, "sid-1") is None  # still live


def test_admin_can_soft_delete_any_schedule(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "sid-1")
    assert schedule_ops.delete_schedule("sid-1", "admin") is True
    assert _deleted_at(tmp_schedule_db, "sid-1") is not None


# -----------------------------------------------------------------------------
# Read paths excluding soft-deleted
# -----------------------------------------------------------------------------

def test_get_schedule_returns_none_for_soft_deleted(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "sid-1", deleted_at="2026-01-01T00:00:00Z")
    assert schedule_ops.get_schedule("sid-1") is None


def test_list_agent_schedules_excludes_soft_deleted(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "live-1")
    _seed_schedule(tmp_schedule_db, "deleted-1", deleted_at="2026-01-01T00:00:00Z")

    schedules = schedule_ops.list_agent_schedules("agent-1")
    ids = {s.id for s in schedules}
    assert "live-1" in ids
    assert "deleted-1" not in ids


def test_list_enabled_schedules_excludes_soft_deleted(tmp_schedule_db, schedule_ops):
    """Critical for the scheduler firing list — soft-deleted must NOT appear."""
    _seed_schedule(tmp_schedule_db, "live-on", enabled=True)
    _seed_schedule(tmp_schedule_db, "deleted-on", enabled=True,
                   deleted_at="2026-01-01T00:00:00Z")
    _seed_schedule(tmp_schedule_db, "live-off", enabled=False)

    schedules = schedule_ops.list_all_enabled_schedules()
    ids = {s.id for s in schedules}
    assert "live-on" in ids
    assert "deleted-on" not in ids
    assert "live-off" not in ids  # disabled — not enabled


def test_get_schedule_by_webhook_token_excludes_soft_deleted(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "live-wh", webhook_token="tok-live")
    _seed_schedule(tmp_schedule_db, "del-wh",
                   webhook_token="tok-del", deleted_at="2026-01-01T00:00:00Z")

    assert schedule_ops.get_schedule_by_webhook_token("tok-live") is not None
    assert schedule_ops.get_schedule_by_webhook_token("tok-del") is None


# -----------------------------------------------------------------------------
# purge_schedule + retention sweep helpers
# -----------------------------------------------------------------------------

def test_purge_schedule_removes_row_and_executions(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "sid-1", deleted_at="2026-01-01T00:00:00Z")
    assert schedule_ops.purge_schedule("sid-1") is True

    assert _count(tmp_schedule_db, "agent_schedules", "id=?", ("sid-1",)) == 0
    # Executions for this schedule_id also gone
    assert _count(tmp_schedule_db, "schedule_executions", "schedule_id=?", ("sid-1",)) == 0


def test_purge_schedule_refuses_live_row(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "live-1")  # deleted_at NULL
    assert schedule_ops.purge_schedule("live-1") is False
    # Untouched
    assert _count(tmp_schedule_db, "agent_schedules", "id=?", ("live-1",)) == 1


def test_find_soft_deleted_schedules_respects_cutoff(tmp_schedule_db, schedule_ops):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    recent = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    _seed_schedule(tmp_schedule_db, "live")
    _seed_schedule(tmp_schedule_db, "old-ghost", deleted_at=old)
    _seed_schedule(tmp_schedule_db, "recent-ghost", deleted_at=recent)

    eligible = schedule_ops.find_soft_deleted_schedules_past_retention(retention_days=30)
    assert "old-ghost" in eligible
    assert "recent-ghost" not in eligible
    assert "live" not in eligible


def test_find_soft_deleted_disabled_when_retention_zero(tmp_schedule_db, schedule_ops):
    _seed_schedule(tmp_schedule_db, "ancient", deleted_at="2020-01-01T00:00:00Z")
    assert schedule_ops.find_soft_deleted_schedules_past_retention(retention_days=0) == []
