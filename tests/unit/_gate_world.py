"""The gated-skill test world for the trinity#3274 suites.

The fixture body of ``test_ent751_gate_entries.world``, as a function: a real
per-test database (``db_harness``), the real ask sink, and a gate map holding
``pay-invoice`` on ``FIN``. Stubbed: the gate map itself (trinity-enterprise#753),
the in-container fingerprint exec, capacity, and the dispatch bodies past the
admission seams. Not a test module (no ``test_`` prefix), so nothing here is
collected on its own.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import services.ask_service as _ASK
import services.chat_execution_service as _CE
import services.dispatch_admission_service as _DISPATCH
import services.operator_queue_service as _OQS
import services.role_addressing as _ROLES
import services.skill_gate_service as _GATE
import services.task_execution_service as _TES
from database import db
from db_models import UserCreate
from models import ChatMessageRequest, ParallelTaskRequest, User
from services.rate_limiter import RateLimitResult

OWNER, OWNER_EMAIL = "gate3274-owner", "owner-3274@example.com"
OTHER, OTHER_EMAIL = "gate3274-other", "other-3274@example.com"
REQ, FIN = "gate3274-mkt", "gate3274-fin"


def build(monkeypatch, *, owner_name=None):
    db.create_user(UserCreate(username=OWNER, role="user", email=OWNER_EMAIL, name=owner_name))
    db.create_user(UserCreate(username=OTHER, role="user", email=OTHER_EMAIL))
    db.register_agent_owner(REQ, OWNER)
    db.register_agent_owner(FIN, OWNER)
    owner = db.get_user_by_username(OWNER)
    other = db.get_user_by_username(OTHER)

    capacity = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(state="admitted", queue_position=None)),
        release=AsyncMock())
    monkeypatch.setattr(_DISPATCH, "get_capacity_manager", lambda: capacity)
    monkeypatch.setattr(_DISPATCH, "dispatch_breaker_active", lambda _n: False)
    monkeypatch.setattr(_TES, "get_capacity_manager", lambda: capacity)
    monkeypatch.setattr(_TES, "dispatch_breaker_active", lambda _n: False)
    monkeypatch.setattr(_CE, "_dispatch_sync", AsyncMock(return_value={"status": "success"}))
    monkeypatch.setattr(_CE, "_dispatch_async", AsyncMock(return_value={"status": "accepted"}))

    class _Audit:
        async def log(self, **kw):
            return "evt"

    class _WS:
        async def broadcast(self, message):
            json.loads(message)

    monkeypatch.setattr(_ASK, "platform_audit_service", _Audit())
    monkeypatch.setattr(_ASK, "_websocket_manager", _WS())
    monkeypatch.setattr(_ASK, "_observers", [])
    monkeypatch.setattr(_ROLES, "owner_email", lambda agent: OWNER_EMAIL)
    monkeypatch.setattr(_OQS, "_workspace_attachment", lambda agent, email, **_: (None, False))
    monkeypatch.setattr(_OQS.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    monkeypatch.setattr(_GATE.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    monkeypatch.setattr(db, "get_operator_resume_enabled", lambda agent: False, raising=False)
    import services.platform_audit_service as _PAS
    monkeypatch.setattr(_PAS, "platform_audit_service", _Audit())

    async def _read(agent, names):
        return {n: {"fingerprint": f"fp-{n}", "kind": "own"} for n in names}

    world = SimpleNamespace(owner_id=owner["id"], other_id=other["id"], capacity=capacity,
                            gates={"pay-invoice": _GATE.SkillGate(approver="primary")},
                            woken=[])

    async def _wake(record, text):
        world.woken.append((record["source_agent"], text))

    monkeypatch.setattr(_GATE, "list_skill_gates", lambda agent: world.gates if agent == FIN else {})
    monkeypatch.setattr(_GATE, "read_skill_fingerprints", _read)
    monkeypatch.setattr(_GATE, "_wake_requester_agent", _wake)
    return world


def agent_key(world, agent=REQ, **kw):
    return User(id=world.owner_id, username=OWNER, email=OWNER_EMAIL, role="user",
                agent_name=agent, mcp_scope="agent", mcp_key_id="k-" + agent, **kw)


def other_person(world, **kw):
    """A signed-in person who is NOT the approver (the owner is)."""
    return User(id=world.other_id, username=OTHER, email=OTHER_EMAIL, role="user", **kw)


def system_key(world):
    return User(id=world.owner_id, username=OWNER, email=OWNER_EMAIL, role="admin",
                mcp_scope="system", mcp_key_id="k-system", mcp_key_name="system")


def task(principal, message="/pay-invoice 100 EUR", idem="idem-task", **kw):
    return asyncio.run(_CE.dispatch_parallel_task(
        request=ParallelTaskRequest(message=message, **kw),
        name=FIN, current_user=principal, container=SimpleNamespace(status="running"),
        x_source_agent=None, x_via_mcp="true", idempotency_key=idem,
        x_event_trigger=None, x_internal_secret=None))


def chat(principal, message="/pay-invoice 100 EUR", idem="idem-chat"):
    return asyncio.run(_DISPATCH.admit_chat_request(
        name=FIN, request=ChatMessageRequest(message=message), current_user=principal,
        x_source_agent=None, x_via_mcp="true", idempotency_key=idem))


def execute(**kw):
    kw.setdefault("agent_name", FIN)
    kw.setdefault("triggered_by", "schedule")
    return asyncio.run(_TES.TaskExecutionService().execute_task(**kw))


def card(request_id):
    return db.get_operator_queue_item_for_agent_by_request_id(FIN, request_id)
