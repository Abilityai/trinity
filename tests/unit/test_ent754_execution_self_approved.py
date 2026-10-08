"""trinity-enterprise#754 — a run that skipped approval because its requester
IS the approver says so (the 10-03 eyeball: the only trace was an audit row).

No new column (UC1, approved): ent#752 already writes, for every self-approved
run, a `skill_gate_requests` row in state `self_approved` whose UNIQUE
`dispatched_execution_id` is the very execution the Tasks tab lists. The
executions list and detail now carry two booleans derived from it:

* `gate_self_approved` — the run went through without approval;
* `gate_self_approved_by_viewer` — and the caller is the person who did it.

The requester's email never leaves the server (it is compared there).

Driven end to end: the record is written through the REAL
`skill_gate_service.record_self_approval` and read back through the REAL
router functions over a real database (`db_harness`; SQLite always,
PostgreSQL when `TEST_POSTGRES_URL` is set).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("fastapi", reason="backend venv required")

from db_harness import db_backend, seed_execution  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT = "finance"

APPROVER = dict(id=1, username="ana", email="Ana@Example.com", role="user")
OTHER = dict(id=2, username="bob", email="bob@example.com", role="user")
AGENT_KEY = dict(id=1, username="ana", email="Ana@Example.com", role="user",
                 mcp_scope="agent", agent_name="sibling")


@pytest.fixture
def world(db_backend, monkeypatch):
    from models import User
    from services import skill_gate_service

    async def _no_audit(*a, **kw):
        return None

    monkeypatch.setattr(skill_gate_service, "audit_self_approved", _no_audit)

    mine = seed_execution("__manual__", AGENT, exec_id="exec-self", triggered_by="manual",
                          started_at="2026-10-08T10:00:00.000000Z")
    plain = seed_execution("__manual__", AGENT, exec_id="exec-plain", triggered_by="manual",
                           started_at="2026-10-08T09:00:00.000000Z")
    decision = skill_gate_service.GateDecision(("send-invoice",), self_approved_by="Ana@Example.com")
    asyncio.run(skill_gate_service.record_self_approval(
        AGENT, decision, execution_id=mine, current_user=User(**APPROVER),
        endpoint=f"/api/agents/{AGENT}/task", request_text="/send-invoice", triggered_by="manual"))
    return {"self": mine, "plain": plain, "User": User}


def _list(world, principal):
    from routers.schedules import get_agent_executions

    rows = asyncio.run(get_agent_executions(AGENT, limit=50, current_user=world["User"](**principal)))
    return {(r["id"] if isinstance(r, dict) else r.id): r for r in rows}


def _detail(world, principal, execution_id):
    from routers.schedules import get_execution

    return asyncio.run(get_execution(AGENT, execution_id, current_user=world["User"](**principal)))


def _get(row, key):
    return row[key] if isinstance(row, dict) else getattr(row, key)


def test_the_list_marks_only_the_self_approved_run(world):
    rows = _list(world, APPROVER)

    assert _get(rows[world["self"]], "gate_self_approved") is True
    assert _get(rows[world["plain"]], "gate_self_approved") is False
    assert _get(rows[world["plain"]], "gate_self_approved_by_viewer") is False


def test_the_list_says_you_only_to_the_approver(world):
    assert _get(_list(world, APPROVER)[world["self"]], "gate_self_approved_by_viewer") is True
    assert _get(_list(world, OTHER)[world["self"]], "gate_self_approved_by_viewer") is False


def test_a_machine_key_sees_neither(world):
    """Operator ruling (review round): the marker is for people. Beside the
    row's `source_user_email` it says that person fills the gate's approver
    kind; the gate map withholds `set_by` from machine keys for the same
    reason (#715), so an agent or MCP key reads both flags false."""
    row = _list(world, AGENT_KEY)[world["self"]]
    detail = _detail(world, AGENT_KEY, world["self"])

    assert (_get(row, "gate_self_approved"), _get(row, "gate_self_approved_by_viewer")) == (False, False)
    assert (detail.gate_self_approved, detail.gate_self_approved_by_viewer) == (False, False)


def test_the_detail_carries_the_same_two_flags(world):
    mine = _detail(world, APPROVER, world["self"])
    theirs = _detail(world, OTHER, world["self"])
    plain = _detail(world, APPROVER, world["plain"])

    assert (mine.gate_self_approved, mine.gate_self_approved_by_viewer) == (True, True)
    assert (theirs.gate_self_approved, theirs.gate_self_approved_by_viewer) == (True, False)
    assert (plain.gate_self_approved, plain.gate_self_approved_by_viewer) == (False, False)


def test_no_email_leaves_the_server(world):
    """The response models carry the two booleans and nothing that names the
    approver beyond what the row already carried before (`source_user_email`)."""
    from models import ExecutionResponse, ExecutionSummary

    for model in (ExecutionSummary, ExecutionResponse):
        new = {f for f in model.model_fields if "self_approved" in f}
        assert new == {"gate_self_approved", "gate_self_approved_by_viewer"}, model.__name__
    detail = _detail(world, OTHER, world["self"]).model_dump()
    assert "ana@example.com" not in str({k: v for k, v in detail.items() if "gate" in k}).lower()


def test_a_run_someone_else_approved_is_not_marked(world):
    """A request the approver let through is dispatched under the same UNIQUE
    `dispatched_execution_id` as a self-approved one; only the `self_approved`
    state means "ran without approval" — and never "you" for its requester."""
    from database import db

    db.create_gate_request(request_id="gate-approved", agent_name=AGENT, skills=["send-invoice"],
                           request_text="/send-invoice", fingerprints={"send-invoice": "f0"}, dispatch={},
                           requester_kind="person", requester_key="person:ana@example.com",
                           requester_email="Ana@Example.com", triggered_by="manual")
    assert db.claim_gate_request_for_dispatch("gate-approved", world["plain"]) is True
    assert db.transition_gate_request("gate-approved", "dispatched") is True

    row = _list(world, APPROVER)[world["plain"]]
    detail = _detail(world, APPROVER, world["plain"])

    assert (_get(row, "gate_self_approved"), _get(row, "gate_self_approved_by_viewer")) == (False, False)
    assert (detail.gate_self_approved, detail.gate_self_approved_by_viewer) == (False, False)


def test_a_large_page_is_read_in_statements_under_every_bind_limit(world):
    """The page is the caller's `limit` (unbounded). SQLite builds cap bound
    parameters (999 on old ones, 32,766 since 3.32) and PostgreSQL at 65,535;
    one IN over the whole page would raise there, and fail open to "no run is
    marked". The read is chunked, so every statement stays far below any cap."""
    from sqlalchemy import event

    from db import skill_gate_requests as sgr
    from services import skill_gate_map_service

    sizes = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        if "skill_gate_requests" in statement and "dispatched_execution_id" in statement:
            sizes.append(len(parameters))

    engine = sgr.get_engine()
    event.listen(engine, "before_cursor_execute", _count)
    try:
        ids = [f"exec-{i}" for i in range(1_201)] + [world["self"]]
        flags = skill_gate_map_service.self_approved_flags(AGENT, ids, world["User"](**APPROVER))
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert flags == {world["self"]: (True, True)}
    assert len(sizes) >= 3 and max(sizes) <= 500 + 2     # the chunk, plus agent_name and state


def test_a_record_for_another_agents_run_does_not_leak_across(world):
    """The batch read is scoped to the agent as well as to the ids."""
    from database import db

    runs = db.get_self_approved_runs("someone-else", [world["self"]])

    assert runs == {}
