"""
Fleet sync-audit tests (#390 / S6).

The audit endpoint aggregates per-agent sync state (#389 data) with the
duplicate-binding check the spec specifies (shared `(github_repo,
working_branch)` pairs where `source_mode = 0`).

These are pure unit tests: the DB layer's `find_duplicate_bindings` helper
is exercised against an in-memory SQLite, and the aggregation function
used by the router is called directly with a mocked agent client.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _BACKEND / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_schema = _load("db/schema.py", "_schema_fa")
_migrations = _load("db/migrations.py", "_migrations_fa")

# Also import the canonical `db.schema` / `db.migrations` so we can patch the
# objects that `database.DatabaseManager.__init__` will reach for. Without
# this, our patches below land on the file-loaded copies above and the real
# imports inside `database.py` see the unpatched originals.
import db.schema as _real_schema  # noqa: E402
import db.migrations as _real_migrations  # noqa: E402


pytestmark = pytest.mark.unit


def _evict_db_modules() -> None:
    """Evict cached db.* modules so the next import re-binds to TRINITY_DB_PATH.

    `db/connection.py` captures DB_PATH at import time, so without an explicit
    eviction the previous test's path lingers across runs.
    """
    for modname in list(sys.modules):
        if modname == "database" or modname.startswith("db.") \
                or modname == "services.sync_health_service" \
                or modname == "services.fleet_audit_service":
            sys.modules.pop(modname, None)


def _patch_s7_index_out():
    """Re-apply the S7 index/migration patches after a module-cache eviction.

    Eviction wipes our module-level patches on `db.schema.INDEXES` and the
    branch-ownership migration. We re-import + re-patch so the next call to
    `database.DatabaseManager.__init__` sees the patched module.
    """
    import db.schema as _s
    import db.migrations as _m
    _s.INDEXES = [s for s in _s.INDEXES if _S7_INDEX_NEEDLE not in s]
    if hasattr(_m, "_migrate_agent_git_config_branch_ownership"):
        _m._migrate_agent_git_config_branch_ownership = lambda cursor, conn: None


# S7 Layer 2: idx_git_config_repo_branch_unique is a partial UNIQUE index that
# prevents the duplicate (github_repo, working_branch) state in production.
# The runtime detector at db/schedules.py::find_duplicate_bindings exists for
# repos that pre-date the index. To exercise the detector we need to seed the
# impossible-in-prod state, so we patch BOTH:
#   1. `INDEXES` — so init_schema (called twice via DatabaseManager.__init__)
#      doesn't recreate the index after duplicate rows exist.
#   2. The S7 migration — so run_all_migrations (also called twice) doesn't
#      raise the duplicate-detection error before the test even runs.
# Production schema/migrations are untouched (verified by the partner test
# tests/git-sync/test_s7_reserve_instance_id.py which exercises the index
# directly).
_S7_INDEX_NEEDLE = "idx_git_config_repo_branch_unique"
for _mod in (_schema, _real_schema):
    _mod.INDEXES = [s for s in _mod.INDEXES if _S7_INDEX_NEEDLE not in s]
for _mod in (_migrations, _real_migrations):
    if hasattr(_mod, "_migrate_agent_git_config_branch_ownership"):
        _mod._migrate_agent_git_config_branch_ownership = lambda cursor, conn: None


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    _schema.init_schema(cur, conn)
    _migrations.run_all_migrations(cur, conn)
    # The S7 partial UNIQUE index is also created by the migration path; drop
    # it here so the duplicate-row seed succeeds in this in-memory DB.
    cur.execute(f"DROP INDEX IF EXISTS {_S7_INDEX_NEEDLE}")
    conn.commit()
    conn.close()

    _evict_db_modules()
    _patch_s7_index_out()  # re-apply after eviction wipes the patch
    try:
        yield db_path
    finally:
        # Re-evict on teardown so the next test file sees a clean module
        # cache (db/connection.py:DB_PATH is captured at import time).
        _evict_db_modules()


@pytest.fixture
def seed(tmp_db):
    """Seed agents with git configs and sync state."""
    def _seed(name, *, repo="org/repo", branch=None, source_mode=False,
              last_sync_status=None, ahead_working=0, last_sync_at=None,
              last_commit_sha=None):
        branch = branch or f"trinity/{name}/abc123"
        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES (?, 1, datetime('now'))",
            (name,),
        )
        conn.execute(
            """INSERT INTO agent_git_config
               (id, agent_name, github_repo, working_branch, instance_id,
                created_at, sync_enabled, source_mode, last_sync_at,
                last_commit_sha, auto_sync_enabled)
               VALUES (?, ?, ?, ?, ?, datetime('now'), 1, ?, ?, ?, 1)""",
            (name + "-g", name, repo, branch, "abc123",
             1 if source_mode else 0, last_sync_at, last_commit_sha),
        )
        if last_sync_status:
            conn.execute(
                """INSERT INTO agent_sync_state
                   (agent_name, last_sync_status, last_sync_at,
                    consecutive_failures, ahead_working, behind_working,
                    ahead_main, behind_main, updated_at)
                   VALUES (?, ?, ?, 0, ?, 0, 0, 0, datetime('now'))""",
                (name, last_sync_status, last_sync_at, ahead_working),
            )
        conn.commit()
        conn.close()
    return _seed


class TestFindDuplicateBindings:
    """SQL-level helper identifies agents sharing (repo, branch) pairs."""

    def test_no_duplicates(self, seed):
        seed("a", branch="trinity/a/abc")
        seed("b", branch="trinity/b/def")
        from db.schedules import ScheduleOperations
        ops = ScheduleOperations(user_ops=None, agent_ops=None)
        assert ops.find_duplicate_bindings() == set()

    def test_detects_pair_sharing_working_branch(self, seed):
        seed("alpha", repo="org/repo", branch="trinity/shared/same")
        seed("beta", repo="org/repo", branch="trinity/shared/same")
        from db.schedules import ScheduleOperations
        ops = ScheduleOperations(user_ops=None, agent_ops=None)
        assert ops.find_duplicate_bindings() == {"alpha", "beta"}

    def test_source_mode_rows_excluded(self, seed):
        """Source-mode agents legitimately share branches (#382 spec)."""
        seed("a", repo="org/repo", branch="main", source_mode=True)
        seed("b", repo="org/repo", branch="main", source_mode=True)
        from db.schedules import ScheduleOperations
        ops = ScheduleOperations(user_ops=None, agent_ops=None)
        assert ops.find_duplicate_bindings() == set()

    def test_one_source_one_legacy_same_branch_not_flagged(self, seed):
        """Source-mode row must not drag the legacy peer into the duplicate set."""
        seed("src", repo="org/repo", branch="main", source_mode=True)
        seed("leg", repo="org/repo", branch="main", source_mode=False)
        from db.schedules import ScheduleOperations
        ops = ScheduleOperations(user_ops=None, agent_ops=None)
        # Only one legacy row on that branch → nothing to flag.
        assert ops.find_duplicate_bindings() == set()


class TestBuildFleetSyncAudit:
    """The service function aggregates DB + live agent data."""

    def test_empty_fleet(self, tmp_db):
        from services.fleet_audit_service import build_fleet_sync_audit
        result = asyncio.run(build_fleet_sync_audit(agent_names=[]))
        assert result["agents"] == []
        assert result["summary"]["total"] == 0

    def test_single_clean_agent(self, seed):
        seed("alpha", last_sync_status="success",
             last_sync_at="2026-04-18T10:00:00+00:00",
             last_commit_sha="abc123")
        from services.fleet_audit_service import build_fleet_sync_audit
        result = asyncio.run(build_fleet_sync_audit(agent_names=["alpha"]))
        assert len(result["agents"]) == 1
        entry = result["agents"][0]
        assert entry["name"] == "alpha"
        assert entry["branch"] == "trinity/alpha/abc123"
        assert entry["last_pushed_sha"] == "abc123"
        assert entry["duplicate_binding"] is False
        assert entry["unpushed_commits"] == 0

    def test_duplicate_binding_flagged(self, seed):
        seed("a", repo="org/repo", branch="trinity/x/shared",
             last_sync_status="success")
        seed("b", repo="org/repo", branch="trinity/x/shared",
             last_sync_status="success")
        from services.fleet_audit_service import build_fleet_sync_audit
        result = asyncio.run(build_fleet_sync_audit(agent_names=["a", "b"]))
        names = {e["name"]: e for e in result["agents"]}
        assert names["a"]["duplicate_binding"] is True
        assert names["b"]["duplicate_binding"] is True
        assert result["summary"]["duplicate_bindings"] == 2

    def test_unpushed_commits_from_ahead_working(self, seed):
        seed("alpha", last_sync_status="success", ahead_working=3)
        from services.fleet_audit_service import build_fleet_sync_audit
        result = asyncio.run(build_fleet_sync_audit(agent_names=["alpha"]))
        assert result["agents"][0]["unpushed_commits"] == 3
        assert result["summary"]["ahead"] == 1

    def test_filter_agents_respected(self, seed):
        seed("a", last_sync_status="success")
        seed("b", last_sync_status="success")
        from services.fleet_audit_service import build_fleet_sync_audit
        # Only 'a' is accessible → 'b' must not appear.
        result = asyncio.run(build_fleet_sync_audit(agent_names=["a"]))
        assert [e["name"] for e in result["agents"]] == ["a"]
        assert result["summary"]["total"] == 1
