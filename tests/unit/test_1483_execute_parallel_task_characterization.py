"""
#1483 characterization — the ``/task`` dispatch monster (``execute_parallel_task``,
CC 82). Drives the **endpoint** end-to-end (the endpoint stays in
``routers.chat`` through the whole split), so the HTTP-contract assertions are
byte-stable; only the ``patch.object(_MOD, ...)`` targets repoint when a
collaborator moves into ``chat_execution_service`` / ``dispatch_admission_service``.

Branches pinned (plan §9):
  * sync immediate (slot admitted → ``execute_task`` → response + ``task_execution_id``)
  * sync backlog long-poll: drain happy-path AND row-reconstruction fallback
  * async queued-202 (payload shape + ``execution_id``, #914) and accepted-202
  * #1672 resume 400 (sentinel) / 404 (IDOR)
  * SELF-EXEC-001 403 spoof guard
  * idempotency replay: 409 in-flight, snapshot replay
  * #1444 ``chat_persist_failed`` marker
  * **502-on-upload-failure LEAVES the idempotency claim in place** (the
    not-byte-true quirk — preserved deliberately, RD11; no ``idempotency.fail``)
  * #1578 reserved-event ``triggered_by="event"`` at every sink (derivation,
    ``create_task_execution``, backlog payload, async override, sync ``execute_task``)
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers.chat import execute_parallel_task as ENDPOINT
from services import chat_persistence_service
import services.dispatch_admission_service as _DISPATCH
import services.chat_execution_service as _CE
from models import ParallelTaskRequest, TaskExecutionStatus

_MOD = sys.modules[ENDPOINT.__module__]  # routers.chat (endpoint stays here)


def _user(role="user", agent_name=None):
    u = MagicMock()
    u.id = 1
    u.email = "u@e.com"
    u.username = "u"
    u.role = role
    u.agent_name = agent_name
    return u


def _idem(replay=False, in_flight=False, execution_id="e0", snapshot=None):
    m = MagicMock()
    m.replay = replay
    m.in_flight = in_flight
    m.execution_id = execution_id
    m.snapshot = snapshot
    return m


def _result(status=TaskExecutionStatus.SUCCESS, response="done", error=None):
    r = MagicMock()
    r.status = status
    r.response = response
    r.error = error
    r.cost = 0.01
    r.context_used = 10
    r.context_max = 200000
    r.raw_response = {"response": response, "cost": 0.01}
    r.execution_id = "exec1"
    r.session_id = None
    return r


@contextmanager
def _env(
    *,
    idem=None,
    cap_state="admitted",
    cap_exc=None,
    exec_result=None,
    wait_payload=None,
    wait_exc=None,
    container_status="running",
):
    idem = idem or _idem()
    container = MagicMock(status=container_status)

    db = MagicMock()
    db.get_execution_timeout.return_value = 3600
    db.get_max_parallel_tasks.return_value = 3
    db.get_agent_subscription_id.return_value = None
    db.create_task_execution.return_value = MagicMock(id="exec1")
    db.resume_session_belongs_to_user.return_value = True
    db.update_execution_status.return_value = True

    isvc = MagicMock()
    isvc.begin.return_value = idem

    cap = MagicMock()
    if cap_exc is not None:
        cap.acquire = AsyncMock(side_effect=cap_exc)
    else:
        cap.acquire = AsyncMock(return_value=MagicMock(state=cap_state, queue_position=0))
    cap.release = AsyncMock()
    cap.force_release = AsyncMock()

    task_service = MagicMock()
    task_service.execute_task = AsyncMock(return_value=exec_result or _result())

    activity = MagicMock(track_activity=AsyncMock(return_value="act1"), complete_activity=AsyncMock())

    persist = AsyncMock(return_value="chatsess1")
    async_bg = AsyncMock()

    if wait_exc is not None:
        waiter = AsyncMock(side_effect=wait_exc)
    else:
        waiter = AsyncMock(return_value=wait_payload)

    with patch.object(_MOD, "get_agent_container", return_value=container), \
         patch.object(_MOD, "db", db), \
         patch.object(_CE, "db", db), \
         patch.object(_CE, "idempotency_service", isvc), \
         patch.object(_DISPATCH, "idempotency_service", isvc), \
         patch.object(_DISPATCH, "platform_audit_service", MagicMock(log=AsyncMock())), \
         patch.object(_CE, "get_capacity_manager", return_value=cap), \
         patch.object(_CE, "dispatch_breaker_active", return_value=False), \
         patch.object(_CE, "get_task_execution_service", return_value=task_service), \
         patch.object(_CE, "activity_service", activity), \
         patch.object(chat_persistence_service, "persist_chat_session", persist), \
         patch.object(_CE, "run_async_task", async_bg), \
         patch.object(_CE, "wait_for_sync_terminal", waiter):
        yield {
            "db": db, "isvc": isvc, "cap": cap, "task_service": task_service,
            "activity": activity, "persist": persist, "async_bg": async_bg,
            "waiter": waiter, "idem": idem,
        }


def _call(request, current_user=None, x_source_agent=None, x_event_trigger=None,
          x_internal_secret=None, idempotency_key="k1"):
    return asyncio.run(ENDPOINT(
        request=request,
        name="agent1",
        current_user=current_user or _user(),
        x_source_agent=x_source_agent,
        x_via_mcp=None,
        idempotency_key=idempotency_key,
        x_event_trigger=x_event_trigger,
        x_internal_secret=x_internal_secret,
    ))


# --- sync immediate -------------------------------------------------------
def test_sync_immediate_success():
    with _env() as m:
        out = _call(ParallelTaskRequest(message="hi"))
    assert out["task_execution_id"] == "exec1"
    m["task_service"].execute_task.assert_awaited_once()
    m["isvc"].complete.assert_called_once()


def test_sync_immediate_failure_maps_503():
    res = _result(status=TaskExecutionStatus.FAILED, error="agent unavailable")
    with _env(exec_result=res):
        with pytest.raises(Exception) as exc:
            _call(ParallelTaskRequest(message="hi"))
    assert getattr(exc.value, "status_code", None) == 503


def test_sync_persist_failed_marker():
    """#1444: save_to_session + SUCCESS but persistence returns no session id →
    chat_persist_failed marker (never 500 a billed turn)."""
    with _env() as m:
        m["persist"].return_value = None
        out = _call(ParallelTaskRequest(message="hi", save_to_session=True))
    assert out.get("chat_persist_failed") is True


# --- sync backlog long-poll ----------------------------------------------
def test_sync_backlog_drain_happy_path():
    """Spilled to backlog, wait returns a full result → response from the drain."""
    res = _result()
    with _env(cap_state="queued_persistent",
              wait_payload={"result": res, "chat_session_id": "cs1"}) as m:
        out = _call(ParallelTaskRequest(message="hi"))
    assert out["task_execution_id"] == "exec1"
    assert out["chat_session_id"] == "cs1"
    m["task_service"].execute_task.assert_not_awaited()  # drain ran it, not the router


def test_sync_backlog_row_reconstruction_fallback():
    """wait returns no result → reconstruct from the DB row."""
    row = MagicMock(status=TaskExecutionStatus.SUCCESS, response="recon", cost=0.02,
                    context_used=5, context_max=200000, claude_session_id=None, error=None)
    with _env(cap_state="queued_persistent", wait_payload={"result": None}) as m:
        m["db"].get_execution.return_value = row
        out = _call(ParallelTaskRequest(message="hi"))
    assert out["task_execution_id"] == "exec1"


def test_sync_backlog_timeout_maps_504():
    with _env(cap_state="queued_persistent", wait_exc=asyncio.TimeoutError()):
        with pytest.raises(Exception) as exc:
            _call(ParallelTaskRequest(message="hi"))
    assert getattr(exc.value, "status_code", None) == 504


# --- async branch ---------------------------------------------------------
def test_async_queued_202_shape():
    """#914: the queued-202 payload shape + execution_id the MCP client recovery
    depends on."""
    with _env(cap_state="queued_persistent") as m:
        out = _call(ParallelTaskRequest(message="hi", async_mode=True))
    assert out["status"] == "queued"
    assert out["execution_id"] == "exec1"
    assert out["async_mode"] is True
    m["isvc"].complete.assert_called_once()
    assert m["isvc"].complete.call_args.args[2] == out  # snapshot == the queued payload


def test_async_accepted_202():
    with _env(cap_state="admitted") as m:
        out = _call(ParallelTaskRequest(message="hi", async_mode=True))
    assert out["status"] == "accepted"
    assert out["execution_id"] == "exec1"
    m["async_bg"].assert_called_once()  # background task spawned


# --- resume validation (#1672) -------------------------------------------
def test_resume_sentinel_rejected_400():
    with _env():
        with pytest.raises(Exception) as exc:
            _call(ParallelTaskRequest(message="hi", resume_session_id="dispatched_async"))
    assert getattr(exc.value, "status_code", None) == 400


def test_resume_idor_404():
    with _env() as m:
        m["db"].resume_session_belongs_to_user.return_value = False
        with pytest.raises(Exception) as exc:
            _call(ParallelTaskRequest(message="hi", resume_session_id="realid"),
                  current_user=_user(role="user"))
    assert getattr(exc.value, "status_code", None) == 404


def test_resume_admin_bypasses_ownership():
    with _env() as m:
        m["db"].resume_session_belongs_to_user.return_value = False
        out = _call(ParallelTaskRequest(message="hi", resume_session_id="realid"),
                    current_user=_user(role="admin"))
    assert out["task_execution_id"] == "exec1"


# --- SELF-EXEC-001 spoof guard -------------------------------------------
def test_source_agent_spoof_403():
    with _env():
        with pytest.raises(Exception) as exc:
            _call(ParallelTaskRequest(message="hi"),
                  current_user=_user(agent_name="realagent"),
                  x_source_agent="fakeagent")
    assert getattr(exc.value, "status_code", None) == 403


# --- idempotency replay ---------------------------------------------------
def test_idempotency_in_flight_409():
    with _env(idem=_idem(replay=True, in_flight=True)):
        with pytest.raises(Exception) as exc:
            _call(ParallelTaskRequest(message="hi"))
    assert getattr(exc.value, "status_code", None) == 409


def test_idempotency_replay_snapshot():
    from fastapi.responses import JSONResponse
    snap = {"task_execution_id": "e0", "async_mode": False}
    with _env(idem=_idem(replay=True, in_flight=False, snapshot=snap)):
        out = _call(ParallelTaskRequest(message="hi"))
    assert isinstance(out, JSONResponse)
    assert out.headers.get("X-Idempotent-Replay") == "true"


# --- upload-502 quirk (RD11: LEAVES idem claim in place) ------------------
def test_upload_502_does_not_fail_idempotency_claim():
    """When ALL file writes fail the endpoint raises 502 BEFORE attach_execution,
    and — unlike the capacity-deny paths — does NOT call idempotency_service.fail.
    This is a deliberately-preserved quirk (RD11); pin it byte-for-byte."""
    from db_models import WebFileUpload
    file_obj = WebFileUpload(name="f.txt", mimetype="text/plain", size=3, data_base64="YWJj")
    req = ParallelTaskRequest(message="hi", files=[file_obj])
    with _env() as m, \
         patch.object(_CE, "decode_web_file", return_value=b"abc"), \
         patch.object(_CE, "process_file_uploads",
                      AsyncMock(return_value=([], "/tmp/up", True, []))):
        with pytest.raises(Exception) as exc:
            _call(req)
    assert getattr(exc.value, "status_code", None) == 502
    m["isvc"].fail.assert_not_called()   # the quirk — claim survives
    m["isvc"].complete.assert_not_called()


def test_capacity_full_DOES_fail_idempotency_claim():
    """Contrast with the quirk above: an at-capacity deny releases the claim."""
    from services.capacity_manager import CapacityFull
    full = CapacityFull(agent_name="agent1", max_concurrent=3, reason="backlog_full", depth=3)
    with _env(cap_exc=full) as m:
        with pytest.raises(Exception) as exc:
            _call(ParallelTaskRequest(message="hi", async_mode=True))
    assert getattr(exc.value, "status_code", None) == 429
    m["isvc"].fail.assert_called_once()


# --- #1578 reserved-event triggered_by="event" at every sink -------------
def _sink_env(**kw):
    return _env(**kw)


def test_1578_reserved_event_sync_sink():
    """Valid X-Event-Trigger + internal secret → triggered_by='event' at
    create_task_execution AND the sync execute_task sink."""
    with patch.object(_CE, "RESERVED_EVENT_TRIGGER_HEADER_VALUE", "agent.task"), \
         patch.object(_CE, "RESERVED_EVENT_TRIGGER", "event"), \
         patch.object(_CE, "verify_internal_dispatch_secret", return_value=True), \
         _env() as m:
        _call(ParallelTaskRequest(message="hi"),
              x_event_trigger="agent.task", x_internal_secret="secret")
    assert m["db"].create_task_execution.call_args.kwargs["triggered_by"] == "event"
    assert m["task_service"].execute_task.call_args.kwargs["triggered_by"] == "event"


def test_1578_reserved_event_async_override_and_backlog_sink():
    """Async path: the reserved tag flows to create_task_execution, the
    PersistentTaskPayload backlog payload, AND the async triggered_by_override."""
    with patch.object(_CE, "RESERVED_EVENT_TRIGGER_HEADER_VALUE", "agent.task"), \
         patch.object(_CE, "RESERVED_EVENT_TRIGGER", "event"), \
         patch.object(_CE, "verify_internal_dispatch_secret", return_value=True), \
         _env(cap_state="admitted") as m:
        _call(ParallelTaskRequest(message="hi", async_mode=True),
              x_event_trigger="agent.task", x_internal_secret="secret")
    # create row
    assert m["db"].create_task_execution.call_args.kwargs["triggered_by"] == "event"
    # backlog payload (overflow_payload arg to acquire)
    payload = m["cap"].acquire.call_args.kwargs["overflow_payload"]
    assert payload.triggered_by == "event"
    # async override threaded to the background wrapper
    assert m["async_bg"].call_args.kwargs["triggered_by_override"] == "event"


def test_1578_spoofed_event_trigger_without_secret_ignored():
    """X-Event-Trigger without a valid internal secret is NOT honored — the
    task keeps its derived triggered_by (C-003 gate)."""
    with patch.object(_CE, "RESERVED_EVENT_TRIGGER_HEADER_VALUE", "agent.task"), \
         patch.object(_CE, "RESERVED_EVENT_TRIGGER", "event"), \
         patch.object(_CE, "verify_internal_dispatch_secret", return_value=False), \
         _env() as m:
        _call(ParallelTaskRequest(message="hi"),
              x_event_trigger="agent.task", x_internal_secret="wrong")
    assert m["db"].create_task_execution.call_args.kwargs["triggered_by"] == "manual"
