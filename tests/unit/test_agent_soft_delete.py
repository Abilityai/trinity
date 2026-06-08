"""
Tests for agent soft-delete + retention purge (Issue #834 Phase 1a).

Exercises `AgentOperations.delete_agent_ownership` + `purge_agent_ownership` +
`find_soft_deleted_agents_past_retention` against an ephemeral SQLite DB.
Children stay intact on soft-delete; `purge` cascades them via the #816
primitive.

Connection routing: `db.connection.DB_PATH` is read at module-import
time, so once the production `db.connection` is loaded (transitively
via any import of `db.agents`), changing `TRINITY_DB_PATH` env has no
effect. The `_route_to_tmp_db` fixture patches the module attribute
directly via monkeypatch.setattr — survives whatever order pytest's
test discovery imports things.
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


def _make_db_schema(conn: sqlite3.Connection) -> None:
    """Build a representative subset of Trinity's schema."""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE agent_ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT
        )
        """
    )
    # Child tables from #816 registry — to verify cascade fires on purge.
    for t in ("agent_sharing", "agent_schedules", "chat_messages",
              "agent_activities", "agent_skills", "agent_tags"):
        cur.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT)")
    cur.execute(
        "CREATE TABLE schedule_executions (id INTEGER PRIMARY KEY, agent_name TEXT)"  # KEEP-policy
    )
    cur.execute(
        "CREATE TABLE nevermined_payment_log (id INTEGER PRIMARY KEY, agent_name TEXT)"  # KEEP
    )
    cur.execute(
        "CREATE TABLE mcp_api_keys (id INTEGER PRIMARY KEY, agent_name TEXT, scope TEXT)"
    )
    cur.execute(
        "CREATE TABLE agent_permissions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, source_agent TEXT, target_agent TEXT)"
    )
    cur.execute(
        "CREATE TABLE agent_event_subscriptions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, subscriber_agent TEXT, source_agent TEXT)"
    )
    cur.execute(
        "CREATE TABLE agent_events "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, source_agent TEXT)"
    )
    cur.execute(
        "CREATE INDEX idx_agent_ownership_deleted_at "
        "ON agent_ownership(deleted_at) WHERE deleted_at IS NOT NULL"
    )
    conn.commit()


@pytest.fixture
def tmp_agent_db(tmp_path, monkeypatch):
    """Route the production `db.connection.get_db_connection()` to an
    ephemeral SQLite file and return its path.

    Skips the test if the backend package itself can't be imported
    (no `pydantic`/`fastapi` available — local dev w/o venv).
    """
    try:
        import db.connection as connection_mod
    except ImportError:
        pytest.skip("backend venv required (no `db.connection` import)")

    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    _make_db_schema(conn)
    conn.close()

    # Patch the module attribute — `get_db_connection()` reads
    # `DB_PATH` on every call (sqlite3.connect happens per
    # context-manager entry), so the override sticks.
    monkeypatch.setattr(connection_mod, "DB_PATH", str(db_path))
    return str(db_path)


@pytest.fixture
def agent_ops(tmp_agent_db):
    """Construct `AgentOperations` routed to the ephemeral DB."""
    try:
        from db.agents import AgentOperations
        from db.users import UserOperations
    except ImportError:
        pytest.skip("backend venv required (no `db.agents` import)")
    return AgentOperations(UserOperations())


def _seed(conn, name: str, deleted_at: str | None = None):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO agent_ownership(agent_name, owner_id, created_at, deleted_at) "
        "VALUES (?, 1, '2026-01-01T00:00:00Z', ?)",
        (name, deleted_at),
    )
    for t in ("agent_sharing", "agent_schedules", "chat_messages",
              "agent_activities", "agent_skills", "agent_tags"):
        cur.execute(f"INSERT INTO {t}(agent_name) VALUES (?)", (name,))
    cur.execute("INSERT INTO schedule_executions(agent_name) VALUES (?)", (name,))
    cur.execute("INSERT INTO nevermined_payment_log(agent_name) VALUES (?)", (name,))
    cur.execute("INSERT INTO mcp_api_keys(agent_name, scope) VALUES (?, 'agent')", (name,))
    conn.commit()


def _seed_into(db_path: str, name: str, deleted_at: str | None = None):
    conn = sqlite3.connect(db_path)
    _seed(conn, name, deleted_at)
    conn.close()


def _count(db_path: str, table: str, where: str, params: tuple) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
    conn.close()
    return n


def _deleted_at(db_path: str, name: str):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT deleted_at FROM agent_ownership WHERE agent_name = ?", (name,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# -----------------------------------------------------------------------------
# delete_agent_ownership → soft-delete
# -----------------------------------------------------------------------------

def test_delete_sets_deleted_at(tmp_agent_db, agent_ops):
    _seed_into(tmp_agent_db, "doomed")

    assert agent_ops.delete_agent_ownership("doomed") is True

    assert _count(tmp_agent_db, "agent_ownership", "agent_name=?", ("doomed",)) == 1, \
        "row must persist after soft-delete"
    assert _deleted_at(tmp_agent_db, "doomed") is not None, "deleted_at must be set"

    # Children untouched
    for t in ("agent_sharing", "agent_schedules", "chat_messages",
              "agent_activities", "agent_skills", "agent_tags",
              "schedule_executions", "nevermined_payment_log"):
        n = _count(tmp_agent_db, t, "agent_name=?", ("doomed",))
        assert n == 1, f"{t} child row must survive soft-delete"


def test_delete_idempotent_on_already_soft_deleted(tmp_agent_db, agent_ops):
    _seed_into(tmp_agent_db, "twice", deleted_at="2026-01-01T00:00:00Z")

    # Second delete on already-deleted row → True (idempotent)
    assert agent_ops.delete_agent_ownership("twice") is True
    # Nonexistent → False
    assert agent_ops.delete_agent_ownership("ghost") is False


# -----------------------------------------------------------------------------
# purge_agent_ownership → cascade_delete + final row removal
# -----------------------------------------------------------------------------

def test_purge_runs_cascade_and_removes_owner_row(tmp_agent_db, agent_ops):
    _seed_into(tmp_agent_db, "purgeme", deleted_at="2026-01-01T00:00:00Z")

    assert agent_ops.purge_agent_ownership("purgeme") is True

    # Owner row gone
    assert _count(tmp_agent_db, "agent_ownership", "agent_name=?", ("purgeme",)) == 0
    # CASCADE children gone
    for t in ("agent_sharing", "agent_schedules", "chat_messages",
              "agent_activities", "agent_skills", "agent_tags"):
        n = _count(tmp_agent_db, t, "agent_name=?", ("purgeme",))
        assert n == 0, f"{t} should be wiped after purge"
    # KEEP children survive (billing rollups)
    assert _count(tmp_agent_db, "schedule_executions", "agent_name=?", ("purgeme",)) == 1
    assert _count(tmp_agent_db, "nevermined_payment_log", "agent_name=?", ("purgeme",)) == 1


def test_purge_refuses_live_agent(tmp_agent_db, agent_ops):
    """Safety: can't purge a row that hasn't been soft-deleted first."""
    _seed_into(tmp_agent_db, "alive")  # deleted_at IS NULL

    # Refuses — returns False, leaves everything intact
    assert agent_ops.purge_agent_ownership("alive") is False
    assert _count(tmp_agent_db, "agent_ownership", "agent_name=?", ("alive",)) == 1
    assert _count(tmp_agent_db, "agent_sharing", "agent_name=?", ("alive",)) == 1


# -----------------------------------------------------------------------------
# find_soft_deleted_agents_past_retention
# -----------------------------------------------------------------------------

def test_find_soft_deleted_respects_cutoff(tmp_agent_db, agent_ops):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    recent = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    _seed_into(tmp_agent_db, "live")  # not soft-deleted
    _seed_into(tmp_agent_db, "old_ghost", deleted_at=old)
    _seed_into(tmp_agent_db, "recent_ghost", deleted_at=recent)

    # 30-day retention: only the 60-day-old ghost qualifies
    eligible = agent_ops.find_soft_deleted_agents_past_retention(retention_days=30)
    assert "old_ghost" in eligible
    assert "recent_ghost" not in eligible
    assert "live" not in eligible


def test_find_soft_deleted_disabled_when_retention_zero(tmp_agent_db, agent_ops):
    """retention_days=0 disables the sweep — return [] regardless of state."""
    _seed_into(tmp_agent_db, "ancient", deleted_at="2020-01-01T00:00:00Z")

    assert agent_ops.find_soft_deleted_agents_past_retention(retention_days=0) == []


# -----------------------------------------------------------------------------
# is_agent_name_reserved — the name-reservation companion (unfiltered)
# -----------------------------------------------------------------------------

def test_name_reservation_sees_soft_deleted_rows(tmp_agent_db, agent_ops):
    """The unfiltered helper must return True for both live AND
    soft-deleted rows so create-time existence checks don't overwrite."""
    _seed_into(tmp_agent_db, "alive")
    _seed_into(tmp_agent_db, "soft_deleted_ghost", deleted_at="2026-01-01T00:00:00Z")

    assert agent_ops.is_agent_name_reserved("alive") is True
    assert agent_ops.is_agent_name_reserved("soft_deleted_ghost") is True
    assert agent_ops.is_agent_name_reserved("nonexistent") is False
