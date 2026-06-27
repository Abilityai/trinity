"""
Characterization tests for the decomposition of routers.chat.chat_with_agent
(#1026, slices 1-3).

- Slice 1 (`_admit_chat_request`): idempotency replay/in-flight (#525),
  dispatch-breaker fast-fail (#526), CapacityManager.acquire (#428) with its
  CapacityFull→429 + idempotency-release. The four deny/replay exits are pinned.
- Slice 2 (`_prepare_chat_execution`): execution record, subscription lookup,
  collaboration broadcast/activity, session, chat-start activity, user-msg log.
- Slice 3 (`_run_chat_and_finalize`): agent dispatch, response persistence,
  activity completion, terminal execution write, idempotency snapshot, the
  httpx/SUB-003 error paths, and the slot + idempotency release in `finally`.

The full admitted path is covered end-to-end via the real `chat_with_agent`
(`test_admitted_full_endpoint_path_succeeds`) so no extracted local can be
stranded without a test failing.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from routers.chat import chat_with_agent
from models import ChatMessageRequest
from services.capacity_manager import CapacityFull

_CHAT = sys.modules[chat_with_agent.__module__]


def _user():
    u = MagicMock()
    u.id = 1
    u.email = "u@e.com"
    u.username = "u"
    return u


def _idem(replay=False, in_flight=False, execution_id="e0"):
    m = MagicMock()
    m.replay = replay
    m.in_flight = in_flight
    m.execution_id = execution_id
    m.snapshot = {"execution": {"task_execution_id": execution_id}}
    return m


@contextmanager
def _env(idem, breaker_state=None, acquire_exc=None):
    container = MagicMock()
    container.status = "running"

    isvc = MagicMock()
    isvc.begin.return_value = idem

    cap = MagicMock()
    cap.release = AsyncMock()
    if acquire_exc is not None:
        cap.acquire = AsyncMock(side_effect=acquire_exc)
    else:
        cap.acquire = AsyncMock(return_value=MagicMock(state="admitted", queue_position=0))

    db = MagicMock()
    db.get_execution_timeout.return_value = 3600
    db.get_max_parallel_tasks.return_value = 3

    breaker = MagicMock()
    breaker.to_dict.return_value = {"state": breaker_state or "closed", "retry_after_seconds": 5}

    with patch.object(_CHAT, "get_agent_container", return_value=container), \
         patch.object(_CHAT, "idempotency_service", isvc), \
         patch.object(_CHAT, "dispatch_breaker_active", return_value=bool(breaker_state)), \
         patch.object(_CHAT, "get_capacity_manager", return_value=cap), \
         patch.object(_CHAT, "platform_audit_service", MagicMock(log=AsyncMock())), \
         patch.object(_CHAT, "db", db), \
         patch("services.dispatch_breaker.DispatchBreaker", return_value=breaker):
        yield {"isvc": isvc, "cap": cap, "db": db}


def _call(idempotency_key="k1"):
    return asyncio.run(chat_with_agent(
        request=ChatMessageRequest(message="hi"),
        name="agent1",
        current_user=_user(),
        x_source_agent=None,
        x_via_mcp=None,
        x_mcp_key_id=None,
        x_mcp_key_name=None,
        idempotency_key=idempotency_key,
    ))


def test_replay_returns_snapshot_response():
    with _env(_idem(replay=True, in_flight=False)) as m:
        resp = _call()
    assert isinstance(resp, JSONResponse)
    assert resp.headers.get("X-Idempotent-Replay") == "true"
    m["cap"].acquire.assert_not_awaited()  # short-circuits before capacity


def test_in_flight_replay_raises_409():
    with _env(_idem(replay=True, in_flight=True)):
        with pytest.raises(HTTPException) as exc:
            _call()
    assert exc.value.status_code == 409


def test_breaker_open_raises_503():
    with _env(_idem(replay=False), breaker_state="open") as m:
        with pytest.raises(HTTPException) as exc:
            _call()
    assert exc.value.status_code == 503
    m["cap"].acquire.assert_not_awaited()  # fast-fail before acquire
    # Nothing dispatched: the idempotency claim is released so the caller can
    # retry with the same key once the breaker recovers (#525), mirroring the
    # CapacityFull branch. Pins the behavior change called out in #1051 review.
    m["isvc"].fail.assert_called_once()


def test_capacity_full_raises_429_and_releases_idem():
    full = CapacityFull(agent_name="agent1", max_concurrent=3, reason="in_memory_full", depth=3)
    with _env(_idem(replay=False), acquire_exc=full) as m:
        with pytest.raises(HTTPException) as exc:
            _call()
    assert exc.value.status_code == 429
    m["isvc"].fail.assert_called_once()  # idempotency claim released for retry


def test_admitted_returns_chat_admission():
    """Happy path: the extracted helper returns a ChatAdmission carrying the
    handoff values the endpoint consumes downstream."""
    from routers.chat import _admit_chat_request, ChatAdmission
    cap_result = MagicMock(state="admitted", queue_position=0)
    with _env(_idem(replay=False)) as m:
        m["cap"].acquire = AsyncMock(return_value=cap_result)
        admission = asyncio.run(_admit_chat_request(
            name="agent1", request=ChatMessageRequest(message="hi"),
            current_user=_user(), x_source_agent=None, x_via_mcp=None,
            x_mcp_key_id=None, x_mcp_key_name=None, idempotency_key="k1",
        ))
    assert isinstance(admission, ChatAdmission)
    assert admission.capacity_result is cap_result
    assert isinstance(admission.execution_id, str) and admission.execution_id
    assert admission.idem is m["isvc"].begin.return_value
    # The handoff must carry queue_result + chat_timeout, otherwise the
    # downstream endpoint body NameErrors on them (regression guard for the
    # #1051 review finding).
    assert admission.queue_result == "running"  # state == "admitted"
    assert admission.chat_timeout == 3600        # db.get_execution_timeout


def test_admitted_full_endpoint_path_succeeds():
    """End-to-end admitted path through the *whole* chat_with_agent body.

    Pins the two values threaded via ChatAdmission that the downstream body
    consumes: `chat_timeout` (agent_post_with_retry timeout) and `queue_result`
    (response `execution.queue_status`). Before the #1051 fix these were stranded
    in the helper's scope and the admitted path raised NameError before the agent
    was ever called — uncaught because no test drove the full endpoint.
    """
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"response": "hi back", "metadata": {}, "session": {}}

    with _env(_idem(replay=False)) as m, \
         patch.object(_CHAT, "activity_service",
                      MagicMock(track_activity=AsyncMock(return_value="act1"),
                                complete_activity=AsyncMock())), \
         patch.object(_CHAT, "agent_post_with_retry", AsyncMock(return_value=resp)) as post, \
         patch.object(_CHAT, "compose_system_prompt", return_value="sys"), \
         patch.object(_CHAT, "is_execution_context_enabled", return_value=False):
        result = _call()

    # No NameError; the endpoint returned the agent response augmented with the
    # execution block built from queue_result + is_queued.
    assert result["execution"]["queue_status"] == "running"
    assert result["execution"]["was_queued"] is False
    # chat_timeout (3600) + 10s HTTP buffer was forwarded to the agent call.
    assert post.await_args.kwargs["timeout"] == 3610


# --- #1026 slice 2: _prepare_chat_execution -------------------------------

def _prep(x_source_agent=None):
    """Run the extracted execution-setup helper under the same patched env."""
    from routers.chat import _prepare_chat_execution
    cap_result = MagicMock(state="admitted", queue_position=0)
    with _env(_idem(replay=False)) as m, \
         patch.object(_CHAT, "activity_service",
                      MagicMock(track_activity=AsyncMock(return_value="act1"),
                                complete_activity=AsyncMock())), \
         patch.object(_CHAT, "broadcast_collaboration_event", AsyncMock()) as bcast:
        ctx = asyncio.run(_prepare_chat_execution(
            name="agent1", request=ChatMessageRequest(message="hi"),
            current_user=_user(), x_source_agent=x_source_agent, x_via_mcp=None,
            x_mcp_key_id=None, x_mcp_key_name=None,
            idem=m["isvc"].begin.return_value,
            chat_execution_id="cex1", capacity_result=cap_result, queue_result="running",
        ))
    return ctx, m, bcast


def test_prepare_returns_full_context():
    """Every field the downstream body consumes is populated; the user message
    is logged and the execution row is linked to the idempotency claim."""
    from routers.chat import ChatExecutionContext
    ctx, m, bcast = _prep(x_source_agent=None)
    assert isinstance(ctx, ChatExecutionContext)
    assert ctx.execution.id == "cex1"
    assert ctx.task_execution_id is not None
    assert ctx.triggered_by == "chat"
    assert ctx.session is not None
    assert ctx.chat_activity_id == "act1"
    assert ctx.collaboration_activity_id is None   # not agent-to-agent
    assert ctx.is_queued is False
    m["isvc"].attach_execution.assert_called_once()  # exec linked to idem claim
    m["db"].add_chat_message.assert_called_once()    # inbound user message logged
    bcast.assert_not_awaited()                       # no collaboration for human caller


def test_prepare_agent_to_agent_broadcasts_collaboration():
    """Agent-to-agent path: triggered_by flips to 'agent', the collaboration
    event is broadcast, and a collaboration activity is tracked."""
    ctx, m, bcast = _prep(x_source_agent="caller-agent")
    assert ctx.triggered_by == "agent"
    assert ctx.collaboration_activity_id == "act1"   # collaboration activity tracked
    bcast.assert_awaited_once()                       # agent-to-agent broadcast fired


# --- #1026 slice 3: _run_chat_and_finalize --------------------------------

def test_run_finalize_http_error_releases_slot_and_idem_then_503():
    """A non-auth agent transport error maps to 503 and the `finally` releases
    BOTH the capacity slot and the in-flight idempotency claim (idem_done stays
    False). SUB-003 is not triggered for a transport error with no status code."""
    import httpx
    from routers.chat import _run_chat_and_finalize
    with _env(_idem(replay=False)) as m, \
         patch.object(_CHAT, "activity_service", MagicMock(complete_activity=AsyncMock())), \
         patch.object(_CHAT, "agent_post_with_retry",
                      AsyncMock(side_effect=httpx.ConnectError("boom"))), \
         patch.object(_CHAT, "compose_system_prompt", return_value="sys"), \
         patch.object(_CHAT, "is_execution_context_enabled", return_value=False), \
         patch("services.subscription_auto_switch.is_auth_failure", return_value=False), \
         patch("services.subscription_auto_switch.handle_subscription_failure",
               AsyncMock(return_value=None)):
        idem = m["isvc"].begin.return_value
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_run_chat_and_finalize(
                name="agent1", request=ChatMessageRequest(message="hi"),
                current_user=_user(), x_source_agent=None, x_mcp_key_name=None,
                triggered_by="chat", task_execution_id="te1", _chat_subscription_id=None,
                chat_activity_id="ca1", collaboration_activity_id=None,
                session=MagicMock(id="s1"), execution=MagicMock(id="cex1"),
                queue_result="running", is_queued=False, chat_timeout=3600,
                idem=idem, capacity=m["cap"],
            ))
    assert exc.value.status_code == 503
    assert "Failed to communicate with agent" in str(exc.value.detail)
    m["cap"].release.assert_awaited_once()   # slot released in finally
    m["isvc"].fail.assert_called_once()      # idem claim released (idem_done False)


def test_run_finalize_http_error_on_cancelled_row_closes_activity_cancelled():
    """#1332: if an operator terminate already set the row CANCELLED and THEN an
    HTTP error surfaces, the chat activity must close CANCELLED (not FAILED) so a
    user-cancel doesn't read as a failure in activity-derived views. The handler's
    error string is not stamped over the cancel, and no FAILED row-write fires."""
    import httpx
    from routers.chat import _run_chat_and_finalize
    from models import ActivityState, TaskExecutionStatus

    act = AsyncMock()
    with _env(_idem(replay=False)) as m, \
         patch.object(_CHAT, "activity_service", MagicMock(complete_activity=act)), \
         patch.object(_CHAT, "agent_post_with_retry",
                      AsyncMock(side_effect=httpx.ConnectError("boom"))), \
         patch.object(_CHAT, "compose_system_prompt", return_value="sys"), \
         patch.object(_CHAT, "is_execution_context_enabled", return_value=False), \
         patch("services.subscription_auto_switch.is_auth_failure", return_value=False), \
         patch("services.subscription_auto_switch.handle_subscription_failure",
               AsyncMock(return_value=None)):
        # Operator-terminate already flipped the row to CANCELLED.
        m["db"].get_execution.return_value = MagicMock(status=TaskExecutionStatus.CANCELLED)
        idem = m["isvc"].begin.return_value
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_run_chat_and_finalize(
                name="agent1", request=ChatMessageRequest(message="hi"),
                current_user=_user(), x_source_agent=None, x_mcp_key_name=None,
                triggered_by="chat", task_execution_id="te1", _chat_subscription_id=None,
                chat_activity_id="ca1", collaboration_activity_id=None,
                session=MagicMock(id="s1"), execution=MagicMock(id="cex1"),
                queue_result="running", is_queued=False, chat_timeout=3600,
                idem=idem, capacity=m["cap"],
            ))
    assert exc.value.status_code == 503
    # Chat activity closed CANCELLED, error dropped (not the HTTP error).
    assert act.await_args_list[0].kwargs["status"] == ActivityState.CANCELLED
    assert act.await_args_list[0].kwargs["error"] is None
    # No FAILED write attempted over the CANCELLED row.
    failed_writes = [
        c for c in m["db"].update_execution_status.call_args_list
        if c.kwargs.get("status") == TaskExecutionStatus.FAILED
    ]
    assert failed_writes == []


def test_run_finalize_budget_exhausted_on_cancelled_row_closes_activity_cancelled():
    """#1332: same invariant on the budget-exhausted (503) handler — a row already
    CANCELLED closes the chat activity CANCELLED, not FAILED."""
    from routers.chat import _run_chat_and_finalize
    from models import ActivityState, TaskExecutionStatus

    # Raise the exact BackendAgentCallBudgetExhausted class object that
    # routers.chat bound at import — NOT a fresh `from services.agent_call_limiter
    # import ...`. test_904_agent_call_limiter.py::test_default_timeout_is_one_hour
    # does `importlib.reload(services.agent_call_limiter)`, which rebinds the
    # module's exception class to a new object. Under pytest-randomly orderings
    # where that test runs first, a fresh import here would yield the post-reload
    # class, which routers.chat's pre-reload `except BackendAgentCallBudgetExhausted`
    # cannot catch — the simulated exception would escape uncaught and this test
    # would fail spuriously. Referencing the handler's own bound class keeps the
    # test order-independent (and is what the production raiser uses anyway).
    budget_exc = _CHAT.BackendAgentCallBudgetExhausted(
        agent_name="agent1", agent_cap=2, global_cap=8, wait_ms=1500,
    )
    act = AsyncMock()
    with _env(_idem(replay=False)) as m, \
         patch.object(_CHAT, "activity_service", MagicMock(complete_activity=act)), \
         patch.object(_CHAT, "agent_post_with_retry", AsyncMock(side_effect=budget_exc)), \
         patch.object(_CHAT, "compose_system_prompt", return_value="sys"), \
         patch.object(_CHAT, "is_execution_context_enabled", return_value=False):
        m["db"].get_execution.return_value = MagicMock(status=TaskExecutionStatus.CANCELLED)
        idem = m["isvc"].begin.return_value
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_run_chat_and_finalize(
                name="agent1", request=ChatMessageRequest(message="hi"),
                current_user=_user(), x_source_agent=None, x_mcp_key_name=None,
                triggered_by="chat", task_execution_id="te1", _chat_subscription_id=None,
                chat_activity_id="ca1", collaboration_activity_id=None,
                session=MagicMock(id="s1"), execution=MagicMock(id="cex1"),
                queue_result="running", is_queued=False, chat_timeout=3600,
                idem=idem, capacity=m["cap"],
            ))
    assert exc.value.status_code == 503
    assert act.await_args_list[0].kwargs["status"] == ActivityState.CANCELLED
    failed_writes = [
        c for c in m["db"].update_execution_status.call_args_list
        if c.kwargs.get("status") == TaskExecutionStatus.FAILED
    ]
    assert failed_writes == []


def test_run_finalize_http_error_on_running_row_still_closes_failed():
    """#1332 regression guard: a genuine HTTP failure on a still-RUNNING row keeps
    closing the chat activity FAILED with the error text — the cancel-aware path
    must not turn real failures into cancels."""
    import httpx
    from routers.chat import _run_chat_and_finalize
    from models import ActivityState, TaskExecutionStatus

    act = AsyncMock()
    with _env(_idem(replay=False)) as m, \
         patch.object(_CHAT, "activity_service", MagicMock(complete_activity=act)), \
         patch.object(_CHAT, "agent_post_with_retry",
                      AsyncMock(side_effect=httpx.ConnectError("boom"))), \
         patch.object(_CHAT, "compose_system_prompt", return_value="sys"), \
         patch.object(_CHAT, "is_execution_context_enabled", return_value=False), \
         patch("services.subscription_auto_switch.is_auth_failure", return_value=False), \
         patch("services.subscription_auto_switch.handle_subscription_failure",
               AsyncMock(return_value=None)):
        m["db"].get_execution.return_value = MagicMock(status=TaskExecutionStatus.RUNNING)
        idem = m["isvc"].begin.return_value
        with pytest.raises(HTTPException):
            asyncio.run(_run_chat_and_finalize(
                name="agent1", request=ChatMessageRequest(message="hi"),
                current_user=_user(), x_source_agent=None, x_mcp_key_name=None,
                triggered_by="chat", task_execution_id="te1", _chat_subscription_id=None,
                chat_activity_id="ca1", collaboration_activity_id=None,
                session=MagicMock(id="s1"), execution=MagicMock(id="cex1"),
                queue_result="running", is_queued=False, chat_timeout=3600,
                idem=idem, capacity=m["cap"],
            ))
    assert act.await_args_list[0].kwargs["status"] == ActivityState.FAILED
    assert act.await_args_list[0].kwargs["error"]  # error text preserved
    failed_writes = [
        c for c in m["db"].update_execution_status.call_args_list
        if c.kwargs.get("status") == TaskExecutionStatus.FAILED
    ]
    assert len(failed_writes) == 1  # FAILED row-write still fires for a real failure
