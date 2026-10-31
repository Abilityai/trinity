"""#3998 — a /chat turn whose setup raises releases its slot and its claim.

`admit_chat_request` acquires the capacity slot and claims the
Idempotency-Key. Only `run_chat_turn`'s `finally` released them, and the
execution setup between the two (`prepare_chat_execution`: the row insert,
activities, the session, the inbound log) ran outside it. When setup raised,
the request answered 500 while the slot stayed held until the cleanup sweep or
its TTL, and a retry with the same key answered 409 until the claim expired.

Drives the real `chat_with_agent` route and the real `prepare_chat_execution`,
with admission stubbed to hand back a real idempotency claim and a capacity
manager that records its releases.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

AGENT = "agent-3998"


class _Capacity:
    def __init__(self):
        self.released = []

    async def release(self, name, execution_id):
        self.released.append((name, execution_id))


def _route(monkeypatch, *, capacity, fail_setup=True):
    from models import ChatMessageRequest
    from routers import chat as chat_router
    from services import chat_execution_service, idempotency_service
    from services.chat_signals import ChatAdmission

    key = f"k-{uuid.uuid4().hex}"
    idem = idempotency_service.begin(f"agent:{AGENT}", key)
    assert idem.enabled and not idem.replay, "control: a fresh claim"
    execution_id = f"ex-{uuid.uuid4().hex[:8]}"

    async def _admit(**_kw):
        return ChatAdmission(
            idem=idem, execution_id=execution_id,
            capacity_result=SimpleNamespace(state="acquired"), capacity=capacity,
            queue_result="running", chat_timeout=60)

    monkeypatch.setattr(chat_router, "resolve_source_agent", lambda user, source, endpoint: source)
    monkeypatch.setattr(chat_router, "get_agent_container",
                        lambda name: SimpleNamespace(status="running"))
    monkeypatch.setattr(chat_router.dispatch_admission_service, "admit_chat_request", _admit)
    if fail_setup:
        def _insert_fails(**_kw):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(chat_execution_service.db, "create_task_execution", _insert_fails)
    else:
        async def _prepared(**_kw):
            return SimpleNamespace(
                execution=SimpleNamespace(id=execution_id), task_execution_id=execution_id,
                triggered_by="user", subscription_id=None, collaboration_activity_id=None,
                chat_activity_id=None, session=None, is_queued=False, isolated_session=False)

        async def _turn(**_kw):
            return {"response": "ok"}

        monkeypatch.setattr(chat_execution_service, "prepare_chat_execution", _prepared)
        monkeypatch.setattr(chat_execution_service, "run_chat_turn", _turn)

    user = SimpleNamespace(id=1, username="u", email="u@example.com", role="user",
                           agent_name=None, mcp_key_id=None, mcp_key_name=None)
    coro = chat_router.chat_with_agent(
        request=ChatMessageRequest(message="hi"), name=AGENT, current_user=user,
        x_source_agent=None, x_via_mcp=None, idempotency_key=key, x_trinity_execution_id=None)
    return coro, key, execution_id


def _claim_is_free(key):
    from services import idempotency_service

    again = idempotency_service.begin(f"agent:{AGENT}", key)
    return not again.replay


def test_a_setup_that_raises_releases_the_slot_and_the_claim(monkeypatch):
    capacity = _Capacity()
    coro, key, execution_id = _route(monkeypatch, capacity=capacity)

    with pytest.raises(RuntimeError, match="database is locked"):
        asyncio.run(coro)

    assert capacity.released == [(AGENT, execution_id)], "the capacity slot leaked"
    assert _claim_is_free(key), "the idempotency claim stayed in_flight: a retry would get 409"


def test_a_pull_pilot_admission_holds_no_slot_but_its_claim_is_released(monkeypatch):
    coro, key, _ = _route(monkeypatch, capacity=None)

    with pytest.raises(RuntimeError, match="database is locked"):
        asyncio.run(coro)

    assert _claim_is_free(key)


def test_a_setup_that_succeeds_leaves_the_release_to_the_turn(monkeypatch):
    """Control: no early release — `run_chat_turn`'s `finally` owns it."""
    capacity = _Capacity()
    coro, key, _ = _route(monkeypatch, capacity=capacity, fail_setup=False)

    assert asyncio.run(coro) == {"response": "ok"}
    assert capacity.released == []
    assert not _claim_is_free(key), "the claim was released before the turn finished"
