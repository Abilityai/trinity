"""#2889 — sync /chat and /task failures carry ``X-Trinity-Error-Code``.

The body of a sync dispatch failure is prose reconstructed from the agent's
own error text, so every consumer that had to tell "the agent server was never
reached" from "the turn ran and failed on the credential" re-derived it by
substring — ~50 live-test sites did, and laundered an exhausted credit balance
into an "agent not ready" skip. The backend already computes
``TaskExecutionErrorCode``; these tests pin that the sync paths now SEND it,
additively (header only — the body shapes are byte-identical to before).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.chat_execution_service as _CE
from services.chat_execution_service import (
    ERROR_CODE_HEADER,
    ChatDispatchError,
    _apply_sub003_autoswitch,
    _classify_agent_http_failure,
    _error_code_headers,
    _map_task_failure,
)
from services.execution_envelope import TaskExecutionErrorCode


# --------------------------------------------------------------------------- #
# the helper
# --------------------------------------------------------------------------- #


def test_header_name_is_the_documented_one():
    assert ERROR_CODE_HEADER == "X-Trinity-Error-Code"


@pytest.mark.parametrize(
    "code,expected",
    [
        (TaskExecutionErrorCode.AUTH, "auth"),
        (TaskExecutionErrorCode.NETWORK, "network"),
        ("billing", "billing"),
    ],
)
def test_error_code_headers_from_enum_or_string(code, expected):
    assert _error_code_headers(code) == {ERROR_CODE_HEADER: expected}


@pytest.mark.parametrize("code", [None, "", MagicMock(), 42])
def test_error_code_headers_sends_nothing_for_an_unknown_code(code):
    """A stub result (MagicMock — the shape the #2661 characterization tests
    use), an absent code, or garbage must NOT produce a header: the reader
    treats an absent header as "classify from the body", and a wrong value
    would override that."""
    assert _error_code_headers(code) is None


def test_error_code_headers_merges_with_existing_headers():
    out = _error_code_headers(TaskExecutionErrorCode.BILLING, {"Retry-After": "15"})
    assert out == {"Retry-After": "15", ERROR_CODE_HEADER: "billing"}


def test_error_code_headers_returns_extra_untouched_when_no_code():
    extra = {"Retry-After": "15"}
    assert _error_code_headers(None, extra) is extra


@pytest.mark.parametrize(
    "agent_status,expected",
    [
        (None, TaskExecutionErrorCode.NETWORK),
        (503, TaskExecutionErrorCode.AUTH),
        (429, TaskExecutionErrorCode.BILLING),
        (500, TaskExecutionErrorCode.AGENT_ERROR),
        (502, TaskExecutionErrorCode.AGENT_ERROR),
    ],
)
def test_chat_path_classification_mirrors_task_execution_service(agent_status, expected):
    """The /chat path applies the SAME producer-side rule
    task_execution_service uses: agent 503 → AUTH, 429 → BILLING, never
    answered → NETWORK."""
    assert _classify_agent_http_failure(agent_status) is expected


# --------------------------------------------------------------------------- #
# /task — _map_task_failure carries result.error_code
# --------------------------------------------------------------------------- #


def _result(status, error, error_code=None):
    return SimpleNamespace(status=status, error=error, error_code=error_code)


@pytest.mark.parametrize(
    "error,error_code,http,header",
    [
        ("Credit balance is too low", TaskExecutionErrorCode.AUTH, 503, "auth"),
        ("HTTP error: ConnectError", TaskExecutionErrorCode.NETWORK, 503, "network"),
        ("agent is at capacity", TaskExecutionErrorCode.CAPACITY, 429, "capacity"),
        ("the task timed out after 600s", TaskExecutionErrorCode.TIMEOUT, 504, "timeout"),
    ],
)
def test_map_task_failure_threads_the_code_onto_the_header(error, error_code, http, header):
    with patch.object(_CE, "idempotency_service"):
        with pytest.raises(ChatDispatchError) as exc:
            _map_task_failure("agent1", _result("failed", error, error_code), idem=MagicMock())
    assert exc.value.status_code == http
    assert exc.value.headers == {ERROR_CODE_HEADER: header}
    # Body is unchanged in shape: still the plain string it always was.
    assert isinstance(exc.value.detail, str)


def test_map_task_failure_without_a_code_sends_no_header():
    """The backlog-reconstruct path builds its result from the DB row, which
    has no error_code — the header is simply absent, never a guess."""
    with patch.object(_CE, "idempotency_service"):
        with pytest.raises(ChatDispatchError) as exc:
            _map_task_failure("agent1", _result("failed", "boom"), idem=MagicMock())
    assert exc.value.status_code == 503
    assert exc.value.headers is None


# --------------------------------------------------------------------------- #
# /chat — _apply_sub003_autoswitch carries the derived code
# --------------------------------------------------------------------------- #


async def _run_autoswitch(error_msg, agent_status, switch_result=None):
    fake = SimpleNamespace(
        handle_subscription_failure=AsyncMock(return_value=switch_result),
        is_auth_failure=lambda msg: False,
    )
    with patch.dict("sys.modules", {"services.subscription_auto_switch": fake}):
        with pytest.raises(ChatDispatchError) as exc:
            await _apply_sub003_autoswitch("agent1", error_msg, agent_status)
    return exc.value


@pytest.mark.asyncio
async def test_chat_transport_failure_is_network():
    e = await _run_autoswitch("HTTP error: ConnectError", None)
    assert e.status_code == 503
    assert e.headers == {ERROR_CODE_HEADER: "network"}
    assert e.detail == "Failed to communicate with agent: HTTP error: ConnectError"


@pytest.mark.asyncio
async def test_chat_agent_503_is_auth():
    e = await _run_autoswitch("Credit balance is too low", 503)
    assert e.status_code == 503
    assert e.headers == {ERROR_CODE_HEADER: "auth"}


@pytest.mark.asyncio
async def test_chat_agent_503_after_a_successful_switch_keeps_the_dict_body_and_adds_the_header():
    e = await _run_autoswitch("token expired", 503, switch_result={"new_subscription": "sub-b"})
    assert e.status_code == 503
    assert isinstance(e.detail, dict) and e.detail["auto_switch"] == {"new_subscription": "sub-b"}
    assert e.headers == {ERROR_CODE_HEADER: "auth"}


@pytest.mark.asyncio
async def test_chat_agent_429_is_billing_on_both_shapes():
    plain = await _run_autoswitch("usage limit", 429)
    assert plain.status_code == 429 and plain.headers == {ERROR_CODE_HEADER: "billing"}
    switched = await _run_autoswitch("usage limit", 429, switch_result={"new_subscription": "sub-b"})
    assert switched.status_code == 429 and switched.headers == {ERROR_CODE_HEADER: "billing"}


# --------------------------------------------------------------------------- #
# #2919 — the three admission-refusal producers carry `capacity`
# --------------------------------------------------------------------------- #
#
# `capacity` was in the enum and in two docs but had zero assignment sites, so
# a queue-full 429 was recognisable only by the ABSENCE of a header plus its
# prose. These pin each producer behaviourally (reusing the harnesses of
# tests/unit/test_chat_admission.py and test_946_task_idempotency_on_deny.py)
# so the header is the authority and the docs are true.


def test_map_task_failure_at_capacity_carries_the_capacity_header_without_a_result_code():
    """`_map_task_failure` decides "429" by the result TEXT and never receives
    a code on that branch (task_execution_service's capacity rejection sets
    none) — so the code it sends must agree with the status it sends."""
    with patch.object(_CE, "idempotency_service"):
        with pytest.raises(ChatDispatchError) as exc:
            _map_task_failure(
                "agent1",
                _result("failed", "Agent at capacity (2/2 parallel tasks running)", None),
                idem=MagicMock(),
            )
    assert exc.value.status_code == 429
    assert exc.value.headers == {ERROR_CODE_HEADER: "capacity"}
    assert isinstance(exc.value.detail, str)


def test_chat_admission_capacity_full_carries_the_capacity_header():
    from tests.unit.test_chat_admission import _call, _env, _idem
    from fastapi import HTTPException
    from services.capacity_manager import CapacityFull

    full = CapacityFull(agent_name="agent1", max_concurrent=3, reason="in_memory_full", depth=3)
    with _env(_idem(replay=False), acquire_exc=full) as m:
        with pytest.raises(HTTPException) as exc:
            _call()
    assert exc.value.status_code == 429
    assert exc.value.headers == {ERROR_CODE_HEADER: "capacity"}
    # Body byte-identical in shape: the dict the router always sent.
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["error"] == "Agent queue is full"
    assert exc.value.detail["agent"] == "agent1" and exc.value.detail["retry_after"] == 30
    m["isvc"].fail.assert_called_once()


def test_dispatch_async_capacity_full_carries_the_capacity_header():
    from tests.unit.test_946_task_idempotency_on_deny import _call, _env, _idem
    from fastapi import HTTPException
    from services.capacity_manager import CapacityFull

    full = CapacityFull(agent_name="agent1", max_concurrent=3, reason="persistent_full", depth=50)
    with _env(_idem(), full) as m:
        with pytest.raises(HTTPException) as exc:
            _call(async_mode=True)
    assert exc.value.status_code == 429
    assert exc.value.headers == {ERROR_CODE_HEADER: "capacity"}
    assert isinstance(exc.value.detail, str) and "is at capacity" in exc.value.detail
    m["isvc"].fail.assert_called_once()
