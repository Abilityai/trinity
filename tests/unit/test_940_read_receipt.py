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
    `relay_unverified` passes on that receipt;
  * an inline delivery is a read (review I3): a sync `/chat`, sync `/task` or
    sync `POST /fan-out` that hands an agent key the FINISHED result writes
    the same receipt; an accepted/queued receipt answer writes none, and a
    raising write changes nothing in the response.

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


# --- an inline delivery is a read (review I3) -------------------------------------

@pytest.fixture
def inline(db_backend, monkeypatch):
    """The real `/chat`, `/task` and `POST /fan-out` routes; the dispatch below
    them answers what a sync turn answers once the callee row is terminal."""
    from types import SimpleNamespace
    from routers import chat, fan_out

    principal = {"user": AGENT_KEY}
    answers = {}
    app = FastAPI()
    app.include_router(chat.router)
    app.include_router(fan_out.router)
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

    monkeypatch.setattr(chat, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(chat, "resolve_source_agent", lambda u, h, **k: u.agent_name)
    monkeypatch.setattr(fan_out, "resolve_source_agent", lambda u, h, **k: u.agent_name)

    async def admit(**kw):
        return SimpleNamespace(idem=None, execution_id=CHILD, capacity_result=None,
                               capacity=None, queue_result="started", chat_timeout=60,
                               chain_depth=1, gate=None)

    async def prepare(**kw):
        return SimpleNamespace(execution=SimpleNamespace(id="q1"), task_execution_id=CHILD,
                               triggered_by="agent", subscription_id=None,
                               collaboration_activity_id=None, chat_activity_id=None,
                               session=None, is_queued=False, isolated_session=False)

    async def run_turn(**kw):
        return answers["chat"]

    async def dispatch_task(**kw):
        return answers["task"]

    async def depth(**kw):
        return 1

    class FanOutService:
        async def execute(self, **kw):
            return answers["fan_out"]

    monkeypatch.setattr(chat.dispatch_admission_service, "admit_chat_request", admit)
    monkeypatch.setattr(chat.chat_execution_service, "prepare_chat_execution", prepare)
    monkeypatch.setattr(chat.chat_execution_service, "run_chat_turn", run_turn)
    monkeypatch.setattr(chat.chat_execution_service, "dispatch_parallel_task", dispatch_task)
    monkeypatch.setattr(fan_out.dispatch_admission_service, "enforce_inter_agent_depth", depth)
    monkeypatch.setattr(fan_out, "get_fan_out_service", lambda: FanOutService())

    answers["chat"] = {"response": "done", "execution": {
        "id": "q1", "task_execution_id": CHILD, "queue_status": "started", "was_queued": False}}
    answers["task"] = {"response": "done", "execution_id": CHILD, "task_execution_id": CHILD}
    tc = TestClient(app)
    tc.as_user = lambda u: principal.__setitem__("user", u)
    tc.answers = answers
    tc.fan_out_result = lambda rows: SimpleNamespace(
        fan_out_id=FAN, status="completed", total=len(rows),
        completed=sum(1 for _, st in rows if st == "success"),
        failed=sum(1 for _, st in rows if st != "success"), pending_approval=0,
        results=[SimpleNamespace(id=f"t{i}", status=st, response="r", error=None,
                                 error_code=None, execution_id=eid, cost=None,
                                 context_used=None, duration_ms=1, request_id=None)
                 for i, (eid, st) in enumerate(rows)])
    return tc


def _chat(c):
    return c.post(f"/api/agents/{CALLEE}/chat", json={"message": "/review PR 12"})


def _task(c):
    return c.post(f"/api/agents/{CALLEE}/task", json={"message": "/review PR 12"})


def test_a_sync_chat_delivery_leaves_one_receipt(inline):
    _exec(CHILD)
    r = _chat(inline)
    assert r.status_code == 200 and r.json()["response"] == "done"
    [row] = _receipts()
    assert (row["agent_name"], row["related_execution_id"]) == (CALLER, CHILD)
    assert json.loads(row["details"])["target_agent"] == CALLEE
    _chat(inline)
    assert len(_receipts()) == 1


def test_a_sync_task_delivery_leaves_one_receipt(inline):
    _exec(CHILD)
    assert _task(inline).status_code == 200
    assert [r["related_execution_id"] for r in _receipts()] == [CHILD]


def test_a_sync_fan_out_delivery_receipts_each_finished_child(inline):
    _exec("FanChild0000000000000", status="success", fan_out_id=FAN)
    _exec("FanChild0000000000001", status="failed", fan_out_id=FAN)
    inline.answers["fan_out"] = inline.fan_out_result(
        [("FanChild0000000000000", "success"), ("FanChild0000000000001", "failed")])
    r = inline.post(f"/api/agents/{CALLEE}/fan-out", json={
        "agent": CALLEE, "tasks": [{"id": "t0", "message": "a"}, {"id": "t1", "message": "b"}]})
    assert r.status_code == 200, r.text
    assert sorted(x["related_execution_id"] for x in _receipts()) == [
        "FanChild0000000000000", "FanChild0000000000001"]


@pytest.mark.parametrize("answer", [
    {"status": "accepted", "execution_id": CHILD, "task_execution_id": CHILD,
     "agent_name": CALLEE, "async_mode": True},
    {"status": "queued", "execution_id": CHILD, "task_execution_id": CHILD,
     "agent_name": CALLEE, "async_mode": True},
])
def test_a_receipt_answer_writes_none_even_once_the_run_has_finished(inline, answer):
    _exec(CHILD)                                    # finished by the time we look
    inline.answers["task"] = answer
    assert _task(inline).status_code == 200
    assert _receipts() == []


def test_an_inline_answer_for_a_run_not_yet_terminal_writes_none(inline):
    _exec(CHILD, status="running", completed=None)
    assert _chat(inline).status_code == 200
    assert _receipts() == []


def test_a_human_sync_chat_writes_nothing(inline):
    _exec(CHILD)
    inline.as_user(HUMAN)
    assert _chat(inline).status_code == 200
    assert _receipts() == []


def test_an_inline_receipt_write_that_raises_changes_nothing(inline, monkeypatch):
    _exec(CHILD)
    inline.as_user(HUMAN)
    base_chat, base_task = _chat(inline), _task(inline)
    inline.as_user(AGENT_KEY)
    from database import db

    def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "insert_read_receipt_if_absent", boom)
    for base, r in ((base_chat, _chat(inline)), (base_task, _task(inline))):
        assert (r.status_code, r.content, r.headers.get("content-type")) == (
            base.status_code, base.content, base.headers.get("content-type"))
    assert _receipts() == []


def test_a_relay_after_an_inline_reply_is_not_unread(inline, monkeypatch):
    """The caller got the reply inline, quoted the callee id, never polled:
    with receipts live on the install, `relay_unverified` passes."""
    from database import db
    from services import tier0_pair_evaluator as pe
    monkeypatch.setattr(db, "get_audit_entries", lambda **kw: [])
    db.insert_read_receipt_if_absent("auditor", CALLEE, "SomeOtherRun0000000001")  # live since
    _exec(CALLER_RUN, agent=CALLER, parent=None, source=None, triggered_by="schedule",
          response=f"Done: worker finished execution {CHILD}.", completed=None)
    _exec(CHILD)
    db.upsert_platform_evaluation(
        CALLEE, execution_id=CHILD, completion=True, archetype="playbook",
        checks={"schema": 1, "headline": {"passed": True, "gate_pass": 1, "gate_fail": 0},
                "intent_ref": {"kind": "playbook", "ref": "review", "unresolved": False},
                "checks": []})
    _chat(inline)                                  # the reply arrives inline ...
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:             # ... then the caller's turn ends
        conn.execute(update(schedule_executions)
                     .where(schedule_executions.c.id == CALLER_RUN)
                     .values(status="success", completed_at=_ago(-1)))
    row = pe.evaluate_pair(CHILD)
    c = next(c for c in row["checks"]["checks"] if c["id"] == "relay_unverified")
    assert c["status"] == "pass", c
