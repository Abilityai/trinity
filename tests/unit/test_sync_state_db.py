"""
Sync State DB Operations Tests (Issue #389 — S1).

Unit tests for the agent_sync_state table and SyncStateOperations class
that backs the sync-health observability feature.

Run in-process against an ephemeral SQLite database (no backend, no Docker).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable (same shim as test_backlog.py).
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _load_module(rel_path: str, name: str):
    path = _BACKEND / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_schema_mod = _load_module("db/schema.py", "_schema_sync")
_migrations_mod = _load_module("db/migrations.py", "_migrations_sync")
init_schema = _schema_mod.init_schema
run_all_migrations = _migrations_mod.run_all_migrations


pytestmark = pytest.mark.unit


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Full schema + migrations in a throwaway SQLite file."""
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    init_schema(cursor, conn)
    run_all_migrations(cursor, conn)
    conn.commit()
    conn.close()

    # Drop cached modules so production code picks up the new path.
    for modname in list(sys.modules):
        if modname == "database" or modname.startswith("db."):
            sys.modules.pop(modname, None)

    yield db_path


@pytest.fixture
def seed_agent(tmp_db):
    """Helper: insert an agent_ownership row so FK constraints pass."""
    def _seed(name: str):
        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES (?, 1, datetime('now'))",
            (name,),
        )
        conn.commit()
        conn.close()
    return _seed


@pytest.fixture
def sync_ops(tmp_db):
    """Fresh SyncStateOperations bound to tmp_db."""
    from db.sync_state import SyncStateOperations  # noqa: WPS433
    return SyncStateOperations()


class TestSyncStateTable:
    """Migration creates the agent_sync_state table with the expected columns."""

    def test_table_exists(self, tmp_db):
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_sync_state'"
        ).fetchone()
        conn.close()
        assert row is not None, "agent_sync_state table should exist"

    def test_expected_columns(self, tmp_db):
        conn = sqlite3.connect(str(tmp_db))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_sync_state)")}
        conn.close()
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
        """Migration adds auto_sync_enabled and freeze_schedules_if_sync_failing."""
        conn = sqlite3.connect(str(tmp_db))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_git_config)")}
        conn.close()
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
