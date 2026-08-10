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
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

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

_TS = "2026-01-01T00:00:00Z"


@pytest.fixture
def tmp_db(db_backend):
    """Active backend with a fresh full schema (db_harness, #300). Runs on
    SQLite and, when TEST_POSTGRES_URL is set, PostgreSQL.

    These tests deliberately seed impossible-in-prod duplicate (github_repo,
    working_branch) rows to exercise find_duplicate_bindings, so drop the S7
    partial UNIQUE index that prevents that state. DROP INDEX IF EXISTS works
    on both backends. Returns the backend marker."""
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS idx_git_config_repo_branch_unique"))
    return db_backend


@pytest.fixture
def seed(tmp_db):
    """Seed agents with git configs and sync state (engine-based, #300)."""
    def _seed(name, *, repo="org/repo", branch=None, source_mode=False,
              last_sync_status=None, ahead_working=0, last_sync_at=None,
              last_commit_sha=None):
        branch = branch or f"trinity/{name}/abc123"
        _hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES (:n, 1, :ts)", n=name, ts=_TS,
        )
        _hrun(
            "INSERT INTO agent_git_config "
            "(id, agent_name, github_repo, working_branch, instance_id, "
            " created_at, sync_enabled, source_mode, last_sync_at, "
            " last_commit_sha, auto_sync_enabled) "
            "VALUES (:gid, :n, :repo, :br, 'abc123', :ts, 1, :sm, :lsa, :lcs, 1)",
            gid=name + "-g", n=name, repo=repo, br=branch,
            sm=1 if source_mode else 0, lsa=last_sync_at, lcs=last_commit_sha, ts=_TS,
        )
        if last_sync_status:
            _hrun(
                "INSERT INTO agent_sync_state "
                "(agent_name, last_sync_status, last_sync_at, consecutive_failures, "
                " ahead_working, behind_working, ahead_main, behind_main, updated_at) "
                "VALUES (:n, :st, :lsa, 0, :aw, 0, 0, 0, :ts)",
                n=name, st=last_sync_status, lsa=last_sync_at, aw=ahead_working, ts=_TS,
            )
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
