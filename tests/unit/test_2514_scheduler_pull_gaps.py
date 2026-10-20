"""#2514 — the scheduler's dispatch-error path and the breaker on the pull path.

1. `_dispatch_and_record_outcome`'s exception branch (a dispatch timeout or
   transport error AFTER the backend may already have handed the row to the
   durable queue). It used to treat anything but `running` as finalized, so a
   `queued` row got `schedule_execution_completed(status=queued)` and no poll —
   the worker's real terminal was never reported and retry/validation never ran.
   Its FAILED write was also unconditional, so it could overwrite a row the
   backend queued, or a pull worker claimed, between the read and the write.
   Now: the FAILED write is a CAS on (`running`, unclaimed); a row it cannot
   fail because the pull path owns it gets the background poll instead.

2. The dispatch breaker on the pull path. An open breaker must refuse the turn
   BEFORE the persistent-queue branch, so nothing is enqueued.

The scheduler half drives the REAL `SchedulerDatabase` over a SQLite file
built from the backend's own `schedule_executions` DDL, so the CAS columns are
the real ones.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

pytestmark = pytest.mark.unit

PILOT = "pilot-a"
EXEC_ID = "exec-2514"


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 1. Scheduler: the dispatch-error path
# ---------------------------------------------------------------------------


def _make_db(tmp_path, *, status, claim_token=None):
    from db.schema import TABLES
    import src.scheduler.database as scheduler_database

    path = tmp_path / "trinity.db"
    conn = sqlite3.connect(path)
    conn.execute(TABLES["schedule_executions"])
    conn.execute(
        "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, "
        "started_at, message, triggered_by, claim_token) "
        "VALUES (?, 'sched-1', ?, ?, '2026-10-02T10:00:00Z', 'm', 'schedule', ?)",
        (EXEC_ID, PILOT, status, claim_token),
    )
    conn.commit()
    conn.close()
    return path, scheduler_database.SchedulerDatabase(str(path))


def _status(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT status, error FROM schedule_executions WHERE id = ?", (EXEC_ID,)
        ).fetchone()
    finally:
        conn.close()


def _run_dispatch_error(db):
    """Drive `_dispatch_and_record_outcome` with a dispatch that raises."""
    import src.scheduler.service as scheduler_service

    svc = object.__new__(scheduler_service.SchedulerService)
    svc.db = db
    svc._active_poll_tasks = set()
    svc._publish_event = AsyncMock()
    svc._call_backend_execute_task = AsyncMock(
        side_effect=Exception("dispatch to /api/internal/execute-task timed out after 30s")
    )
    poll = AsyncMock()
    svc._poll_and_finalize = poll

    schedule = MagicMock(
        id="sched-1", agent_name=PILOT, model=None, timeout_seconds=600,
        allowed_tools=None, deliver_to_workspace_email=None,
    )
    schedule.name = "nightly"
    execution = MagicMock(id=EXEC_ID)

    async def go():
        await svc._dispatch_and_record_outcome(schedule, execution, "m", "schedule")
        # Let the spawned poll task run.
        await asyncio.sleep(0)

    _await(go())
    return svc, poll


class TestDispatchErrorPath:
    def test_queued_row_gets_the_poll_and_no_completion(self, tmp_path):
        """The reported defect: a row the backend already queued must be polled
        to its real terminal, not announced complete as `queued`."""
        path, db = _make_db(tmp_path, status="queued")
        svc, poll = _run_dispatch_error(db)

        assert _status(path)[0] == "queued"
        poll.assert_awaited_once()
        assert poll.await_args.kwargs["execution_id"] == EXEC_ID
        assert poll.await_args.kwargs["timeout_seconds"] == 600
        svc._publish_event.assert_not_awaited()

    def test_claimed_running_row_is_not_failed(self, tmp_path):
        """A row a pull worker claimed is owned by that worker: writing FAILED
        under it would make its result CAS lose and record a finished run as a
        failure."""
        path, db = _make_db(tmp_path, status="running", claim_token="tok-1")
        svc, poll = _run_dispatch_error(db)

        assert _status(path)[0] == "running"
        poll.assert_awaited_once()
        svc._publish_event.assert_not_awaited()

    def test_unclaimed_running_row_is_still_failed(self, tmp_path):
        """Push path, unchanged: nothing else owns the row, so it is FAILED
        with the dispatch error and the completion is published."""
        path, db = _make_db(tmp_path, status="running")
        svc, poll = _run_dispatch_error(db)

        status, error = _status(path)
        assert status == "failed"
        assert "timed out after 30s" in error
        poll.assert_not_awaited()
        event = svc._publish_event.await_args.args[0]
        assert event["status"] == "failed"
        assert event["execution_id"] == EXEC_ID

    def test_finalized_row_is_left_alone(self, tmp_path):
        path, db = _make_db(tmp_path, status="success")
        svc, poll = _run_dispatch_error(db)

        assert _status(path)[0] == "success"
        poll.assert_not_awaited()
        event = svc._publish_event.await_args.args[0]
        assert event["status"] == "success"
        assert event["error"] is None


class TestFailedWriteIsACas:
    def test_require_unclaimed_refuses_a_claimed_row(self, tmp_path):
        path, db = _make_db(tmp_path, status="running", claim_token="tok-1")
        assert db.update_execution_status(
            EXEC_ID, "failed", error="x", expected_status="running", require_unclaimed=True
        ) is False
        assert _status(path)[0] == "running"

    def test_expected_status_refuses_a_queued_row(self, tmp_path):
        """The read-then-write race: the backend queued the row after the
        scheduler read it as `running`."""
        path, db = _make_db(tmp_path, status="queued")
        assert db.update_execution_status(
            EXEC_ID, "failed", error="x", expected_status="running", require_unclaimed=True
        ) is False
        assert _status(path)[0] == "queued"

    def test_unclaimed_running_row_is_written(self, tmp_path):
        path, db = _make_db(tmp_path, status="running")
        assert db.update_execution_status(
            EXEC_ID, "failed", error="x", expected_status="running", require_unclaimed=True
        ) is True
        assert _status(path)[0] == "failed"


# ---------------------------------------------------------------------------
# 2. The dispatch breaker on the pull path
# ---------------------------------------------------------------------------


class _FakeQueueDb:
    def __init__(self):
        self.queued = {}

    def get_queued_count(self, agent_name):
        return 0

    def get_max_backlog_depth(self, agent_name):
        return 50

    def update_execution_to_queued(self, execution_id, metadata, queued_at, conversation_key=None):
        self.queued[execution_id] = metadata
        return True


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)


class TestBreakerOnThePullPath:
    def test_open_breaker_refuses_before_the_queue(self, pilot, monkeypatch):
        """Real `CapacityManager` + `BacklogService`: with the breaker enabled
        and open, a pull payload is refused with `CircuitOpen` and no row is
        enqueued."""
        from services import capacity_manager as cm_module
        from services.backlog_service import BacklogService
        from services.task_execution_service import build_pull_queue_payload

        fake_db = _FakeQueueDb()
        monkeypatch.setattr(cm_module.redis, "from_url", lambda *_a, **_kw: MagicMock())
        slots = AsyncMock()
        slots.slots_prefix = "agent:slots:"
        slots.acquire_slot = AsyncMock(return_value=True)
        slots.register_on_release = lambda cb: None
        capacity = cm_module.CapacityManager(
            redis_url="redis://test", slot_service=slots, backlog_service=BacklogService()
        )

        payload = build_pull_queue_payload(
            agent_name=PILOT, triggered_by="schedule", execution_id=EXEC_ID,
            message="m", model=None, allowed_tools=None, system_prompt=None,
            timeout_seconds=600, resume_session_id=None, subscription_id=None,
            source_user_id=None, source_user_email=None, source_agent_name=None,
            slot_already_held=False,
        )
        assert payload is not None

        breaker = MagicMock()
        breaker.allow_dispatch.return_value = False
        breaker.retry_after_seconds.return_value = 30

        import config

        with (
            patch("database.db", fake_db),
            patch("services.settings_service.clamp_to_ceiling", lambda v: v),
            patch.object(config, "DISPATCH_BREAKER_ENABLED", True),
            patch.object(cm_module, "DispatchBreaker", return_value=breaker),
        ):
            with pytest.raises(cm_module.CircuitOpen):
                _await(
                    capacity.acquire(
                        agent_name=PILOT,
                        execution_id=EXEC_ID,
                        max_concurrent=3,
                        overflow_policy="queue_persistent",
                        overflow_payload=payload,
                        breaker_enabled=True,
                    )
                )

        assert fake_db.queued == {}
        slots.acquire_slot.assert_not_awaited()

    def test_producer_fails_the_turn_and_never_queues(self, pilot):
        """`execute_task` on a pilot with an open breaker: the turn ends FAILED,
        the agent is never called, nothing is queued."""
        import config
        from services.capacity_manager import CircuitOpen
        from services.task_execution_service import TaskExecutionService, TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.get_execution_timeout.return_value = 300
        mock_db.update_execution_status.return_value = True
        capacity = MagicMock()
        capacity.acquire = AsyncMock(side_effect=CircuitOpen(PILOT, 30))
        capacity.release = AsyncMock()
        circuit = MagicMock()
        circuit.allow_request.return_value = True
        post = AsyncMock()

        with (
            patch.object(config, "DISPATCH_ASYNC", False),
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=capacity),
            patch("services.task_execution_service.activity_service", MagicMock(
                track_activity=AsyncMock(return_value="act-1"), complete_activity=AsyncMock(),
            )),
            patch("services.task_execution_service.CircuitState", return_value=circuit),
            patch("services.task_execution_service.agent_post_with_retry", post),
            patch("services.task_execution_service.dispatch_breaker_active", return_value=True),
            patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        ):
            result = _await(
                TaskExecutionService().execute_task(
                    agent_name=PILOT, message="m", triggered_by="schedule",
                    execution_id=EXEC_ID, timeout_seconds=300,
                )
            )

        assert result.status == TaskExecutionStatus.FAILED
        post.assert_not_awaited()
        kw = capacity.acquire.await_args.kwargs
        assert kw["breaker_enabled"] is True
        assert kw["overflow_policy"] == "queue_persistent"
        mock_db.update_execution_to_queued.assert_not_called()


# ---------------------------------------------------------------------------
# 3. The half-open probe on a pull pilot goes through the queue, and the pull
#    sink records the breaker verdict
# ---------------------------------------------------------------------------


def _capacity(monkeypatch):
    from services import capacity_manager as cm_module
    from services.backlog_service import BacklogService

    monkeypatch.setattr(cm_module.redis, "from_url", lambda *_a, **_kw: MagicMock())
    slots = AsyncMock()
    slots.slots_prefix = "agent:slots:"
    slots.acquire_slot = AsyncMock(return_value=True)
    slots.register_on_release = lambda cb: None
    return cm_module, slots, cm_module.CapacityManager(
        redis_url="redis://test", slot_service=slots, backlog_service=BacklogService()
    )


def _probe_acquire(monkeypatch, *, agent, triggered_by="schedule"):
    """`acquire` with the breaker OPEN and this call holding the half-open probe."""
    import config
    from services.capacity_manager import PersistentTaskPayload
    from services.task_execution_service import build_pull_queue_payload

    cm_module, slots, capacity = _capacity(monkeypatch)
    fake_db = _FakeQueueDb()
    breaker = MagicMock()
    breaker.allow_dispatch.return_value = True  # this caller won the probe
    breaker.to_dict.return_value = {"state": "open"}
    # A real pilot payload (it is serialised into the queued row); the
    # non-pilot probe never reaches the queue, so a stub request is enough.
    payload = build_pull_queue_payload(
        agent_name=agent, triggered_by=triggered_by, execution_id=EXEC_ID,
        message="m", model=None, allowed_tools=None, system_prompt=None,
        timeout_seconds=600, resume_session_id=None, subscription_id=None,
        source_user_id=None, source_user_email=None, source_agent_name=None,
        slot_already_held=False,
    ) or PersistentTaskPayload(
        request=MagicMock(), effective_timeout=600, user_id=None, user_email=None,
        subscription_id=None, x_source_agent=None, triggered_by=triggered_by,
        collaboration_activity_id=None,
    )
    with (
        patch("database.db", fake_db),
        patch("services.settings_service.clamp_to_ceiling", lambda v: v),
        patch.object(config, "DISPATCH_BREAKER_ENABLED", True),
        patch.object(cm_module, "DispatchBreaker", return_value=breaker),
    ):
        result = _await(
            capacity.acquire(
                agent_name=agent,
                execution_id=EXEC_ID,
                max_concurrent=3,
                overflow_policy="queue_persistent",
                overflow_payload=payload,
                breaker_enabled=True,
            )
        )
    return result, fake_db, slots


class TestHalfOpenProbeOnAPilot:
    def test_probe_is_queued_not_pushed(self, pilot, monkeypatch):
        """A pilot's work reaches it only through the queue (#1982). The probe
        used to take a push slot instead, running the turn outside the pool."""
        result, fake_db, slots = _probe_acquire(monkeypatch, agent=PILOT)
        assert result.state == "queued_persistent"
        slots.acquire_slot.assert_not_awaited()
        assert EXEC_ID in fake_db.queued

    def test_non_pilot_probe_still_takes_a_slot(self, pilot, monkeypatch):
        """Push path unchanged: the probe is admitted into a free slot and never
        enqueued (#526 F1)."""
        result, fake_db, slots = _probe_acquire(monkeypatch, agent="not-a-pilot")
        assert result.state == "admitted"
        slots.acquire_slot.assert_awaited_once()
        assert fake_db.queued == {}


class TestPullSinkRecordsTheBreakerVerdict:
    def _apply(self, *, status, error_code=None, cas_won=True, breaker_on=True):
        from types import SimpleNamespace

        from services import pull_coordination_service as pcs
        import services.task_execution_service as tes

        mock_db = MagicMock()
        mock_db.get_execution.return_value = SimpleNamespace(
            id=EXEC_ID, agent_name=PILOT, status="running", triggered_by="schedule",
            duration_ms=None, cost=None, fan_out_id=None, loop_id=None,
            claude_session_id=None, timeout_seconds=600,
        )
        mock_db.update_execution_status.return_value = cas_won
        record = AsyncMock()

        async def go():
            outcome = pcs.apply_task_result(
                EXEC_ID, "tok", status=status, content="c", error_code=error_code,
            )
            await asyncio.sleep(0)
            return outcome

        with (
            patch.object(pcs, "db", mock_db),
            patch.object(pcs, "event_dispatch_service", MagicMock()),
            patch.object(pcs, "activity_service", MagicMock()),
            patch.object(pcs, "subscription_auto_switch", MagicMock()),
            patch.object(pcs, "channel_completion_report", MagicMock()),
            patch.object(tes, "_record_dispatch_terminal", record),
            patch.object(tes, "dispatch_breaker_active", return_value=breaker_on),
        ):
            _await(go())
        return record

    def test_success_records_a_success(self):
        """Without this the probe a pilot ran could never close the breaker."""
        record = self._apply(status="success")
        record.assert_awaited_once_with(PILOT, True, None)

    def test_auth_failure_records_auth(self):
        record = self._apply(status="failed", error_code="auth")
        record.assert_awaited_once_with(PILOT, True, "auth")

    def test_other_failures_record_nothing(self):
        """A non-auth failure's verdict is "nothing" — passing None would read
        as a success and reset the counter."""
        record = self._apply(status="failed", error_code="timeout")
        record.assert_not_awaited()

    def test_lost_cas_records_nothing(self):
        record = self._apply(status="success", cas_won=False)
        record.assert_not_awaited()
