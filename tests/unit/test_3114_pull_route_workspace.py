"""Workspace surfaces on a pull pilot (#3114).

What is pinned here:

* The pull sink reports a delegated child back to the channel / portal / room
  it came from, as the push terminals do; an inline-trigger turn is not
  reported.
* Rooms: the working marker lives as long as a queued room turn can, and a room
  turn waits at most ``ROOM_CLAIM_BUDGET_SECONDS`` for a claim
  (``dispatch_and_await_terminal(claim_budget=...)``).
* An interactive turn no worker claimed in time is stored FAILED; a caller that
  went away leaves it CANCELLED.
* The agent's pull worker registers a claimed turn as pending, so a terminate
  before spawn skips the turn and reports it cancelled.
* Two wakes of one agent in one room run one after the other; the second reads
  the cursor the first advanced. A mention chain back to the same agent does
  not wait on its own lock.

Runs against the real db layer through db_harness (SQLite; Postgres too when
TEST_POSTGRES_URL is set).
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _scalar  # noqa: E402,F401

pytestmark = pytest.mark.unit

_STUBBED_MODULE_NAMES = [
    "db.connection",
    "db.schedules",
    "db.agent_settings.resources",
    "database",
]

AGENT = "alpha"


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def tmp_db(db_backend):
    def _evict():
        for mod in _STUBBED_MODULE_NAMES:
            sys.modules.pop(mod, None)

    _evict()
    try:
        yield db_backend
    finally:
        _evict()


@pytest.fixture
def seed_agent(tmp_db):
    def _seed(name: str = AGENT, timeout: int = 900):
        _hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, "
            " execution_timeout_seconds, created_at) "
            "VALUES (:n, 1, :t, '2026-01-01T00:00:00Z')",
            n=name, t=timeout,
        )

    return _seed


def _row(eid: str, *, status: str = "queued", trigger: str = "mcp",
         channel=None, chat_id=None):
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
        " triggered_by, source_channel, source_channel_chat_id) "
        "VALUES (:id, '__manual__', :a, :s, '2026-09-30T09:00:00Z', "
        " CASE WHEN :s = 'queued' THEN '2026-09-30T09:00:00Z' END, 'hello', :t, "
        " :ch, :cid)",
        id=eid, a=AGENT, s=status, t=trigger, ch=channel, cid=chat_id,
    )
    return eid


def _db():
    from database import db

    return db


# ---------------------------------------------------------------------------
# 1. Pull sink → completion report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger,reported", [("mcp", True), ("public", False)])
async def test_pull_sink_reports_a_delegated_child_back(seed_agent, monkeypatch,
                                                        trigger, reported):
    seed_agent()
    _row("e1", trigger=trigger, channel="portal", chat_id="thread-1")
    import contextlib
    import importlib

    pcs = importlib.import_module("services.pull_coordination_service")
    ccr = importlib.import_module("services.channel_completion_report")
    idem = importlib.import_module("services.idempotency_service")

    delivered = []

    def _resolver(**kw):
        async def _deliver():
            delivered.append(kw)
            return True
        return _deliver

    @contextlib.asynccontextmanager
    async def _guard(*a, **k):
        yield SimpleNamespace(replay=False, snapshot=None)

    monkeypatch.setattr(ccr, "_inflight", set())     # other tests' loops are closed
    monkeypatch.setitem(ccr._CHANNEL_RESOLVERS, "portal", _resolver)
    monkeypatch.setattr(idem, "effect_guard", _guard)

    row = _db().claim_next_queued(AGENT, worker_id="w1", lease_seconds=900)
    outcome = pcs.apply_task_result("e1", row["claim_token"], status="success",
                                    content="child done")
    assert outcome.kind == "applied"
    await asyncio.gather(*list(ccr._inflight))

    if reported:
        assert len(delivered) == 1
        assert delivered[0]["chat_id"] == "thread-1"
        assert delivered[0]["status"] == "success"
        assert delivered[0]["summary_or_error"] == "child done"
    else:
        assert delivered == []


# ---------------------------------------------------------------------------
# 2. Rooms: working-marker TTL + claim budget
# ---------------------------------------------------------------------------


def test_room_working_marker_ttl_covers_the_queued_wait(seed_agent, monkeypatch):
    from shared_sessions import service

    seed_agent(timeout=900)
    base = service.ROOM_TURN_TIMEOUT_SECONDS + 30
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
    assert service._working_ttl(AGENT) == base
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT)
    assert service._working_ttl(AGENT) == base + service.ROOM_CLAIM_BUDGET_SECONDS
    _hrun("UPDATE agent_ownership SET execution_timeout_seconds = 120 WHERE agent_name = :n",
          n=AGENT)
    assert service._working_ttl(AGENT) == base + 120


@pytest.mark.asyncio
async def test_claim_budget_bounds_the_claim_wait(seed_agent, monkeypatch):
    seed_agent(timeout=3600)
    _row("e1", trigger="room")
    from services import task_execution_service as tes
    from services.execution_envelope import TaskExecutionErrorCode, TaskExecutionResult

    monkeypatch.setattr(tes, "QUEUE_CLAIM_POLL_INTERVAL", 0.05)
    svc = MagicMock()
    svc.execute_task = AsyncMock(return_value=TaskExecutionResult(
        execution_id="e1", status="queued", response=""))

    out = await asyncio.wait_for(tes.dispatch_and_await_terminal(
        agent_name=AGENT, message="m", triggered_by="room", service=svc,
        claim_budget=1,
    ), 5)
    assert out.error_code == TaskExecutionErrorCode.CAPACITY
    assert out.error == "Agent at capacity (queued turn not claimed in 1s)"
    assert "claim_budget" not in svc.execute_task.await_args.kwargs


# ---------------------------------------------------------------------------
# 3. Unclaimed → FAILED at the db layer (the CAS is unchanged)
# ---------------------------------------------------------------------------


def test_cancel_queued_execution_writes_the_requested_terminal(seed_agent):
    seed_agent()
    db = _db()
    _row("f1")
    assert db.cancel_queued_execution("f1", reason="not claimed", status="failed") is True
    row = db.get_execution("f1")
    assert (row.status, row.error) == ("failed", "not claimed")
    assert row.completed_at

    _row("c1")
    assert db.cancel_queued_execution("c1", reason="gone") is True
    assert db.get_execution("c1").status == "cancelled"

    _row("r1", status="running")
    assert db.cancel_queued_execution("r1", status="failed") is False
    assert db.get_execution("r1").status == "running"


# ---------------------------------------------------------------------------
# 4. Pull worker: terminate between claim and spawn
# ---------------------------------------------------------------------------


def _drive_worker(monkeypatch, *, before_run=None, run=None):
    from agent_server.services import pull_worker as pw
    from agent_server.services import process_registry as pr

    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "get_process_registry", lambda: registry)
    seen = {}

    async def _execute(**kw):
        seen["pending_at_spawn"] = kw["execution_id"] in registry.list_pending_ids()
        if run:
            return run(registry)
        md = SimpleNamespace(model_dump=lambda: {"cost_usd": 0.0})
        return ("ok", [], md, "s")

    runtime = SimpleNamespace(execute_headless=AsyncMock(side_effect=_execute))
    stub = types.ModuleType("agent_server.services.runtime_adapter")
    stub.get_runtime = lambda: runtime
    monkeypatch.setitem(sys.modules, "agent_server.services.runtime_adapter", stub)
    if before_run:
        real_start = pw.agent_state.record_task_start

        def _start():
            before_run(registry)
            real_start()

        monkeypatch.setattr(pw.agent_state, "record_task_start", _start)
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(pw, "_deliver_result", deliver)
    monkeypatch.setattr(pw, "_persist_pull_result", lambda *a, **k: None)
    monkeypatch.setattr(pw, "_delete_pull_result", lambda *a, **k: None)
    claim = {"execution_id": "e1", "claim_token": "t", "envelope": {"payload": {"message": "m"}}}
    asyncio.run(pw._run_and_report(claim, MagicMock(), "http://b", "k", AGENT))
    return runtime.execute_headless, deliver.await_args.args[2], registry, seen


def test_claimed_turn_is_pending_until_spawn(monkeypatch):
    run, body, registry, seen = _drive_worker(monkeypatch)
    assert seen["pending_at_spawn"] is True
    assert body["status"] == "success"
    assert registry.list_pending_ids() == []


def test_terminate_before_spawn_skips_the_turn_and_reports_cancelled(monkeypatch):
    outcome = {}

    def _terminate(registry):
        outcome["terminate"] = registry.terminate("e1")

    run, body, registry, _ = _drive_worker(monkeypatch, before_run=_terminate)
    assert outcome["terminate"]["reason"] == "cancelled_before_start"
    run.assert_not_called()
    assert (body["status"], body["error_code"], body["claim_token"]) == ("cancelled", None, "t")
    assert registry.list_pending_ids() == []


def test_terminated_turn_keeps_an_auth_failure_class(monkeypatch):
    def _run(registry):
        registry.terminate("e1")
        raise HTTPException(status_code=503, detail="auth failed")

    _, body, _, _ = _drive_worker(monkeypatch, run=_run)
    assert (body["status"], body["error_code"]) == ("failed", "auth")


# ---------------------------------------------------------------------------
# 5. Two wakes of one agent in one room
# ---------------------------------------------------------------------------


class _FakeRedis:
    """SET NX EX + the token-checked release: what ResumeLock uses."""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    async def eval(self, _lua, _n, key, token):
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


def _fake_room(monkeypatch, *, on_post=None):
    """A room with one unread message for AGENT; the cursor is real state."""
    from services import session_turn_service as sts
    from shared_sessions import service

    state = {"cursor": 0, "dispatches": [], "since": []}
    messages = [{"seq": 1, "sender_kind": "user", "sender_identity": "c@example.com",
                 "content": f"@{AGENT} hi", "kind": "message"}]

    def _get_messages(_room, since_seq=0):
        state["since"].append(since_seq)
        return [m for m in messages if m["seq"] > since_seq]

    def _advance(_room, _agent, seq, _sid):
        state["cursor"] = seq

    async def _dispatch(**kw):
        state["dispatches"].append(kw)
        await asyncio.sleep(0.05)
        return SimpleNamespace(status="success", response="hello", execution_id="x",
                               session_id="s1", error=None)

    async def _post(*a, **k):
        if on_post:
            await on_post()
        return {}

    monkeypatch.setattr(service.db, "get_participant",
                        lambda *a, **k: {"last_read_seq": state["cursor"],
                                         "cached_session_id": "s1"})
    monkeypatch.setattr(service.db, "get_room", lambda *a, **k: {
        "id": "r1", "name": "Room", "status": "open", "topic": None})
    monkeypatch.setattr(service.db, "get_messages", _get_messages)
    monkeypatch.setattr(service.db, "list_participants", lambda _r: [
        {"kind": "agent", "identity": AGENT, "left_at": None}])
    monkeypatch.setattr(service.db, "advance_read_cursor", _advance)
    for name in ("_mark_agent_working", "_clear_agent_working", "_broadcast", "_post_system"):
        monkeypatch.setattr(service, name, lambda *a, **k: None)
    monkeypatch.setattr(service, "_room_queue_allowance", lambda _a: 0)
    monkeypatch.setattr(service, "_room_inbox_context", AsyncMock(return_value=("", [])))
    monkeypatch.setattr(service, "post_message", _post)
    import services.task_execution_service as tes

    monkeypatch.setattr(tes, "dispatch_and_await_terminal", _dispatch)
    fake = _FakeRedis()
    monkeypatch.setattr(sts, "get_async_redis", lambda: fake)
    monkeypatch.setattr(sts, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    return service, state, fake


@pytest.mark.asyncio
async def test_second_wake_reads_what_the_first_left(monkeypatch):
    service, state, fake = _fake_room(monkeypatch)
    user = SimpleNamespace(email="c@example.com")

    await asyncio.wait_for(asyncio.gather(
        service._wake_agent(user, "r1", AGENT, 0),
        service._wake_agent(user, "r1", AGENT, 0),
    ), 5)

    assert len(state["dispatches"]) == 1              # the delta is answered once
    assert state["since"] == [0, 1]                   # second wake saw the advanced cursor
    assert state["dispatches"][0]["claim_budget"] == service.ROOM_CLAIM_BUDGET_SECONDS
    assert fake.store == {}
    assert fake.ttls[f"room_wake_lock:r1:{AGENT}"] == (
        service.ROOM_TURN_TIMEOUT_SECONDS + service.ROOM_WAKE_LOCK_BUFFER_SECONDS)


@pytest.mark.asyncio
async def test_mention_chain_back_to_the_same_agent_does_not_wait_on_itself(monkeypatch):
    calls = {"n": 0}
    user = SimpleNamespace(email="c@example.com")

    async def _reply_mentions_back():
        calls["n"] += 1
        if calls["n"] == 1:      # A's reply wakes B, whose reply wakes A again
            await service._wake_agent(user, "r1", AGENT, 1)

    service, state, fake = _fake_room(monkeypatch, on_post=_reply_mentions_back)
    await asyncio.wait_for(service._wake_agent(user, "r1", AGENT, 0), 5)
    assert len(state["dispatches"]) == 2
    assert fake.store == {}
