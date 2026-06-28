"""
Characterization tests for CleanupService._run_cleanup_inner (#1026).

These pin the *current* behavior of the cleanup cycle so the strategy-per-sweep
refactor can be proven behavior-preserving. They run without a backend — `db`,
the capacity manager, the HTTP-bearing watchdog/slot methods, and the WAL
checkpoint are all mocked.

Invariants pinned:
- every sweep runs and writes its CleanupReport field (happy path)
- a sweep raising does NOT abort the cycle (per-sweep error isolation)
- rate-limit-event prune is cycle-gated (only every 12th cycle)
- WAL checkpoint fires only when a retention sweep reclaimed rows
- retention sweeps are skipped when their retention window is 0 (disabled)
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.cleanup_service import CleanupService, CleanupReport

# The unit-test harness can register the backend package under more than one
# module name; resolve the exact module object the class methods bind their
# globals to so patch.object targets the same `db` / `get_capacity_manager`
# the running code looks up.
_CS = sys.modules[CleanupService.__module__]


def _make_service():
    """A CleanupService with the HTTP-bearing methods stubbed out."""
    svc = CleanupService(poll_interval=300)
    # Watchdog (#129) and slot reclaim do real HTTP / Redis — stub them.
    svc._reconcile_orphaned_executions = AsyncMock(return_value=(8, 9, set()))
    svc._process_stale_slot_reclaims = AsyncMock(return_value=None)
    return svc


def _setting_side_effect(key, default=None):
    if key == "agent_soft_delete_retention_days":
        return "180"
    if key == "schedule_soft_delete_retention_days":
        return "30"
    return default


def _configure_db(db):
    db.mark_stale_executions_failed.return_value = 1
    db.mark_no_session_executions_failed.return_value = 2
    db.finalize_orphaned_skipped_executions.return_value = 3
    db.mark_stale_activities_failed.return_value = 4
    db.get_all_execution_timeouts.return_value = {}
    db.cleanup_old_rate_limit_events.return_value = 0
    db.delete_expired_and_revoked_shared_files.return_value = ["a", "b"]
    db.prune_execution_logs.return_value = 5
    db.prune_execution_rows.return_value = 6
    db.cleanup_old_health_records.return_value = 7
    db.get_setting_value.side_effect = _setting_side_effect
    db.find_soft_deleted_agents_past_retention.return_value = ["ag1"]
    db.purge_agent_ownership.return_value = True
    db.find_soft_deleted_schedules_past_retention.return_value = ["s1", "s2"]
    db.purge_schedule.return_value = True
    db.idempotency_purge_expired.return_value = 11  # RELIABILITY-006 / #525
    db.prune_agent_reports.return_value = 3  # #918 agent_reports retention


def _run(svc):
    return asyncio.run(svc._run_cleanup_inner())


def test_happy_path_runs_all_sweeps_and_populates_report():
    svc = _make_service()
    capacity = MagicMock()
    capacity.reclaim_stale = AsyncMock(return_value={})
    with patch.object(_CS, "db") as db, \
         patch.object(_CS, "get_capacity_manager", return_value=capacity), \
         patch.object(_CS, "_read_retention_settings", return_value=(30, 90, 7, 90)), \
         patch.object(_CS, "_wal_checkpoint_truncate") as wal:
        _configure_db(db)
        report = _run(svc)

    assert report.orphaned_executions == 8
    assert report.auto_terminated == 9
    assert report.stale_executions == 1
    assert report.no_session_executions == 2
    assert report.orphaned_skipped == 3
    assert report.stale_activities == 4
    assert report.shared_files_purged == 2
    assert report.execution_logs_pruned == 5
    assert report.execution_rows_pruned == 6
    assert report.health_checks_pruned == 7
    assert report.soft_deleted_agents_purged == 1
    assert report.soft_deleted_schedules_purged == 2
    assert report.idempotency_keys_purged == 11
    assert report.agent_reports_pruned == 3  # #918
    assert report.total == 8 + 9 + 1 + 2 + 3 + 4 + 2 + 5 + 6 + 7 + 1 + 2 + 11 + 3
    # retention reclaimed rows ⇒ WAL checkpoint fires
    wal.assert_called_once()
    # cycle counter advanced
    assert svc._cycle_count == 1


def test_sweep_error_does_not_abort_cycle():
    """One sweep raising must not stop subsequent sweeps (per-sweep try/except)."""
    svc = _make_service()
    capacity = MagicMock()
    capacity.reclaim_stale = AsyncMock(return_value={})
    with patch.object(_CS, "db") as db, \
         patch.object(_CS, "get_capacity_manager", return_value=capacity), \
         patch.object(_CS, "_read_retention_settings", return_value=(30, 90, 7, 90)), \
         patch.object(_CS, "_wal_checkpoint_truncate"):
        _configure_db(db)
        db.mark_stale_executions_failed.side_effect = RuntimeError("boom")
        report = _run(svc)

    # the failing sweep contributes 0, everything after it still ran
    assert report.stale_executions == 0
    assert report.no_session_executions == 2
    assert report.execution_rows_pruned == 6
    assert report.soft_deleted_schedules_purged == 2


def test_rate_limit_prune_is_cycle_gated():
    capacity = MagicMock()
    capacity.reclaim_stale = AsyncMock(return_value={})

    def run_at_cycle(cycle):
        svc = _make_service()
        svc._cycle_count = cycle
        with patch.object(_CS, "db") as db, \
             patch.object(_CS, "get_capacity_manager", return_value=capacity), \
             patch.object(_CS, "_read_retention_settings", return_value=(0, 0, 0, 0)), \
             patch.object(_CS, "_wal_checkpoint_truncate"):
            _configure_db(db)
            _run(svc)
            return db.cleanup_old_rate_limit_events.called

    assert run_at_cycle(0) is True       # 0 % 12 == 0 → runs
    assert run_at_cycle(1) is False      # 1 % 12 != 0 → skipped
    assert run_at_cycle(12) is True      # wraps


def test_wal_checkpoint_skipped_when_no_retention_work():
    svc = _make_service()
    capacity = MagicMock()
    capacity.reclaim_stale = AsyncMock(return_value={})
    with patch.object(_CS, "db") as db, \
         patch.object(_CS, "get_capacity_manager", return_value=capacity), \
         patch.object(_CS, "_read_retention_settings", return_value=(30, 90, 7, 90)), \
         patch.object(_CS, "_wal_checkpoint_truncate") as wal:
        _configure_db(db)
        # nothing reclaimed by any retention sweep
        db.prune_execution_logs.return_value = 0
        db.prune_execution_rows.return_value = 0
        db.cleanup_old_health_records.return_value = 0
        db.find_soft_deleted_agents_past_retention.return_value = []
        db.find_soft_deleted_schedules_past_retention.return_value = []
        db.idempotency_purge_expired.return_value = 0
        db.prune_agent_reports.return_value = 0  # #918
        _run(svc)
    wal.assert_not_called()


def test_wal_checkpoint_fires_when_only_agent_reports_pruned():
    """#918: agent_reports pruning alone must still trigger the WAL checkpoint.

    Regression guard — `agent_reports_pruned` was originally omitted from the
    `_maybe_wal_checkpoint` retention_total, so a cycle that reclaimed only
    report rows left the freed pages in the WAL.
    """
    svc = _make_service()
    capacity = MagicMock()
    capacity.reclaim_stale = AsyncMock(return_value={})
    with patch.object(_CS, "db") as db, \
         patch.object(_CS, "get_capacity_manager", return_value=capacity), \
         patch.object(_CS, "_read_retention_settings", return_value=(30, 90, 7, 90)), \
         patch.object(_CS, "_wal_checkpoint_truncate") as wal:
        _configure_db(db)
        # Every other retention sweep reclaims nothing; only agent_reports prunes.
        db.prune_execution_logs.return_value = 0
        db.prune_execution_rows.return_value = 0
        db.cleanup_old_health_records.return_value = 0
        db.find_soft_deleted_agents_past_retention.return_value = []
        db.find_soft_deleted_schedules_past_retention.return_value = []
        db.idempotency_purge_expired.return_value = 0
        db.prune_agent_reports.return_value = 4  # #918 — the only work this cycle
        _run(svc)
    wal.assert_called_once()


def test_retention_sweeps_skipped_when_disabled():
    """retention_days == 0 disables the #772 + #834 sweeps."""
    svc = _make_service()
    capacity = MagicMock()
    capacity.reclaim_stale = AsyncMock(return_value={})
    with patch.object(_CS, "db") as db, \
         patch.object(_CS, "get_capacity_manager", return_value=capacity), \
         patch.object(_CS, "_read_retention_settings", return_value=(0, 0, 0, 0)), \
         patch.object(_CS, "_wal_checkpoint_truncate"):
        _configure_db(db)
        db.get_setting_value.side_effect = lambda key, default=None: "0"
        _run(svc)
        db.prune_execution_logs.assert_not_called()
        db.prune_execution_rows.assert_not_called()
        db.cleanup_old_health_records.assert_not_called()
        db.find_soft_deleted_agents_past_retention.assert_not_called()
        db.find_soft_deleted_schedules_past_retention.assert_not_called()
