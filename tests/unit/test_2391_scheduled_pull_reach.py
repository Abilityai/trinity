"""#2391 — scheduled work can reach the durable pull queue.

Until this change `task_execution_service` — the producer behind the scheduler,
i.e. **all cron**, plus webhooks, reminders, loops and fan-out — dispatched with
`overflow_policy="reject"` unconditionally. `capacity_manager.acquire` only
offers a row to the durable queue when its producer passed
`"queue_persistent"`, so `PULL_MODE_PILOT_AGENTS` was structurally inert for the
fleet's dominant traffic class no matter how it was set. #2048 made that
legible (`PULL_REACHABLE_TRIGGERS`, `note_unreachable_pull_trigger`) and
deliberately deferred fixing it; this is the deferred half.

The whole risk of the change is in *how* the policy widens, so that is what this
file pins:

  * **Gated, never unconditional.** The policy flips to `queue_persistent` only
    when `pull_owns_dispatch` says a pilot owns the trigger. That keeps the
    blast radius exactly equal to the pilot allowlist — the reason #2048
    declined to bundle Option 2 was that an *unconditional* persistent queue on
    this producer would change what happens to scheduled work under capacity
    pressure for every agent, flag or no flag.
  * **Flag OFF is byte-for-byte.** Verified against the real acquire call, not
    assumed: same policy, no payload, same `CapacityFull` → FAILED terminal.
  * **Stranded stays stranded.** `loop` / `fan_out` / `a2a` /
    `operator_response` have a caller that reads `TaskExecutionResult`
    synchronously, so queueing them would silently return nothing to read.
  * **#1083 does not stack.** A pull-queued row is never dispatched, so no 202
    ACK can arrive and `async_result` is never sent — one mechanism per turn.
  * **The queued row is actually claimable.** The real producer's metadata is
    fed to the real claim-response builder, so "it enqueued" is not mistaken for
    "a worker can run it".

Plus the scheduler side: `queued` is NOT a terminal, and the async-poll loop
must poll through it or it publishes a bogus failure and schedules a retry for
work that is queued and about to run.

Pure unit test — mocked transport / capacity collaborators for the producer
assertions, the REAL `CapacityManager` + `BacklogService` + claim builder for
the end-to-end, and the scheduler service driven via `object.__new__` (the
`test_1823_scheduler_permanent_classification.py` precedent).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
# Appended, never inserted at 0, so the repo root cannot shadow the
# conftest-managed `src/backend` entries (mirrors test_1823 / test_1994).
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

# `src/scheduler/config.py` reads these at import time (#589).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

pytestmark = pytest.mark.unit


PILOT = "pilot-a"
NON_PILOT = "bob"


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _resp(status_code, body=None):
    r = MagicMock()
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    r.json.return_value = body if body is not None else {}
    return r


_SUCCESS_BODY = {
    "response": "done",
    "session_id": "s1",
    "metadata": {"cost_usd": 0.01, "context_window": 200000},
    "execution_log": [],
}


# ---------------------------------------------------------------------------
# Producer harness — real execute_task, mocked capacity + transport
# ---------------------------------------------------------------------------


def _run(
    *,
    agent_name=PILOT,
    triggered_by="schedule",
    execution_id="exec-2391",
    acquire_result=None,
    acquire_raises=None,
    dispatch_async=False,
    **execute_kwargs,
):
    """Drive the real `execute_task` and return `(result, mocks)`.

    `capacity` is a mock so the test can read back the exact `overflow_policy`
    and `overflow_payload` this producer chose — which is the decision under
    test. The end-to-end class below uses the real one instead.
    """
    import config
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution_timeout.return_value = 300
    mock_db.get_execution.return_value = MagicMock(status="cancelled")
    mock_db.update_execution_status.return_value = True

    mock_capacity = MagicMock()
    if acquire_raises is not None:
        mock_capacity.acquire = AsyncMock(side_effect=acquire_raises)
    else:
        mock_capacity.acquire = AsyncMock(
            return_value=acquire_result or MagicMock(state="admitted")
        )
    mock_capacity.release = AsyncMock()

    mock_circuit = MagicMock()
    mock_circuit.allow_request.return_value = True
    mock_activity = MagicMock(
        track_activity=AsyncMock(return_value="act-1"),
        complete_activity=AsyncMock(),
    )
    post_mock = AsyncMock(return_value=_resp(200, _SUCCESS_BODY))

    with (
        patch.object(config, "DISPATCH_ASYNC", dispatch_async),
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
        patch("services.task_execution_service.agent_post_with_retry", post_mock),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
    ):
        svc = TaskExecutionService()
        result = _await(
            svc.execute_task(
                agent_name=agent_name,
                message="do the thing",
                triggered_by=triggered_by,
                execution_id=execution_id,
                timeout_seconds=300,
                model="sonnet",
                **execute_kwargs,
            )
        )
    return result, {"db": mock_db, "capacity": mock_capacity, "post": post_mock}


def _acquire_kwargs(capacity_mock):
    return capacity_mock.acquire.await_args.kwargs


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)


@pytest.fixture
def no_pilots(monkeypatch):
    """The default the whole fleet runs on."""
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")


# ---------------------------------------------------------------------------
# 1. Flag OFF — scheduled dispatch is unchanged (AC: verified, not assumed)
# ---------------------------------------------------------------------------


class TestFlagOffIsUnchanged:
    @pytest.mark.parametrize(
        "trigger", ["schedule", "webhook", "reminder", "loop", "fan_out"]
    )
    def test_policy_stays_reject_with_no_pilots(self, no_pilots, trigger):
        """The property the entire risk assessment rests on: with an empty
        allowlist this producer still asks for `reject` and offers no payload,
        so `capacity_manager` cannot queue even if it wanted to."""
        _, m = _run(triggered_by=trigger)
        kw = _acquire_kwargs(m["capacity"])
        assert kw["overflow_policy"] == "reject"
        assert kw["overflow_payload"] is None

    def test_a_different_agent_being_a_pilot_changes_nothing(self, pilot):
        """Blast radius is the allowlist, not the trigger."""
        _, m = _run(agent_name=NON_PILOT, triggered_by="schedule")
        kw = _acquire_kwargs(m["capacity"])
        assert kw["overflow_policy"] == "reject"
        assert kw["overflow_payload"] is None

    def test_at_capacity_still_fails_fast_with_the_same_terminal(self, no_pilots):
        """The behaviour #2391 was warned not to change: a scheduled fire that
        arrives at capacity is REJECTED, and the row is written FAILED with the
        parallel-tasks wording."""
        from services.capacity_manager import CapacityFull
        from services.task_execution_service import TaskExecutionStatus

        result, m = _run(
            triggered_by="schedule",
            acquire_raises=CapacityFull(NON_PILOT, 3, "rejected"),
        )
        assert result.status == TaskExecutionStatus.FAILED
        assert "at capacity (3/3 parallel tasks running)" in result.error
        m["post"].assert_not_awaited()
        m["db"].update_execution_status.assert_called_once()

    def test_the_agent_is_still_dispatched_normally(self, no_pilots):
        from services.task_execution_service import TaskExecutionStatus

        result, m = _run(triggered_by="schedule")
        assert result.status == TaskExecutionStatus.SUCCESS
        m["post"].assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Flag ON — the scheduler's triggers reach the queue
# ---------------------------------------------------------------------------


class TestFlagOnQueuesScheduledWork:
    @pytest.mark.parametrize("trigger", ["schedule", "webhook", "reminder"])
    def test_policy_widens_and_a_payload_is_offered(self, pilot, trigger):
        _, m = _run(triggered_by=trigger)
        kw = _acquire_kwargs(m["capacity"])
        assert kw["overflow_policy"] == "queue_persistent"
        assert kw["overflow_payload"] is not None
        assert kw["overflow_payload"].triggered_by == trigger

    def test_a_queued_row_returns_QUEUED_and_never_calls_the_agent(self, pilot):
        """The handoff. No agent call, no slot to release — the worker owns the
        turn from here and reports its terminal through the claim-token CAS."""
        from services.task_execution_service import TaskExecutionStatus

        result, m = _run(
            triggered_by="schedule",
            acquire_result=MagicMock(state="queued_persistent"),
        )
        assert result.status == TaskExecutionStatus.QUEUED
        assert result.execution_id == "exec-2391"
        m["post"].assert_not_awaited()
        m["capacity"].release.assert_not_awaited()
        m["db"].mark_execution_dispatched.assert_not_called()

    def test_the_payload_carries_the_per_task_settings(self, pilot):
        """`backlog_service.enqueue` reads these off the request; a pulled turn
        that lost them would silently run on agent/global defaults (#2317)."""
        _, m = _run(
            triggered_by="schedule",
            allowed_tools=["Bash", "Read"],
            system_prompt="be brief",
            resume_session_id="sess-9",
        )
        req = _acquire_kwargs(m["capacity"])["overflow_payload"].request
        assert req.message == "do the thing"
        assert req.model == "sonnet"
        assert req.allowed_tools == ["Bash", "Read"]
        assert req.timeout_seconds == 300
        assert req.resume_session_id == "sess-9"
        # The CALLER's prompt, uncomposed — the claim path rebuilds the platform
        # prompt around it (#1629), so composing here would double it.
        assert req.system_prompt == "be brief"

    def test_a_full_backlog_names_the_limit_that_actually_bound(self, pilot):
        """On the pull path `CapacityFull` means max_backlog_depth, not busy
        parallel slots — the pull branch never asks for a slot. Reporting
        "3/3 parallel tasks running" would send the operator to the wrong knob."""
        from services.capacity_manager import CapacityFull
        from services.task_execution_service import TaskExecutionStatus

        result, _ = _run(
            triggered_by="schedule",
            acquire_raises=CapacityFull(PILOT, 3, "persistent_full"),
        )
        assert result.status == TaskExecutionStatus.FAILED
        assert "backlog full" in result.error
        assert "parallel tasks" not in result.error


# ---------------------------------------------------------------------------
# 3. The triggers that stay stranded, and why
# ---------------------------------------------------------------------------


class TestStrandedTriggersStayPushed:
    def test_nothing_autonomous_is_stranded_any_more(self, pilot):
        """#2391 opened this producer; #2523 and #2524 finished the job.

        `a2a` and `operator_response` were the last two, and they needed the
        sync edge adapter (`dispatch_and_await_terminal`) rather than a policy
        change: their callers genuinely need the answer in-line, they just do
        not need it from the dispatch's return value.
        """
        from services.task_execution_service import _AUTONOMOUS_TRIGGERS

        for trigger in sorted(_AUTONOMOUS_TRIGGERS):
            _, m = _run(triggered_by=trigger)
            kw = _acquire_kwargs(m["capacity"])
            assert kw["overflow_policy"] == "queue_persistent", trigger
            assert kw["overflow_payload"] is not None, trigger

    @pytest.mark.parametrize("trigger", ["manual", "mcp", "chat", "public", "voice"])
    def test_interactive_triggers_are_untouched(self, pilot, trigger):
        """Open Question 7's scope cut: a human turn keeps the synchronous push
        path and today's Redis session lock."""
        _, m = _run(triggered_by=trigger)
        assert _acquire_kwargs(m["capacity"])["overflow_policy"] == "reject"


# ---------------------------------------------------------------------------
# 4. Preconditions on the payload builder
# ---------------------------------------------------------------------------


class TestPayloadPreconditions:
    def _build(self, **over):
        from services.task_execution_service import build_pull_queue_payload

        kwargs = dict(
            agent_name=PILOT,
            triggered_by="schedule",
            execution_id="exec-1",
            message="m",
            model="sonnet",
            allowed_tools=None,
            system_prompt=None,
            timeout_seconds=300,
            resume_session_id=None,
            subscription_id=None,
            source_user_id=None,
            source_user_email=None,
            source_agent_name=None,
            slot_already_held=False,
        )
        kwargs.update(over)
        return build_pull_queue_payload(**kwargs)

    def test_refuses_without_an_execution_id(self, pilot):
        """`BacklogService.enqueue` transitions an EXISTING row RUNNING→QUEUED
        under a CAS; with no row there is nothing to transition."""
        assert self._build(execution_id=None) is None

    def test_refuses_when_a_slot_is_already_held(self, pilot):
        """The caller (a drain, or /task's pre-flight) owns a real slot that
        `execute_task`'s `finally` releases. Queueing under it would leak the
        slot for the whole lease TTL."""
        assert self._build(slot_already_held=True) is None

    def test_builds_for_a_pilot(self, pilot):
        assert self._build() is not None

    def test_refuses_for_a_non_pilot(self, pilot):
        assert self._build(agent_name=NON_PILOT) is None


# ---------------------------------------------------------------------------
# 5. #1083 fire-and-forget does not stack on pull (AC #4)
# ---------------------------------------------------------------------------


class TestFireAndForgetInteraction:
    def test_pull_wins_and_no_async_ack_is_ever_requested(self, pilot):
        """`schedule` is eligible for BOTH mechanisms once `DISPATCH_ASYNC` is
        on. They cannot both apply to one turn, and pull wins by construction
        rather than by precedence: the row is queued before any dispatch, so
        there is no request for the agent to answer 202 to and `async_result` is
        never sent."""
        from services.task_execution_service import TaskExecutionStatus

        result, m = _run(
            triggered_by="schedule",
            dispatch_async=True,
            acquire_result=MagicMock(state="queued_persistent"),
        )
        assert result.status == TaskExecutionStatus.QUEUED
        assert result.dispatched_async is False
        m["post"].assert_not_awaited()

    def test_fire_and_forget_is_untouched_for_a_non_pilot(self, no_pilots):
        """The other direction: #1083 keeps working exactly as it did on every
        agent that is not a pull pilot."""
        from services.task_execution_service import TaskExecutionStatus

        result, m = _run(
            triggered_by="schedule",
            dispatch_async=True,
        )
        m["post"].assert_awaited_once()
        assert m["post"].await_args.args[2]["async_result"] is True
        assert result.status == TaskExecutionStatus.SUCCESS


# ---------------------------------------------------------------------------
# 6. End-to-end: a scheduled row really lands in the queue, claimable
# ---------------------------------------------------------------------------


class _FakeQueueDb:
    """The slice of the `database.db` singleton `BacklogService.enqueue` uses."""

    def __init__(self):
        self.queued: dict[str, str] = {}

    def get_queued_count(self, agent_name):
        return 0

    def get_max_backlog_depth(self, agent_name):
        return 50

    def update_execution_to_queued(self, execution_id, metadata, queued_at):
        self.queued[execution_id] = metadata
        return True


class TestEnqueuedScheduledRowIsClaimable:
    """Drives the REAL `CapacityManager` + `BacklogService` so the assertion is
    about what a worker would actually receive, not about a mock's call args.

    "It enqueued" and "a worker can run it" are different claims, and #2317 is
    the precedent for them coming apart silently: the pull envelope read a
    nested key no producer wrote, so every pulled turn ran on defaults while
    every enqueue test stayed green.
    """

    def _enqueue_via_capacity(self, monkeypatch, trigger="schedule"):
        from services import capacity_manager as cm_module
        from services.backlog_service import BacklogService
        from services.task_execution_service import build_pull_queue_payload

        fake_db = _FakeQueueDb()
        monkeypatch.setattr(cm_module.redis, "from_url", lambda *_a, **_kw: MagicMock())

        slots = AsyncMock()
        slots.slots_prefix = "agent:slots:"
        slots.acquire_slot = AsyncMock(return_value=True)  # a slot IS free
        slots.register_on_release = lambda cb: None

        backlog = BacklogService()
        capacity = cm_module.CapacityManager(
            redis_url="redis://test", slot_service=slots, backlog_service=backlog
        )

        payload = build_pull_queue_payload(
            agent_name=PILOT,
            triggered_by=trigger,
            execution_id="exec-e2e",
            message="run the nightly report",
            model="opus",
            allowed_tools=["Bash"],
            system_prompt="be brief",
            timeout_seconds=1800,
            resume_session_id="sess-e2e",
            subscription_id="sub-1",
            source_user_id=None,
            source_user_email=None,
            source_agent_name=None,
            slot_already_held=False,
        )
        assert payload is not None

        # `acquire` and `enqueue` both late-import their collaborators, so patch
        # the modules those names resolve from. The ephemeral-budget read is not
        # stubbed deliberately: it is fail-open on any error, and letting the
        # real guard run proves this path does not depend on stubbing it out.
        with (
            patch("database.db", fake_db),
            patch("services.settings_service.clamp_to_ceiling", lambda v: v),
        ):
            result = _await(
                capacity.acquire(
                    agent_name=PILOT,
                    execution_id="exec-e2e",
                    max_concurrent=3,
                    overflow_policy="queue_persistent",
                    overflow_payload=payload,
                )
            )
        return result, fake_db, slots

    def test_the_row_is_queued_not_admitted(self, pilot, monkeypatch):
        result, fake_db, slots = self._enqueue_via_capacity(monkeypatch)
        assert result.state == "queued_persistent"
        # No ZADD even though a slot was free: capacity is the worker pool now.
        slots.acquire_slot.assert_not_awaited()
        assert "exec-e2e" in fake_db.queued

    def test_the_claim_envelope_a_worker_receives_is_complete(self, pilot, monkeypatch):
        """Feed the REAL producer's metadata to the REAL claim-response builder,
        through a row shaped like `claim_next_queued`'s RETURNING columns."""
        from services import pull_coordination_service as pcs

        _, fake_db, _ = self._enqueue_via_capacity(monkeypatch)
        meta = fake_db.queued["exec-e2e"]

        claimed_row = {
            "id": "exec-e2e",
            "agent_name": PILOT,
            "message": "run the nightly report",
            "backlog_metadata": meta,
            "source_user_id": None,
            "source_user_email": None,
            "source_agent_name": None,
            "source_mcp_key_id": None,
            "source_mcp_key_name": None,
            "subscription_id": "sub-1",
            "triggered_by": "schedule",
            "claude_session_id": None,
            "model_used": "opus",
            "started_at": "2026-01-01T00:00:00.000000Z",
            "claim_token": "tok-1",
            "lease_expires_at": "2026-01-01T00:30:00.000000Z",
            "claimed_by_worker": f"{PILOT}#w1",
            "redelivery_count": 0,
        }
        with patch.object(pcs, "_compose_pull_system_prompt", lambda *a, **kw: "composed"):
            claim = pcs._build_claim_response(claimed_row)

        assert claim["execution_id"] == "exec-e2e"
        assert claim["claim_token"] == "tok-1"
        env = claim["envelope"]
        assert env["to"] == PILOT
        assert env["payload"]["message"] == "run the nightly report"
        # Session identity comes from the key the producer writes (#2317).
        assert env["payload"]["session_id"] == "sess-e2e"
        overrides = env["payload"]["task_overrides"]
        assert overrides["model"] == "opus"
        assert overrides["allowed_tools"] == ["Bash"]
        assert overrides["timeout_seconds"] == 1800

    def test_the_persisted_metadata_records_the_scheduled_trigger(self, pilot, monkeypatch):
        """Provenance survives the round-trip: the queued row still says it came
        from cron, which is what `_compose_pull_system_prompt` derives the
        execution-context mode from."""
        _, fake_db, _ = self._enqueue_via_capacity(monkeypatch)
        assert json.loads(fake_db.queued["exec-e2e"])["triggered_by"] == "schedule"


# ---------------------------------------------------------------------------
# 7. Scheduler side — `queued` is not a terminal
# ---------------------------------------------------------------------------


class _PollDb:
    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.calls = 0

    def get_execution(self, execution_id):
        self.calls += 1
        status = self._statuses[min(self.calls - 1, len(self._statuses) - 1)]
        return MagicMock(
            id=execution_id, status=status, response="ok", error=None,
            cost=0.0, context_used=None, context_max=None,
        )


def _poller(monkeypatch, statuses):
    import src.scheduler.service as scheduler_service

    monkeypatch.setattr(scheduler_service.config, "poll_interval", 0)
    monkeypatch.setattr(scheduler_service.config, "poll_deadline_buffer", 5)
    svc = object.__new__(scheduler_service.SchedulerService)
    svc.db = _PollDb(statuses)
    return scheduler_service, svc


class TestSchedulerPollsThroughQueued:
    def test_queued_does_not_end_the_poll(self, monkeypatch):
        """Without this the scheduler reads `queued` as "completed", publishes
        `schedule_execution_completed(status=queued)` — a failure, since only
        `success` is not — and hands it to `_maybe_schedule_retry`, duplicating
        work that is queued and about to run."""
        _, svc = _poller(monkeypatch, ["queued", "queued", "running", "success"])
        result = _await(svc._poll_execution_completion("exec-1", 30))
        assert result["status"] == "success"
        assert svc.db.calls == 4

    def test_a_real_terminal_still_ends_the_poll_immediately(self, monkeypatch):
        _, svc = _poller(monkeypatch, ["failed"])
        result = _await(svc._poll_execution_completion("exec-1", 30))
        assert result["status"] == "failed"
        assert svc.db.calls == 1

    def test_queued_is_declared_non_terminal(self, monkeypatch):
        module, _ = _poller(monkeypatch, ["success"])
        assert module.ExecutionStatus.QUEUED in module._NON_TERMINAL_POLL_STATES
        assert module.ExecutionStatus.RUNNING in module._NON_TERMINAL_POLL_STATES
        assert module.ExecutionStatus.SUCCESS not in module._NON_TERMINAL_POLL_STATES
