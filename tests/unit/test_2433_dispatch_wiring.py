"""#2433 — ``agent_post_with_retry`` wires the in-flight registry, the
re-anchor-at-grant callback and the cancelled-while-parked exception; the
operator terminate path cancels a PARKED execution without ever asking the
agent.

Modules under test:
    src/backend/services/task_execution_service.py
    src/backend/services/chat_execution_service.py
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import agent_call_limiter as acl  # noqa: E402
from services import task_execution_service as tes  # noqa: E402
from services import chat_execution_service as ces  # noqa: E402


@pytest.fixture
def limiter(monkeypatch):
    acl._reset_for_testing(global_limit=100, queue_timeout_s=10.0, client_factory=lambda: None,
                           slot_renewer=lambda a, e: True)
    monkeypatch.setattr(acl, "INFLIGHT_TICK_SECONDS", 0.01)
    yield acl
    acl._reset_for_testing()


def _fake_httpx_client(seen: dict):
    @asynccontextmanager
    async def _client(agent_name, timeout=None):
        client = MagicMock()

        async def _post(url, json=None):
            entry = acl.inflight_entry(json.get("execution_id")) if json else None
            seen["entry_during_post"] = entry
            seen["phase_during_post"] = entry.phase if entry else None
            resp = MagicMock(status_code=200)
            return resp

        client.post = _post
        yield client

    return _client


@pytest.mark.asyncio
async def test_agent_post_registers_inflight_for_the_whole_call(limiter):
    seen: dict = {}
    with patch.object(tes, "agent_httpx_client", _fake_httpx_client(seen)):
        resp = await tes.agent_post_with_retry(
            "agent-a", "/api/task", {"execution_id": "exec-1"}, timeout=60, execution_id="exec-1"
        )
    assert resp.status_code == 200
    assert seen["entry_during_post"] is not None
    assert seen["phase_during_post"] == "calling"
    assert acl.inflight_entry("exec-1") is None, "unregistered when the call ends"


@pytest.mark.asyncio
async def test_agent_post_without_execution_id_registers_nothing(limiter):
    seen: dict = {}
    with patch.object(tes, "agent_httpx_client", _fake_httpx_client(seen)):
        await tes.agent_post_with_retry("agent-a", "/api/task", {"execution_id": None}, timeout=60)
    assert not acl._INFLIGHT


@pytest.mark.asyncio
async def test_park_at_grant_restamps_row_and_renews_slot(limiter, monkeypatch):
    monkeypatch.setattr(acl, "DISPATCH_RESTAMP_THRESHOLD_SECONDS", 0.05)
    fake_db = MagicMock()
    fake_db.restamp_execution_dispatch.return_value = True
    slot = MagicMock()
    slot.renew_slot.return_value = True
    slot_mod = MagicMock()
    slot_mod.get_slot_service.return_value = slot
    monkeypatch.setitem(sys.modules, "services.slot_service", slot_mod)

    release = asyncio.Event()

    async def hold():
        async with acl.acquire_agent_call_slot("agent-a"):
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(3)]  # per-agent cap 3
    await asyncio.sleep(0.02)
    seen: dict = {}
    with patch.object(tes, "db", fake_db), patch.object(tes, "agent_httpx_client", _fake_httpx_client(seen)):
        call = asyncio.create_task(tes.agent_post_with_retry(
            "agent-a", "/api/task", {"execution_id": "exec-p"}, timeout=60, execution_id="exec-p"
        ))
        await asyncio.sleep(0.1)
        assert acl.inflight_entry("exec-p").phase == "parked"
        release.set()
        await call
    await asyncio.gather(*holders)
    fake_db.restamp_execution_dispatch.assert_called_once_with("exec-p")
    slot.renew_slot.assert_called_once_with("agent-a", "exec-p")


@pytest.mark.asyncio
async def test_cancelled_while_parked_propagates_cancelled_exception(limiter):
    release = asyncio.Event()

    async def hold():
        async with acl.acquire_agent_call_slot("agent-a"):
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(3)]
    await asyncio.sleep(0.02)
    seen: dict = {}
    with patch.object(tes, "agent_httpx_client", _fake_httpx_client(seen)):
        call = asyncio.create_task(tes.agent_post_with_retry(
            "agent-a", "/api/task", {"execution_id": "exec-c"}, timeout=60, execution_id="exec-c"
        ))
        await asyncio.sleep(0.05)
        assert acl.cancel_inflight("exec-c") == "parked"
        release.set()
        with pytest.raises(acl.BackendAgentCallCancelled):
            await call
    await asyncio.gather(*holders)
    assert "entry_during_post" not in seen, "a cancelled park must never POST"


# ---------------------------------------------------------------------------
# terminate_execution — the parked branch
# ---------------------------------------------------------------------------

def _terminate(*, local_phase, remote_phase, container_calls: list):
    fake_db = MagicMock()
    fake_db.update_execution_status.return_value = True
    # The belt reads the row: it must belong to the agent the caller is authorised on.
    fake_db.get_execution.return_value = MagicMock(agent_name="agent-a", status="running")
    capacity = AsyncMock()

    def _container(name):
        container_calls.append(name)
        return None

    with (
        patch.object(ces, "_cancel_queued_if_queued", AsyncMock(return_value=None)),
        patch.object(ces.agent_call_limiter, "cancel_inflight", return_value=local_phase),
        patch.object(ces.agent_call_limiter, "request_cross_worker_cancel", AsyncMock(return_value=remote_phase)),
        patch.object(ces, "get_capacity_manager", return_value=capacity),
        patch.object(ces, "db", fake_db),
        patch.object(ces, "_close_dispatch_activity_cancelled", AsyncMock()),
        patch.object(ces.activity_service, "track_activity", AsyncMock()),
        patch.object(ces, "get_agent_container", _container),
    ):
        try:
            result = asyncio.run(ces.terminate_execution(
                name="agent-a", execution_id="exec-1", task_execution_id="exec-1", current_user=None,
            ))
        except ces.ChatDispatchError as e:
            result = e
    return result, fake_db, capacity


def test_terminate_cancels_a_locally_parked_execution_without_the_agent():
    calls: list = []
    result, fake_db, capacity = _terminate(local_phase="parked", remote_phase=None, container_calls=calls)
    assert result == {"status": "cancelled_while_parked", "execution_id": "exec-1"}
    assert calls == [], "a parked row must be cancelled without asking the agent"
    fake_db.update_execution_status.assert_called_once()
    assert fake_db.update_execution_status.call_args.kwargs["status"] == "cancelled"
    capacity.release_if_matches.assert_awaited_once_with("agent-a", "exec-1")


def test_terminate_cancels_a_park_owned_by_the_other_worker():
    calls: list = []
    result, fake_db, _ = _terminate(local_phase=None, remote_phase="parked", container_calls=calls)
    assert result["status"] == "cancelled_while_parked"
    assert calls == []


def test_terminate_falls_through_when_not_parked():
    calls: list = []
    result, fake_db, _ = _terminate(local_phase=None, remote_phase=None, container_calls=calls)
    assert isinstance(result, ces.ChatDispatchError)
    assert calls == ["agent-a"], "not parked → the normal agent-proxy path"
    fake_db.update_execution_status.assert_not_called()


def test_terminate_calling_phase_goes_to_the_agent():
    calls: list = []
    result, fake_db, _ = _terminate(local_phase="calling", remote_phase=None, container_calls=calls)
    assert isinstance(result, ces.ChatDispatchError)
    assert calls == ["agent-a"]


# ---------------------------------------------------------------------------
# review findings: agent scoping + the dispatcher writes CANCELLED itself
# ---------------------------------------------------------------------------

def test_terminate_refuses_a_park_owned_by_another_agent():
    """The operator route proves `name`, not the execution id; a parked entry
    registered for agent-b must not be cancellable through agent-a. The row is
    unknown here (`get_execution` → None), so the entry gate passes and the
    REGISTRY scoping is what refuses — falling through to the self-scoping
    proxy path."""
    calls: list = []
    fake_db = MagicMock()
    fake_db.get_execution.return_value = None
    capacity = AsyncMock()

    def _container(name):
        calls.append(name)
        return None

    with (
        patch.object(ces, "_cancel_queued_if_queued", AsyncMock(return_value=None)),
        patch.object(ces.agent_call_limiter, "cancel_inflight",
                     lambda eid, agent_name=None: "parked" if agent_name == "agent-b" else None),
        patch.object(ces.agent_call_limiter, "request_cross_worker_cancel", AsyncMock(return_value=None)),
        patch.object(ces, "get_capacity_manager", return_value=capacity),
        patch.object(ces, "db", fake_db),
        patch.object(ces, "get_agent_container", _container),
    ):
        with pytest.raises(ces.ChatDispatchError):
            asyncio.run(ces.terminate_execution(
                name="agent-a", execution_id="exec-b", task_execution_id="exec-b", current_user=None,
            ))
    assert calls == ["agent-a"], "must fall through to the (self-scoping) proxy path"
    fake_db.update_execution_status.assert_not_called()


def test_terminate_parked_belt_refuses_when_row_belongs_elsewhere():
    """A row owned by another agent is refused at the ENTRY gate — before any
    arm runs, before the container is even looked up — with the proxy's own
    uniform 404 (a foreign id must read exactly like an unknown one)."""
    calls: list = []
    fake_db = MagicMock()
    fake_db.get_execution.return_value = MagicMock(agent_name="agent-z", status="running")

    def _container(name):
        calls.append(name)
        return None

    with (
        patch.object(ces, "_cancel_queued_if_queued", AsyncMock(return_value=None)),
        patch.object(ces.agent_call_limiter, "cancel_inflight", lambda eid, agent_name=None: "parked"),
        patch.object(ces.agent_call_limiter, "request_cross_worker_cancel", AsyncMock(return_value=None)),
        patch.object(ces, "get_capacity_manager", return_value=AsyncMock()),
        patch.object(ces, "db", fake_db),
        patch.object(ces, "get_agent_container", _container),
    ):
        with pytest.raises(ces.ChatDispatchError) as exc:
            asyncio.run(ces.terminate_execution(
                name="agent-a", execution_id="exec-1", task_execution_id="exec-1", current_user=None,
            ))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Execution not found in agent"
    fake_db.update_execution_status.assert_not_called()
    assert calls == [], "refused before any arm — the container is never consulted"


def test_proxy_arm_cannot_flip_a_foreign_row_via_task_execution_id():
    """The exploit the independent verifier traced: a caller authorised on
    agent-a passes agent-b's row id as `task_execution_id` (a query param on
    the operator route). The proxy's 404 scopes only `execution_id` — the id
    the AGENT is asked about — while the CANCELLED CAS and the activity close
    are keyed on `task_execution_id`, so before the entry gate agent-a's
    container answering `terminated` for ITS id flipped agent-b's row and
    discarded agent-b's later SUCCESS. Now: uniform 404, no write, no proxy."""
    calls: list = []
    fake_db = MagicMock()
    fake_db.get_execution.return_value = MagicMock(agent_name="agent-b", status="running")
    close_mock = AsyncMock()

    def _container(name):
        calls.append(name)
        return MagicMock(status="running")

    with (
        patch.object(ces, "_cancel_queued_if_queued", AsyncMock(return_value=None)),
        patch.object(ces.agent_call_limiter, "cancel_inflight", lambda eid, agent_name=None: None),
        patch.object(ces.agent_call_limiter, "request_cross_worker_cancel", AsyncMock(return_value=None)),
        patch.object(ces, "get_capacity_manager", return_value=AsyncMock()),
        patch.object(ces, "db", fake_db),
        patch.object(ces, "get_agent_container", _container),
        patch.object(ces, "_close_dispatch_activity_cancelled", close_mock),
    ):
        with pytest.raises(ces.ChatDispatchError) as exc:
            asyncio.run(ces.terminate_execution(
                name="agent-a", execution_id="exec-a-own", task_execution_id="exec-b-foreign",
                current_user=None,
            ))
    assert exc.value.status_code == 404
    fake_db.update_execution_status.assert_not_called()
    close_mock.assert_not_called()
    assert calls == []


def test_terminate_gate_fails_closed_when_the_row_cannot_be_read():
    """An unreadable row cannot prove ownership of a cross-tenant terminal
    write: the gate refuses with a retryable 503 rather than falling through."""
    fake_db = MagicMock()
    fake_db.get_execution.side_effect = RuntimeError("db down")

    with (
        patch.object(ces, "_cancel_queued_if_queued", AsyncMock(return_value=None)),
        patch.object(ces, "db", fake_db),
        patch.object(ces, "get_agent_container", lambda name: MagicMock(status="running")),
    ):
        with pytest.raises(ces.ChatDispatchError) as exc:
            asyncio.run(ces.terminate_execution(
                name="agent-a", execution_id="exec-1", task_execution_id="exec-1", current_user=None,
            ))
    assert exc.value.status_code == 503
    fake_db.update_execution_status.assert_not_called()


def test_terminate_gate_passes_own_row_and_unknown_row():
    """Positive controls: the agent's own row, and an id with no row yet, both
    reach the arms (here: the proxy path, so the container is consulted)."""
    for row in (MagicMock(agent_name="agent-a", status="running"), None):
        calls: list = []
        fake_db = MagicMock()
        fake_db.get_execution.return_value = row
        with (
            patch.object(ces, "_cancel_queued_if_queued", AsyncMock(return_value=None)),
            patch.object(ces.agent_call_limiter, "cancel_inflight", lambda eid, agent_name=None: None),
            patch.object(ces.agent_call_limiter, "request_cross_worker_cancel", AsyncMock(return_value=None)),
            patch.object(ces, "db", fake_db),
            patch.object(ces, "get_agent_container", lambda name: (calls.append(name), None)[1]),
        ):
            with pytest.raises(ces.ChatDispatchError) as exc:
                asyncio.run(ces.terminate_execution(
                    name="agent-a", execution_id="exec-1", task_execution_id="exec-1", current_user=None,
                ))
        assert exc.value.status_code == 404 and exc.value.detail == "Agent not found"
        assert calls == ["agent-a"], row


def test_queued_cancel_is_agent_scoped():
    """Adjacent pre-existing gap: the BACKLOG cancel-if-queued branch never
    asked the agent either, so it must scope itself."""
    fake_db = MagicMock()
    fake_db.get_execution.return_value = MagicMock(agent_name="agent-z", status="queued")
    with patch.object(ces, "db", fake_db), patch.object(ces.activity_service, "track_activity", AsyncMock()):
        result = asyncio.run(ces._cancel_queued_if_queued("agent-a", "exec-1", "exec-1", None))
    assert result is None
    fake_db.cancel_queued_execution.assert_not_called()


def test_chat_budget_finalizer_writes_cancelled_and_raises_409_for_a_cancelled_park():
    fake_db = MagicMock()
    fake_db.get_execution.return_value = MagicMock(status="running")
    exc = acl.BackendAgentCallCancelled("agent-a", 3, 8, 1234)
    with patch.object(ces, "db", fake_db), patch.object(ces.activity_service, "complete_activity", AsyncMock()) as ca:
        with pytest.raises(ces.ChatDispatchError) as ei:
            asyncio.run(ces._finalize_budget_exhausted(
                budget_exc=exc, task_execution_id="exec-1", chat_activity_id="act-1", collaboration_activity_id=None,
            ))
    assert ei.value.status_code == 409
    assert fake_db.update_execution_status.call_args.kwargs["status"] == "cancelled"
    assert ca.call_args.kwargs["status"] == "cancelled"


def test_chat_budget_finalizer_keeps_failed_503_for_a_real_exhaustion():
    fake_db = MagicMock()
    fake_db.get_execution.return_value = MagicMock(status="running")
    exc = acl.BackendAgentCallBudgetExhausted("agent-a", 3, 8, 3600000)
    with patch.object(ces, "db", fake_db), patch.object(ces.activity_service, "complete_activity", AsyncMock()):
        with pytest.raises(ces.ChatDispatchError) as ei:
            asyncio.run(ces._finalize_budget_exhausted(
                budget_exc=exc, task_execution_id="exec-1", chat_activity_id="act-1", collaboration_activity_id=None,
            ))
    assert ei.value.status_code == 503
    assert fake_db.update_execution_status.call_args.kwargs["status"] == "failed"


# ---------------------------------------------------------------------------
# execute_task: a cancelled park is recorded CANCELLED by the dispatcher itself
# ---------------------------------------------------------------------------

def _drive_execute_task(post_side_effect):
    """The tests/unit/test_1083_dispatch_return.py harness shape."""
    import config
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution_timeout.return_value = 300
    mock_db.get_execution.return_value = MagicMock(status="cancelled")
    mock_db.update_execution_status.return_value = True
    mock_capacity = MagicMock()
    mock_capacity.acquire = AsyncMock(return_value=MagicMock(state="admitted"))
    mock_capacity.release = AsyncMock()
    mock_circuit = MagicMock()
    mock_circuit.allow_request.return_value = True
    mock_activity = MagicMock(
        track_activity=AsyncMock(return_value="act-1"),
        complete_activity=AsyncMock(),
        close_execution_activity=AsyncMock(return_value=True),
    )
    with (
        patch.object(config, "DISPATCH_ASYNC", False),
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
        patch("services.task_execution_service.agent_post_with_retry", AsyncMock(side_effect=post_side_effect)),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        patch("services.task_execution_service.event_dispatch_service", MagicMock()),
        patch("services.task_execution_service.channel_completion_report", MagicMock()),
    ):
        svc = TaskExecutionService()
        result = asyncio.run(svc.execute_task(
            agent_name="agent-a", message="hi", triggered_by="schedule",
            execution_id="exec-c", timeout_seconds=300, model="sonnet",
        ))
    return result, mock_db


def test_execute_task_records_a_cancelled_park_as_cancelled():
    result, mock_db = _drive_execute_task(acl.BackendAgentCallCancelled("agent-a", 3, 8, 12000))
    assert result.status == "cancelled"
    assert mock_db.update_execution_status.call_args.kwargs["status"] == "cancelled"


def test_execute_task_still_records_a_real_exhaustion_as_failed():
    result, mock_db = _drive_execute_task(acl.BackendAgentCallBudgetExhausted("agent-a", 3, 8, 3600000))
    assert result.status == "failed"
    assert mock_db.update_execution_status.call_args.kwargs["status"] == "failed"
