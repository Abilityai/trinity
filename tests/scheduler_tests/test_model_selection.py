"""
Tests for MODEL-001 scheduler integration: model field passthrough and tracking.

Updated for EXEC-024: Scheduler now delegates to backend's TaskExecutionService
via _call_backend_execute_task() instead of calling agents directly.
"""

# Path setup must happen before scheduler imports
import sys
from pathlib import Path
_this_file = Path(__file__).resolve()
_src_path = str(_this_file.parent.parent.parent / 'src')
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime

from scheduler.service import SchedulerService
from scheduler.database import SchedulerDatabase
from scheduler.locking import LockManager


class TestSchedulerModelSelection:
    """Tests for model field in scheduler execution."""

    @pytest.mark.asyncio
    async def test_execute_schedule_with_model_passes_to_backend(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager
    ):
        """Test that schedule model is passed to backend execute-task call."""
        # Add a schedule with model to the database
        schedule_id = "schedule-with-model"
        with db_with_data.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()

            cursor.execute("""
                INSERT INTO agent_schedules (
                    id, agent_name, name, cron_expression, message,
                    enabled, created_at, updated_at, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                schedule_id, "test-agent", "Test Schedule",
                "0 * * * *", "Test message",
                1, now, now, "claude-opus-4-6"
            ))
            conn.commit()

        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager
        )

        # Mock lock acquisition
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock_manager.try_acquire_schedule_lock = MagicMock(return_value=mock_lock)

        # Mock backend call
        with patch.object(service, '_call_backend_execute_task', new_callable=AsyncMock) as mock_backend:

            mock_backend.return_value = {
                "status": "success",
                "execution_id": "exec-123",
                "response": "Done",
            }

            await service._execute_schedule_with_lock(schedule_id)

            # Verify that backend was called with model parameter
            mock_backend.assert_called_once()
            call_kwargs = mock_backend.call_args[1]
            assert call_kwargs.get("model") == "claude-opus-4-6"

    @pytest.mark.asyncio
    async def test_execute_schedule_without_model_omits_model_param(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager
    ):
        """Test that schedules without model field pass None for model."""
        # Use existing schedule-1 which has no model
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager
        )

        # Mock lock acquisition
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock_manager.try_acquire_schedule_lock = MagicMock(return_value=mock_lock)

        # Mock backend call
        with patch.object(service, '_call_backend_execute_task', new_callable=AsyncMock) as mock_backend:

            mock_backend.return_value = {
                "status": "success",
                "execution_id": "exec-456",
                "response": "Done",
            }

            await service._execute_schedule_with_lock("schedule-1")

            # Verify that backend was called
            mock_backend.assert_called_once()
            call_kwargs = mock_backend.call_args[1]
            # model should be None
            assert call_kwargs.get("model") is None

    @pytest.mark.asyncio
    async def test_execute_schedule_tracks_model_used_in_execution(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager
    ):
        """Test that model_used is recorded in schedule_executions table."""
        # Add a schedule with model to the database
        schedule_id = "schedule-track-model"
        with db_with_data.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()

            cursor.execute("""
                INSERT INTO agent_schedules (
                    id, agent_name, name, cron_expression, message,
                    enabled, created_at, updated_at, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                schedule_id, "test-agent", "Track Model Schedule",
                "0 * * * *", "Test message",
                1, now, now, "claude-sonnet-4-6"
            ))
            conn.commit()

        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager
        )

        # Mock lock acquisition
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock_manager.try_acquire_schedule_lock = MagicMock(return_value=mock_lock)

        # Mock backend call
        with patch.object(service, '_call_backend_execute_task', new_callable=AsyncMock) as mock_backend:

            mock_backend.return_value = {
                "status": "success",
                "execution_id": "exec-789",
                "response": "Done",
            }

            await service._execute_schedule_with_lock(schedule_id)

            # Check that execution was recorded with model_used
            with db_with_data.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT model_used FROM schedule_executions
                    WHERE schedule_id = ?
                    ORDER BY started_at DESC
                    LIMIT 1
                """, (schedule_id,))
                row = cursor.fetchone()

                assert row is not None
                assert row[0] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_execute_schedule_with_custom_model(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager
    ):
        """Test that custom model strings are passed through correctly."""
        # Add a schedule with custom model
        schedule_id = "schedule-custom-model"
        custom_model = "claude-opus-4-5-20250228"

        with db_with_data.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()

            cursor.execute("""
                INSERT INTO agent_schedules (
                    id, agent_name, name, cron_expression, message,
                    enabled, created_at, updated_at, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                schedule_id, "test-agent", "Custom Model Schedule",
                "0 * * * *", "Test message",
                1, now, now, custom_model
            ))
            conn.commit()

        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager
        )

        # Mock lock acquisition
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock_manager.try_acquire_schedule_lock = MagicMock(return_value=mock_lock)

        # Mock backend call
        with patch.object(service, '_call_backend_execute_task', new_callable=AsyncMock) as mock_backend:

            mock_backend.return_value = {
                "status": "success",
                "execution_id": "exec-custom",
                "response": "Done",
            }

            await service._execute_schedule_with_lock(schedule_id)

            # Verify custom model was passed
            mock_backend.assert_called_once()
            call_kwargs = mock_backend.call_args[1]
            assert call_kwargs.get("model") == custom_model


class TestScheduleDatabaseModelField:
    """Tests for model field in scheduler database operations."""

    def test_row_to_schedule_includes_model(self, db_with_data: SchedulerDatabase):
        """Test that _row_to_schedule() reads model field from database."""
        # Add schedule with model
        with db_with_data.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            schedule_id = "test-model-read"

            cursor.execute("""
                INSERT INTO agent_schedules (
                    id, agent_name, name, cron_expression, message,
                    enabled, created_at, updated_at, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                schedule_id, "test-agent", "Test Model Read",
                "0 * * * *", "Test message",
                1, now, now, "claude-haiku-4-5"
            ))
            conn.commit()

        # Read schedule
        schedule = db_with_data.get_schedule(schedule_id)

        assert schedule is not None
        assert schedule.model == "claude-haiku-4-5"

    def test_row_to_schedule_null_model(self, db_with_data: SchedulerDatabase):
        """Test that NULL model is handled correctly."""
        # Add schedule without model
        with db_with_data.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            schedule_id = "test-null-model"

            cursor.execute("""
                INSERT INTO agent_schedules (
                    id, agent_name, name, cron_expression, message,
                    enabled, created_at, updated_at, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                schedule_id, "test-agent", "Test Null Model",
                "0 * * * *", "Test message",
                1, now, now, None
            ))
            conn.commit()

        # Read schedule
        schedule = db_with_data.get_schedule(schedule_id)

        assert schedule is not None
        assert schedule.model is None

    def test_create_execution_with_model_used(self, db_with_data: SchedulerDatabase):
        """Test that create_execution() stores model_used field."""
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test message",
            model_used="claude-opus-4-6"
        )

        assert execution is not None
        execution_id = execution.id

        # Verify execution was created with model_used
        with db_with_data.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT model_used FROM schedule_executions WHERE id = ?
            """, (execution_id,))
            row = cursor.fetchone()

            assert row is not None
            assert row[0] == "claude-opus-4-6"

    def test_create_execution_without_model_used(self, db_with_data: SchedulerDatabase):
        """Test that create_execution() handles NULL model_used."""
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test message",
            model_used=None
        )

        assert execution is not None
        execution_id = execution.id

        # Verify execution was created with NULL model_used
        with db_with_data.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT model_used FROM schedule_executions WHERE id = ?
            """, (execution_id,))
            row = cursor.fetchone()

            assert row is not None
            assert row[0] is None
