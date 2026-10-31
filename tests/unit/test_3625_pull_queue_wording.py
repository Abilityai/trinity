"""
#3625: on a pull pilot every `/task` goes to the durable queue, so "queued"
says nothing about load. The async 202 must not tell the caller the agent is
"at capacity" on that path; the push path keeps its text byte-for-byte.

Two halves:
  1. `CapacityManager.acquire` marks a queued result `pull=True` exactly when
     the pilot gate routed it (and False on a push overflow).
  2. `_dispatch_async` words the 202 from that flag.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers.chat import execute_parallel_task
import services.dispatch_admission_service as _DISPATCH
import services.chat_execution_service as _CE
from models import ParallelTaskRequest
from services import capacity_manager as cm_module
from services.capacity_manager import AcquireResult, PersistentTaskPayload

_CHAT = sys.modules[execute_parallel_task.__module__]

pytestmark = pytest.mark.unit

_PUSH_MESSAGE = (
    "Agent at capacity; task queued. Poll GET "
    "/api/agents/agent1/executions/e1 for results."
)


# ---------------------------------------------------------------------------
# 1. acquire() reports which route queued the row
# ---------------------------------------------------------------------------


def _capacity(monkeypatch, slot_free):
    slots = AsyncMock()
    slots.slots_prefix = "agent:slots:"
    slots.acquire_slot = AsyncMock(return_value=slot_free)
    slots.register_on_release = lambda cb: None
    backlog = AsyncMock()
    backlog.enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(cm_module.redis, "from_url", lambda *_a, **_kw: MagicMock())
    return cm_module.CapacityManager(
        redis_url="redis://test", slot_service=slots, backlog_service=backlog
    )


def _acquire(cap, agent):
    return asyncio.run(cap.acquire(
        agent_name=agent,
        execution_id="e1",
        max_concurrent=3,
        overflow_policy="queue_persistent",
        overflow_payload=PersistentTaskPayload(
            request=MagicMock(), effective_timeout=900, user_id=1,
            user_email="u@x", subscription_id=None, x_source_agent=None,
            triggered_by="manual", collaboration_activity_id=None,
        ),
    ))


def test_pilot_queue_is_marked_pull(monkeypatch):
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "agent1")
    result = _acquire(_capacity(monkeypatch, slot_free=True), "agent1")
    assert result.state == "queued_persistent"
    assert result.pull is True


def test_push_overflow_is_not_marked_pull(monkeypatch):
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
    result = _acquire(_capacity(monkeypatch, slot_free=False), "agent1")
    assert result.state == "queued_persistent"
    assert result.pull is False


# ---------------------------------------------------------------------------
# 2. the async 202 is worded from the flag
# ---------------------------------------------------------------------------


@contextmanager
def _env(acquire_result):
    container = MagicMock()
    container.status = "running"

    idem = MagicMock(replay=False, in_flight=False, execution_id="e1", snapshot=None)
    isvc = MagicMock()
    isvc.begin.return_value = idem
    isvc.make_agent_scope.return_value = "agent:agent1"

    cap = MagicMock()
    cap.acquire = AsyncMock(return_value=acquire_result)

    db = MagicMock()
    db.get_execution_timeout.return_value = 3600
    db.get_max_parallel_tasks.return_value = 3
    db.get_agent_subscription_id.return_value = None
    db.create_task_execution.return_value = MagicMock(id="e1")

    with patch.object(_CHAT, "get_agent_container", return_value=container), \
         patch.object(_CE, "idempotency_service", isvc), \
         patch.object(_DISPATCH, "idempotency_service", isvc), \
         patch.object(_DISPATCH, "platform_audit_service", MagicMock(log=AsyncMock())), \
         patch.object(_CE, "dispatch_breaker_active", return_value=False), \
         patch.object(_CE, "get_capacity_manager", return_value=cap), \
         patch.object(_CE, "activity_service",
                      MagicMock(track_activity=AsyncMock(return_value="act1"))), \
         patch.object(_CE, "db", db), \
         patch.object(_CHAT, "db", db):
        yield


def _call_async():
    user = MagicMock(id=1, email="u@e.com", username="u", agent_name=None)
    return asyncio.run(execute_parallel_task(
        request=ParallelTaskRequest(message="hi", async_mode=True),
        name="agent1",
        current_user=user,
        x_source_agent=None,
        x_via_mcp=None,
        idempotency_key="k1",
    ))


def test_pull_queued_202_does_not_claim_capacity():
    with _env(AcquireResult("queued_persistent", "e1", pull=True)):
        payload = _call_async()
    assert payload["status"] == "queued"
    assert "at capacity" not in payload["message"]
    assert "worker pool" in payload["message"]
    assert "/api/agents/agent1/executions/e1" in payload["message"]


def test_push_queued_202_keeps_its_text():
    with _env(AcquireResult("queued_persistent", "e1")):
        payload = _call_async()
    assert payload["message"] == _PUSH_MESSAGE
