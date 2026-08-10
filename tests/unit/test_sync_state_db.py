"""
Sync State DB Operations Tests (Issue #389 — S1).

Unit tests for the agent_sync_state table and SyncStateOperations class
that backs the sync-health observability feature.

Backend-agnostic via ``db_harness`` (#300): runs on SQLite and, when
``TEST_POSTGRES_URL`` is set, PostgreSQL too. Schema introspection uses
SQLAlchemy ``inspect()`` (dialect-agnostic) instead of SQLite ``PRAGMA`` /
``sqlite_master`` so the column checks hold on both backends.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
# #2080: the shadow-eviction loop that used to sit here is GONE. It popped
# `utils` (and the test-helper submodules) from sys.modules to defeat
# `tests/utils` shadowing `src/backend/utils`. That package is now
# `tests/testkit`, so `utils` IS the backend package — and popping it
# evicted the canonical module mid-session, leaving anything that had
# already imported it holding a stale reference (observed as
# `ImportError: module services.subscription_auto_switch not in sys.modules`
# from an importlib.reload several hundred tests later).
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402


pytestmark = pytest.mark.unit


def _inspector():
    from sqlalchemy import inspect
    from db.engine import get_engine

    return inspect(get_engine())


@pytest.fixture
def tmp_db(db_backend):
    """Active backend with a fresh full schema (db_harness, #300). Returns the
    backend marker. Pops cached db modules so production code re-resolves."""
    for modname in list(sys.modules):
        if modname == "database" or modname.startswith("db."):
            # keep the harness-loaded engine/tables modules importable
            if modname in ("db.engine", "db.tables", "db.schema"):
                continue
            sys.modules.pop(modname, None)
    return db_backend


@pytest.fixture
def seed_agent(tmp_db):
    """Helper: insert an agent_ownership row so FK constraints pass."""
    def _seed(name: str):
        _hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES (:n, 1, '2026-01-01T00:00:00Z')",
            n=name,
        )
    return _seed


@pytest.fixture
def sync_ops(tmp_db):
    """Fresh SyncStateOperations bound to the active backend."""
    from db.sync_state import SyncStateOperations  # noqa: WPS433
    return SyncStateOperations()


class TestSyncStateTable:
    """Schema build creates the agent_sync_state table with expected columns."""

    def test_table_exists(self, tmp_db):
        assert _inspector().has_table("agent_sync_state"), \
            "agent_sync_state table should exist"

    def test_expected_columns(self, tmp_db):
        cols = {c["name"] for c in _inspector().get_columns("agent_sync_state")}
        expected = {
            "agent_name",
            "last_sync_at",
            "last_sync_status",
            "consecutive_failures",
            "last_error_summary",
            "last_remote_sha_main",
            "last_remote_sha_working",
            "ahead_main",
            "behind_main",
            "ahead_working",
            "behind_working",
            "last_check_at",
            "updated_at",
        }
        missing = expected - cols
        assert not missing, f"Missing columns: {missing}"

    def test_agent_git_config_auto_sync_columns_added(self, tmp_db):
        """Schema includes auto_sync_enabled and freeze_schedules_if_sync_failing."""
        cols = {c["name"] for c in _inspector().get_columns("agent_git_config")}
        assert "auto_sync_enabled" in cols
        assert "freeze_schedules_if_sync_failing" in cols


class TestSyncStateUpsert:
    """SyncStateOperations.upsert persists/refreshes a row per agent."""

    def test_get_returns_none_when_absent(self, sync_ops, seed_agent):
        seed_agent("alpha")
        assert sync_ops.get("alpha") is None

    def test_upsert_creates_row(self, sync_ops, seed_agent):
        seed_agent("alpha")
        sync_ops.upsert(
            agent_name="alpha",
            last_sync_at="2026-04-18T10:00:00+00:00",
            last_sync_status="success",
            last_error_summary=None,
            last_remote_sha_main="abc",
            last_remote_sha_working="def",
            ahead_main=1,
            behind_main=0,
            ahead_working=0,
            behind_working=0,
        )
        row = sync_ops.get("alpha")
        assert row is not None
        assert row["last_sync_status"] == "success"
        assert row["consecutive_failures"] == 0
        assert row["ahead_main"] == 1
        assert row["last_remote_sha_main"] == "abc"
        assert row["last_remote_sha_working"] == "def"

    def test_upsert_updates_existing(self, sync_ops, seed_agent):
        seed_agent("alpha")
        sync_ops.upsert(agent_name="alpha", last_sync_status="success")
        sync_ops.upsert(agent_name="alpha", last_sync_status="failed",
                        last_error_summary="boom")
        row = sync_ops.get("alpha")
        assert row["last_sync_status"] == "failed"
        assert row["last_error_summary"] == "boom"


class TestConsecutiveFailures:
    """consecutive_failures increments on failure, resets on success."""

    def test_increment_on_failure(self, sync_ops, seed_agent):
        seed_agent("alpha")
        sync_ops.upsert(agent_name="alpha", last_sync_status="failed",
                        last_error_summary="e1")
        sync_ops.upsert(agent_name="alpha", last_sync_status="failed",
                        last_error_summary="e2")
        sync_ops.upsert(agent_name="alpha", last_sync_status="failed",
                        last_error_summary="e3")
        row = sync_ops.get("alpha")
        assert row["consecutive_failures"] == 3

    def test_reset_on_success(self, sync_ops, seed_agent):
        seed_agent("alpha")
        sync_ops.upsert(agent_name="alpha", last_sync_status="failed",
                        last_error_summary="e1")
        sync_ops.upsert(agent_name="alpha", last_sync_status="failed",
                        last_error_summary="e2")
        sync_ops.upsert(agent_name="alpha", last_sync_status="success")
        row = sync_ops.get("alpha")
        assert row["consecutive_failures"] == 0
        assert row["last_sync_status"] == "success"

    def test_never_status_does_not_increment(self, sync_ops, seed_agent):
        """An initial 'never' upsert (no attempt yet) should not count as a failure."""
        seed_agent("alpha")
        sync_ops.upsert(agent_name="alpha", last_sync_status="never")
        assert sync_ops.get("alpha")["consecutive_failures"] == 0


class TestListAll:
    """list_all returns every tracked agent."""

    def test_list_all(self, sync_ops, seed_agent):
        seed_agent("a")
        seed_agent("b")
        sync_ops.upsert(agent_name="a", last_sync_status="success")
        sync_ops.upsert(agent_name="b", last_sync_status="failed",
                        last_error_summary="e")
        rows = {r["agent_name"]: r for r in sync_ops.list_all()}
        assert set(rows) == {"a", "b"}
        assert rows["b"]["last_sync_status"] == "failed"

    def test_list_many_is_empty_initially(self, sync_ops, seed_agent):
        assert sync_ops.list_all() == []


class TestDeleteOnAgentDelete:
    """When an agent is deleted, its sync_state row should go too (FK CASCADE or explicit)."""

    def test_explicit_delete(self, sync_ops, seed_agent):
        seed_agent("alpha")
        sync_ops.upsert(agent_name="alpha", last_sync_status="success")
        sync_ops.delete("alpha")
        assert sync_ops.get("alpha") is None
