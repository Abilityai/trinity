"""
Tests for Issue #46: Recording skipped scheduled executions.

When APScheduler's max_instances=1 constraint causes a job to be skipped
(because previous execution is still running), we should record this as
a skipped execution rather than silently dropping it.
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch, AsyncMock

from scheduler.models import ExecutionStatus, ScheduleExecution
from scheduler.database import SchedulerDatabase


class TestExecutionStatusEnum:
    """Tests for ExecutionStatus enum including SKIPPED status."""

    def test_skipped_status_exists(self):
        """Verify SKIPPED status is defined in enum."""
        assert hasattr(ExecutionStatus, 'SKIPPED')
        assert ExecutionStatus.SKIPPED.value == 'skipped'

    def test_all_statuses_defined(self):
        """Verify all expected statuses exist."""
        expected = ['RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED', 'SKIPPED']
        for status in expected:
            assert hasattr(ExecutionStatus, status), f"Missing status: {status}"


class TestSkippedExecutionDatabase:
    """Tests for create_skipped_execution database method."""

    def test_create_skipped_execution_basic(self, db_with_data):
        """Test creating a basic skipped execution record."""
        execution = db_with_data.create_skipped_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Run morning report",
            triggered_by="schedule"
        )

        assert execution is not None
        assert execution.status == "skipped"
        assert execution.schedule_id == "schedule-1"
        assert execution.agent_name == "test-agent"
        assert execution.message == "Run morning report"
        assert execution.triggered_by == "schedule"
        assert execution.duration_ms == 0
        assert execution.started_at is not None
        assert execution.completed_at is not None

    def test_create_skipped_execution_with_reason(self, db_with_data):
        """Test creating a skipped execution with skip reason."""
        skip_reason = "Previous execution still running (max_instances=1)"

        execution = db_with_data.create_skipped_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Run morning report",
            triggered_by="schedule",
            skip_reason=skip_reason
        )

        assert execution is not None
        assert execution.status == "skipped"
        assert execution.error == skip_reason

    def test_create_skipped_execution_appears_in_history(self, db_with_data):
        """Test that skipped executions appear in execution history."""
        # Create a skipped execution
        execution = db_with_data.create_skipped_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Run morning report",
            triggered_by="schedule",
            skip_reason="Previous execution still running"
        )

        # Verify execution was created and can be retrieved
        retrieved = db_with_data.get_execution(execution.id)

        assert retrieved is not None
        assert retrieved.status == "skipped"
        assert retrieved.schedule_id == "schedule-1"
        assert retrieved.error == "Previous execution still running"

    def test_create_skipped_execution_invalid_schedule(self, db):
        """Test creating skipped execution with non-existent schedule."""
        # Should still create the record (schedule_id is just a reference)
        execution = db.create_skipped_execution(
            schedule_id="non-existent-schedule",
            agent_name="test-agent",
            message="Some message",
            triggered_by="schedule"
        )

        assert execution is not None
        assert execution.schedule_id == "non-existent-schedule"


# Check if apscheduler is available (it runs in Docker, not locally)
try:
    import apscheduler
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False


@pytest.mark.skipif(not APSCHEDULER_AVAILABLE, reason="apscheduler not installed - runs in Docker")
class TestSchedulerServiceSkippedEvents:
    """Tests for scheduler service handling of max_instances events.

    NOTE: These tests require apscheduler which runs in the scheduler Docker container.
    They are skipped when apscheduler is not available locally.
    """

    def test_on_job_max_instances_parses_schedule_job_id(self, db_with_data):
        """Test that _on_job_max_instances correctly parses schedule_ prefix job IDs."""
        from scheduler.service import SchedulerService
        from scheduler.config import SchedulerConfig
        from unittest.mock import patch

        config = SchedulerConfig(
            database_path=":memory:",
            redis_url="redis://localhost:6379",
            lock_timeout=60,
            lock_auto_renewal=False,
            health_port=8099,
            log_level="DEBUG"
        )

        service = SchedulerService(config)
        service.db = db_with_data

        # Create a mock event with schedule_ prefix (matches real job_id format)
        event = MagicMock()
        event.job_id = "schedule_schedule-1"
        event.scheduled_run_time = datetime.utcnow()

        # Mock _record_skipped_agent_schedule to verify it's called with correct ID
        with patch.object(service, '_record_skipped_agent_schedule') as mock_record:
            service._on_job_max_instances(event)
            mock_record.assert_called_once_with("schedule-1")

    def test_on_job_max_instances_handles_unknown_prefix(self, db_with_data):
        """Test that _on_job_max_instances logs warning for unknown job ID format."""
        from scheduler.service import SchedulerService
        from scheduler.config import SchedulerConfig
        from unittest.mock import patch

        config = SchedulerConfig(
            database_path=":memory:",
            redis_url="redis://localhost:6379",
            lock_timeout=60,
            lock_auto_renewal=False,
            health_port=8099,
            log_level="DEBUG"
        )

        service = SchedulerService(config)
        service.db = db_with_data

        # Create a mock event with unknown prefix
        event = MagicMock()
        event.job_id = "unknown_prefix_some-id"
        event.scheduled_run_time = datetime.utcnow()

        # The record method should not be called
        with patch.object(service, '_record_skipped_agent_schedule') as mock_agent:
            service._on_job_max_instances(event)
            mock_agent.assert_not_called()

    def test_on_job_max_instances_legacy_process_prefix_is_unknown(self, db_with_data, caplog):
        """#3432: a legacy process_schedule_ id falls to the unknown-prefix warning."""
        import logging
        from scheduler.service import SchedulerService
        from scheduler.config import SchedulerConfig
        from unittest.mock import patch

        config = SchedulerConfig(
            database_path=":memory:",
            redis_url="redis://localhost:6379",
            lock_timeout=60,
            lock_auto_renewal=False,
            health_port=8099,
            log_level="DEBUG"
        )

        service = SchedulerService(config)
        service.db = db_with_data

        event = MagicMock()
        event.job_id = "process_schedule_legacy-1"
        event.scheduled_run_time = datetime.utcnow()

        with patch.object(service, '_record_skipped_agent_schedule') as mock_agent:
            with caplog.at_level(logging.WARNING, logger="scheduler.service"):
                service._on_job_max_instances(event)
            mock_agent.assert_not_called()
        assert "Unknown job_id format" in caplog.text

    def test_record_skipped_agent_schedule_creates_execution(self, db_with_data):
        """Test that _record_skipped_agent_schedule creates a skipped execution record."""
        from scheduler.service import SchedulerService
        from scheduler.config import SchedulerConfig
        from unittest.mock import patch, AsyncMock

        config = SchedulerConfig(
            database_path=":memory:",
            redis_url="redis://localhost:6379",
            lock_timeout=60,
            lock_auto_renewal=False,
            health_port=8099,
            log_level="DEBUG"
        )

        service = SchedulerService(config)
        service.db = db_with_data

        # Mock asyncio.create_task to prevent actual task creation
        with patch('asyncio.create_task'):
            # Record the skip
            service._record_skipped_agent_schedule("schedule-1")

        # Verify execution was created
        recent = db_with_data.get_recent_executions(limit=10)
        skipped = [e for e in recent if e.status == "skipped"]
        assert len(skipped) >= 1
        assert "max_instances" in skipped[0].error


class TestSkippedExecutionModel:
    """Tests for ScheduleExecution model with skipped status."""

    def test_schedule_execution_with_skipped_status(self):
        """Test creating a ScheduleExecution with skipped status."""
        now = datetime.utcnow()
        execution = ScheduleExecution(
            id="exec-skipped-1",
            schedule_id="schedule-1",
            agent_name="test-agent",
            status="skipped",
            started_at=now,
            message="Run task",
            triggered_by="schedule",
            completed_at=now,
            duration_ms=0,
            error="Previous execution still running (max_instances=1)"
        )

        assert execution.status == "skipped"
        assert execution.duration_ms == 0
        assert "max_instances" in execution.error
