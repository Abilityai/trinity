"""
The pre-admission policy seam (trinity-enterprise#1059, checkpoint A).

Targets, each through its own layer:
  * ``services/admission_policy`` — the registry: no policy admits everything;
    a policy that refuses names its reason; a policy that breaks is ignored.
  * ``/chat``  → ``dispatch_admission_service.admit_chat_request``
  * ``/task``  → ``chat_execution_service.dispatch_parallel_task``
  * every other door → ``TaskExecutionService.execute_task``: the row closes
    ``skipped`` with the policy's plain-words message (not ``failed`` — a stop
    on an agent's share is not the agent's failure, so it must not feed a
    schedule's failure count or the dispatch breaker)
  * ``routers/chat.py`` → 429 with ``X-Trinity-Error-Code`` and ``Retry-After``
  * the Workspace classifier → a ``limit`` refusal, not retryable
  * ``platform_prompt_service.compose_system_prompt`` → the ``Usage`` line

Real per-test database (``db_harness``) and the real gate world
(``_gate_world``). Stubbed: capacity, the dispatch bodies past the seams.
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import count as _count, db_backend  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parent))   # `_gate_world`, the #2320 harness

import _gate_world as W  # noqa: E402
import services.admission_policy as _POLICY  # noqa: E402
import services.chat_execution_service as _CE  # noqa: E402
import services.dispatch_admission_service as _DISPATCH  # noqa: E402
from database import db  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from models import ChatMessageRequest, ParallelTaskRequest, TaskExecutionStatus, User  # noqa: E402
from services.capacity_manager import CapacityFull  # noqa: E402
from services.chat_signals import AdmissionRefused  # noqa: E402
from services.execution_envelope import TaskExecutionErrorCode  # noqa: E402

# The Workspace classifier harness, reused rather than copied: the REAL
# `portal_chat` with its boundaries stubbed.
from test_2320_portal_failed_turn_visibility import (  # noqa: E402
    AGENT as PORTAL_AGENT, EMAIL as PORTAL_EMAIL, SESSION as PORTAL_SESSION, chat as portal,  # noqa: F401
)

pytestmark = pytest.mark.unit

STOP = "You have used 15.2 of your 15 usage budget in the last 5 hours."


class _Refuse:
    """A policy that stops every turn, and records who asked."""

    def __init__(self, frees_in=timedelta(minutes=30)):
        self.calls = []
        self.frees_at = datetime.now(timezone.utc) + frees_in

    def enforce(self, agent_name, *, triggered_by, source):
        self.calls.append((agent_name, triggered_by, source))
        raise AdmissionRefused(TaskExecutionErrorCode.USAGE_SHARE.value, agent_name, STOP,
                               frees_at=self.frees_at)

    def notice(self, agent_name):
        return None


class _Broken:
    def enforce(self, agent_name, *, triggered_by, source):
        raise RuntimeError("policy store unreachable")

    def notice(self, agent_name):
        raise RuntimeError("policy store unreachable")


class _AsyncAdmit:
    def __init__(self):
        self.calls = 0

    async def enforce(self, agent_name, *, triggered_by, source):
        self.calls += 1

    def notice(self, agent_name):
        return "You have used 81% of your 7-day usage share; raise your ask now."


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """Every test starts from an empty registry and leaves none behind."""
    monkeypatch.setattr(_POLICY, "_POLICIES", [])
    return _POLICY


@pytest.fixture
def world(db_backend, monkeypatch):
    return W.build(monkeypatch)


def _person(world):
    return User(id=world.owner_id, username=W.OWNER, email=W.OWNER_EMAIL, role="user")


def _spy(monkeypatch, module, attr):
    calls = []
    real = getattr(module, attr)

    def _wrapped(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)

    monkeypatch.setattr(module, attr, _wrapped)
    return calls


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def test_with_no_policy_registered_the_seam_admits_and_says_nothing(registry):
    assert asyncio.run(registry.enforce("any-agent", triggered_by="chat", source="chat")) is None
    assert registry.notice("any-agent") is None


def test_a_refusing_policy_raises_its_named_reason(registry):
    policy = _Refuse()
    registry.register(policy)
    with pytest.raises(AdmissionRefused) as info:
        asyncio.run(registry.enforce("a1", triggered_by="schedule", source="execute_task"))
    assert info.value.code == "usage_share"
    assert info.value.message == STOP
    assert info.value.retryable is False
    assert policy.calls == [("a1", "schedule", "execute_task")]


def test_a_broken_policy_is_ignored_fail_open(registry):
    """A policy that cannot read its own state must not 500 every dispatch."""
    registry.register(_Broken())
    assert asyncio.run(registry.enforce("a1", triggered_by="chat", source="chat")) is None
    assert registry.notice("a1") is None


def test_an_async_policy_is_awaited(registry):
    policy = _AsyncAdmit()
    registry.register(policy)
    asyncio.run(registry.enforce("a1", triggered_by="chat", source="chat"))
    assert policy.calls == 1


def test_registering_the_same_policy_twice_runs_it_once(registry):
    policy = _Refuse()
    registry.register(policy)
    registry.register(policy)
    with pytest.raises(AdmissionRefused):
        asyncio.run(registry.enforce("a1", triggered_by="chat", source="chat"))
    assert len(policy.calls) == 1


def test_retry_after_counts_down_to_when_the_window_frees():
    now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    e = AdmissionRefused("usage_share", "a1", STOP, frees_at=now + timedelta(seconds=90))
    assert e.retry_after_seconds(now=now) == 90
    assert e.retry_after_seconds(now=now + timedelta(hours=1)) == 0
    assert AdmissionRefused("usage_share", "a1", STOP).retry_after_seconds(now=now) is None
    body = e.detail()
    assert body["error"] == "usage_share" and body["message"] == STOP
    assert body["frees_at"] == "2026-11-02T10:01:30Z"


# ---------------------------------------------------------------------------
# /chat — refused before the claim and the acquire
# ---------------------------------------------------------------------------

def test_chat_with_no_policy_admits_as_before(world, registry):
    admission = W.chat(_person(world), message="hello", idem="idem-ok")
    assert admission.execution_id
    world.capacity.acquire.assert_called_once()


def test_chat_refused_by_a_policy_claims_nothing_and_acquires_nothing(world, registry, monkeypatch):
    policy = _Refuse()
    registry.register(policy)
    begun = _spy(monkeypatch, _DISPATCH.idempotency_service, "begin")
    with pytest.raises(AdmissionRefused) as info:
        W.chat(_person(world), message="hello", idem="idem-refused")
    assert info.value.message == STOP
    assert begun == []                         # the same key works once the share frees
    world.capacity.acquire.assert_not_called()
    assert policy.calls == [(W.FIN, "mcp", "chat")]


# ---------------------------------------------------------------------------
# /task — refused before the claim, the uploads, the row and the acquire
# ---------------------------------------------------------------------------

def test_task_refused_by_a_policy_writes_no_row(world, registry, monkeypatch):
    registry.register(_Refuse())
    begun = _spy(monkeypatch, _DISPATCH, "begin_task_idempotency")
    before = _count("schedule_executions", "agent_name = :a", a=W.FIN)
    with pytest.raises(AdmissionRefused):
        W.task(_person(world), message="hello", idem="idem-task-refused")
    assert begun == []
    assert _count("schedule_executions", "agent_name = :a", a=W.FIN) == before
    world.capacity.acquire.assert_not_called()


# ---------------------------------------------------------------------------
# Every other door — execute_task closes the row SKIPPED with the message
# ---------------------------------------------------------------------------

def test_a_door_through_execute_task_with_no_policy_reaches_admission(world, registry):
    """Characterisation: with nothing registered the turn goes on to the
    capacity acquire exactly as before (made to refuse here, so the test can
    see it got that far without running an agent)."""
    world.capacity.acquire.side_effect = CapacityFull(W.FIN, 1, "rejected")
    row = db.create_task_execution(agent_name=W.FIN, message="hello", triggered_by="schedule")
    result = W.execute(message="hello", execution_id=row.id)
    world.capacity.acquire.assert_called_once()
    assert result.error_code == TaskExecutionErrorCode.CAPACITY


def test_a_door_through_execute_task_refused_closes_the_row_skipped(world, registry):
    policy = _Refuse()
    registry.register(policy)
    row = db.create_task_execution(agent_name=W.FIN, message="hello", triggered_by="schedule")
    result = W.execute(message="hello", execution_id=row.id, slot_already_held=True)

    assert result.status == TaskExecutionStatus.SKIPPED
    # The enum is a @dataclass str-Enum whose __eq__ says every member is equal
    # to every other (#1085) — compare the value.
    assert result.error_code.value == "usage_share"
    assert result.error == STOP
    assert result.admission_refusal is not None and result.admission_refusal.message == STOP
    after = db.get_execution(row.id)
    assert after.status == TaskExecutionStatus.SKIPPED
    assert after.error == STOP
    world.capacity.acquire.assert_not_called()
    world.capacity.release.assert_called_once()        # the caller's held slot is given back
    assert _count("agent_activities", "agent_name = :a", a=W.FIN) == 0
    assert policy.calls == [(W.FIN, "schedule", "execute_task")]


def test_a_sync_task_refused_inside_execute_task_releases_its_claim():
    """A share that runs out between the /task seam and the dispatch: the
    claim is released and the same refusal reaches the router."""
    refusal = AdmissionRefused("usage_share", "a1", STOP)
    result = SimpleNamespace(status="skipped", error=STOP, admission_refusal=refusal,
                             error_code=TaskExecutionErrorCode.USAGE_SHARE)
    idem = object()
    failed = []
    original = _CE.idempotency_service.fail
    _CE.idempotency_service.fail = lambda i: failed.append(i)
    try:
        with pytest.raises(AdmissionRefused) as info:
            _CE._map_task_failure("a1", result, idem=idem)
    finally:
        _CE.idempotency_service.fail = original
    assert info.value is refusal
    assert failed == [idem]


# ---------------------------------------------------------------------------
# The router — 429, the code header, Retry-After
# ---------------------------------------------------------------------------

def _router_env(monkeypatch, refusal):
    import routers.chat as _CHAT
    monkeypatch.setattr(_CHAT, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(_CHAT.dispatch_admission_service, "admit_chat_request",
                        AsyncMock(side_effect=refusal))
    monkeypatch.setattr(_CHAT.chat_execution_service, "dispatch_parallel_task",
                        AsyncMock(side_effect=refusal))
    return _CHAT


def _user():
    return User(id=1, username="u", email="u@example.com", role="user")


def _assert_refusal_response(exc, refusal):
    assert exc.status_code == 429
    assert exc.headers["X-Trinity-Error-Code"] == "usage_share"
    assert 1700 <= int(exc.headers["Retry-After"]) <= 1800
    assert exc.detail["message"] == STOP
    assert exc.detail["error"] == "usage_share"
    assert exc.detail["retryable"] is False


def test_the_chat_route_answers_429_with_the_code_and_when_it_frees(monkeypatch):
    refusal = _Refuse().frees_at
    refusal = AdmissionRefused("usage_share", "a1", STOP, frees_at=refusal)
    chat_router = _router_env(monkeypatch, refusal)
    with pytest.raises(HTTPException) as info:
        asyncio.run(chat_router.chat_with_agent(
            request=ChatMessageRequest(message="hi"), name="a1", current_user=_user(),
            x_source_agent=None, x_via_mcp=None, idempotency_key=None))
    _assert_refusal_response(info.value, refusal)


def test_the_task_route_answers_the_same(monkeypatch):
    refusal = AdmissionRefused("usage_share", "a1", STOP, frees_at=_Refuse().frees_at)
    chat_router = _router_env(monkeypatch, refusal)
    with pytest.raises(HTTPException) as info:
        asyncio.run(chat_router.execute_parallel_task(
            request=ParallelTaskRequest(message="hi"), name="a1", current_user=_user(),
            x_source_agent=None, x_via_mcp=None, idempotency_key=None,
            x_event_trigger=None, x_internal_secret=None, x_trinity_execution_id=None))
    _assert_refusal_response(info.value, refusal)


# ---------------------------------------------------------------------------
# The Workspace — plain words, category `limit`, not retryable
# ---------------------------------------------------------------------------

def test_the_workspace_tells_the_person_in_plain_words_and_offers_no_retry(portal):
    from test_2320_portal_failed_turn_visibility import _refuse
    svc, state = portal
    refusal = AdmissionRefused("usage_share", PORTAL_AGENT, STOP)
    result = SimpleNamespace(status="skipped", response="", error=STOP, session_id=None,
                             cost=None, error_code=TaskExecutionErrorCode.USAGE_SHARE,
                             admission_refusal=refusal)
    err = _refuse(svc, state, result=result)
    assert err.status_code == 429
    assert err.category == "limit"
    assert err.retryable is False
    assert err.detail == STOP


# ---------------------------------------------------------------------------
# The context line
# ---------------------------------------------------------------------------

def _prompt(agent="a1"):
    from services import platform_prompt_service as P
    ctx = P.ExecutionContext(agent_name=agent, triggered_by="schedule", collaborators=[],
                             platform_url="", ended_asks=[], pending_asks=[],
                             pending_asks_total=0, primary_user_display="x")
    return P.compose_system_prompt(ctx)


def test_the_usage_line_is_absent_with_no_policy(registry):
    assert "**Usage**" not in _prompt()


def test_the_usage_line_carries_the_policys_notice(registry):
    registry.register(_AsyncAdmit())
    assert "- **Usage**: You have used 81% of your 7-day usage share; raise your ask now." in _prompt()


def test_a_broken_notice_leaves_the_prompt_intact(registry):
    registry.register(_Broken())
    text = _prompt()
    assert "**Usage**" not in text
    assert "## Execution Context" in text
