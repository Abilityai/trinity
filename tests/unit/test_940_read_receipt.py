"""The first-read receipt (trinity-enterprise#940, checkpoint D1 — TD-1).

`relay_unverified` asks "did the caller read the callee's result before
relaying it?", which needs a stored read. An agent key reading another agent's
run — `GET /api/agents/{name}/executions/{id}` (MCP `get_execution_result`) or
`GET /api/agents/{name}/fan-out/{fan_out_id}` (MCP `get_fan_out_result`) — now
writes ONE `agent_collaboration` activity with `"action": "read_result"` and
`related_execution_id` = the run read, on the reader's own agent. Pinned:

  * first read of a FINISHED run only: a poll of a running run writes nothing
    (a receipt dated before the callee ended never verifies a relay), a second
    read writes no second row, and two racing writes leave one row — the
    primary key decides, not a SELECT;
  * principal-keyed: a human read and an agent reading its own run write
    nothing; the row's agent is the key's own agent;
  * fail-open: a receipt write that raises leaves status and body byte-for-byte
    what they were;
  * the live consumer: after the real GET, the pair evaluator's
    `relay_unverified` passes on that receipt.

Driven through a real FastAPI app carrying the real `routers.schedules` and
`routers.fan_out` routers on the db harness; only the principal and the
path-access dependency are supplied.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent.parent / "src" / "backend"
for p in (str(_BACKEND), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)
from db_harness import count, db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

CALLER, CALLEE = "boss", "worker"
CALLER_RUN = "CallerRun0000000000001"
CHILD = "ChildRun00000000000001"
FAN = "fo_batch000000000001"
READS = "activity_type = 'agent_collaboration' AND details LIKE '%\"action\": \"read_result\"%'"


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _exec(exec_id, agent=CALLEE, *, status="success", parent=CALLER_RUN, source=CALLER,
          triggered_by="agent", response="ok", completed=60, **extra):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id="__agent__", agent_name=agent, status=status,
            triggered_by=triggered_by, message="/review PR 12", response=response,
            source_agent_name=source, parent_execution_id=parent,
            queued_at=_ago(300), started_at=_ago(300),
            completed_at=None if completed is None else _ago(completed), **extra))


def _user(username="owner", *, role="admin", agent_name=None, mcp_scope=None):
    from models import User
    return User(id=1, username=username, role=role, agent_name=agent_name, mcp_scope=mcp_scope)


AGENT_KEY = _user(agent_name=CALLER, mcp_scope="agent")
HUMAN = _user()


@pytest.fixture
def client(db_backend, monkeypatch):
    from routers import fan_out, schedules
    from services import skill_gate_map_service

    principal = {"user": AGENT_KEY}
    app = FastAPI()
    app.include_router(schedules.router)
    app.include_router(fan_out.router)
    # override the function objects the routes actually hold (a sibling test
    # may have reloaded `dependencies`, so its module attributes can differ)
    fakes = {"get_current_user": lambda: principal["user"],
             "get_authorized_agent": lambda name: name}

    def walk(dep):
        for d in dep.dependencies:
            if getattr(d.call, "__name__", None) in fakes:
                app.dependency_overrides[d.call] = fakes[d.call.__name__]
            walk(d)
    for route in app.routes:
        if hasattr(route, "dependant"):
            walk(route.dependant)
    monkeypatch.setattr(skill_gate_map_service, "self_approved_flags",
                        lambda name, ids, user: {})
    tc = TestClient(app)
    tc.as_user = lambda u: principal.__setitem__("user", u)
    return tc


def _get(client, exec_id=CHILD, agent=CALLEE):
    return client.get(f"/api/agents/{agent}/executions/{exec_id}")


def _receipts():
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import agent_activities
    with get_engine().connect() as conn:
        rows = conn.execute(select(agent_activities)).mappings().all()
    return [dict(r) for r in rows
            if r["details"] and json.loads(r["details"]).get("action") == "read_result"]


# --- the execution-result read ----------------------------------------------

def test_an_agent_reading_a_finished_run_leaves_one_receipt(client):
    _exec(CHILD)
    r = _get(client)
    assert r.status_code == 200 and r.json()["id"] == CHILD
    [row] = _receipts()
    assert row["agent_name"] == CALLER and row["related_execution_id"] == CHILD
    assert row["activity_type"] == "agent_collaboration"
    assert json.loads(row["details"])["target_agent"] == CALLEE
    # the shape B part 1's channel probe matches on (serialised `"action": "read_result"`)
    assert count("agent_activities", READS) == 1
    from database import db
    assert db.earliest_collaboration_action("read_result") is not None


def test_a_second_read_writes_no_second_receipt(client):
    _exec(CHILD)
    first = _get(client)
    second = _get(client)
    assert len(_receipts()) == 1
    assert (second.status_code, second.content) == (first.status_code, first.content)


def test_a_poll_of_a_running_run_writes_nothing_and_the_finished_read_does(client):
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import schedule_executions
    _exec(CHILD, status="running", completed=None)
    assert _get(client).status_code == 200
    assert _receipts() == []
    with get_engine().begin() as conn:
        conn.execute(update(schedule_executions).where(schedule_executions.c.id == CHILD)
                     .values(status="success", completed_at=_ago(5)))
    _get(client)
    assert len(_receipts()) == 1


def test_a_human_read_writes_nothing(client):
    _exec(CHILD)
    client.as_user(HUMAN)
    assert _get(client).status_code == 200
    assert _receipts() == []


def test_an_agent_reading_its_own_run_writes_nothing(client):
    _exec(CHILD, agent=CALLER, parent=None, source=None)
    assert _get(client, agent=CALLER).status_code == 200
    assert _receipts() == []


def test_a_missing_run_writes_nothing(client):
    assert _get(client, exec_id="NoSuchRun00000000001").status_code == 404
    assert _receipts() == []


def test_a_receipt_write_that_raises_changes_nothing_in_the_response(client, monkeypatch):
    _exec(CHILD)
    client.as_user(HUMAN)
    baseline = _get(client)                         # no receipt path at all
    client.as_user(AGENT_KEY)
    from database import db

    def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "insert_read_receipt_if_absent", boom)
    r = _get(client)
    assert (r.status_code, r.content, r.headers.get("content-type")) == (
        baseline.status_code, baseline.content, baseline.headers.get("content-type"))
    assert _receipts() == []


# --- the store: insert-if-absent on the primary key ----------------------------

def test_two_racing_receipt_writes_leave_one_row(db_backend):
    from database import db
    _exec(CHILD)
    gate = threading.Barrier(2)
    results = []

    def write():
        gate.wait()
        results.append(db.insert_read_receipt_if_absent(CALLER, CALLEE, CHILD))
    threads = [threading.Thread(target=write) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    assert len(_receipts()) == 1


def test_receipts_are_one_per_reader_and_run(db_backend):
    from database import db
    _exec(CHILD)
    assert db.insert_read_receipt_if_absent(CALLER, CALLEE, CHILD) is True
    assert db.insert_read_receipt_if_absent(CALLER, CALLEE, CHILD) is False
    assert db.insert_read_receipt_if_absent("auditor", CALLEE, CHILD) is True
    assert sorted(r["agent_name"] for r in _receipts()) == ["auditor", CALLER]


# --- the fan-out result read ---------------------------------------------------

def _fan_out(statuses):
    ids = []
    for i, st in enumerate(statuses):
        eid = f"FanChild000000000000{i}"
        _exec(eid, status=st, triggered_by="fan_out", completed=None if st == "running" else 60,
              fan_out_id=FAN, fan_out_task_id=f"t{i}")
        ids.append(eid)
    return ids


def _get_fan(client):
    return client.get(f"/api/agents/{CALLEE}/fan-out/{FAN}")


def test_a_fan_out_read_receipts_each_finished_child_once(client):
    done, failed, running = _fan_out(["success", "failed", "running"])
    first = _get_fan(client)
    assert first.status_code == 200
    assert sorted(r["related_execution_id"] for r in _receipts()) == sorted([done, failed])
    second = _get_fan(client)
    assert len(_receipts()) == 2
    assert (second.status_code, second.content) == (first.status_code, first.content)


def test_a_human_fan_out_read_writes_nothing(client):
    _fan_out(["success"])
    client.as_user(HUMAN)
    assert _get_fan(client).status_code == 200
    assert _receipts() == []


def test_a_fan_out_receipt_write_that_raises_changes_nothing(client, monkeypatch):
    _fan_out(["success", "failed"])
    client.as_user(HUMAN)
    baseline = _get_fan(client)
    client.as_user(AGENT_KEY)
    from database import db

    def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "insert_read_receipt_if_absent", boom)
    r = _get_fan(client)
    assert (r.status_code, r.content) == (baseline.status_code, baseline.content)


# --- the live consumer: the pair evaluator's relay check ------------------------

def test_the_real_read_verifies_the_callers_relay(client, monkeypatch):
    from database import db
    from services import tier0_pair_evaluator as pe
    monkeypatch.setattr(db, "get_audit_entries", lambda **kw: [])
    _exec(CALLER_RUN, agent=CALLER, parent=None, source=None, triggered_by="schedule",
          response=f"Done: worker finished execution {CHILD}.", completed=None)
    _exec(CHILD)
    db.upsert_platform_evaluation(
        CALLEE, execution_id=CHILD, completion=True, archetype="playbook",
        checks={"schema": 1, "headline": {"passed": True, "gate_pass": 1, "gate_fail": 0},
                "intent_ref": {"kind": "playbook", "ref": "review", "unresolved": False},
                "checks": []})
    _get(client)                                   # the caller reads the result ...
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:             # ... then its own turn ends
        conn.execute(update(schedule_executions)
                     .where(schedule_executions.c.id == CALLER_RUN)
                     .values(status="success", completed_at=_ago(-1)))
    row = pe.evaluate_pair(CHILD)
    c = next(c for c in row["checks"]["checks"] if c["id"] == "relay_unverified")
    assert c["status"] == "pass", c
