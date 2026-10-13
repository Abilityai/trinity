"""
Tests for RETRY-001: Scheduler retry mechanism for failed executions.

Tests automatic retry functionality including:
- Retry configuration (max_retries, retry_delay_seconds)
- Execution record fields (attempt_number, retry_of_execution_id, retry_scheduled_at)
- Database operations for retry management
- Enum values (PENDING_RETRY status, RETRY trigger)
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, AsyncMock

from scheduler.models import ExecutionStatus, Schedule, ScheduleExecution
from scheduler.database import SchedulerDatabase


class TestRetryEnums:
    """Tests for RETRY-001 enum additions."""

    def test_pending_retry_status_exists(self):
        """Verify PENDING_RETRY status is defined in enum."""
        assert hasattr(ExecutionStatus, 'PENDING_RETRY')
        assert ExecutionStatus.PENDING_RETRY.value == 'pending_retry'

    def test_all_execution_statuses_defined(self):
        """Verify all expected statuses exist including PENDING_RETRY."""
        expected = ['RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED', 'SKIPPED', 'PENDING_RETRY']
        for status in expected:
            assert hasattr(ExecutionStatus, status), f"Missing status: {status}"

class TestScheduleRetryConfiguration:
    """Tests for Schedule model retry configuration fields."""

    def test_schedule_has_retry_fields(self, sample_schedule):
        """Verify Schedule model has retry configuration fields."""
        assert hasattr(sample_schedule, 'max_retries')
        assert hasattr(sample_schedule, 'retry_delay_seconds')

    def test_schedule_default_retry_values(self):
        """Test Schedule default values for retry fields."""
        from scheduler.models import Schedule
        now = datetime.utcnow()
        schedule = Schedule(
            id="test-id",
            agent_name="test-agent",
            name="Test",
            cron_expression="* * * * *",
            message="test",
            enabled=True,
            timezone="UTC",
            description="Test schedule",
            owner_id=1,
            created_at=now,
            updated_at=now
        )
        assert schedule.max_retries == 0  # #476: default flipped 1 → 0 (opt-in)
        assert schedule.retry_delay_seconds == 60

    def test_schedule_custom_retry_values(self):
        """Test Schedule with custom retry configuration."""
        from scheduler.models import Schedule
        now = datetime.utcnow()
        schedule = Schedule(
            id="test-id",
            agent_name="test-agent",
            name="Test",
            cron_expression="* * * * *",
            message="test",
            enabled=True,
            timezone="UTC",
            description="Test schedule",
            owner_id=1,
            created_at=now,
            updated_at=now,
            max_retries=3,
            retry_delay_seconds=120
        )
        assert schedule.max_retries == 3
        assert schedule.retry_delay_seconds == 120


class TestExecutionRetryTracking:
    """Tests for ScheduleExecution model retry tracking fields."""

    def test_execution_has_retry_fields(self, sample_execution):
        """Verify ScheduleExecution model has retry tracking fields."""
        assert hasattr(sample_execution, 'attempt_number')
        assert hasattr(sample_execution, 'retry_of_execution_id')
        assert hasattr(sample_execution, 'retry_scheduled_at')

    def test_execution_default_attempt_number(self, sample_execution):
        """Test ScheduleExecution default attempt_number is 1."""
        assert sample_execution.attempt_number == 1

    def test_execution_retry_fields_optional(self):
        """Test ScheduleExecution retry fields are optional."""
        from scheduler.models import ScheduleExecution
        now = datetime.utcnow()
        execution = ScheduleExecution(
            id="exec-1",
            schedule_id="sched-1",
            agent_name="test-agent",
            status="running",
            started_at=now,
            message="test",
            triggered_by="schedule"
        )
        assert execution.retry_of_execution_id is None
        assert execution.retry_scheduled_at is None


class TestRetryDatabaseOperations:
    """Tests for retry-related database operations."""

    def test_create_execution_with_retry_fields(self, db_with_data):
        """Test creating an execution with retry tracking fields."""
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test retry",
            triggered_by="retry",
            attempt_number=2,
            retry_of_execution_id="original-exec-id"
        )

        assert execution is not None
        assert execution.triggered_by == "retry"
        assert execution.attempt_number == 2
        assert execution.retry_of_execution_id == "original-exec-id"

    def test_schedule_retry_marks_execution(self, db_with_data):
        """Test schedule_retry updates execution status and retry_scheduled_at."""
        # Create an initial execution
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
            triggered_by="schedule"
        )

        # Mark execution as failed first
        db_with_data.update_execution_status(
            execution.id,
            ExecutionStatus.FAILED,
            error="Test failure"
        )

        # Schedule a retry
        retry_time = datetime.utcnow() + timedelta(seconds=60)
        result = db_with_data.schedule_retry(execution.id, retry_time)

        assert result is True

        # Verify the execution was updated
        updated = db_with_data.get_execution(execution.id)
        assert updated.status == ExecutionStatus.PENDING_RETRY.value
        assert updated.retry_scheduled_at is not None

    def test_get_pending_retries(self, db_with_data):
        """Test retrieving executions pending retry."""
        # Create and mark execution as pending_retry
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
            triggered_by="schedule"
        )

        retry_time = datetime.utcnow() + timedelta(seconds=60)
        db_with_data.schedule_retry(execution.id, retry_time)

        # Get pending retries
        pending = db_with_data.get_pending_retries()

        assert len(pending) >= 1
        assert any(e.id == execution.id for e in pending)

    def test_clear_retry_scheduled(self, db_with_data):
        """Test clearing retry_scheduled_at after retry fires."""
        # Create and schedule retry
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
            triggered_by="schedule"
        )

        retry_time = datetime.utcnow() + timedelta(seconds=60)
        db_with_data.schedule_retry(execution.id, retry_time)

        # Clear the scheduled retry
        result = db_with_data.clear_retry_scheduled(execution.id)
        assert result is True

        # Verify it's cleared
        updated = db_with_data.get_execution(execution.id)
        assert updated.retry_scheduled_at is None

    def test_get_original_execution_id_single(self, db_with_data):
        """Test get_original_execution_id returns the ID for non-retry execution."""
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
            triggered_by="schedule"
        )

        original_id = db_with_data.get_original_execution_id(execution.id)
        assert original_id == execution.id

    def test_get_original_execution_id_chain(self, db_with_data):
        """Test get_original_execution_id traverses retry chain."""
        # Create original execution
        original = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
            triggered_by="schedule"
        )

        # Create first retry
        retry1 = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
            triggered_by="retry",
            attempt_number=2,
            retry_of_execution_id=original.id
        )

        # Create second retry
        retry2 = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
            triggered_by="retry",
            attempt_number=3,
            retry_of_execution_id=original.id  # Points to original
        )

        # Get original from second retry
        original_id = db_with_data.get_original_execution_id(retry2.id)
        assert original_id == original.id


class TestRetryDatabaseReadParsing:
    """Tests for parsing retry fields from database rows."""

    def test_row_to_schedule_parses_retry_config(self, db_with_data):
        """Test _row_to_schedule correctly parses retry configuration."""
        schedule = db_with_data.get_schedule("schedule-1")

        # #476: default is 0 (was 1 before the flip). Existing schedules also
        # got 0 from the RETRY-001 migration, so this is now unambiguous.
        assert schedule.max_retries == 0
        assert schedule.retry_delay_seconds == 60

    def test_row_to_execution_parses_retry_tracking(self, db_with_data):
        """Test _row_to_execution correctly parses retry tracking fields."""
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
            triggered_by="retry",
            attempt_number=2,
            retry_of_execution_id="original-id"
        )

        # Re-fetch from database
        fetched = db_with_data.get_execution(execution.id)

        assert fetched.attempt_number == 2
        assert fetched.retry_of_execution_id == "original-id"
