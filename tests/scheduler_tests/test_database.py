"""
Tests for scheduler database operations.
"""

# Path setup must happen before scheduler imports
import sys
from pathlib import Path
_this_file = Path(__file__).resolve()
_src_path = str(_this_file.parent.parent.parent / 'src')
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)
import os

from datetime import datetime
import pytest

from scheduler.database import SchedulerDatabase
from scheduler.models import Schedule


class TestSchedulerDatabase:
    """Tests for SchedulerDatabase."""

    def test_get_schedule(self, db_with_data: SchedulerDatabase):
        """Test getting a schedule by ID."""
        schedule = db_with_data.get_schedule("schedule-1")

        assert schedule is not None
        assert schedule.id == "schedule-1"
        assert schedule.agent_name == "test-agent"
        assert schedule.name == "Morning Task"
        assert schedule.cron_expression == "0 9 * * *"
        assert schedule.enabled is True

    def test_get_schedule_not_found(self, db: SchedulerDatabase):
        """Test getting a non-existent schedule."""
        schedule = db.get_schedule("nonexistent")
        assert schedule is None

    def test_list_all_enabled_schedules(self, db_with_data: SchedulerDatabase):
        """Test listing all enabled schedules."""
        schedules = db_with_data.list_all_enabled_schedules()

        assert len(schedules) == 2
        assert all(s.enabled for s in schedules)
        assert "schedule-3" not in [s.id for s in schedules]  # Disabled schedule

    def test_list_all_enabled_excludes_soft_deleted_agent(
        self, db_with_data: SchedulerDatabase, initialized_db: str
    ):
        """#834 Phase 1a gap: soft-deleting the agent (setting
        agent_ownership.deleted_at) must drop its enabled schedules from
        the primary firing list — otherwise the scheduler fires them and
        writes a failure row per attempt until the 180-day purge."""
        import sqlite3

        # Pre-condition: both enabled schedules visible while agent live.
        assert len(db_with_data.list_all_enabled_schedules()) == 2

        conn = sqlite3.connect(initialized_db)
        conn.execute(
            "UPDATE agent_ownership SET deleted_at = ? WHERE agent_name = ?",
            (datetime.utcnow().isoformat(), "test-agent"),
        )
        conn.commit()
        conn.close()

        assert db_with_data.list_all_enabled_schedules() == [], (
            "soft-deleted agent's enabled schedules must not be returned "
            "by the cron-firing list"
        )

    def test_list_agent_schedules(self, db_with_data: SchedulerDatabase):
        """Test listing schedules for a specific agent."""
        schedules = db_with_data.list_agent_schedules("test-agent")

        assert len(schedules) == 3  # All schedules including disabled
        assert all(s.agent_name == "test-agent" for s in schedules)

    def test_get_autonomy_enabled(self, db_with_data: SchedulerDatabase):
        """Test checking autonomy status for an agent."""
        assert db_with_data.get_autonomy_enabled("test-agent") is True
        assert db_with_data.get_autonomy_enabled("nonexistent") is False

    def test_update_schedule_run_times(self, db_with_data: SchedulerDatabase):
        """Test updating schedule run times."""
        now = datetime.utcnow()
        next_run = datetime(2025, 1, 1, 9, 0, 0)

        result = db_with_data.update_schedule_run_times(
            "schedule-1",
            last_run_at=now,
            next_run_at=next_run
        )

        assert result is True

        schedule = db_with_data.get_schedule("schedule-1")
        assert schedule.last_run_at is not None
        assert schedule.next_run_at is not None

    def test_create_execution(self, db_with_data: SchedulerDatabase):
        """Test creating an execution record."""
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test message",
            triggered_by="schedule"
        )

        assert execution is not None
        assert execution.schedule_id == "schedule-1"
        assert execution.agent_name == "test-agent"
        assert execution.status == "running"
        assert execution.message == "Test message"

    def test_update_execution_status(self, db_with_data: SchedulerDatabase):
        """Test updating execution status."""
        # Create execution first
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test message",
            triggered_by="schedule"
        )

        # Update status
        result = db_with_data.update_execution_status(
            execution_id=execution.id,
            status="success",
            response="Task completed",
            context_used=1000,
            context_max=200000,
            cost=0.05
        )

        assert result is True

        # Verify update
        updated = db_with_data.get_execution(execution.id)
        assert updated.status == "success"
        assert updated.response == "Task completed"
        assert updated.context_used == 1000
        assert updated.cost == 0.05
        assert updated.duration_ms is not None

    def test_get_recent_executions(self, db_with_data: SchedulerDatabase):
        """Test getting recent executions."""
        # Create some executions
        for i in range(5):
            db_with_data.create_execution(
                schedule_id="schedule-1",
                agent_name="test-agent",
                message=f"Message {i}",
                triggered_by="schedule"
            )

        executions = db_with_data.get_recent_executions(limit=3)
        assert len(executions) == 3

    def test_generate_id_uniqueness(self, db: SchedulerDatabase):
        """Test that generated IDs are unique."""
        ids = [db._generate_id() for _ in range(100)]
        assert len(ids) == len(set(ids))  # All unique
