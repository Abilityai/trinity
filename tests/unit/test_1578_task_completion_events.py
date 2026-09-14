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

    def test_summary_credential_sanitized(self):
        """The emit chokepoint redacts credentials in summary_or_error even when
        the producing terminal writer passed a raw (un-sanitized) error string —
        the failure paths (envelope.error / str(exc)) reach the helper raw."""
        from models import TaskExecutionStatus

        secret = "sk-" + "A" * 48
        mock_db, _ = self._run(
            terminal_status=TaskExecutionStatus.FAILED,
            execution=_execution(status="failed"),
            summary_or_error=f"worker failed leaking {secret} in its error",
        )
        emitted = mock_db.create_agent_event.call_args.kwargs["payload"]["summary_or_error"]
        assert secret not in emitted
        assert "***REDACTED***" in emitted

    def test_summary_credential_sanitized_across_truncation_boundary(self):
        """A secret straddling the TASK_EVENT_SUMMARY_MAX boundary is still fully
        redacted: the chokepoint sanitizes a 2×cap window BEFORE the final
        truncation, so a secret whose tail falls past the cap can't leak its
        head. A naive `[:cap]`-then-sanitize would slice the token and leave an
        unmatchable (un-redactable) head fragment in the delivered summary."""
        from models import TaskExecutionStatus
        from services import event_dispatch_service as eds

        secret = "sk-" + "B" * 48  # same shape the sanitizer redacts
        cap = eds.TASK_EVENT_SUMMARY_MAX
        # Start the secret ~30 chars before the cap so its tail lands past it.
        summary = ("x" * (cap - 30)) + secret + " tail"
        mock_db, _ = self._run(
            terminal_status=TaskExecutionStatus.FAILED,
            execution=_execution(status="failed"),
            summary_or_error=summary,
        )
        emitted = mock_db.create_agent_event.call_args.kwargs["payload"]["summary_or_error"]
        assert secret not in emitted
        assert "sk-BBB" not in emitted  # no head fragment survives the boundary
        assert "***REDACTED***" in emitted
        assert len(emitted) <= cap

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
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.update_execution_status.return_value = won
        mock_eds = MagicMock()
        with (
            patch.object(tes, "db", mock_db),
            patch.object(
                tes,
                "activity_service",
                # #1804: _write_terminal_and_gate closes through the shared owner.
                MagicMock(
                    complete_activity=AsyncMock(),
                    close_execution_activity=AsyncMock(return_value=True),
                ),
            ),
            patch.object(tes, "event_dispatch_service", mock_eds),
        ):
            _await(
                tes._write_terminal_and_gate(
                    "exec-1578",
                    "act-1",
                    status=TaskExecutionStatus.FAILED,
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
# Layer B2 — recursion-break header authentication (spoof guard, finding #2)
# ===========================================================================
class TestInternalDispatchSecret:
    """The recursion-break X-Event-Trigger is honored by the /task router ONLY
    with a valid backend-internal X-Internal-Secret (C-003). Without it an
    external caller can't spoof the tag to suppress a real completion event."""

    def test_valid_secret_accepted(self):
        import os
        from unittest.mock import patch as _patch
        from services import event_dispatch_service as eds

        with _patch.dict(os.environ, {"INTERNAL_API_SECRET": "top-secret-xyz"}):
            assert eds.verify_internal_dispatch_secret("top-secret-xyz") is True

    def test_wrong_secret_rejected(self):
        import os
        from unittest.mock import patch as _patch
        from services import event_dispatch_service as eds

        with _patch.dict(os.environ, {"INTERNAL_API_SECRET": "top-secret-xyz"}):
            assert eds.verify_internal_dispatch_secret("guessed") is False

    def test_missing_secret_rejected(self):
        from services import event_dispatch_service as eds

        # None / empty must never authenticate (constant-time short-circuit).
        assert eds.verify_internal_dispatch_secret(None) is False
        assert eds.verify_internal_dispatch_secret("") is False

    def test_trigger_subscription_stamps_internal_secret_on_reserved(self):
        """trigger_subscription must send BOTH the tag and the authenticating
        secret for a reserved-namespace dispatch, so the router's gate passes."""
        import os
        from unittest.mock import patch as _patch
        from services import event_dispatch_service as eds

        captured = {}

        class _Resp:
            status_code = 200
            text = ""

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured["headers"] = headers
                return _Resp()

        sub = SimpleNamespace(id="s1", subscriber_agent="orch", target_message="done")
        event = SimpleNamespace(
            id="e1", source_agent="worker-a", event_type="agent.task.completed", payload={}
        )
        with (
            _patch.dict(os.environ, {"INTERNAL_API_SECRET": "top-secret-xyz"}),
            _patch("httpx.AsyncClient", _Client),
        ):
            _await(eds.trigger_subscription(sub, event))

        assert captured["headers"]["X-Event-Trigger"] == eds.RESERVED_EVENT_TRIGGER_HEADER_VALUE
        assert captured["headers"]["X-Internal-Secret"] == "top-secret-xyz"

    def test_trigger_subscription_no_secret_leak_on_non_reserved(self):
        """A normal agent-emitted event dispatch must NOT carry the internal
        secret (only reserved-namespace loopbacks are backend-authenticated)."""
        import os
        from unittest.mock import patch as _patch
        from services import event_dispatch_service as eds

        captured = {}

        class _Resp:
            status_code = 200
            text = ""

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured["headers"] = headers
                return _Resp()

        sub = SimpleNamespace(id="s1", subscriber_agent="orch", target_message="done")
        event = SimpleNamespace(
            id="e1", source_agent="worker-a", event_type="prediction.resolved", payload={}
        )
        with (
            _patch.dict(os.environ, {"INTERNAL_API_SECRET": "top-secret-xyz"}),
            _patch("httpx.AsyncClient", _Client),
        ):
            _await(eds.trigger_subscription(sub, event))

        assert "X-Internal-Secret" not in captured["headers"]
        assert "X-Event-Trigger" not in captured["headers"]


# ===========================================================================
# Layer B3 — the /task ROUTER gate: the recursion-break is honored ONLY with a
# valid X-Internal-Secret (the actual spoof-guard call-site, finding #2)
# ===========================================================================
class TestRouterRecursionBreakGate:
    """execute_parallel_task promotes triggered_by → 'event' from
    X-Event-Trigger ONLY when a valid backend-internal X-Internal-Secret
    accompanies it. TestInternalDispatchSecret above pins the helper + the
    header-stamping in isolation; THIS pins the router call-site so a refactor
    can't silently drop the `and verify_internal_dispatch_secret(...)` and let
    an external /task caller spoof the tag to suppress a real agent's completion
    event.

    Modeled on tests/unit/test_946_task_idempotency_on_deny.py: the real
    endpoint with collaborators mocked, capacity.acquire forced to raise
    CapacityFull so it short-circuits just AFTER create_task_execution (where
    triggered_by is persisted, chat.py:1541) — before any real dispatch.
    """

    _SECRET = "backend-internal-xyz"

    def _triggered_by(self, *, x_event_trigger, x_internal_secret):
        import os
        from fastapi import HTTPException
        from routers.chat import execute_parallel_task
        import services.dispatch_admission_service as dispatch
        import services.chat_execution_service as ce
        from models import ParallelTaskRequest
        from services.capacity_manager import CapacityFull

        chat = sys.modules[execute_parallel_task.__module__]

        container = MagicMock()
        container.status = "running"

        isvc = MagicMock()
        isvc.begin.return_value = SimpleNamespace(
            replay=False, in_flight=False, execution_id="e1", snapshot=None
        )
        isvc.make_agent_scope.return_value = "agent:worker-a"

        cap = MagicMock()
        cap.acquire = AsyncMock(
            side_effect=CapacityFull(
                agent_name="worker-a", max_concurrent=3, reason="full", depth=50
            )
        )

        mock_db = MagicMock()
        mock_db.get_execution_timeout.return_value = 3600
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.get_agent_subscription_id.return_value = None
        mock_db.create_task_execution.return_value = SimpleNamespace(id="e1")

        user = SimpleNamespace(id=1, email="u@e.com", username="u", agent_name=None)

        with (
            patch.dict(os.environ, {"INTERNAL_API_SECRET": self._SECRET}),
            patch.object(chat, "get_agent_container", return_value=container),
            patch.object(ce, "idempotency_service", isvc),
            patch.object(dispatch, "idempotency_service", isvc),
            patch.object(ce, "dispatch_breaker_active", return_value=False),
            patch.object(ce, "get_capacity_manager", return_value=cap),
            patch.object(
                ce, "activity_service",
                MagicMock(track_activity=AsyncMock(return_value="act1")),
            ),
            patch.object(ce, "db", mock_db),
            patch.object(chat, "db", mock_db),
        ):
            with pytest.raises(HTTPException):  # CapacityFull → 429 after create
                _await(
                    execute_parallel_task(
                        request=ParallelTaskRequest(message="hi", async_mode=True),
                        name="worker-a",
                        current_user=user,
                        x_source_agent=None,
                        x_via_mcp=None,
                        idempotency_key=None,
                        x_event_trigger=x_event_trigger,
                        x_internal_secret=x_internal_secret,
                    )
                )
        return mock_db.create_task_execution.call_args.kwargs["triggered_by"]

    def test_valid_secret_promotes_to_event(self):
        from services.event_dispatch_service import RESERVED_EVENT_TRIGGER

        assert (
            self._triggered_by(x_event_trigger="agent_task", x_internal_secret=self._SECRET)
            == RESERVED_EVENT_TRIGGER
        )

    def test_spoofed_tag_without_secret_stays_manual(self):
        """The security case: an external /task caller sends X-Event-Trigger
        alone. The tag must NOT flip triggered_by, so the caller's OWN terminal
        still emits a completion event (no suppression-by-spoof)."""
        assert (
            self._triggered_by(x_event_trigger="agent_task", x_internal_secret=None)
            == "manual"
        )

    def test_tag_with_wrong_secret_stays_manual(self):
        assert (
            self._triggered_by(x_event_trigger="agent_task", x_internal_secret="wrong")
            == "manual"
        )


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
        # The owner check now lives in dependencies.assert_agent_owner (#1310),
        # not the router's own db call. This test targets the #1578 reserved-
        # namespace guard (which runs AFTER the owner check), so no-op the owner
        # gate via the router's imported binding — robust against the sibling
        # test that re-imports the dependencies module (module-identity gotcha).
        with patch("routers.event_subscriptions.db", mock_db), patch(
            "routers.event_subscriptions.assert_agent_owner", lambda *a, **k: None
        ):
            with pytest.raises(Exception) as ei:
                _await(update_event_subscription(
                    "sub-1", EventSubscriptionUpdate(event_type="agent.task.completed"), user
                ))
        assert _status(ei.value) == 400
        mock_db.update_event_subscription.assert_not_called()
