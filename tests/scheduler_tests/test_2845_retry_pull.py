"""#2845 — RETRY-001 once a retry can be pulled.

Making `retry` pullable changes two things on the scheduler side:

* A pulled row the lease reaper poison-parks is FAILED like any other failure,
  so RETRY-001 retried it — and each pulled retry then earns its own full set of
  lease re-deliveries and its own high-priority alert. A park means "stop and
  ask a human"; it must not be retried automatically (#2514 §2).
* `_execute_retry` wrote FAILED unconditionally when the dispatch raised. On the
  pull path the backend may already have handed the row to the durable queue
  (`queued`) before the scheduler's dispatch timed out; that write would strand a
  row no worker can claim. It now only fails a row still `running`, like the
  cron path does.

And the counting boundary: a retry is a NEW row, so it starts with its own
re-delivery budget and never inherits or bumps the original's.
"""

import sys
from pathlib import Path

_src_path = str(Path(__file__).resolve().parent.parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from scheduler.database import SchedulerDatabase
from scheduler.locking import LockManager
from scheduler.models import ExecutionStatus
from scheduler.service import SchedulerService

POISON_ERROR = (
    "poison_lease: pull lease expired and re-delivery cap (3) reached — "
    "parked to operator queue"
)


def _service(db: SchedulerDatabase, lock_manager: LockManager) -> SchedulerService:
    service = SchedulerService(database=db, lock_manager=lock_manager)
    service._schedule_retry_job = MagicMock()
    return service


def _enable_retries(db: SchedulerDatabase, max_retries: int = 2) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE agent_schedules SET max_retries = ? WHERE id = 'schedule-1'",
            (max_retries,),
        )
        conn.commit()


def _failed_execution(db: SchedulerDatabase, error: str):
    execution = db.create_execution(
        schedule_id="schedule-1",
        agent_name="test-agent",
        message="Run morning report",
        triggered_by="schedule",
    )
    db.update_execution_status(execution.id, ExecutionStatus.FAILED, error=error)
    return db.get_execution(execution.id)


class TestPoisonParkedRowsAreNotRetried:
    @pytest.mark.asyncio
    async def test_a_poison_parked_run_is_not_retried(self, db_with_data, mock_lock_manager):
        _enable_retries(db_with_data)
        service = _service(db_with_data, mock_lock_manager)

        await service._maybe_schedule_retry(
            _failed_execution(db_with_data, POISON_ERROR), error_msg=POISON_ERROR
        )

        service._schedule_retry_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_is_still_retried(self, db_with_data, mock_lock_manager):
        _enable_retries(db_with_data)
        service = _service(db_with_data, mock_lock_manager)

        await service._maybe_schedule_retry(
            _failed_execution(db_with_data, "Agent at capacity"),
            error_msg="Agent at capacity",
        )

        service._schedule_retry_job.assert_called_once()


async def _run_retry(service: SchedulerService) -> None:
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


def _retry_row(db: SchedulerDatabase):
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT * FROM schedule_executions WHERE triggered_by = 'retry' "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()


class TestRetryDispatchFailureDoesNotClobberAQueuedRow:
    @pytest.mark.asyncio
    async def test_a_row_the_backend_already_queued_stays_queued(
        self, db_with_data, mock_lock_manager
    ):
        service = SchedulerService(database=db_with_data, lock_manager=mock_lock_manager)

        async def queued_then_timeout(**kwargs):
            # The backend handed the row to the pilot's durable queue, then the
            # scheduler's dispatch deadline fired before the ack came back.
            with db_with_data.get_connection() as conn:
                conn.execute(
                    "UPDATE schedule_executions SET status = ? WHERE id = ?",
                    (ExecutionStatus.QUEUED, kwargs["execution_id"]),
                )
                conn.commit()
            raise httpx.ReadTimeout("")

        service._call_backend_execute_task = AsyncMock(side_effect=queued_then_timeout)
        await _run_retry(service)

        assert _retry_row(db_with_data)["status"] == ExecutionStatus.QUEUED


class TestRetryAndRedeliveryCountsStayDistinct:
    @pytest.mark.asyncio
    async def test_a_retry_starts_with_its_own_redelivery_budget(
        self, db_with_data, mock_lock_manager
    ):
        with db_with_data.get_connection() as conn:
            conn.execute(
                "ALTER TABLE schedule_executions ADD COLUMN redelivery_count INTEGER DEFAULT 0"
            )
            conn.execute(
                "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, "
                "started_at, message, triggered_by, error, redelivery_count) VALUES "
                "('orig-exec', 'schedule-1', 'test-agent', 'failed', "
                "'2026-09-20T09:00:00Z', 'Retry me', 'schedule', ?, 3)",
                (POISON_ERROR,),
            )
            conn.commit()
        service = SchedulerService(database=db_with_data, lock_manager=mock_lock_manager)
        service._call_backend_execute_task = AsyncMock(return_value={"status": "dispatched"})

        await _run_retry(service)

        retry = _retry_row(db_with_data)
        assert retry["attempt_number"] == 2
        assert retry["retry_of_execution_id"] == "orig-exec"
        assert retry["redelivery_count"] == 0
        with db_with_data.get_connection() as conn:
            original = conn.execute(
                "SELECT redelivery_count FROM schedule_executions WHERE id = 'orig-exec'"
            ).fetchone()
        assert original["redelivery_count"] == 3
