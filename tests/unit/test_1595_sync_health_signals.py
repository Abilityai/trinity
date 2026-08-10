"""#1595 — git-maintenance health signals (backend slice).

The killed-auto-gc failure class was "completely silent until the disk fills".
Covers:
  - boundary coercion of agent-supplied sync-state ints (`sync-state.json` is
    agent-writable — never trust its JSON values)
  - pack_count / loose_objects / maintenance_failures round-trip through the
    REAL SyncStateOperations + db_harness (a live select is the only guard
    that catches a missed db/tables.py column — learnings 2026-06-23)
  - edge-triggered git_bloat operator alerts (size ceiling + maintenance
    failure streak), firing once per crossing like sync_failing
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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


# This file evicts shadow `utils.*` entries at import time and (in fixtures)
# pops cached backend db/service modules so db_harness loads a fresh schema.
# The autouse fixture below snapshots + restores those entries per test so the
# swaps can't leak into sibling test files (Issue #762; sanctioned
# import-time-stub pattern — precedent: tests/unit/test_telegram_webhook_backfill.py).
_STUBBED_MODULE_NAMES = [
    "utils",
    "utils.api_client",
    "utils.assertions",
    "utils.cleanup",
    "database",
    "services.sync_health_service",
    "services.agent_client",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def tmp_db(db_backend, monkeypatch):
    """Fresh FULL schema; evict cached db/service modules; stub agent_client
    (tests patch _fetch_git_status so it is never called). Mirrors
    test_sync_health_service.py."""
    for modname in list(sys.modules):
        if modname == "database" or modname.startswith("db.") \
                or modname in ("services.sync_health_service", "services.agent_client"):
            if modname in ("db.engine", "db.tables", "db.schema"):
                continue
            sys.modules.pop(modname, None)
    monkeypatch.setitem(
        sys.modules,
        "services.agent_client",
        types.SimpleNamespace(AgentClient=MagicMock()),
    )
    return db_backend


@pytest.fixture
def seed_agent(tmp_db):
    def _seed(name: str):
        _hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES (:n, 1, '2026-01-01T00:00:00Z')",
            n=name,
        )
        _hrun(
            "INSERT INTO agent_git_config "
            "(id, agent_name, github_repo, working_branch, instance_id, "
            " created_at, sync_enabled, auto_sync_enabled) "
            "VALUES (:gid, :n, 'org/repo', :wb, 'abc123', "
            " '2026-01-01T00:00:00Z', 1, 1)",
            gid=name + "-git", n=name, wb=f"trinity/{name}/abc123",
        )
    return _seed


def _payload(**sync_state_extra):
    sync_state = {
        "last_sync_status": "success",
        "last_sync_at": "2026-07-14T10:00:00+00:00",
        "last_error_summary": None,
        "consecutive_failures": 0,
    }
    sync_state.update(sync_state_extra)
    return {
        "git_enabled": True,
        "last_commit": {"sha": "deadbeef"},
        "ahead_main": 0,
        "behind_main": 0,
        "ahead_working": 0,
        "behind_working": 0,
        "sync_state": sync_state,
    }


@pytest.fixture
def service(tmp_db):
    from services.sync_health_service import SyncHealthService
    return SyncHealthService(poll_interval=0)


def _sync_once(service, payload, agent="alpha"):
    service._fetch_git_status = AsyncMock(return_value=payload)
    cfg = types.SimpleNamespace(agent_name=agent)
    asyncio.run(service._sync_agent(cfg))


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


class TestCoerceNonnegInt:
    def test_values(self, tmp_db):
        from services.sync_health_service import _coerce_nonneg_int
        assert _coerce_nonneg_int(42) == 42
        assert _coerce_nonneg_int(0) == 0
        assert _coerce_nonneg_int(None) is None
        assert _coerce_nonneg_int(True) is None  # bool is an int subclass
        assert _coerce_nonneg_int(-1) is None
        assert _coerce_nonneg_int(2**63) is None
        assert _coerce_nonneg_int("1000000") is None
        assert _coerce_nonneg_int({"nested": 1}) is None
        assert _coerce_nonneg_int(3.5) is None

    def test_malformed_agent_values_stored_as_null(self, service, seed_agent, tmp_db):
        seed_agent("alpha")
        _sync_once(service, _payload(
            git_dir_bytes="44 gigabytes",
            pack_count={"evil": True},
            loose_objects=-5,
            maintenance_failures="many",
        ))
        from database import db
        row = db.get_sync_state("alpha")
        assert row["git_dir_bytes"] is None
        assert row["pack_count"] is None
        assert row["loose_objects"] is None
        assert row["maintenance_failures"] == 0


# ---------------------------------------------------------------------------
# Round-trip through the real ops + harness (guards db/tables.py columns)
# ---------------------------------------------------------------------------


class TestGcSignalsRoundTrip:
    def _ops(self):
        from db.sync_state import SyncStateOperations
        return SyncStateOperations()

    def test_upsert_and_live_select(self, tmp_db):
        ops = self._ops()
        ops.upsert(
            "a1", last_sync_status="success",
            pack_count=2267, loose_objects=49289, maintenance_failures=2,
        )
        row = ops.get("a1")
        assert row["pack_count"] == 2267
        assert row["loose_objects"] == 49289
        assert row["maintenance_failures"] == 2

    def test_partial_update_preserves(self, tmp_db):
        ops = self._ops()
        ops.upsert("a1", last_sync_status="success", pack_count=10, loose_objects=20)
        ops.upsert("a1", last_sync_status="failed", last_error_summary="x")
        row = ops.get("a1")
        assert row["pack_count"] == 10
        assert row["loose_objects"] == 20

    def test_maintenance_failures_zero_wins(self, tmp_db):
        """0 is a meaningful reset (post-success), not 'unset'."""
        ops = self._ops()
        ops.upsert("a1", last_sync_status="success", maintenance_failures=3)
        ops.upsert("a1", last_sync_status="success", maintenance_failures=0)
        assert ops.get("a1")["maintenance_failures"] == 0


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------


class TestGitBloatAlerts:
    def test_size_crossing_emits_once(self, service, seed_agent, tmp_db):
        from database import db
        from services.sync_health_service import GIT_DIR_ALERT_BYTES
        seed_agent("alpha")

        below = GIT_DIR_ALERT_BYTES - 1
        above = GIT_DIR_ALERT_BYTES + 1

        _sync_once(service, _payload(git_dir_bytes=below))
        assert _bloat_items(db) == []

        _sync_once(service, _payload(git_dir_bytes=above))
        items = _bloat_items(db)
        assert len(items) == 1
        assert "GiB" in items[0]["question"]

        # Same value next poll: no re-emission (edge-triggered).
        _sync_once(service, _payload(git_dir_bytes=above))
        assert len(_bloat_items(db)) == 1

    def test_maintenance_failure_streak_emits_once(self, service, seed_agent, tmp_db):
        from database import db
        seed_agent("alpha")

        _sync_once(service, _payload(maintenance_failures=2))
        assert _bloat_items(db) == []

        _sync_once(service, _payload(maintenance_failures=3))
        items = _bloat_items(db)
        assert len(items) == 1
        assert "maintenance has failed" in items[0]["question"]

        _sync_once(service, _payload(maintenance_failures=4))
        assert len(_bloat_items(db)) == 1

    def test_reset_re_arms_the_streak_alert(self, service, seed_agent, tmp_db):
        from database import db
        seed_agent("alpha")
        _sync_once(service, _payload(maintenance_failures=3))
        _sync_once(service, _payload(maintenance_failures=0))  # repack succeeded
        _sync_once(service, _payload(maintenance_failures=3))  # new episode
        assert len(_bloat_items(db)) == 2


def _bloat_items(db):
    return [
        i for i in db.list_operator_queue_items(agent_name="alpha")
        if i.get("type") == "git_bloat"
    ]
