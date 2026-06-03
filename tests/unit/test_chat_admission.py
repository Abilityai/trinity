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
