"""Delegation link stamping at admission (ent#935 addendum A, checkpoint M.2).

The caller's turn id arrives on two doors — the platform-injected
`X-Trinity-Execution-Id` header (#2392) and the model-typed
`ParallelTaskRequest.parent_execution_id` (ent#224) — and was accepted but
never stored. It is now persisted on the child row's `parent_execution_id`,
kept ONLY when the parent row's agent is the child's `source_agent_name` (the
`_inherited_channel_context` provenance rule): `db.get_execution` is a global
lookup, so an unchecked id would let any caller graft its run onto another
agent's delegation record.

Runs on the real schema (`tests/db_harness.py`) through the real service
entries — `dispatch_parallel_task` (/task) and `prepare_chat_execution`
(/chat). Only the agent dispatch, capacity and activity seams are doubled.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend, seed_agent, seed_user  # noqa: E402,F401

from db.write_params import TaskExecutionFields  # noqa: E402
from models import ParallelTaskRequest, User  # noqa: E402
from services import chat_execution_service as ces  # noqa: E402
import services.dispatch_admission_service as _DISPATCH  # noqa: E402

pytestmark = pytest.mark.unit

CALLER = "deleg-a"
WORKER = "deleg-b"
OTHER = "deleg-evil"


def _agent(agent=CALLER) -> User:
    return User(id=1, username="owner", email="owner@example.com", role="user",
                agent_name=agent, mcp_scope="agent", mcp_key_id="k-" + agent,
                mcp_key_name=f"agent-{agent}-key")


def _turn(agent: str) -> str:
    from database import db
    return db.create_task_execution(
        agent_name=agent, message="caller turn", triggered_by="schedule",
        fields=TaskExecutionFields()).id


def _stored_parent(execution_id: str):
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return conn.execute(
            text("SELECT parent_execution_id FROM schedule_executions WHERE id = :i"),
            {"i": execution_id}).scalar()


@pytest.fixture
def world(db_backend, monkeypatch):
    from database import db
    seed_user(1, "owner", role="user")
    seed_user(2, "stranger", role="user")
    seed_agent(CALLER, 1)
    seed_agent(WORKER, 1)
    seed_agent(OTHER, 2)
    db.add_agent_permission(CALLER, WORKER, created_by="owner")
    capacity = SimpleNamespace(acquire=AsyncMock(
        return_value=SimpleNamespace(state="admitted", queue_position=None)))
    monkeypatch.setattr(_DISPATCH, "get_capacity_manager", lambda: capacity)
    monkeypatch.setattr(_DISPATCH, "dispatch_breaker_active", lambda _n: False)
    monkeypatch.setattr(ces, "_dispatch_async", AsyncMock(return_value={"status": "accepted"}))
    monkeypatch.setattr(ces, "_dispatch_sync", AsyncMock(return_value={"status": "success"}))
    monkeypatch.setattr(ces, "activity_service",
                        MagicMock(track_activity=AsyncMock(return_value="act-x")))
    monkeypatch.setattr(ces, "broadcast_collaboration_event", AsyncMock())
    return db_backend


def _task(*, header=None, body_parent=None, idem="idem-1"):
    """/task through the real dispatch orchestrator; returns the child row id."""
    from database import db
    asyncio.run(ces.dispatch_parallel_task(
        request=ParallelTaskRequest(message="do it", async_mode=True,
                                    parent_execution_id=body_parent),
        name=WORKER, current_user=_agent(), container=SimpleNamespace(status="running"),
        x_source_agent=CALLER, x_via_mcp="true", idempotency_key=idem,
        x_event_trigger=None, x_internal_secret=None, x_trinity_execution_id=header,
    ))
    rows = [e for e in db.get_agent_executions(WORKER, limit=10) if e.message == "do it"]
    assert len(rows) == 1
    return rows[0].id


def _chat(*, header=None):
    from services.idempotency_service import IdempotencyDecision
    ctx = asyncio.run(ces.prepare_chat_execution(
        name=WORKER, request=SimpleNamespace(message="hello", model=None),
        current_user=_agent(), x_source_agent=CALLER, x_via_mcp="true",
        idem=IdempotencyDecision(enabled=False, replay=False, in_flight=False),
        chat_execution_id="q-1", capacity_result=SimpleNamespace(state="immediate"),
        queue_result="immediate", x_trinity_execution_id=header,
    ))
    return ctx.task_execution_id


class TestTaskDoor:
    def test_two_agent_chain_joins_child_to_parent(self, world):
        from db.engine import get_engine
        from sqlalchemy import text
        parent = _turn(CALLER)
        child = _task(header=parent)
        assert _stored_parent(child) == parent
        # the join an evaluator will run: child -> parent, through the column
        with get_engine().connect() as conn:
            row = conn.execute(text(
                "SELECT p.agent_name, c.source_agent_name FROM schedule_executions c "
                "JOIN schedule_executions p ON p.id = c.parent_execution_id "
                "WHERE c.id = :c"), {"c": child}).one()
        assert tuple(row) == (CALLER, CALLER)

    def test_body_parent_is_the_fallback_door(self, world):
        parent = _turn(CALLER)
        assert _stored_parent(_task(body_parent=parent)) == parent

    def test_header_wins_over_body(self, world):
        real, forged = _turn(CALLER), _turn(OTHER)
        assert _stored_parent(_task(header=real, body_parent=forged)) == real

    def test_forged_parent_owned_by_another_agent_stores_null(self, world):
        assert _stored_parent(_task(header=_turn(OTHER))) is None

    def test_unknown_parent_stores_null(self, world):
        assert _stored_parent(_task(header="no-such-execution")) is None

    def test_missing_header_stores_null(self, world):
        _turn(CALLER)
        assert _stored_parent(_task()) is None


class TestChatDoor:
    def test_header_parent_is_stored(self, world):
        parent = _turn(CALLER)
        assert _stored_parent(_chat(header=parent)) == parent

    def test_forged_parent_stores_null(self, world):
        assert _stored_parent(_chat(header=_turn(OTHER))) is None

    def test_missing_header_stores_null(self, world):
        assert _stored_parent(_chat()) is None


def test_chat_router_hands_the_header_to_the_row_writer():
    """Production wiring for /chat: the router passes its
    `x_trinity_execution_id` header into `prepare_chat_execution`. (The /task
    door is driven end to end above through `dispatch_parallel_task`.)"""
    tree = ast.parse((_BACKEND / "routers" / "chat.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "prepare_chat_execution"]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get("x_trinity_execution_id"), ast.Name)
    assert kw["x_trinity_execution_id"].id == "x_trinity_execution_id"
