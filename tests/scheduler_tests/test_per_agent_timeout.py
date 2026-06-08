"""
Tests for #913: scheduler honors per-agent execution_timeout_seconds.

Before #913, the scheduler always passed a concrete integer (the
schedule row's `timeout_seconds`, default 900 / later 3600) to
`/api/internal/execute-task`. That made the backend's per-agent
fallback at `task_execution_service.py:281` dead code on the scheduler
path — `PUT /api/agents/{name}/timeout` was silently ineffective for
cron-triggered runs.

The fix round-trips `None` through the scheduler boundary so the
backend's fallback fires. These tests pin the round-trip:

- DB read: NULL → `Schedule.timeout_seconds is None` (no fallback to 900)
- Dispatch payload: `Schedule.timeout_seconds=None` → JSON `"timeout_seconds": null`
- Polling deadline: None → uses `_POLL_DEADLINE_WHEN_NULL` (7200s)
- Pass-through: explicit int still propagates as that int (no regression)
"""

# Path setup must happen before scheduler imports
import sys
from pathlib import Path
_this_file = Path(__file__).resolve()
_src_path = str(_this_file.parent.parent.parent / 'src')
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

import sqlite3
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from scheduler.database import SchedulerDatabase
from scheduler.locking import LockManager
from scheduler.models import ExecutionStatus
from scheduler.service import SchedulerService, _POLL_DEADLINE_WHEN_NULL


# ---------------------------------------------------------------------------
# DB read: NULL → None
# ---------------------------------------------------------------------------


class TestRowToScheduleNullTimeout:
    """The scheduler's DB layer must return None for a NULL timeout column.

    Before #913 this site fell back to `900`, which is what made the
    backend's per-agent fallback dead code.
    """

    def test_null_column_returns_none(self, initialized_db: str):
        """Insert a schedule with `timeout_seconds=NULL`; read must yield None."""
        # The conftest schema doesn't carry `timeout_seconds`; add it so
        # this test exercises the realistic production read path.
        conn = sqlite3.connect(initialized_db)
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE agent_schedules ADD COLUMN timeout_seconds INTEGER")
        cursor.execute(
            """
            INSERT INTO agent_schedules (
                id, agent_name, name, cron_expression, message, enabled,
                timezone, owner_id, created_at, updated_at, timeout_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                "sched-null",
                "agent-a",
                "Inherit timeout",
                "*/5 * * * *",
                "hi",
                1,
                "UTC",
                1,
                "2026-05-23T00:00:00",
                "2026-05-23T00:00:00",
            ),
        )
        conn.commit()
        conn.close()

        db = SchedulerDatabase(database_path=initialized_db)
        schedule = db.get_schedule("sched-null")

        assert schedule is not None
        assert schedule.timeout_seconds is None, (
            "NULL in DB must surface as None so the backend's "
            "per-agent fallback fires (#913)."
        )

    def test_explicit_value_round_trips(self, initialized_db: str):
        """Explicit per-schedule overrides survive the read path unchanged."""
        conn = sqlite3.connect(initialized_db)
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE agent_schedules ADD COLUMN timeout_seconds INTEGER")
        cursor.execute(
            """
            INSERT INTO agent_schedules (
                id, agent_name, name, cron_expression, message, enabled,
                timezone, owner_id, created_at, updated_at, timeout_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sched-explicit",
                "agent-a",
                "Override timeout",
                "*/5 * * * *",
                "hi",
                1,
                "UTC",
                1,
                "2026-05-23T00:00:00",
                "2026-05-23T00:00:00",
                1234,
            ),
        )
        conn.commit()
        conn.close()

        db = SchedulerDatabase(database_path=initialized_db)
        schedule = db.get_schedule("sched-explicit")

        assert schedule is not None
        assert schedule.timeout_seconds == 1234


# ---------------------------------------------------------------------------
# Dispatch payload: round-trip None to backend
# ---------------------------------------------------------------------------


class TestDispatchPayloadCarriesNone:
    """`_call_backend_execute_task` must forward `None` as JSON `null` so
    `task_execution_service.py:281` per-agent fallback fires.
    """

    @staticmethod
    def _stub_async_accepted_response() -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "status": "accepted",
            "execution_id": "exec-1",
            "async_mode": True,
        }
        return resp

    @pytest.mark.asyncio
    async def test_none_propagates_to_payload(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Inherit",
        )
        # Resolve immediately so the background poll task exits fast.
        db_with_data.update_execution_status(
            execution_id=execution.id,
            status=ExecutionStatus.SUCCESS,
            response="ok",
        )

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("scheduler.service.config") as mock_config:
            mock_config.poll_interval = 0.01  # keep the spawned poll task short
            mock_config.poll_deadline_buffer = 1.0
            mock_config.internal_api_secret = None
            mock_config.backend_url = "http://backend"

            mock_client = AsyncMock()
            mock_client.post.return_value = self._stub_async_accepted_response()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await service._call_backend_execute_task(
                agent_name="test-agent",
                message="Inherit",
                triggered_by="schedule",
                timeout_seconds=None,
                execution_id=execution.id,
            )

            payload = mock_client.post.call_args.kwargs["json"]
            # Field present and explicitly None — pins the wire contract so
            # a regression to a default integer surfaces immediately.
            assert "timeout_seconds" in payload
            assert payload["timeout_seconds"] is None

        # Drain the spawned background poll task so pytest doesn't warn.
        for t in list(service._active_poll_tasks):
            t.cancel()

    @pytest.mark.asyncio
    async def test_explicit_int_still_propagates(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Per-schedule overrides must continue to work unchanged."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Explicit",
        )
        db_with_data.update_execution_status(
            execution_id=execution.id,
            status=ExecutionStatus.SUCCESS,
            response="ok",
        )

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("scheduler.service.config") as mock_config:
            mock_config.poll_interval = 0.01
            mock_config.poll_deadline_buffer = 1.0
            mock_config.internal_api_secret = None
            mock_config.backend_url = "http://backend"

            mock_client = AsyncMock()
            mock_client.post.return_value = self._stub_async_accepted_response()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await service._call_backend_execute_task(
                agent_name="test-agent",
                message="Explicit",
                triggered_by="schedule",
                timeout_seconds=1234,
                execution_id=execution.id,
            )

            payload = mock_client.post.call_args.kwargs["json"]
            assert payload["timeout_seconds"] == 1234

        for t in list(service._active_poll_tasks):
            t.cancel()


# ---------------------------------------------------------------------------
# Polling deadline fallback
# ---------------------------------------------------------------------------


class TestPollingDeadlineFallback:
    """The poll deadline math must use `_POLL_DEADLINE_WHEN_NULL` when the
    schedule's timeout is None — without it the deadline calc would
    `float(None) + buffer` and raise TypeError.
    """

    def test_constant_matches_api_clamp(self):
        """`PUT /api/agents/{name}/timeout` clamps at 7200s; the scheduler's
        None-fallback must be at least that high so it never gives up
        before the backend's enforcement does."""
        assert _POLL_DEADLINE_WHEN_NULL == 7200

    @pytest.mark.asyncio
    async def test_poll_completion_with_none_timeout_does_not_raise(
        self,
        db_with_data: SchedulerDatabase,
        mock_lock_manager: LockManager,
    ):
        """Poll must not TypeError when `timeout_seconds=None` and the row
        resolves cleanly. Before #913 the deadline math would have hit
        `float(None) + buffer` and raised."""
        service = SchedulerService(
            database=db_with_data,
            lock_manager=mock_lock_manager,
        )

        execution = db_with_data.create_execution(
            schedule_id="schedule-1",
            agent_name="test-agent",
            message="Inherit",
        )
        # Pre-resolve so the first poll iteration returns the terminal row.
        db_with_data.update_execution_status(
            execution_id=execution.id,
            status=ExecutionStatus.SUCCESS,
            response="done",
        )

        # Patch the config so we don't sleep 10s for the first poll tick.
        with patch("scheduler.service.config") as mock_config:
            mock_config.poll_interval = 0.01
            mock_config.poll_deadline_buffer = 1.0

            result = await service._poll_execution_completion(
                execution_id=execution.id,
                timeout_seconds=None,  # the #913 path
            )

        assert result["status"] == ExecutionStatus.SUCCESS
