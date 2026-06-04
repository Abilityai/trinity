"""
Characterization tests for the admission gate of routers.chat.chat_with_agent
(#1026, slice 1).

The front of chat_with_agent gates a request before any work: idempotency
replay/in-flight (#525), dispatch-breaker fast-fail (#526), and the
CapacityManager.acquire (#428) with its CapacityFull→429 + idempotency-release.
These pin the four exit modes so the extraction of `_admit_chat_request` is
provably behavior-preserving. The admitted (happy) path is covered separately
against the extracted helper.
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
