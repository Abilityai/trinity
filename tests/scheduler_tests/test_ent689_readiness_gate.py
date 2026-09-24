"""
Readiness gate on a companion's proactive brief (trinity-enterprise#689).

The scheduler asks the backend before a CRON fire of a seat-delivery schedule
(`deliver_to_workspace_email`) and records a skipped execution with the reason
on `fire: false` — the #454 pre-check's shape, through the same helper.

Pinned here:
- held → skipped row carrying the reason, the skipped event, never dispatched;
- manual / webhook / non-seat schedules → the backend is not even asked;
- every error shape → fires (fail-open), and `fire: true` fires.
"""
import sqlite3
import sys
from pathlib import Path

_src_path = str(Path(__file__).resolve().parent.parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from scheduler.models import ExecutionStatus

HELD = "Held: test-agent is a calibrating companion — its proactive brief runs once its owner marks it ready (Workspace › Info › Role)."


def _make_seat_brief(db_path: str, schedule_id: str = "schedule-1") -> None:
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_schedules)")}
    if "deliver_to_workspace_email" not in cols:
        conn.execute("ALTER TABLE agent_schedules ADD COLUMN deliver_to_workspace_email TEXT")
    conn.execute("UPDATE agent_schedules SET deliver_to_workspace_email = ? WHERE id = ?",
                 ("seat@example.com", schedule_id))
    conn.commit()
    conn.close()


@pytest.fixture
def svc(db_with_data):
    from scheduler.service import SchedulerService

    s = SchedulerService.__new__(SchedulerService)
    s.db = db_with_data
    s.lock_manager = MagicMock()
    s._publish_event = AsyncMock()
    s._call_backend_execute_task = AsyncMock(return_value={"status": "dispatched"})
    s._get_next_run_time = MagicMock(return_value=None)
    s._run_pre_check = AsyncMock(return_value=None)
    return s


class TestReadinessGate:
    @pytest.mark.asyncio
    async def test_held_brief_is_a_skipped_row_with_the_reason(self, svc, db_with_data, initialized_db):
        _make_seat_brief(initialized_db)
        svc._run_readiness_check = AsyncMock(return_value={"fire": False, "reason": HELD})

        await svc._execute_schedule_with_lock("schedule-1")

        svc._run_readiness_check.assert_awaited_once_with("test-agent")
        svc._call_backend_execute_task.assert_not_called()
        event = svc._publish_event.await_args.args[0]
        assert event["type"] == "schedule_execution_skipped"
        assert event["reason"] == HELD
        row = db_with_data.get_execution(event["execution_id"])
        assert row.status == ExecutionStatus.SKIPPED
        assert HELD in (row.error or "")

    @pytest.mark.asyncio
    async def test_ready_fires(self, svc, initialized_db):
        _make_seat_brief(initialized_db)
        svc._run_readiness_check = AsyncMock(return_value={"fire": True, "reason": "stamped ready"})
        await svc._execute_schedule_with_lock("schedule-1")
        svc._call_backend_execute_task.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trigger", ["manual", "webhook"])
    async def test_a_person_or_webhook_is_not_gated(self, svc, initialized_db, trigger):
        _make_seat_brief(initialized_db)
        svc._run_readiness_check = AsyncMock(return_value={"fire": False, "reason": HELD})
        await svc._execute_schedule_with_lock("schedule-1", triggered_by=trigger)
        svc._run_readiness_check.assert_not_called()
        svc._call_backend_execute_task.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_schedule_that_delivers_to_no_seat_is_not_gated(self, svc):
        svc._run_readiness_check = AsyncMock(return_value={"fire": False, "reason": HELD})
        await svc._execute_schedule_with_lock("schedule-1")
        svc._run_readiness_check.assert_not_called()
        svc._call_backend_execute_task.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("decision", [None, {}, {"reason": "no fire key"}, {"fire": None}])
    async def test_anything_but_an_explicit_false_fires(self, svc, initialized_db, decision):
        _make_seat_brief(initialized_db)
        svc._run_readiness_check = AsyncMock(return_value=decision)
        await svc._execute_schedule_with_lock("schedule-1")
        svc._call_backend_execute_task.assert_awaited_once()


def _mock_get(response_json=None, status_code=200, raise_exc=None):
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=response_json)
    ctx = AsyncMock()
    if raise_exc is not None:
        ctx.__aenter__.return_value.get = AsyncMock(side_effect=raise_exc)
    else:
        ctx.__aenter__.return_value.get = AsyncMock(return_value=response)
    return patch("scheduler.service.httpx.AsyncClient", return_value=ctx)


class TestRunReadinessCheck:
    @pytest.mark.asyncio
    async def test_passes_the_verdict_through(self, svc):
        with _mock_get({"fire": False, "reason": HELD}):
            assert await svc._run_readiness_check("test-agent") == {"fire": False, "reason": HELD}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs", [
        {"raise_exc": httpx.ReadTimeout("slow")},
        {"status_code": 404},
        {"status_code": 500},
        {"response_json": ["not", "a", "dict"]},
    ])
    async def test_every_failure_is_none_so_the_brief_fires(self, svc, kwargs):
        with _mock_get(**kwargs):
            assert await svc._run_readiness_check("test-agent") is None
