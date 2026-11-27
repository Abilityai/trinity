"""
Tests for SCHED-ASYNC-001: Scheduler async fire-and-forget with DB polling.

Covers:
- Backend async_mode dispatch and immediate return
- Scheduler DB polling for execution completion
- Status overwrite guard in exception handler
- Backward compatibility with sync (old backend) responses
- Polling timeout handling
"""

# Path setup must happen before scheduler imports
import sys
from pathlib import Path
_this_file = Path(__file__).resolve()
_src_path = str(_this_file.parent.parent.parent / 'src')
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

import asyncio
import httpx
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from datetime import datetime

from scheduler.service import SchedulerService
from scheduler.database import SchedulerDatabase
from scheduler.models import ExecutionStatus, ScheduleExecution
from scheduler.locking import LockManager


class TestAsyncDispatch:
    """Tests for async fire-and-forget dispatch to backend."""

    @pytest.mark.asyncio
    async def test_dispatch_sends_async_mode_true(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that _call_backend_execute_task sends async_mode=True in payload."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        # Mock httpx response for async accepted
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "accepted",
            "execution_id": "exec-1",
            "async_mode": True,
        }

        # Create an execution record so polling can find it
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
        )
        # Immediately mark it as success so polling resolves
        db_with_data.update_execution_status(
            execution_id=execution.id,
            status=ExecutionStatus.SUCCESS,
            response="Done",
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await service._call_backend_execute_task(
                agent_name="test-agent",
                message="Test",
                triggered_by="schedule",
                execution_id=execution.id,
            )

            # Verify async_mode was in the payload
            call_kwargs = mock_client.post.call_args
            payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs.kwargs["json"]
            assert payload["async_mode"] is True

    @pytest.mark.asyncio
    async def test_dispatch_uses_short_timeout(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that dispatch uses 30s timeout, not the full task timeout."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "accepted",
            "execution_id": "exec-1",
            "async_mode": True,
        }

        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
        )
        db_with_data.update_execution_status(
            execution_id=execution.id,
            status=ExecutionStatus.SUCCESS,
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await service._call_backend_execute_task(
                agent_name="test-agent",
                message="Test",
                triggered_by="schedule",
                timeout_seconds=3600,  # 1 hour task timeout
                execution_id=execution.id,
            )

            # Verify timeout was 30s, not 3630s
            call_kwargs = mock_client.post.call_args
            assert call_kwargs[1]["timeout"] == 30.0


class TestDBPolling:
    """Tests for DB polling after async dispatch."""

    @pytest.mark.asyncio
    async def test_poll_finds_success_on_first_poll(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that polling returns immediately when execution is already complete."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        # Create and immediately complete an execution
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
        )
        db_with_data.update_execution_status(
            execution_id=execution.id,
            status=ExecutionStatus.SUCCESS,
            response="Task completed",
            cost=0.05,
        )

        with patch("scheduler.service.config") as mock_config:
            mock_config.poll_interval = 0.01  # Fast polling for tests
            mock_config.poll_deadline_buffer = 60

            result = await service._poll_execution_completion(
                execution_id=execution.id,
                timeout_seconds=60,
            )

        assert result["status"] == ExecutionStatus.SUCCESS
        assert result["response"] == "Task completed"
        assert result["cost"] == 0.05

    @pytest.mark.asyncio
    async def test_poll_finds_failed_status(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that polling correctly returns failed status."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
        )
        db_with_data.update_execution_status(
            execution_id=execution.id,
            status=ExecutionStatus.FAILED,
            error="Agent error",
        )

        with patch("scheduler.service.config") as mock_config:
            mock_config.poll_interval = 0.01
            mock_config.poll_deadline_buffer = 60

            result = await service._poll_execution_completion(
                execution_id=execution.id,
                timeout_seconds=60,
            )

        assert result["status"] == ExecutionStatus.FAILED
        assert result["error"] == "Agent error"

    @pytest.mark.asyncio
    async def test_poll_waits_for_completion(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that polling waits when execution is still running, then returns on completion."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
        )

        # Track poll count to complete after 3 polls
        poll_count = {"value": 0}
        original_get_execution = db_with_data.get_execution

        def get_execution_with_delay(exec_id):
            poll_count["value"] += 1
            if poll_count["value"] >= 3:
                # Mark as complete after 3rd poll
                db_with_data.update_execution_status(
                    execution_id=exec_id,
                    status=ExecutionStatus.SUCCESS,
                    response="Delayed result",
                )
            return original_get_execution(exec_id)

        with patch.object(db_with_data, "get_execution", side_effect=get_execution_with_delay), \
             patch("scheduler.service.config") as mock_config:
            mock_config.poll_interval = 0.01
            mock_config.poll_deadline_buffer = 60

            result = await service._poll_execution_completion(
                execution_id=execution.id,
                timeout_seconds=60,
            )

        assert result["status"] == ExecutionStatus.SUCCESS
        assert result["response"] == "Delayed result"
        assert poll_count["value"] >= 3

    @pytest.mark.asyncio
    async def test_poll_timeout_raises_exception(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that polling raises exception when deadline exceeded."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        # Create execution that stays running forever
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
        )

        with patch("scheduler.service.config") as mock_config:
            mock_config.poll_interval = 0.01
            mock_config.poll_deadline_buffer = 0  # disable grace buffer (#415)

            with pytest.raises(Exception, match="Polling deadline exceeded"):
                await service._poll_execution_completion(
                    execution_id=execution.id,
                    timeout_seconds=0,  # Immediate deadline with buffer=0
                )


class TestStatusOverwriteGuard:
    """Tests for SCHED-ASYNC-001 status overwrite protection."""

    @pytest.mark.asyncio
    async def test_exception_does_not_overwrite_success(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that exception handler doesn't overwrite a success status."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        mock_lock = MagicMock()
        mock_lock_manager.try_acquire_schedule_lock = MagicMock(return_value=mock_lock)

        # Mock backend call to raise an exception (simulating connection drop)
        with patch.object(service, '_call_backend_execute_task', new_callable=AsyncMock) as mock_backend:

            mock_backend.side_effect = Exception("Server disconnected without sending a response")

            await service._execute_schedule_with_lock("schedule-1")

        # The execution was created, then the exception was caught.
        # Find the execution that was created for schedule-1.
        with db_with_data.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, status FROM schedule_executions
                WHERE schedule_id = 'schedule-1'
                ORDER BY started_at DESC
                LIMIT 1
            """)
            row = cursor.fetchone()

        assert row is not None
        # Since the backend never actually ran (mock raised immediately),
        # status should be 'failed' because it was genuinely still 'running'
        assert row["status"] == ExecutionStatus.FAILED

    @pytest.mark.asyncio
    async def test_exception_preserves_backend_finalized_status(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that exception handler preserves status when backend already finalized."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        mock_lock = MagicMock()
        mock_lock_manager.try_acquire_schedule_lock = MagicMock(return_value=mock_lock)

        # We need to intercept after execution is created but before exception is handled
        created_execution_id = {"value": None}
        original_create_execution = db_with_data.create_execution

        def create_and_capture(*args, **kwargs):
            execution = original_create_execution(*args, **kwargs)
            created_execution_id["value"] = execution.id
            return execution

        # Mock backend to raise, but first mark execution as success
        async def backend_raises_after_success(**kwargs):
            # Simulate: backend completed the task, but connection dropped
            if created_execution_id["value"]:
                db_with_data.update_execution_status(
                    execution_id=created_execution_id["value"],
                    status=ExecutionStatus.SUCCESS,
                    response="Completed before disconnect",
                )
            raise Exception("Server disconnected without sending a response")

        with patch.object(db_with_data, 'create_execution', side_effect=create_and_capture), \
             patch.object(service, '_call_backend_execute_task', new_callable=AsyncMock) as mock_backend:

            mock_backend.side_effect = backend_raises_after_success

            await service._execute_schedule_with_lock("schedule-1")

        # The execution should still be 'success', not overwritten to 'failed'
        execution = db_with_data.get_execution(created_execution_id["value"])
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCESS
        assert execution.response == "Completed before disconnect"


class TestBackwardCompatibility:
    """Tests for backward compatibility with old backends."""

    @pytest.mark.asyncio
    async def test_sync_response_returned_directly(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that a sync response (no async_mode) is returned as-is."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        # Simulate an old backend that doesn't know about async_mode
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "execution_id": "exec-old",
            "status": "success",
            "response": "Sync result",
            "cost": 0.10,
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await service._call_backend_execute_task(
                agent_name="test-agent",
                message="Test",
                triggered_by="schedule",
                execution_id="exec-old",
            )

        # Should return the sync result directly without polling
        assert result["status"] == "success"
        assert result["response"] == "Sync result"

    @pytest.mark.asyncio
    async def test_http_error_raises_exception(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that HTTP errors are raised as exceptions."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(Exception, match="Backend execute-task returned 500"):
                await service._call_backend_execute_task(
                    agent_name="test-agent",
                    message="Test",
                    triggered_by="schedule",
                    execution_id="exec-err",
                )


class TestEndToEndAsyncSchedule:
    """Integration test for full async schedule execution flow."""

    @pytest.mark.asyncio
    async def test_full_async_execution_flow(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test full flow: dispatch → accept → poll → success."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        mock_lock = MagicMock()
        mock_lock_manager.try_acquire_schedule_lock = MagicMock(return_value=mock_lock)

        # Track the execution_id created by the service
        created_exec_id = {"value": None}

        async def mock_backend_call(**kwargs):
            # Capture execution_id and simulate async backend completing
            created_exec_id["value"] = kwargs.get("execution_id")
            if created_exec_id["value"]:
                db_with_data.update_execution_status(
                    execution_id=created_exec_id["value"],
                    status=ExecutionStatus.SUCCESS,
                    response="Task result",
                    cost=0.25,
                )
            return {
                "execution_id": created_exec_id["value"],
                "status": ExecutionStatus.SUCCESS,
                "response": "Task result",
                "cost": 0.25,
            }

        with patch.object(service, '_call_backend_execute_task', new_callable=AsyncMock) as mock_backend:

            mock_backend.side_effect = mock_backend_call

            await service._execute_schedule_with_lock("schedule-1")

        # Verify execution was created and completed
        assert created_exec_id["value"] is not None
        execution = db_with_data.get_execution(created_exec_id["value"])
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCESS


class TestFireAndForget:
    """Tests for Issue #132: Fire-and-forget dispatch to prevent APScheduler skips."""

    @pytest.mark.asyncio
    async def test_dispatch_returns_immediately_with_dispatched_status(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that _call_backend_execute_task returns immediately with 'dispatched' status."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        # Create an execution record
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
        )

        # Mock httpx response for async accepted
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "accepted",
            "execution_id": execution.id,
            "async_mode": True,
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # Should return immediately, not block on polling
            result = await service._call_backend_execute_task(
                agent_name="test-agent",
                message="Test",
                triggered_by="schedule",
                execution_id=execution.id,
            )

            # Verify we got "dispatched" status (not final status)
            assert result["status"] == "dispatched"
            assert result["async_mode"] is True
            assert result["execution_id"] == execution.id

    @pytest.mark.asyncio
    async def test_background_poll_task_created(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that a background poll task is created and tracked."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        # Create execution and mark it as success immediately (so poll finishes)
        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
        )
        db_with_data.update_execution_status(
            execution_id=execution.id,
            status=ExecutionStatus.SUCCESS,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "accepted",
            "execution_id": execution.id,
            "async_mode": True,
        }

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("scheduler.service.config") as mock_config:
            mock_config.poll_interval = 0.01
            mock_config.poll_deadline_buffer = 60
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # Call should spawn a background task
            await service._call_backend_execute_task(
                agent_name="test-agent",
                message="Test",
                triggered_by="schedule",
                execution_id=execution.id,
            )

            # Task was added (may have already completed)
            # Wait briefly for task to complete
            await asyncio.sleep(0.05)

            # Verify task tracking works (task removed itself on completion)
            # The set should be empty after task completes
            assert len(service._active_poll_tasks) == 0

    @pytest.mark.asyncio
    async def test_execute_schedule_updates_last_run_at_immediately(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that _execute_schedule_with_lock updates last_run_at on dispatch."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        mock_lock = MagicMock()
        mock_lock_manager.try_acquire_schedule_lock = MagicMock(return_value=mock_lock)

        # Get original schedule
        schedule = db_with_data.get_schedule("schedule-1")
        original_last_run = schedule.last_run_at

        # Mock backend to return "dispatched" status
        async def mock_dispatch(**kwargs):
            return {
                "status": "dispatched",
                "async_mode": True,
                "execution_id": kwargs.get("execution_id"),
            }

        with patch.object(service, '_call_backend_execute_task', new_callable=AsyncMock) as mock_backend:

            mock_backend.side_effect = mock_dispatch

            await service._execute_schedule_with_lock("schedule-1")

        # Verify last_run_at was updated immediately
        updated_schedule = db_with_data.get_schedule("schedule-1")
        assert updated_schedule.last_run_at is not None
        if original_last_run:
            assert updated_schedule.last_run_at > original_last_run

    @pytest.mark.asyncio
    async def test_shutdown_cancels_active_poll_tasks(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Test that shutdown() cancels active poll tasks."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        # Create a task that would run for a long time
        async def long_running():
            await asyncio.sleep(100)

        task = asyncio.create_task(long_running())
        service._active_poll_tasks.add(task)

        # Shutdown should cancel it
        service.shutdown()

        # Wait for cancellation to propagate
        try:
            await asyncio.wait_for(task, timeout=0.1)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        assert task.cancelled() or task.done()
        assert len(service._active_poll_tasks) == 0


class TestDescribeException:
    """#1022: _describe_exception never persists a blank/whitespace error."""

    def test_blank_stringifying_exception_falls_back_to_type_name(self):
        # httpx timeout exceptions str() to '' — the exact #1022 trigger.
        from scheduler.service import _describe_exception

        msg = _describe_exception(httpx.ReadTimeout(""))
        assert msg.strip()  # non-empty
        assert "ReadTimeout" in msg

    def test_whitespace_only_exception_falls_back_to_type_name(self):
        from scheduler.service import _describe_exception

        msg = _describe_exception(Exception("   "))
        assert msg.strip()
        assert "Exception" in msg

    def test_normal_exception_message_preserved(self):
        from scheduler.service import _describe_exception

        assert _describe_exception(Exception("boom")) == "boom"


class TestDispatchTimeout:
    """#1022: dispatch POST timeout → descriptive raise + config-driven deadline."""

    @pytest.mark.asyncio
    async def test_dispatch_timeout_raises_descriptive_error(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """A blank-stringifying httpx timeout on the POST becomes a descriptive,
        non-empty Exception naming the threshold + subtype (was the silent bug)."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ReadTimeout("")  # str() == ''
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(Exception) as exc_info:
                await service._call_backend_execute_task(
                    agent_name="test-agent",
                    message="Test",
                    triggered_by="schedule",
                    execution_id=execution.id,
                )

        msg = str(exc_info.value)
        assert "timed out" in msg
        assert "30" in msg  # the default dispatch_timeout value names the threshold
        assert "ReadTimeout" in msg

    @pytest.mark.asyncio
    async def test_dispatch_uses_config_timeout(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """The dispatch deadline comes from config.dispatch_timeout, not a literal.
        Complements test_dispatch_uses_short_timeout (30.0 default)."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "accepted",
            "execution_id": "exec-1",
            "async_mode": True,
        }

        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Test",
        )
        db_with_data.update_execution_status(
            execution_id=execution.id,
            status=ExecutionStatus.SUCCESS,
        )

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("scheduler.service.config.dispatch_timeout", 12.5):
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await service._call_backend_execute_task(
                agent_name="test-agent",
                message="Test",
                triggered_by="schedule",
                execution_id=execution.id,
            )

            call_kwargs = mock_client.post.call_args
            assert call_kwargs[1]["timeout"] == 12.5


class TestDispatchTimeoutRegression:
    """#1022 regression — the exact failure signature, INVERTED.

    A dispatch httpx timeout (str()=='') driven end-to-end must persist a
    NON-EMPTY `error` on a FAILED row, instead of the silent `error=''`.
    """

    @pytest.mark.asyncio
    async def test_dispatch_timeout_persists_nonempty_error(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        mock_lock = MagicMock()
        mock_lock_manager.try_acquire_schedule_lock = MagicMock(return_value=mock_lock)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            # Every POST (pre-check fail-open + dispatch) blank-stringifies.
            mock_client.post.side_effect = httpx.ReadTimeout("")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await service._execute_schedule_with_lock("schedule-1")

        with db_with_data.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status, error FROM schedule_executions
                WHERE schedule_id = 'schedule-1'
                ORDER BY started_at DESC
                LIMIT 1
            """)
            row = cursor.fetchone()

        assert row is not None
        assert row["status"] == ExecutionStatus.FAILED
        # The #1022 signature inverted: error must NOT be blank.
        assert row["error"] is not None and row["error"].strip() != ""
        assert "timed out" in row["error"]


class TestRetryDispatchTimeoutRegression:
    """#1022 — the second persistence path the fix touched: `_execute_retry`.

    A blank-stringifying timeout raised by the backend dispatch during a
    retry attempt must land a NON-EMPTY `error` on the retry's FAILED row
    (service.py:`error=_describe_exception(e)[:2000]`), not the silent
    `error=str(e)=''` it replaced.
    """

    @pytest.mark.asyncio
    async def test_retry_dispatch_timeout_persists_nonempty_error(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        # The dispatch inside the retry blank-stringifies (the #1022 trigger).
        service._call_backend_execute_task = AsyncMock(
            side_effect=httpx.ReadTimeout("")
        )

        await service._execute_retry(
            original_execution_id="orig-exec",
            failed_execution_id="failed-exec",
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Retry me",
            timeout_seconds=None,
            model="claude-sonnet-4-6",
            allowed_tools=[],
            next_attempt_number=2,
        )

        with db_with_data.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status, error FROM schedule_executions
                WHERE schedule_id = 'schedule-1' AND triggered_by = 'retry'
                ORDER BY started_at DESC
                LIMIT 1
            """)
            row = cursor.fetchone()

        assert row is not None, "retry execution row was never created"
        assert row["status"] == ExecutionStatus.FAILED
        # #1022 inverted: a blank-stringifying timeout must NOT persist blank.
        assert row["error"] is not None and row["error"].strip() != ""
        assert "ReadTimeout" in row["error"]
