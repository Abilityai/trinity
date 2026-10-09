"""
Regression tests for the scheduler sync loop (Issue #420).

The periodic sync loop must be idempotent: if nothing has changed in the DB,
consecutive sync ticks must not re-register jobs. The original bug was a
self-triggering loop where `_add_job` wrote a fresh `updated_at` via
`update_schedule_run_times`, which the next sync tick then interpreted as a
config change and re-added the job — amplifying to N jobs/minute × fleet size.
"""

# Path setup must happen before scheduler imports
import sys
from pathlib import Path
_this_file = Path(__file__).resolve()
_src_path = str(_this_file.parent.parent.parent / 'src')
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from scheduler.service import SchedulerService
from scheduler.database import SchedulerDatabase
from scheduler.locking import LockManager


class TestSyncLoopIdempotence:
    """Sync loop must not re-register schedules when nothing has changed."""

    @pytest.mark.asyncio
    async def test_update_run_times_does_not_bump_updated_at(
        self, db_with_data: SchedulerDatabase
    ):
        """
        `update_schedule_run_times` must not bump `updated_at`. That column
        signals config changes and is watched by `_sync_agent_schedules`;
        bumping it on every run caused the sync loop to fire perpetually.
        """
        before = db_with_data.get_schedule("schedule-1")
        assert before is not None

        db_with_data.update_schedule_run_times(
            "schedule-1",
            last_run_at=datetime.utcnow(),
            next_run_at=datetime(2099, 1, 1, 9, 0, 0),
        )

        after = db_with_data.get_schedule("schedule-1")
        assert after.updated_at == before.updated_at, (
            "update_schedule_run_times must not modify updated_at — doing so "
            "triggers a self-reinforcing sync loop (Issue #420)"
        )
        assert after.last_run_at is not None
        assert after.next_run_at is not None

    @pytest.mark.asyncio
    async def test_sync_is_noop_when_db_unchanged(
        self, db_with_data: SchedulerDatabase, mock_lock_manager: LockManager
    ):
        """
        After `initialize()` registers jobs, calling `_sync_agent_schedules`
        repeatedly with no DB edits must not call `_add_job` or `_remove_job`
        again. This is the direct regression test for Issue #420.
        """
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )
        service.initialize()

        try:
            # Reset spies AFTER init so we only observe sync-tick behavior
            with patch.object(service, "_add_job") as add_spy, \
                 patch.object(service, "_remove_job") as remove_spy:
                await service._sync_agent_schedules()
                await service._sync_agent_schedules()
                await service._sync_agent_schedules()

                assert add_spy.call_count == 0, (
                    f"Sync re-registered jobs {add_spy.call_count} times with "
                    "no DB changes — self-triggering loop regression (#420)"
                )
                assert remove_spy.call_count == 0
        finally:
            service.shutdown()

    @pytest.mark.asyncio
    async def test_sync_detects_legitimate_config_change(
        self, db_with_data: SchedulerDatabase, mock_lock_manager: LockManager
    ):
        """
        A real config edit (one that bumps `updated_at` via SQL) must still
        trigger the sync loop's update branch. This guards against over-
        correcting the fix.
        """
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )
        service.initialize()

        try:
            # Simulate a user editing cron_expression via the backend router,
            # which bumps updated_at.
            import sqlite3
            conn = sqlite3.connect(db_with_data.database_path)
            try:
                conn.execute(
                    "UPDATE agent_schedules SET cron_expression = ?, "
                    "updated_at = ? WHERE id = ?",
                    ("30 9 * * *", datetime.utcnow().isoformat(), "schedule-1"),
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(service, "_add_job") as add_spy, \
                 patch.object(service, "_remove_job") as remove_spy:
                await service._sync_agent_schedules()

                # Sync should remove + re-add exactly the edited schedule
                add_calls = [c for c in add_spy.call_args_list
                             if c.args and c.args[0].id == "schedule-1"]
                remove_calls = [c for c in remove_spy.call_args_list
                                if c.args and c.args[0] == "schedule-1"]
                assert len(add_calls) == 1, (
                    f"Edited schedule not re-added (got {len(add_calls)} calls)"
                )
                assert len(remove_calls) == 1
        finally:
            service.shutdown()


# Legacy DDL the scheduler's boot path used to create ad hoc (#3432). Existing
# installs still carry these tables; nothing may read them any more.
_LEGACY_PROCESS_SCHEDULE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS process_schedules (
        id TEXT PRIMARY KEY,
        process_id TEXT NOT NULL,
        process_name TEXT NOT NULL,
        trigger_id TEXT NOT NULL,
        cron_expression TEXT NOT NULL,
        enabled INTEGER DEFAULT 1,
        timezone TEXT DEFAULT 'UTC',
        description TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_run_at TEXT,
        next_run_at TEXT,
        UNIQUE(process_id, trigger_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS process_schedule_executions (
        id TEXT PRIMARY KEY,
        schedule_id TEXT NOT NULL,
        process_id TEXT NOT NULL,
        process_name TEXT NOT NULL,
        execution_id TEXT,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        duration_ms INTEGER,
        triggered_by TEXT NOT NULL,
        error TEXT,
        FOREIGN KEY (schedule_id) REFERENCES process_schedules(id)
    )
    """,
)


class TestLegacyProcessScheduleTables:
    """#3432: the process-schedule remnant is gone; its tables stay inert."""

    @pytest.mark.asyncio
    async def test_legacy_process_schedule_tables_are_ignored(
        self, db_with_data: SchedulerDatabase, mock_lock_manager: LockManager
    ):
        """
        An upgraded install still has both legacy tables and possibly an
        enabled row. Boot and the sync tick must register agent jobs only —
        never a `process_schedule_*` job — and must not raise.
        """
        import sqlite3
        now_iso = datetime.utcnow().isoformat()
        conn = sqlite3.connect(db_with_data.database_path)
        try:
            for ddl in _LEGACY_PROCESS_SCHEDULE_DDL:
                conn.execute(ddl)
            conn.execute(
                "INSERT INTO process_schedules (id, process_id, process_name, "
                "trigger_id, cron_expression, enabled, timezone, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("ps-legacy", "proc-1", "legacy", "trig-1",
                 "0 * * * *", 1, "UTC", now_iso, now_iso),
            )
            conn.commit()
        finally:
            conn.close()

        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )
        service.initialize()
        try:
            await service._sync_schedules()
            job_ids = [job.id for job in service.scheduler.get_jobs()]
            assert any(j.startswith("schedule_") for j in job_ids), job_ids
            assert not [j for j in job_ids if j.startswith("process_schedule_")], (
                f"legacy process schedule registered as a job: {job_ids}"
            )
        finally:
            service.shutdown()
