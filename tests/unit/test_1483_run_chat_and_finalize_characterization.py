"""
#1483 characterization — sync-chat execute+finalize (``_run_chat_and_finalize``,
which the split moves to ``chat_execution_service.run_chat_turn``).

Pins the observable behavior of the CC-57 monster BEFORE any move, and stays
green after it — the only per-move edits are the two ``FN`` / ``_MOD`` anchor
lines (module-identity gotcha: the collaborators the function reads are globals
of *its own* module, so ``_MOD = sys.modules[FN.__module__]`` retargets every
``patch.object(_MOD, ...)`` automatically when the import changes).

Coverage (plan §9 — the SUB-003 429/auth switch matrix is the top-risk branch):
  * SUCCESS: assistant ``add_chat_message``, chat + collaboration activities
    COMPLETED, ``update_execution_status(SUCCESS)`` with a UUID-validated
    ``claude_session_id``, ``idempotency_service.complete``, slot released.
  * SUCCESS with a malformed agent session id → discarded (sentinel kept).
  * ``BackendAgentCallBudgetExhausted`` → 503, FAILED row on a RUNNING row.
  * SUB-003 **429**: switch-authoritative (429 + ``auto_switch``); switch-raised
    (inner ``except HTTPException: raise`` propagates); switch real-errored
    (outer ``except Exception: log`` → plain 429); no-switch → plain 429.
  * SUB-003 **503/auth**: the same four sub-cases (503 + ``auto_switch`` / raised
    / real-errored → plain 503 / no-switch → plain 503).
  * #678 partial-metadata salvage → cost/context on the FAILED row.
  * ``finally`` always releases the slot and ``fail``s the idem claim on a
    non-success exit.

The failure exits are asserted on ``.status_code`` / ``.detail`` (not the
exception *type*) so the same assertions hold whether the function raises a
FastAPI ``HTTPException`` (pre-move) or the HTTP-free ``ChatDispatchError``
domain signal the router maps (post-move).
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.chat_execution_service import run_chat_turn as FN  # moved from routers.chat (#1483)
from models import ChatMessageRequest, ActivityState, TaskExecutionStatus

_MOD = sys.modules[FN.__module__]


# --------------------------------------------------------------------------
def _user():
    u = MagicMock()
    u.id = 1
    u.email = "u@e.com"
    u.username = "u"
    return u


def _http_status_error(status_code: int, body: dict | None = None, text: str = ""):
    """Build an ``httpx.HTTPStatusError`` carrying a response the handler reads
    (``e.response.status_code`` + ``e.response.json()`` / ``.text``)."""
    resp = MagicMock()
    resp.status_code = status_code
    if body is not None:
        resp.json.return_value = body
    else:
        resp.json.side_effect = ValueError("no json")
    resp.text = text
    return httpx.HTTPStatusError("boom", request=MagicMock(), response=resp)


@contextmanager
def _env(*, post_return=None, post_exc=None, get_execution_status=TaskExecutionStatus.RUNNING):
    """Patch every collaborator ``_run_chat_and_finalize`` reads from its own
    module. ``agent_post_with_retry`` either returns ``post_return`` or raises
    ``post_exc``."""
    db = MagicMock()
    db.get_execution.return_value = MagicMock(status=get_execution_status)
    assistant_msg = MagicMock(id="am1")
    db.add_chat_message.return_value = assistant_msg
    db.update_execution_status.return_value = True

    isvc = MagicMock()
    activity = MagicMock(complete_activity=AsyncMock(), track_activity=AsyncMock())

    if post_exc is not None:
        post = AsyncMock(side_effect=post_exc)
    else:
        post = AsyncMock(return_value=post_return)

    with patch.object(_MOD, "db", db), \
         patch.object(_MOD, "idempotency_service", isvc), \
         patch.object(_MOD, "activity_service", activity), \
         patch.object(_MOD, "agent_post_with_retry", post), \
         patch.object(_MOD, "compose_system_prompt", return_value="sys"), \
         patch.object(_MOD, "get_platform_system_prompt", return_value="sys"), \
         patch.object(_MOD, "is_execution_context_enabled", return_value=False):
        yield {"db": db, "isvc": isvc, "activity": activity, "post": post}


def _capacity():
    cap = MagicMock()
    cap.release = AsyncMock()
    return cap


def _run(env_capacity, *, collaboration_activity_id=None, session=None):
    return asyncio.run(FN(
        name="agent1",
        request=ChatMessageRequest(message="hi"),
        current_user=_user(),
        x_source_agent=None,
        triggered_by="chat",
        task_execution_id="te1",
        _chat_subscription_id=None,
        chat_activity_id="ca1",
        collaboration_activity_id=collaboration_activity_id,
        session=session or MagicMock(id="s1"),
        execution=MagicMock(id="cex1"),
        queue_result="running",
        is_queued=False,
        chat_timeout=3600,
        idem=MagicMock(),
        capacity=env_capacity,
    ))


def _ok_response(session_id="11111111-1111-1111-1111-111111111111"):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "response": "hi back",
        "metadata": {"cost_usd": 0.02, "output_tokens": 10, "session_id": session_id},
        "session": {"context_tokens": 100, "context_window": 200000},
        "execution_log": [],
        "execution_log_simplified": [],
    }
    return resp


# --- SUCCESS --------------------------------------------------------------
def test_success_persists_and_completes():
    cap = _capacity()
    with _env(post_return=_ok_response()) as m:
        result = _run(cap, collaboration_activity_id="col1")
    assert result["response"] == "hi back"
    assert result["execution"]["queue_status"] == "running"
    # assistant message logged
    roles = [c.kwargs.get("role") for c in m["db"].add_chat_message.call_args_list]
    assert "assistant" in roles
    # terminal SUCCESS write carries the UUID-validated session id
    su = [c for c in m["db"].update_execution_status.call_args_list
          if c.kwargs.get("status") == TaskExecutionStatus.SUCCESS]
    assert len(su) == 1
    assert su[0].kwargs["claude_session_id"] == "11111111-1111-1111-1111-111111111111"
    # both activities completed COMPLETED
    states = [c.kwargs.get("status") for c in m["activity"].complete_activity.call_args_list]
    assert states.count(ActivityState.COMPLETED) == 2  # chat + collaboration
    # idempotency snapshot stored, slot released, no fail()
    m["isvc"].complete.assert_called_once()
    m["isvc"].fail.assert_not_called()
    cap.release.assert_awaited_once()


def test_success_discards_malformed_session_id():
    """A non-UUID agent-supplied session id is rejected (the 'dispatched'
    sentinel is kept) — SUCCESS write carries claude_session_id=None."""
    cap = _capacity()
    with _env(post_return=_ok_response(session_id="not-a-uuid")) as m:
        _run(cap)
    su = [c for c in m["db"].update_execution_status.call_args_list
          if c.kwargs.get("status") == TaskExecutionStatus.SUCCESS]
    assert su[0].kwargs["claude_session_id"] is None


# --- Budget exhausted -----------------------------------------------------
def test_budget_exhausted_maps_503_and_fails_idem():
    cap = _capacity()
    budget_exc = _MOD.BackendAgentCallBudgetExhausted(
        agent_name="agent1", agent_cap=2, global_cap=8, wait_ms=1500,
    )
    with _env(post_exc=budget_exc) as m:
        with pytest.raises(Exception) as exc:
            _run(cap)
    assert getattr(exc.value, "status_code", None) == 503
    # FAILED row written for a genuine (RUNNING) row
    fw = [c for c in m["db"].update_execution_status.call_args_list
          if c.kwargs.get("status") == TaskExecutionStatus.FAILED]
    assert len(fw) == 1
    cap.release.assert_awaited_once()
    m["isvc"].fail.assert_called_once()


# --- SUB-003 429 matrix ---------------------------------------------------
def _sub003(handle_return=None, handle_exc=None, is_auth=False):
    """Patch the lazily-imported SUB-003 helpers at their source module."""
    hsf = AsyncMock(side_effect=handle_exc) if handle_exc else AsyncMock(return_value=handle_return)
    return (
        patch("services.subscription_auto_switch.handle_subscription_failure", hsf),
        patch("services.subscription_auto_switch.is_auth_failure", return_value=is_auth),
    )


def test_sub003_429_switch_authoritative():
    cap = _capacity()
    err = _http_status_error(429, body={"detail": "rate limited"})
    hsf, isauth = _sub003(handle_return={"new_subscription": "sub2"})
    with _env(post_exc=err), hsf, isauth:
        with pytest.raises(Exception) as exc:
            _run(cap)
    assert getattr(exc.value, "status_code", None) == 429
    detail = exc.value.detail
    assert detail["auto_switch"] == {"new_subscription": "sub2"}
    assert "auto-switched" in detail["message"].lower()
    assert detail["retry_after"] == 15


def test_sub003_429_switch_raises_httpexception_propagates():
    """handle_subscription_failure itself raising an HTTPException propagates
    verbatim (the inner ``except HTTPException: raise``)."""
    from fastapi import HTTPException
    cap = _capacity()
    err = _http_status_error(429, body={"detail": "rate limited"})
    raised = HTTPException(status_code=418, detail="teapot")
    hsf, isauth = _sub003(handle_exc=raised)
    with _env(post_exc=err), hsf, isauth:
        with pytest.raises(Exception) as exc:
            _run(cap)
    assert getattr(exc.value, "status_code", None) == 418


def test_sub003_429_switch_real_error_falls_through_to_plain_429():
    """A non-HTTP error inside handle_subscription_failure is logged (outer
    ``except Exception``) and the handler falls through to a plain 429."""
    cap = _capacity()
    err = _http_status_error(429, body={"detail": "rate limited"})
    hsf, isauth = _sub003(handle_exc=RuntimeError("switch blew up"))
    with _env(post_exc=err), hsf, isauth:
        with pytest.raises(Exception) as exc:
            _run(cap)
    assert getattr(exc.value, "status_code", None) == 429
    assert exc.value.detail == "rate limited"  # plain error_msg, not the auto_switch dict


def test_sub003_429_no_switch_falls_through_to_plain_429():
    cap = _capacity()
    err = _http_status_error(429, body={"detail": "rate limited"})
    hsf, isauth = _sub003(handle_return=None)
    with _env(post_exc=err), hsf, isauth:
        with pytest.raises(Exception) as exc:
            _run(cap)
    assert getattr(exc.value, "status_code", None) == 429
    assert exc.value.detail == "rate limited"


# --- SUB-003 503 / auth matrix --------------------------------------------
def test_sub003_503_switch_authoritative():
    cap = _capacity()
    err = _http_status_error(503, body={"detail": "auth failure"})
    hsf, isauth = _sub003(handle_return={"new_subscription": "sub2"}, is_auth=True)
    with _env(post_exc=err), hsf, isauth:
        with pytest.raises(Exception) as exc:
            _run(cap)
    assert getattr(exc.value, "status_code", None) == 503
    detail = exc.value.detail
    assert detail["auto_switch"] == {"new_subscription": "sub2"}
    assert detail["retry_after"] == 15


def test_sub003_503_switch_raises_httpexception_propagates():
    from fastapi import HTTPException
    cap = _capacity()
    err = _http_status_error(503, body={"detail": "auth failure"})
    hsf, isauth = _sub003(handle_exc=HTTPException(status_code=418, detail="teapot"), is_auth=True)
    with _env(post_exc=err), hsf, isauth:
        with pytest.raises(Exception) as exc:
            _run(cap)
    assert getattr(exc.value, "status_code", None) == 418


def test_sub003_503_switch_real_error_falls_through_to_plain_503():
    cap = _capacity()
    err = _http_status_error(503, body={"detail": "auth failure"})
    hsf, isauth = _sub003(handle_exc=RuntimeError("switch blew up"), is_auth=True)
    with _env(post_exc=err), hsf, isauth:
        with pytest.raises(Exception) as exc:
            _run(cap)
    assert getattr(exc.value, "status_code", None) == 503
    assert "Failed to communicate with agent" in str(exc.value.detail)


def test_sub003_503_no_switch_falls_through_to_plain_503():
    cap = _capacity()
    err = _http_status_error(503, body={"detail": "auth failure"})
    hsf, isauth = _sub003(handle_return=None, is_auth=True)
    with _env(post_exc=err), hsf, isauth:
        with pytest.raises(Exception) as exc:
            _run(cap)
    assert getattr(exc.value, "status_code", None) == 503
    assert "Failed to communicate with agent" in str(exc.value.detail)


# --- #678 partial-metadata salvage ----------------------------------------
def test_http_error_salvages_partial_metadata_onto_failed_row():
    """A structured agent error dict carrying metadata → cost/context salvaged
    onto the FAILED row (#678). No SUB-003 (no 429/503, is_auth False)."""
    cap = _capacity()
    err = _http_status_error(
        500,
        body={"detail": {"message": "empty result", "metadata": {"cost_usd": 0.03, "context_window": 200000, "input_tokens": 5, "output_tokens": 7}}},
    )
    hsf, isauth = _sub003(handle_return=None, is_auth=False)
    with _env(post_exc=err) as m, hsf, isauth:
        with pytest.raises(Exception) as exc:
            _run(cap)
    assert getattr(exc.value, "status_code", None) == 503
    fw = [c for c in m["db"].update_execution_status.call_args_list
          if c.kwargs.get("status") == TaskExecutionStatus.FAILED]
    assert len(fw) == 1
    assert fw[0].kwargs.get("cost") == 0.03  # salvaged from partial_metadata
    assert fw[0].kwargs.get("context_max") == 200000
