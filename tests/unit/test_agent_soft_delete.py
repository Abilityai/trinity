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

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402


@pytest.fixture
def tmp_agent_db(db_backend):
    """Active backend with a fresh FULL production schema (db_harness, #300).

    Runs on SQLite and, when TEST_POSTGRES_URL is set, PostgreSQL. Returns the
    backend marker (leading positional arg the seed/count helpers accept).
    Replaces the prior simplified hand-rolled cascade schema — the seeds below
    now supply full valid rows against the real tables (NOT NULL columns and
    real id/PK types), so the #816 cascade primitive is exercised on the
    production schema on both backends."""
    return db_backend


@pytest.fixture
def agent_ops(tmp_agent_db):
    """Construct `AgentOperations` routed to the ephemeral DB."""
    try:
        from db.agents import AgentOperations
        from db.users import UserOperations
    except ImportError:
        pytest.skip("backend venv required (no `db.agents` import)")
    return AgentOperations(UserOperations())


_TS = "2026-01-01T00:00:00Z"


def _seed_into(_db, name: str, deleted_at: str | None = None):
    """Seed an agent + one valid row in each cascade child table on the real
    schema (full NOT NULL columns, correct id/PK types). Engine-based so it
    runs on both backends. First positional arg is the ignored backend marker."""
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at) "
        "VALUES (:n, 1, :ts, :deleted)", n=name, ts=_TS, deleted=deleted_at,
    )
    # WIPE-on-purge children
    _hrun("INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id, created_at) "
          "VALUES (:n, 'u@example.com', 1, :ts)", n=name, ts=_TS)
    _hrun("INSERT INTO agent_schedules (id, agent_name, name, cron_expression, message, "
          "owner_id, created_at, updated_at) "
          "VALUES (:id, :n, 's', '0 0 * * *', 'm', 1, :ts, :ts)", id=f"sch-{name}", n=name, ts=_TS)
    _hrun("INSERT INTO chat_messages (id, session_id, agent_name, user_id, user_email, role, "
          "content, timestamp) VALUES (:id, 'sess', :n, 1, 'u@example.com', 'user', 'hi', :ts)",
          id=f"cm-{name}", n=name, ts=_TS)
    _hrun("INSERT INTO agent_activities (id, agent_name, activity_type, activity_state, "
          "started_at, triggered_by) VALUES (:id, :n, 'chat_start', 'started', :ts, 'user')",
          id=f"act-{name}", n=name, ts=_TS)
    _hrun("INSERT INTO agent_skills (agent_name, skill_name, assigned_by, assigned_at) "
          "VALUES (:n, 'sk', 'admin', :ts)", n=name, ts=_TS)
    _hrun("INSERT INTO agent_tags (agent_name, tag) VALUES (:n, 't1')", n=name)
    # #918 reports — CASCADE on purge, re-key on rename (cross-tenant guard)
    _hrun("INSERT INTO agent_reports (id, agent_name, user_id, report_type, title, "
          "payload, created_at) VALUES (:id, :n, 1, 'ops.health', 't', '{}', :ts)",
          id=f"rep-{name}", n=name, ts=_TS)
    # KEEP-on-purge children (billing rollups)
    _hrun("INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, "
          "message, triggered_by) VALUES (:id, :sch, :n, 'success', :ts, 'm', 'schedule')",
          id=f"ex-{name}", sch=f"sch-{name}", n=name, ts=_TS)
    _hrun("INSERT INTO nevermined_payment_log (id, agent_name, action, success, created_at) "
          "VALUES (:id, :n, 'charge', 1, :ts)", id=f"npl-{name}", n=name, ts=_TS)
    # agent-scoped mcp key (cascade target)
    _hrun("INSERT INTO mcp_api_keys (id, name, key_prefix, key_hash, created_at, user_id, "
          "agent_name, scope) VALUES (:id, :n, 'pfx', :kh, :ts, 1, :n, 'agent')",
          id=f"key-{name}", n=name, kh=f"hash-{name}", ts=_TS)


def _count(_db, table: str, where: str, params: tuple) -> int:
    binds = {f"p{i}": v for i, v in enumerate(params)}
    clause = where
    for i in range(len(params)):
        clause = clause.replace("?", f":p{i}", 1)
    return _hscalar(f"SELECT COUNT(*) FROM {table} WHERE {clause}", **binds) or 0


def _deleted_at(_db, name: str):
    return _hscalar(
        "SELECT deleted_at FROM agent_ownership WHERE agent_name = :n", n=name
    )


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
              "agent_activities", "agent_skills", "agent_tags", "agent_reports",
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
              "agent_activities", "agent_skills", "agent_tags", "agent_reports"):
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


# -----------------------------------------------------------------------------
# rename_agent re-keys reports (#918) — orphaned rows under a freed name are a
# cross-tenant disclosure when the name is reused, so reports must move with it.
# -----------------------------------------------------------------------------

def test_rename_rekeys_reports(tmp_agent_db, agent_ops):
    _seed_into(tmp_agent_db, "old_name")

    assert agent_ops.rename_agent("old_name", "new_name") is True

    # The report follows the rename — no row left under the freed old name.
    assert _count(tmp_agent_db, "agent_reports", "agent_name=?", ("old_name",)) == 0, \
        "no orphaned report under the freed old name (cross-tenant guard)"
    assert _count(tmp_agent_db, "agent_reports", "agent_name=?", ("new_name",)) == 1, \
        "report must be re-keyed to the new agent name"
