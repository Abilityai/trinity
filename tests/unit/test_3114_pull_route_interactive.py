"""Interactive producers on the durable queue (#3114).

What is pinned here:

* ``pull_owns_dispatch`` covers every interactive trigger (``chat`` since
  #3127) plus ``validation`` on a pull pilot.

* ``execute_task``'s queue payload carries what the push payload sends beside
  the request (conversation key, persist_session, images, schedule context,
  attempt), and ``BacklogService.enqueue`` stores it; an explicit conversation
  key wins on a pilot and is dropped elsewhere.
* The claim envelope hands ``persist_session``/``images`` to the worker, and the
  claim-time prompt gets the push path's provenance/timeout/schedule/attempt.
* The worker runs the turn with them.
* The pull sink persists compact events and wakes an in-process sync waiter.
* ``result_from_execution_row`` rebuilds a push-shaped result.
* ``dispatch_and_await_terminal`` waits at most one agent timeout for a claim on
  interactive triggers, stores FAILED/CAPACITY, and carries on when the
  cancel loses to a claim.
* ``run_resumable_turn`` goes through the adapter with a per-conversation key,
  and the Session lock TTL grows by the queue allowance on a pilot.
* Live-stream proxies hold while a pilot's turn is queued.
* Concurrency: a burst of queued turns of one conversation is claimed one at a
  time (claim guard), and two Session-tab turns of one session never overlap
  (the ResumeLock is held across the queued wait).

Runs against the real db layer through db_harness (SQLite; Postgres too when
TEST_POSTGRES_URL is set).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
        for mod in ("db.connection", "db.schedules", "db.agent_settings.resources", "database"):
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


def _row(eid: str, *, status: str = "queued", trigger: str = "session", meta=None,
         error=None, response=None, session=None, compact=None, email=None):
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
        " triggered_by, backlog_metadata, error, response, claude_session_id, "
        " compact_metadata, source_user_email) "
        "VALUES (:id, '__manual__', :a, :s, '2026-09-30T09:00:00Z', "
        " CASE WHEN :s = 'queued' THEN '2026-09-30T09:00:00Z' END, 'hello', :t, "
        " :meta, :err, :resp, :sess, :cm, :em)",
        id=eid, a=AGENT, s=status, t=trigger,
        meta=json.dumps(meta) if meta is not None else None,
        err=error, resp=response, sess=session, cm=compact, em=email,
    )
    return eid


def _db():
    from database import db

    return db


_IMG = [{"media_type": "image/png", "data": "iVBORw0KGgo+AIzaQQQQ"}]


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def test_pilot_pulls_every_interactive_trigger(monkeypatch):
    from services import pull_pilot as pp

    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT)
    # #3127: ``chat`` is pulled too.
    assert pp.PULL_REACHABLE_NON_AUTONOMOUS == pp.INTERACTIVE_TRIGGERS | {"validation"}
    for trigger in pp.PULL_REACHABLE_NON_AUTONOMOUS:
        assert pp.pull_owns_dispatch(AGENT, trigger) is True, trigger
        assert pp.pull_owns_dispatch("not-a-pilot", trigger) is False, trigger
    assert pp.pull_owns_dispatch(AGENT, None) is False


# ---------------------------------------------------------------------------
# Producer: execute_task → payload → enqueue
# ---------------------------------------------------------------------------


def test_queue_payload_carries_the_turn_fields():
    from services import task_execution_service as tes

    with patch.object(tes, "pull_owns_dispatch", return_value=True):
        payload = tes.build_pull_queue_payload(
            agent_name=AGENT, triggered_by="session", execution_id="e1",
            message="hi", model="opus", allowed_tools=None, system_prompt=None,
            timeout_seconds=600, resume_session_id="uuid-1", subscription_id=None,
            source_user_id=7, source_user_email="u@example.com",
            source_agent_name=None, slot_already_held=False,
            conversation_key="session:k1", persist_session=True,
            schedule_context={"name": "nightly"}, attempt=2, images=_IMG,
        )
    assert payload.conversation_key == "session:k1"
    assert payload.persist_session is True
    assert payload.images == _IMG
    assert payload.schedule_context == {"name": "nightly"}
    assert payload.attempt == 2
    assert payload.request.resume_session_id == "uuid-1"


def test_capacity_manager_forwards_every_payload_field_to_enqueue():
    """Structural: a field added to PersistentTaskPayload must reach enqueue."""
    from dataclasses import fields

    from services import capacity_manager as cm

    src = inspect.getsource(cm.CapacityManager.acquire)
    for f in fields(cm.PersistentTaskPayload):
        if f.name in ("request", "effective_timeout"):
            continue
        assert re.search(rf"{f.name}=overflow_payload\.{f.name}\b", src), f.name


def _enqueue(eid="e1", **extra):
    from models import ParallelTaskRequest
    from services.backlog_service import BacklogService

    req = ParallelTaskRequest(message="hi", async_mode=True, chat_session_id="chat-row-1")
    return asyncio.run(BacklogService().enqueue(
        agent_name=AGENT, execution_id=eid, request=req, effective_timeout=600,
        user_id=None, user_email=None, subscription_id=None, x_source_agent=None,
        triggered_by="session", collaboration_activity_id=None, **extra,
    ))


def test_enqueue_stores_turn_fields_and_explicit_key_wins_on_pilot(seed_agent, monkeypatch):
    seed_agent()
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT)
    _row("e1", status="running")

    assert _enqueue(conversation_key="session:k1", persist_session=True,
                    schedule_context={"name": "n"}, attempt=3, images=_IMG) is True

    assert _scalar("SELECT conversation_key FROM schedule_executions WHERE id='e1'") == "session:k1"
    meta = json.loads(_scalar("SELECT backlog_metadata FROM schedule_executions WHERE id='e1'"))
    assert meta["persist_session"] is True
    assert meta["images"] == _IMG
    assert meta["schedule_context"] == {"name": "n"}
    assert meta["attempt"] == 3


def test_enqueue_falls_back_to_chat_session_and_ignores_key_off_pilot(seed_agent, monkeypatch):
    seed_agent()
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT)
    _row("e1", status="running")
    assert _enqueue() is True
    assert _scalar("SELECT conversation_key FROM schedule_executions WHERE id='e1'") == "chat-row-1"

    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
    _hrun("UPDATE schedule_executions SET status='running', conversation_key=NULL WHERE id='e1'")
    assert _enqueue(conversation_key="session:k1") is True
    assert _scalar("SELECT conversation_key FROM schedule_executions WHERE id='e1'") is None


# ---------------------------------------------------------------------------
# Claim envelope + prompt
# ---------------------------------------------------------------------------


def test_claim_envelope_carries_persist_images_and_push_prompt_context(seed_agent):
    seed_agent(timeout=900)
    _row("e1", meta={
        "message": "hello", "timeout_seconds": 600, "persist_session": True,
        "images": _IMG, "schedule_context": {"name": "nightly", "cron": "0 3 * * *"},
        "attempt": 2, "resume_session_id": "uuid-1",
    }, email="u@example.com")
    from services import pull_coordination_service as pcs

    seen = {}

    def _compose(**kw):
        seen["ctx"] = kw["execution_context"]
        return "PROMPT"

    with patch.object(pcs, "_resolve_agent_runtime", return_value="claude-code"), \
         patch.object(pcs, "compose_system_prompt", side_effect=_compose), \
         patch.object(pcs, "record_worker_poll"):
        claim = pcs.claim_next_task(AGENT, "w1")

    payload = claim["envelope"]["payload"]
    assert payload["persist_session"] is True
    assert payload["images"] == _IMG
    assert payload["session_id"] == "uuid-1"
    ctx = seen["ctx"]
    assert ctx.source_user_email == "u@example.com"
    assert ctx.timeout_seconds == 600
    assert ctx.schedule_name == "nightly"
    assert ctx.schedule_cron == "0 3 * * *"
    assert ctx.attempt == 2


def test_claim_envelope_omits_the_fields_for_older_rows(seed_agent):
    seed_agent()
    _row("e1", meta={"message": "hello"})
    from services import pull_coordination_service as pcs

    with patch.object(pcs, "record_worker_poll"):
        payload = pcs.claim_next_task(AGENT, "w1")["envelope"]["payload"]
    assert "persist_session" not in payload
    assert "images" not in payload


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _run_worker(monkeypatch, payload):
    from agent_server.services import pull_worker as pw

    md = SimpleNamespace(model_dump=lambda: {"cost_usd": 0.0})
    runtime = SimpleNamespace(execute_headless=AsyncMock(return_value=("ok", [], md, "s")))
    stub = types.ModuleType("agent_server.services.runtime_adapter")
    stub.get_runtime = lambda: runtime
    monkeypatch.setitem(sys.modules, "agent_server.services.runtime_adapter", stub)
    monkeypatch.setattr(pw, "_deliver_result", AsyncMock(return_value=True))
    monkeypatch.setattr(pw, "_persist_pull_result", lambda *a, **k: None)
    monkeypatch.setattr(pw, "_delete_pull_result", lambda *a, **k: None)
    claim = {"execution_id": "e1", "claim_token": "t", "envelope": {"payload": payload}}
    asyncio.run(pw._run_and_report(claim, MagicMock(), "http://b", "k", AGENT))
    return runtime.execute_headless.call_args.kwargs


def test_worker_takes_persist_session_and_images_from_the_payload(monkeypatch):
    kw = _run_worker(monkeypatch, {"message": "m", "session_id": None,
                                   "persist_session": True, "images": _IMG})
    assert kw["persist_session"] is True       # cold Session turn still writes the JSONL
    assert kw["images"] == _IMG


def test_worker_falls_back_to_session_presence(monkeypatch):
    kw = _run_worker(monkeypatch, {"message": "m", "session_id": "uuid-1"})
    assert kw["persist_session"] is True
    assert kw["images"] is None
    kw = _run_worker(monkeypatch, {"message": "m", "session_id": "uuid-1",
                                   "persist_session": False})
    assert kw["persist_session"] is False


# ---------------------------------------------------------------------------
# Pull sink + row → result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_persists_compact_events_and_wakes_the_waiter(seed_agent, monkeypatch):
    seed_agent()
    _row("e1")
    import importlib

    # sys.modules entries: what apply_task_result's lazy import resolves.
    pcs = importlib.import_module("services.pull_coordination_service")
    sync_waiter = importlib.import_module("services.sync_waiter")
    tes = importlib.import_module("services.task_execution_service")

    monkeypatch.setattr(sync_waiter, "SYNC_WAITER_POLL_INTERVAL", 60.0)
    row = _db().claim_next_queued(AGENT, worker_id="w1", lease_seconds=900)
    waiter = asyncio.create_task(sync_waiter.wait_for_sync_terminal("e1", 5))
    await asyncio.sleep(0)

    events = [{"trigger": "auto", "pre_tokens": 150000}]
    outcome = pcs.apply_task_result(
        "e1", row["claim_token"], status="success", content="done",
        session_id="uuid-9", metadata={"compact_events": events},
    )
    assert outcome.kind == "applied"
    assert await asyncio.wait_for(waiter, 1) == {"result": None, "chat_session_id": None}

    result = tes.result_from_execution_row("e1")
    assert result.response == "done"
    assert result.session_id == "uuid-9"
    assert result.raw_response == {"metadata": {"compact_events": events}}


def test_row_result_maps_like_push(seed_agent):
    seed_agent()
    from services import task_execution_service as tes
    from services.execution_envelope import TaskExecutionErrorCode

    _row("f1", status="failed", error="[timeout] took too long", response="partial",
         session="dispatched")
    r = tes.result_from_execution_row("f1")
    assert r.error_code == TaskExecutionErrorCode.TIMEOUT
    assert r.error == "took too long"   # channel adapters post this text to people
    assert r.response == ""
    assert r.session_id is None
    assert r.raw_response == {}

    _row("f2", status="failed", error="[not_a_code] x", session="dispatched_async")
    r = tes.result_from_execution_row("f2")
    assert r.error_code is None and r.session_id is None
    assert r.error == "[not_a_code] x"

    # #679: a cancelled turn's partial reply is returned.
    _row("c1", status="cancelled", response="half an answer")
    assert tes.result_from_execution_row("c1").response == "half an answer"

    _row("s1", status="success", response="answer", session="uuid-1")
    r = tes.result_from_execution_row("s1")
    assert (r.response, r.session_id, r.error_code) == ("answer", "uuid-1", None)


# ---------------------------------------------------------------------------
# dispatch_and_await_terminal — claim phase
# ---------------------------------------------------------------------------


def _queued_service(eid):
    from services.execution_envelope import TaskExecutionResult

    svc = MagicMock()
    svc.execute_task = AsyncMock(return_value=TaskExecutionResult(
        execution_id=eid, status="queued", response=""))
    return svc


def _terminal_hooks(monkeypatch, tes):
    """Record the activity close and terminal event a CAS winner owes."""
    calls = []
    monkeypatch.setattr(tes.activity_service, "spawn_close_execution_activity",
                        lambda eid, st, **k: calls.append(("close", eid, getattr(st, "value", st))))
    monkeypatch.setattr(tes.event_dispatch_service, "spawn_task_terminal_event",
                        lambda a, eid, **k: calls.append(
                            ("event", eid, getattr(k["terminal_status"], "value", k["terminal_status"]))))
    return calls


@pytest.mark.asyncio
async def test_unclaimed_interactive_turn_fails_as_capacity(seed_agent, monkeypatch):
    seed_agent(timeout=1)
    _row("e1")
    from services import task_execution_service as tes
    from services.execution_envelope import TaskExecutionErrorCode

    monkeypatch.setattr(tes, "QUEUE_CLAIM_POLL_INTERVAL", 0.05)
    waited = AsyncMock()
    monkeypatch.setattr("services.sync_waiter.wait_for_sync_terminal", waited)
    hooks = _terminal_hooks(monkeypatch, tes)

    out = await tes.dispatch_and_await_terminal(
        agent_name=AGENT, message="m", triggered_by="session",
        service=_queued_service("e1"), conversation_key="session:k",
    )
    assert hooks == [("close", "e1", "failed"), ("event", "e1", "failed")]
    assert out.status == "failed"
    assert out.error_code == TaskExecutionErrorCode.CAPACITY
    assert out.error == "Agent at capacity (queued turn not claimed in 1s)"
    row = _db().get_execution("e1")
    assert (row.status, row.error) == ("failed", out.error)
    waited.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_that_loses_to_a_claim_waits_for_the_terminal(seed_agent, monkeypatch):
    seed_agent(timeout=1)
    _row("e1")
    from services import task_execution_service as tes

    db = _db()
    real_cancel = db.cancel_queued_execution

    def _claim_then_cancel(eid, reason="cancelled", status="cancelled"):
        db.claim_next_queued(AGENT, worker_id="w1", lease_seconds=900)
        return real_cancel(eid, reason, status)

    monkeypatch.setattr(tes, "QUEUE_CLAIM_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(tes.db, "cancel_queued_execution", _claim_then_cancel)
    waited = AsyncMock()
    monkeypatch.setattr("services.sync_waiter.wait_for_sync_terminal", waited)

    out = await tes.dispatch_and_await_terminal(
        agent_name=AGENT, message="m", triggered_by="mcp", service=_queued_service("e1"),
    )
    waited.assert_awaited_once_with("e1", 1 + 120.0)
    assert out.status == "running"      # rebuilt from the claimed row, never CAPACITY
    assert out.error_code is None


@pytest.mark.asyncio
async def test_turn_finished_before_the_waiter_registers_returns_at_once(seed_agent, monkeypatch):
    """A short turn can be claimed and finished inside one claim poll; the
    sink's wake-up then fires before anyone listens."""
    seed_agent(timeout=60)
    _row("e1", status="success", response="done")
    from services import task_execution_service as tes

    waited = AsyncMock(side_effect=AssertionError("must not wait on a finished row"))
    monkeypatch.setattr("services.sync_waiter.wait_for_sync_terminal", waited)

    out = await tes.dispatch_and_await_terminal(
        agent_name=AGENT, message="m", triggered_by="session", service=_queued_service("e1"),
    )
    assert (out.status, out.response) == ("success", "done")


@pytest.mark.asyncio
async def test_caller_going_away_cancels_the_queued_turn(seed_agent, monkeypatch):
    seed_agent(timeout=60)
    _row("e1")
    from services import task_execution_service as tes

    monkeypatch.setattr(tes, "QUEUE_CLAIM_POLL_INTERVAL", 0.05)
    hooks = _terminal_hooks(monkeypatch, tes)
    task = asyncio.ensure_future(tes.dispatch_and_await_terminal(
        agent_name=AGENT, message="m", triggered_by="paid", service=_queued_service("e1"),
    ))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _db().get_execution("e1").status == "cancelled"
    assert hooks == [("close", "e1", "cancelled"), ("event", "e1", "cancelled")]


@pytest.mark.asyncio
async def test_autonomous_trigger_skips_the_claim_phase(seed_agent, monkeypatch):
    """A row nobody is blocked on waits for its terminal, not for a claim.

    Driven with ``schedule``. It used to be driven with ``a2a``, which stopped
    being purely autonomous in abilityai/trinity-enterprise#679 (T6) — see the
    companion test below. The property under test is unchanged and still has
    members; what moved is which trigger demonstrates it.
    """
    seed_agent(timeout=1)
    _row("e1", trigger="schedule")
    from services import task_execution_service as tes

    waited = AsyncMock()
    monkeypatch.setattr("services.sync_waiter.wait_for_sync_terminal", waited)
    await tes.dispatch_and_await_terminal(
        agent_name=AGENT, message="m", triggered_by="schedule", service=_queued_service("e1"),
    )
    waited.assert_awaited_once()
    assert _db().get_execution("e1").status == "queued"


@pytest.mark.asyncio
async def test_a2a_takes_the_claim_phase(seed_agent, monkeypatch):
    """ent#679 T6: an inbound A2A caller is blocked in-line, so it waits for a claim.

    This is the principal-path effect of adding ``a2a`` to
    ``INTERACTIVE_TRIGGERS`` and it is deliberate: the JSON-RPC request is held
    open for the whole turn, so a row no worker claims within one agent timeout
    must come back FAILED/CAPACITY — the same answer push gives an agent with
    no free slot — rather than leaving the caller waiting out its RPC deadline
    on a row that was never going to run.
    """
    seed_agent(timeout=1)
    _row("e1", trigger="a2a")
    from services import task_execution_service as tes

    monkeypatch.setattr(tes, "QUEUE_CLAIM_POLL_INTERVAL", 0.05)
    waited = AsyncMock(side_effect=AssertionError("must not reach the terminal wait"))
    monkeypatch.setattr("services.sync_waiter.wait_for_sync_terminal", waited)
    out = await tes.dispatch_and_await_terminal(
        agent_name=AGENT, message="m", triggered_by="a2a", service=_queued_service("e1"),
    )
    assert out.status == "failed"
    assert out.error_code.value == "capacity"


@pytest.mark.asyncio
async def test_agent_chat_turn_takes_the_claim_phase(seed_agent, monkeypatch):
    """#3127: an agent-to-agent ``/chat`` turn (trigger ``agent``, autonomous)
    has a caller blocked on it. ``run_resumable_turn`` opts into the claim
    phase, so an unclaimed turn answers FAILED/CAPACITY within one agent
    timeout instead of running later for nobody."""
    seed_agent(timeout=1)
    _row("e1", trigger="agent")
    import importlib

    from services.execution_envelope import TaskExecutionResult

    sts = importlib.import_module("services.session_turn_service")
    tes = importlib.import_module("services.task_execution_service")
    seen = {}

    async def _execute(**kw):
        seen.update(kw)
        return TaskExecutionResult(execution_id="e1", status="queued", response="")

    monkeypatch.setattr(tes, "get_task_execution_service",
                        lambda: SimpleNamespace(execute_task=_execute))
    monkeypatch.setattr(tes, "QUEUE_CLAIM_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(sts, "ResumeLock", _NoLock)
    waited = AsyncMock(side_effect=AssertionError("must not reach the terminal wait"))
    monkeypatch.setattr("services.sync_waiter.wait_for_sync_terminal", waited)
    _terminal_hooks(monkeypatch, tes)

    turn = await sts.run_resumable_turn(
        agent_name=AGENT, session_key="chat:s1", message="hi", cached_uuid=None,
        triggered_by="agent", collaboration_activity_id="act-collab",
    )
    assert turn.result.status == "failed"
    assert turn.result.error_code.value == "capacity"
    assert seen["conversation_key"] == "session:chat:s1"
    assert seen["collaboration_activity_id"] == "act-collab"
    assert "caller_waiting" not in seen  # consumed by the adapter


def test_agent_chat_payload_carries_the_collaboration_activity(monkeypatch):
    """#3127: the pull sink can only close what the queued row carries."""
    from services import task_execution_service as tes

    monkeypatch.setattr(tes, "pull_owns_dispatch", lambda a, t: True)
    payload = tes.build_pull_queue_payload(
        agent_name=AGENT, triggered_by="agent", execution_id="e1", message="m",
        model=None, allowed_tools=None, system_prompt=None, timeout_seconds=60,
        resume_session_id=None, subscription_id=None, source_user_id=1,
        source_user_email=None, source_agent_name="caller", slot_already_held=False,
        collaboration_activity_id="act-collab",
    )
    assert payload.collaboration_activity_id == "act-collab"


@pytest.mark.parametrize("status", ["success", "failed"])
def test_sink_closes_the_collaboration_activity(status):
    """#3127: a turn whose caller gave up (504) still closes its collaboration
    activity at the terminal, instead of waiting for the 120-min backstop."""
    import services.pull_coordination_service as pcs

    execution = MagicMock(status="running", agent_name=AGENT,
                          backlog_metadata=json.dumps({"collaboration_activity_id": "act-collab"}))
    mock_db = MagicMock()
    mock_db.get_execution.return_value = execution
    mock_db.update_execution_status.return_value = True
    activity = MagicMock()
    with patch.object(pcs, "db", mock_db), \
         patch.object(pcs, "event_dispatch_service", MagicMock()), \
         patch.object(pcs, "channel_completion_report", MagicMock()), \
         patch.object(pcs, "subscription_auto_switch", MagicMock()), \
         patch.object(pcs, "_spawn_breaker_verdict", MagicMock()), \
         patch.object(pcs, "activity_service", activity):
        assert pcs.apply_task_result("e1", "tok", status=status, content="x").kind == "applied"
    ids = [c.kwargs.get("activity_id") for c in activity.spawn_close_execution_activity.call_args_list]
    assert ids == [None, "act-collab"]


@pytest.mark.asyncio
async def test_terminal_wait_timeout_text_matches_callers(seed_agent, monkeypatch):
    """public_chat_service and mcp_auth_service map on the substring "timed out"."""
    seed_agent(timeout=1)
    _row("e1", status="running")
    from services import task_execution_service as tes

    monkeypatch.setattr("services.sync_waiter.wait_for_sync_terminal",
                        AsyncMock(side_effect=asyncio.TimeoutError()))
    out = await tes.dispatch_and_await_terminal(
        agent_name=AGENT, message="m", triggered_by="public", service=_queued_service("e1"),
    )
    assert "timed out" in out.error


# ---------------------------------------------------------------------------
# Session tab: run_resumable_turn + lock TTL
# ---------------------------------------------------------------------------


class _NoLock:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_resumable_turn_on_pilot_returns_real_uuid_and_retries_cold(seed_agent, monkeypatch):
    seed_agent()
    import importlib

    from services.execution_envelope import TaskExecutionResult

    # sys.modules entries: what run_resumable_turn's lazy import resolves.
    sts = importlib.import_module("services.session_turn_service")
    tes = importlib.import_module("services.task_execution_service")
    _row("q1", status="failed", error="[agent_error] No conversation found with session ID uuid-old")
    _row("q2", status="success", response="hi again", session="uuid-new")
    calls = []

    async def _execute(**kw):
        calls.append(kw)
        eid = "q1" if len(calls) == 1 else "q2"
        return TaskExecutionResult(execution_id=eid, status="queued", response="")

    monkeypatch.setattr(tes, "get_task_execution_service",
                        lambda: SimpleNamespace(execute_task=_execute))
    monkeypatch.setattr(sts, "ResumeLock", _NoLock)
    monkeypatch.setattr(sts, "supports_session_resume", lambda a: True)
    monkeypatch.setattr("services.sync_waiter.wait_for_sync_terminal", AsyncMock())

    turn = await sts.run_resumable_turn(
        agent_name=AGENT, session_key="k1", message="hi", cached_uuid="uuid-old",
        triggered_by="session",
    )
    assert turn.fallback_fired is True
    assert turn.real_uuid == "uuid-new"
    assert turn.result.response == "hi again"
    assert [c["conversation_key"] for c in calls] == ["session:k1", "session:k1"]
    assert [c["resume_session_id"] for c in calls] == ["uuid-old", None]
    assert all(c["persist_session"] is True for c in calls)


def test_lock_ttl_adds_the_queue_allowance_on_a_pilot(seed_agent, monkeypatch):
    seed_agent(timeout=600)
    seed_agent(name="beta", timeout=7200)
    from services import session_turn_service as sts

    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
    assert sts.resolve_lock_ttl(AGENT) == 630
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", f"{AGENT},beta")
    # Two claim waits: the turn and its cold retry.
    assert sts.resolve_lock_ttl(AGENT) == 630 + 2 * 600
    assert sts.resolve_lock_ttl("beta") == sts.LOCK_TTL_FALLBACK + 2 * 7200


# ---------------------------------------------------------------------------
# Channel router key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_turn_is_keyed_by_its_chat_session(monkeypatch):
    import adapters.message_router as mr

    execute = AsyncMock(return_value=SimpleNamespace(
        status="success", response="ok", execution_id="e1", error=None, cost=0.0))
    monkeypatch.setattr(mr, "get_task_execution_service", lambda: MagicMock(execute_task=execute))
    monkeypatch.setattr(mr, "build_public_channel_caller_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "build_voice_capability_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "_get_channel_allowed_tools", lambda: ["WebSearch"])
    monkeypatch.setattr(mr.db, "get_public_channel_model", lambda agent: None)

    adapter = MagicMock()
    adapter.get_source_identifier.return_value = "telegram:1:2"
    message = SimpleNamespace(channel_id="2", thread_id="7", files=[], metadata={})
    router = mr.ChannelMessageRouter.__new__(mr.ChannelMessageRouter)
    await router._run_agent_task(
        adapter, message, AGENT, "tok", "telegram", False, None, None, "hi",
        None, [], session_id="pcs-42",
    )
    assert execute.await_args.kwargs["conversation_key"] == "channel:pcs-42"


# ---------------------------------------------------------------------------
# G-04 does not scan base64 image data
# ---------------------------------------------------------------------------


def _g04(meta: dict):
    from canary.invariants import g04_no_creds_in_backlog_metadata as g04

    agent = SimpleNamespace(name=AGENT, queued_exec_ids=["e1"],
                            queued_meta={"e1": {"backlog_metadata": json.dumps(meta)}})
    return g04.check(SimpleNamespace(agents=[agent], snapshot_time="t"))


def test_g04_ignores_images_but_still_scans_the_rest():
    assert _g04({"message": "hi", "images": _IMG}) == []
    hits = _g04({"system_prompt": "key AIzaSyExample", "images": _IMG})
    assert [v.observed_state["matched_pattern"] for v in hits] == ["google_api_key"]
    # Only each image's encoded bytes are skipped.
    for images, pattern in (
        ([{"media_type": "image/png", "data": "x", "note": "ghp_Abcdefgh"}], "github_pat"),
        (["xoxb-Abc123"], "slack_bot_token"),
    ):
        hits = _g04({"message": "hi", "images": images})
        assert [v.observed_state["matched_pattern"] for v in hits] == [pattern]


# ---------------------------------------------------------------------------
# Live-stream proxy
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status, chunks=()):
        self.status_code = status
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_text(self):
        for c in self._chunks:
            yield c


class _FakeAgentClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url):
        self.calls += 1
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_stream_holds_while_queued_then_attaches_after_the_claim(seed_agent, monkeypatch):
    seed_agent()
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT)
    _row("e1")
    client = _FakeAgentClient([_FakeResponse(404), _FakeResponse(200, ["data: x\n\n"])])
    from routers import chat
    from services import sync_waiter

    monkeypatch.setattr(sync_waiter, "STREAM_QUEUED_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sync_waiter, "STREAM_ATTACH_RETRY_INTERVAL", 0.01)
    monkeypatch.setattr(chat, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(chat, "agent_httpx_client", client)
    resp = await chat.stream_execution_log(
        execution_id="e1", name=AGENT, current_user=SimpleNamespace(id=1))
    body = resp.body_iterator

    assert await body.__anext__() == ": queued\n\n"
    assert await body.__anext__() == ": queued\n\n"      # still queued: still holding
    assert client.calls == 0
    _hrun("UPDATE schedule_executions SET status='running', "
          "lease_expires_at='2099-01-01T00:00:00Z' WHERE id='e1'")   # claimed
    rest = [c async for c in body]
    assert rest[-1] == "data: x\n\n"                      # 404 before registration retried
    assert client.calls == 2


@pytest.mark.asyncio
async def test_stream_off_pilot_is_unchanged(seed_agent, monkeypatch):
    seed_agent()
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
    _row("e1")
    client = _FakeAgentClient([_FakeResponse(404)])
    from routers import chat

    monkeypatch.setattr(chat, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(chat, "agent_httpx_client", client)
    resp = await chat.stream_execution_log(
        execution_id="e1", name=AGENT, current_user=SimpleNamespace(id=1))
    chunks = [c async for c in resp.body_iterator]
    assert not any(c.startswith(":") for c in chunks)
    assert '"retryable": true' in chunks[0] and "stream_end" in chunks[-1]
    assert client.calls == 1


def test_every_stream_proxy_holds_while_queued():
    """Structural: the three live-stream proxies share the hold."""
    from client_portal import router as portal_router
    from routers import chat, public

    for fn in (chat.stream_execution_log, public.public_stream_execution,
               portal_router.portal_stream_execution):
        src = inspect.getsource(fn)
        assert "wait_while_queued(" in src and "pull_queue_allowance(" in src, fn.__name__


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _running_with_key(key):
    return _scalar(
        "SELECT COUNT(*) FROM schedule_executions WHERE conversation_key=:k AND status='running'",
        k=key,
    )


def test_burst_of_one_conversation_is_claimed_one_at_a_time(seed_agent, monkeypatch):
    """AC: on a pilot, queued turns of one conversation never run concurrently,
    however many workers poll."""
    seed_agent()
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT)
    burst = [f"b{i}" for i in range(3)]
    for eid in burst + ["other"]:
        _row(eid, status="running")
        key = "session:other" if eid == "other" else "session:k"
        assert _enqueue(eid, conversation_key=key) is True
    from services import pull_coordination_service as pcs

    def _claim(worker):
        with patch.object(pcs, "record_worker_poll"):
            c = pcs.claim_next_task(AGENT, worker)
        return c["execution_id"] if c else None

    done = []
    first = _claim("w1")
    assert first in burst
    assert _claim("w2") == "other"            # another conversation is not held back
    assert _claim("w3") is None               # the rest of the burst waits
    assert _running_with_key("session:k") == 1
    current = first
    while True:
        _hrun("UPDATE schedule_executions SET status='success' WHERE id=:id", id=current)
        done.append(current)
        nxt = _claim("w1")
        if nxt is None:
            break
        assert _running_with_key("session:k") == 1
        assert _claim("w2") is None
        current = nxt
    assert sorted(done) == burst


class _FakeRedis:
    """SET NX EX + the token-checked release: what ResumeLock uses."""

    def __init__(self):
        self.store = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def eval(self, _lua, _n, key, token):
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


@pytest.mark.asyncio
async def test_session_turns_of_one_session_never_overlap_on_a_pilot(seed_agent, monkeypatch):
    """AC: the ResumeLock is held across the queued wait, so a second turn of
    the same Session cannot even enqueue until the first reached its terminal."""
    seed_agent(timeout=30)
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT)
    import importlib

    from services.execution_envelope import TaskExecutionResult

    sts = importlib.import_module("services.session_turn_service")
    tes = importlib.import_module("services.task_execution_service")
    sync_waiter = importlib.import_module("services.sync_waiter")
    db = _db()
    events = []
    first_enqueued = asyncio.Event()

    async def _execute(**kw):
        eid = f"t{sum(1 for e in events if e[0] == 'enqueue') + 1}"
        _row(eid, status="queued", trigger="session")
        _hrun("UPDATE schedule_executions SET conversation_key=:k WHERE id=:id",
              k=kw["conversation_key"], id=eid)
        events.append(("enqueue", eid))
        first_enqueued.set()
        return TaskExecutionResult(execution_id=eid, status="queued", response="")

    async def _worker():
        finished = 0
        while finished < 2:
            row = db.claim_next_queued(AGENT, worker_id="w1", lease_seconds=900)
            if row is None:
                await asyncio.sleep(0.01)
                continue
            events.append(("claim", row["id"]))
            await asyncio.sleep(0.05)
            _hrun("UPDATE schedule_executions SET status='success', response='ok', "
                  "claude_session_id='uuid-1' WHERE id=:id", id=row["id"])
            events.append(("done", row["id"]))
            finished += 1

    monkeypatch.setattr(tes, "get_task_execution_service",
                        lambda: SimpleNamespace(execute_task=_execute))
    monkeypatch.setattr(tes, "QUEUE_CLAIM_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sync_waiter, "SYNC_WAITER_POLL_INTERVAL", 0.01)
    fake = _FakeRedis()
    monkeypatch.setattr(sts, "get_async_redis", lambda: fake)
    monkeypatch.setattr(sts, "LOCK_POLL_INTERVAL_SECONDS", 0.01)

    def _turn():
        return sts.run_resumable_turn(
            agent_name=AGENT, session_key="k1", message="hi", cached_uuid=None,
            triggered_by="session",
        )

    worker = asyncio.create_task(_worker())
    turn1 = asyncio.create_task(_turn())
    await first_enqueued.wait()
    turn2 = asyncio.create_task(_turn())
    r1, r2 = await asyncio.wait_for(asyncio.gather(turn1, turn2), 5)
    await asyncio.wait_for(worker, 5)

    assert [e for e in events if e[1] == "t1"] == [("enqueue", "t1"), ("claim", "t1"), ("done", "t1")]
    assert events.index(("enqueue", "t2")) > events.index(("done", "t1"))
    assert r1.result.response == r2.result.response == "ok"
    assert fake.store == {}                   # both locks released


def test_awaits_claim_covers_the_pre_enqueue_moment(seed_agent):
    """A pre-created portal row is `running` for a moment before it is
    enqueued; the stream must keep holding through it (#3114 live check)."""
    seed_agent()
    db = _db()
    _row("pre", status="running")                                  # pre-created
    _row("q", status="queued")
    _row("pushed", status="running", session="dispatched")         # push sentinel
    _row("claimed", status="running")
    _hrun("UPDATE schedule_executions SET lease_expires_at='2099-01-01T00:00:00Z' "
          "WHERE id='claimed'")
    _row("done", status="success")
    assert [db.execution_awaits_claim(e) for e in ("pre", "q", "pushed", "claimed", "done", "gone")] == [
        True, True, False, False, False, False]


@pytest.mark.asyncio
async def test_stream_holds_from_pre_created_row_until_the_claim(seed_agent, monkeypatch):
    seed_agent()
    _row("e2", status="running")                                   # not yet enqueued
    from services import sync_waiter

    monkeypatch.setattr(sync_waiter, "STREAM_QUEUED_POLL_INTERVAL", 0.01)
    hold = sync_waiter.wait_while_queued("e2", budget=5)
    assert await hold.__anext__() == ": queued\n\n"
    _hrun("UPDATE schedule_executions SET status='queued' WHERE id='e2'")
    assert await hold.__anext__() == ": queued\n\n"
    _hrun("UPDATE schedule_executions SET status='running', "
          "lease_expires_at='2099-01-01T00:00:00Z' WHERE id='e2'")
    assert [t async for t in hold] == []
