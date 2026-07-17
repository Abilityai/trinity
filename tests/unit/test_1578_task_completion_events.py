"""Unit tests for system-emitted task-completion events (#1578).

The backend deterministically emits ``agent.task.completed`` / ``agent.task.failed``
at EVERY CAS-won execution terminal, delivered over the existing EVT-001
subscription-dispatch path, so a subscribed orchestrator is woken instead of
polling. These pure unit tests pin the whole contract without a live backend:

Layer B — the shared emit helper (``event_dispatch_service.emit_task_terminal_event``):
  fired / not-fired (no sub, AC #1/#5) / recursion-break / status-as-.value /
  payload shape (fan_out_id, loop_id) / fail-open.

Layer A — every CAS-won terminal writer invokes the spawner on won, never on a
  lost CAS. Crucially this covers BOTH #1083 terminal paths GENUINELY (dossier
  §5): the inline sync path (``apply_result`` called directly) AND the async
  result-callback path (the ``agent_execution_result`` endpoint driving the REAL
  ``apply_result``) — two distinct entry points, not two tests hitting the same
  inline call. Plus the timeout/crash writer (``_write_terminal_and_gate``), the
  #1083 lease-reaper, and the pull sink.

Layer C — reserved-namespace loop guards on the router coroutines (emit / create
  self-sub / PUT self-sub / cross-agent allowed).

Mocks mirror tests/unit/test_1083_apply_result.py + test_1083_callback_endpoint.py.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _execution(**over):
    base = dict(
        id="exec-1578",
        agent_name="worker-a",
        status="success",
        triggered_by="manual",
        duration_ms=1234,
        cost=0.05,
        fan_out_id=None,
        loop_id=None,
        claude_session_id="dispatched_async",
    )
    base.update(over)
    return SimpleNamespace(**base)


# ===========================================================================
# Layer B — the shared emit helper
# ===========================================================================
class TestEmitHelper:
    """Direct tests of event_dispatch_service.emit_task_terminal_event."""

    def _run(
        self,
        *,
        terminal_status,
        execution,
        matching=None,
        summary_or_error="done",
        duration_ms=None,
        cost=None,
    ):
        from services import event_dispatch_service as eds

        mock_db = MagicMock()
        mock_db.get_execution.return_value = execution
        mock_db.find_matching_event_subscriptions.return_value = (
            matching if matching is not None else [SimpleNamespace(id="sub-1")]
        )
        mock_db.create_agent_event.return_value = SimpleNamespace(id="evt-1")

        with (
            patch.object(eds, "db", mock_db),
            patch.object(eds, "trigger_subscription", MagicMock()),
            patch.object(eds, "_spawn_emit_dispatch", MagicMock()) as spawn,
        ):
            _await(
                eds.emit_task_terminal_event(
                    "worker-a",
                    "exec-1578",
                    terminal_status=terminal_status,
                    summary_or_error=summary_or_error,
                    duration_ms=duration_ms,
                    cost=cost,
                )
            )
        return mock_db, spawn

    def test_success_emits_completed_event(self):
        from models import TaskExecutionStatus

        mock_db, spawn = self._run(
            terminal_status=TaskExecutionStatus.SUCCESS,
            execution=_execution(status="success", fan_out_id="fo-9", loop_id="lp-3"),
        )
        mock_db.create_agent_event.assert_called_once()
        kwargs = mock_db.create_agent_event.call_args.kwargs
        assert kwargs["event_type"] == "agent.task.completed"
        assert kwargs["source_agent"] == "worker-a"
        payload = kwargs["payload"]
        assert payload["execution_id"] == "exec-1578"
        assert payload["status"] == "success"  # string value, NOT the enum member
        assert payload["triggered_by"] == "manual"
        assert payload["fan_out_id"] == "fo-9"
        assert payload["loop_id"] == "lp-3"
        # one dispatch spawned per matching subscription
        assert spawn.call_count == 1

    def test_failed_emits_failed_event(self):
        from models import TaskExecutionStatus

        mock_db, spawn = self._run(
            terminal_status=TaskExecutionStatus.FAILED,
            execution=_execution(status="failed"),
            summary_or_error="boom",
        )
        kwargs = mock_db.create_agent_event.call_args.kwargs
        assert kwargs["event_type"] == "agent.task.failed"
        assert kwargs["payload"]["status"] == "failed"
        assert kwargs["payload"]["summary_or_error"] == "boom"

    def test_cancelled_maps_to_failed_event_with_cancelled_status(self):
        from models import TaskExecutionStatus

        mock_db, _ = self._run(
            terminal_status=TaskExecutionStatus.CANCELLED,
            execution=_execution(status="cancelled"),
        )
        kwargs = mock_db.create_agent_event.call_args.kwargs
        # non-SUCCESS terminal → agent.task.failed, but the precise status rides
        # in the payload so a subscriber can distinguish a cancel from a failure.
        assert kwargs["event_type"] == "agent.task.failed"
        assert kwargs["payload"]["status"] == "cancelled"

    def test_no_matching_subscription_emits_nothing(self):
        """AC #1/#5: zero matching subs ⇒ NO agent_events row, NO dispatch."""
        from models import TaskExecutionStatus

        mock_db, spawn = self._run(
            terminal_status=TaskExecutionStatus.SUCCESS,
            execution=_execution(status="success"),
            matching=[],
        )
        mock_db.create_agent_event.assert_not_called()
        spawn.assert_not_called()

    def test_recursion_break_suppresses_emit(self):
        """A task spawned BY an agent.task.* dispatch (triggered_by='event') must
        not re-emit — the decisive loop guard. No row, no even a find_matching."""
        from models import TaskExecutionStatus

        mock_db, spawn = self._run(
            terminal_status=TaskExecutionStatus.SUCCESS,
            execution=_execution(status="success", triggered_by="event"),
        )
        mock_db.find_matching_event_subscriptions.assert_not_called()
        mock_db.create_agent_event.assert_not_called()
        spawn.assert_not_called()

    def test_status_value_not_enum_member(self):
        """The #1085 footgun: str(TaskExecutionStatus.SUCCESS) is
        'TaskExecutionStatus.SUCCESS'. The payload MUST carry '.value'."""
        from models import TaskExecutionStatus

        # execution.status is the ENUM member (defensive fallback path).
        mock_db, _ = self._run(
            terminal_status=TaskExecutionStatus.SUCCESS,
            execution=_execution(status=TaskExecutionStatus.SUCCESS),
        )
        assert mock_db.create_agent_event.call_args.kwargs["payload"]["status"] == "success"

    def test_duration_and_cost_fall_back_to_row(self):
        from models import TaskExecutionStatus

        mock_db, _ = self._run(
            terminal_status=TaskExecutionStatus.SUCCESS,
            execution=_execution(status="success", duration_ms=777, cost=0.42),
            duration_ms=None,
            cost=None,
        )
        payload = mock_db.create_agent_event.call_args.kwargs["payload"]
        assert payload["duration_ms"] == 777
        assert payload["cost"] == 0.42

    def test_summary_truncated(self):
        from models import TaskExecutionStatus
        from services import event_dispatch_service as eds

        mock_db, _ = self._run(
            terminal_status=TaskExecutionStatus.SUCCESS,
            execution=_execution(status="success"),
            summary_or_error="x" * (eds.TASK_EVENT_SUMMARY_MAX + 500),
        )
        payload = mock_db.create_agent_event.call_args.kwargs["payload"]
        assert len(payload["summary_or_error"]) == eds.TASK_EVENT_SUMMARY_MAX

    def test_fail_open_on_db_error(self):
        """A broken emit never raises into the (already billed) terminal."""
        from models import TaskExecutionStatus
        from services import event_dispatch_service as eds

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _execution(status="success")
        mock_db.find_matching_event_subscriptions.side_effect = RuntimeError("db down")

        with patch.object(eds, "db", mock_db):
            # Must NOT raise.
            _await(
                eds.emit_task_terminal_event(
                    "worker-a",
                    "exec-1578",
                    terminal_status=TaskExecutionStatus.SUCCESS,
                    summary_or_error="done",
                )
            )

    def test_no_execution_id_is_noop(self):
        from models import TaskExecutionStatus
        from services import event_dispatch_service as eds

        mock_db = MagicMock()
        with patch.object(eds, "db", mock_db):
            _await(
                eds.emit_task_terminal_event(
                    "worker-a",
                    None,
                    terminal_status=TaskExecutionStatus.SUCCESS,
                )
            )
        mock_db.find_matching_event_subscriptions.assert_not_called()
        mock_db.create_agent_event.assert_not_called()


# ===========================================================================
# Layer A1 — apply_result (inline sync path) emits on won only
# ===========================================================================
def _success_envelope(**over):
    from services.task_execution_service import TerminalEnvelope, TaskExecutionStatus

    base = dict(
        execution_id="exec-1578",
        status=TaskExecutionStatus.SUCCESS,
        response="all done",
        metadata={"cost_usd": 0.05, "input_tokens": 100, "context_window": 200000,
                  "session_id": "meta-sess"},
        execution_log=[{"type": "tool_use", "name": "Bash"}],
        session_id="resp-sess",
        execution_time_ms=1234,
    )
    base.update(over)
    return TerminalEnvelope(**base)


def _failed_envelope(**over):
    from services.task_execution_service import TerminalEnvelope, TaskExecutionStatus

    base = dict(
        execution_id="exec-1578",
        status=TaskExecutionStatus.FAILED,
        error="agent said no",
        error_code=None,
        metadata={"cost_usd": 0.02},
    )
    base.update(over)
    return TerminalEnvelope(**base)


def _run_apply(envelope, *, cas_won=True, reconciled_status="cancelled"):
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.update_execution_status.return_value = cas_won
    mock_db.get_execution.return_value = SimpleNamespace(id="exec-1578", status=reconciled_status)

    mock_eds = MagicMock()

    with (
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager",
              return_value=MagicMock(release=AsyncMock())),
        patch("services.task_execution_service.activity_service",
              MagicMock(complete_activity=AsyncMock())),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        patch("services.task_execution_service._spawn_bg", MagicMock(side_effect=_close_coro)),
        patch("services.task_execution_service.event_dispatch_service", mock_eds),
    ):
        svc = TaskExecutionService()
        _await(svc.apply_result("worker-a", envelope, activity_id="act-1"))
    return mock_eds


def _close_coro(coro):
    try:
        coro.close()
    except Exception:
        pass


class TestApplyResultInlineEmit:
    def test_success_won_emits_completed(self):
        from models import TaskExecutionStatus

        eds = _run_apply(_success_envelope(), cas_won=True)
        eds.spawn_task_terminal_event.assert_called_once()
        _, kwargs = eds.spawn_task_terminal_event.call_args
        assert kwargs["terminal_status"] == TaskExecutionStatus.SUCCESS
        assert kwargs["summary_or_error"] == "all done"

    def test_success_lost_cas_emits_nothing(self):
        """A replayed/late callback loses the CAS → reconcile, no double-wake."""
        eds = _run_apply(_success_envelope(), cas_won=False)
        eds.spawn_task_terminal_event.assert_not_called()

    def test_failure_won_emits_failed(self):
        from models import TaskExecutionStatus

        eds = _run_apply(_failed_envelope(), cas_won=True)
        eds.spawn_task_terminal_event.assert_called_once()
        _, kwargs = eds.spawn_task_terminal_event.call_args
        assert kwargs["terminal_status"] == TaskExecutionStatus.FAILED
        assert kwargs["summary_or_error"] == "agent said no"

    def test_failure_lost_cas_emits_nothing(self):
        eds = _run_apply(_failed_envelope(), cas_won=False)
        eds.spawn_task_terminal_event.assert_not_called()


# ===========================================================================
# Layer A2 — _write_terminal_and_gate (timeout / budget / crash class)
# ===========================================================================
class TestWriteTerminalAndGateEmit:
    """The failure terminals the feature exists for — timeout/budget/crash — go
    through _write_terminal_and_gate, NOT apply_result. This is the critical
    regression pin: without threading agent_name + emitting here,
    agent.task.failed would be dead for the exact 'long task wedged' case."""

    def _run(self, *, won, agent_name="worker-a"):
        from services import task_execution_service as tes
        from models import TaskExecutionStatus, ActivityState

        mock_db = MagicMock()
        mock_db.update_execution_status.return_value = won
        mock_eds = MagicMock()
        with (
            patch.object(tes, "db", mock_db),
            patch.object(tes, "activity_service", MagicMock(complete_activity=AsyncMock())),
            patch.object(tes, "event_dispatch_service", mock_eds),
        ):
            _await(
                tes._write_terminal_and_gate(
                    "exec-1578",
                    "act-1",
                    status=TaskExecutionStatus.FAILED,
                    activity_status=ActivityState.FAILED,
                    error="Task execution timed out after 600 seconds",
                    agent_name=agent_name,
                )
            )
        return mock_eds

    def test_timeout_terminal_won_emits_failed(self):
        from models import TaskExecutionStatus

        eds = self._run(won=True)
        eds.spawn_task_terminal_event.assert_called_once()
        _, kwargs = eds.spawn_task_terminal_event.call_args
        assert kwargs["terminal_status"] == TaskExecutionStatus.FAILED
        assert "timed out" in kwargs["summary_or_error"]

    def test_lost_cas_emits_nothing(self):
        eds = self._run(won=False)
        eds.spawn_task_terminal_event.assert_not_called()

    def test_no_agent_name_emits_nothing(self):
        """Defensive: an un-threaded caller (agent_name=None) never emits."""
        eds = self._run(won=True, agent_name=None)
        eds.spawn_task_terminal_event.assert_not_called()


# ===========================================================================
# Layer A3 — the #1083 ASYNC result-callback path (genuinely distinct entry)
# ===========================================================================
class TestAsyncCallbackPathEmit:
    """Drives the real ``agent_execution_result`` endpoint → the REAL
    ``apply_result`` → the emit seam. This is a DIFFERENT entry point than the
    inline test above (which calls apply_result directly), so AC #4's 'both
    terminal paths covered' is genuine, not two tests hitting the same call."""

    def test_async_callback_success_emits_completed(self):
        from models import ExecutionResultEnvelope, TaskExecutionStatus
        from routers.agents import agent_execution_result

        class _Req:
            headers = {"Authorization": "Bearer k"}

        # Endpoint-side db (auth + ownership + activity lookup).
        endpoint_db = MagicMock()
        endpoint_db.validate_mcp_api_key.return_value = {"scope": "agent", "agent_name": "worker-a"}
        endpoint_db.get_execution.return_value = _execution(
            agent_name="worker-a", status="running", claude_session_id="dispatched_async"
        )
        endpoint_db.get_open_activity_id_for_execution.return_value = "act-1"

        # apply_result-side db (CAS win).
        tes_db = MagicMock()
        tes_db.update_execution_status.return_value = True

        mock_eds = MagicMock()
        payload = ExecutionResultEnvelope(status="success", response="ok", metadata={"cost_usd": 0.01})

        with (
            patch("routers.agents.db", endpoint_db),
            patch("services.heartbeat_service.authorize_heartbeat", return_value=True),
            patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
            patch("services.task_execution_service.db", tes_db),
            patch("services.task_execution_service.activity_service",
                  MagicMock(complete_activity=AsyncMock())),
            patch("services.task_execution_service.get_capacity_manager",
                  return_value=MagicMock(release=AsyncMock())),
            patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
            patch("services.task_execution_service._spawn_bg", MagicMock(side_effect=_close_coro)),
            patch("services.task_execution_service.event_dispatch_service", mock_eds),
        ):
            resp = _await(agent_execution_result("worker-a", "exec-1578", payload, _Req()))

        assert resp["ok"] is True and resp["replayed"] is False
        mock_eds.spawn_task_terminal_event.assert_called_once()
        _, kwargs = mock_eds.spawn_task_terminal_event.call_args
        assert kwargs["terminal_status"] == TaskExecutionStatus.SUCCESS


# ===========================================================================
# Layer A4 — the #1083 lease-reaper (async died) emits failed
# ===========================================================================
class TestLeaseReaperEmit:
    def _run(self, *, won):
        from services import cleanup_service as cs

        report = cs.CleanupReport()
        mock_db = MagicMock()
        mock_db.fail_stale_slot_execution.return_value = won
        mock_eds = MagicMock()

        svc = cs.CleanupService.__new__(cs.CleanupService)  # no __init__ deps
        with (
            patch.object(cs, "db", mock_db),
            patch.object(cs, "event_dispatch_service", mock_eds),
            patch.object(cs.CleanupService, "_get_agent_running_ids",
                         AsyncMock(return_value=set())),
            patch.object(cs.CleanupService, "_terminate_on_agent", AsyncMock()),
            patch.object(cs.CleanupService, "_close_stale_slot_activity", AsyncMock()),
        ):
            _await(
                svc._process_stale_slot_reclaims(
                    {"worker-a": ["exec-1578"]}, set(), report
                )
            )
        return mock_eds

    def test_reaper_won_emits_failed(self):
        from models import TaskExecutionStatus

        eds = self._run(won=True)
        eds.spawn_task_terminal_event.assert_called_once()
        _, kwargs = eds.spawn_task_terminal_event.call_args
        assert kwargs["terminal_status"] == TaskExecutionStatus.FAILED

    def test_reaper_lost_cas_emits_nothing(self):
        eds = self._run(won=False)
        eds.spawn_task_terminal_event.assert_not_called()


# ===========================================================================
# Layer A5 — the pull sink emits on won (dark today, wired for the pilot)
# ===========================================================================
class TestPullSinkEmit:
    def _run(self, *, status="success", cas_won=True, row_status="running"):
        from services import pull_coordination_service as pcs

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _execution(
            agent_name="worker-a", status=row_status
        )
        mock_db.update_execution_status.return_value = cas_won
        mock_eds = MagicMock()
        with (
            patch.object(pcs, "db", mock_db),
            patch.object(pcs, "event_dispatch_service", mock_eds),
        ):
            outcome = pcs.apply_task_result(
                "exec-1578", "tok", status=status, content="done", cost=0.1
            )
        return outcome, mock_eds

    def test_pull_success_won_emits_completed(self):
        from models import TaskExecutionStatus

        outcome, eds = self._run(status="success", cas_won=True)
        assert outcome.kind == "applied"
        eds.spawn_task_terminal_event.assert_called_once()
        _, kwargs = eds.spawn_task_terminal_event.call_args
        assert kwargs["terminal_status"] == TaskExecutionStatus.SUCCESS

    def test_pull_replayed_terminal_emits_nothing(self):
        """An already-authoritative-terminal row short-circuits — no emit."""
        outcome, eds = self._run(status="success", row_status="success")
        assert outcome.kind == "replayed"
        eds.spawn_task_terminal_event.assert_not_called()


# ===========================================================================
# Layer C — reserved-namespace loop guards (router coroutines)
# ===========================================================================
def _status(exc):
    return getattr(exc, "status_code", None)


class TestReservedNamespaceGuards:
    def test_emit_reserved_namespace_rejected(self):
        from routers.event_subscriptions import emit_event
        from models import EmitEventRequest

        user = SimpleNamespace(agent_name="worker-a", username="worker-a")
        with pytest.raises(Exception) as ei:
            _await(emit_event(EmitEventRequest(event_type="agent.task.completed"), user))
        assert _status(ei.value) == 400

    def test_emit_for_agent_reserved_namespace_rejected(self):
        from routers.event_subscriptions import emit_event_for_agent
        from models import EmitEventRequest

        user = SimpleNamespace(agent_name="worker-a", username="worker-a")
        with pytest.raises(Exception) as ei:
            _await(emit_event_for_agent("worker-a", EmitEventRequest(event_type="agent.task.failed"), user))
        assert _status(ei.value) == 400

    def test_create_self_subscription_to_reserved_rejected(self):
        from routers.event_subscriptions import create_event_subscription
        from db_models import EventSubscriptionCreate

        user = SimpleNamespace(username="owner")
        data = EventSubscriptionCreate(
            source_agent="worker-a",
            event_type="agent.task.completed",
            target_message="report {{payload.status}}",
        )
        with patch("routers.event_subscriptions.db", MagicMock()):
            with pytest.raises(Exception) as ei:
                _await(create_event_subscription("worker-a", data, user))  # name == source
        assert _status(ei.value) == 400

    def test_create_cross_agent_subscription_to_reserved_allowed(self):
        from routers.event_subscriptions import create_event_subscription
        from db_models import EventSubscriptionCreate

        user = SimpleNamespace(username="owner")
        data = EventSubscriptionCreate(
            source_agent="worker-a",
            event_type="agent.task.completed",
            target_message="worker-a finished: {{payload.status}}",
        )
        mock_db = MagicMock()
        mock_db.get_agent_owner.return_value = "owner"
        mock_db.is_agent_permitted.return_value = True
        mock_db.create_event_subscription.return_value = SimpleNamespace(
            id="sub-1", subscriber_agent="orchestrator", source_agent="worker-a",
            event_type="agent.task.completed", target_message="x", enabled=True,
            created_at="t", updated_at="t", created_by="owner",
        )
        with patch("routers.event_subscriptions.db", mock_db):
            # orchestrator (subscriber) != worker-a (source) → allowed.
            sub = _await(create_event_subscription("orchestrator", data, user))
        assert sub.source_agent == "worker-a"
        mock_db.create_event_subscription.assert_called_once()

    def test_update_benign_self_subscription_into_reserved_rejected(self):
        """The PUT bypass: a benign foo.bar self-sub must not be updated into
        agent.task.completed, dodging the create-time guard."""
        from routers.event_subscriptions import update_event_subscription
        from db_models import EventSubscriptionUpdate

        user = SimpleNamespace(username="owner")
        existing = SimpleNamespace(
            id="sub-1", subscriber_agent="worker-a", source_agent="worker-a",
            event_type="foo.bar", target_message="x", enabled=True,
        )
        mock_db = MagicMock()
        mock_db.get_event_subscription.return_value = existing
        mock_db.can_user_share_agent.return_value = True
        with patch("routers.event_subscriptions.db", mock_db):
            with pytest.raises(Exception) as ei:
                _await(update_event_subscription(
                    "sub-1", EventSubscriptionUpdate(event_type="agent.task.completed"), user
                ))
        assert _status(ei.value) == 400
        mock_db.update_event_subscription.assert_not_called()
