"""``POST /chat`` on the durable queue for pull pilots (#3127).

What is pinned here:

* Admission: a pilot's ``/chat`` takes no slot (``capacity=None``, a
  ``queued_persistent`` result); a non-pilot still acquires with
  ``queue_in_memory``.
* The pulled turn runs through the resumable-turn engine: the admitted row id,
  the session's cached Claude id, ``persist_session`` and a per-chat-session
  conversation key reach the dispatch; nothing is pushed, nothing is marked
  dispatched, and no second terminal is written.
* Success persists the assistant message, caches the Claude id, completes the
  idempotency claim and closes the collaboration activity. Turn 2 resumes
  turn 1's id; a stale id clears the cache and the cold retry keeps the
  chain depth.
* Failures: unclaimed (capacity) → 429 with the claim released; wait timeout
  with the row still running → receipt + 504; resume lock busy → FAILED + 429.
* ``chat_sessions.cached_claude_session_id``: accessors, keep set, reset,
  pilot history, SQLite migration idempotency.
* The app's middleware stack does not cancel a handler whose client
  disconnects, so an MCP abort leaves the queued turn running.

Runs against the real db layer through db_harness (SQLite; Postgres too when
TEST_POSTGRES_URL is set).
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _scalar  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT = "alpha"
UUID1 = "11111111-2222-3333-4444-555555555555"
UUID2 = "66666666-7777-8888-9999-aaaaaaaaaaaa"


@pytest.fixture
def tmp_db(db_backend, monkeypatch):
    for mod in ("db.connection", "db.schedules", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    return db_backend


def _db():
    from database import db

    return db


def _user(uid=1):
    return SimpleNamespace(
        id=uid, email=f"u{uid}@example.com", username=f"u{uid}",
        mcp_key_id=None, mcp_key_name=None,
    )


def _result(status="success", *, eid="e1", response="hi back", session_id=UUID1,
            error=None, error_code=None):
    from services.execution_envelope import TaskExecutionResult

    return TaskExecutionResult(
        execution_id=eid, status=status, response=response if status == "success" else "",
        cost=0.01, context_used=1200, context_max=200000, session_id=session_id,
        execution_log='[{"type": "text"}]',
        raw_response={"metadata": {"compact_events": []}},
        error=error, error_code=error_code,
    )


@pytest.fixture
def turn_env(tmp_db, monkeypatch):
    """A pilot chat session + its admitted row, with the queue dispatch, Redis
    and Docker stubbed. ``dispatch`` is the queue adapter mock."""
    import importlib

    from db.write_params import TaskExecutionFields

    # sys.modules is the authority: run_resumable_turn imports the adapter
    # lazily from there, and ce reaches the engine through its own attribute.
    ce = importlib.import_module("services.chat_execution_service")
    sts = ce.session_turn_service
    tes = importlib.import_module("services.task_execution_service")

    db = _db()
    monkeypatch.setattr(ce, "db", db)
    monkeypatch.setattr(sts, "db", db)
    monkeypatch.setattr(sts, "get_async_redis", lambda: None)
    monkeypatch.setattr(sts, "supports_session_resume", lambda name: True)
    monkeypatch.setattr(sts, "resolve_lock_ttl", lambda name: 60)

    session = db.get_or_create_chat_session(AGENT, 1, "u1@example.com")
    row = db.create_task_execution(
        AGENT, "hello", triggered_by="agent",
        fields=TaskExecutionFields(chain_depth=2),
    )

    dispatch = AsyncMock(return_value=_result(eid=row.id))
    monkeypatch.setattr(tes, "dispatch_and_await_terminal", dispatch)
    push = AsyncMock()
    monkeypatch.setattr(ce, "agent_post_with_retry", push)
    activity = MagicMock(complete_activity=AsyncMock(), close_execution_activity=AsyncMock())
    monkeypatch.setattr(ce, "activity_service", activity)
    idem_svc = MagicMock()
    monkeypatch.setattr(ce, "idempotency_service", idem_svc)
    marked = MagicMock()
    monkeypatch.setattr(db, "mark_execution_dispatched", marked)

    def run(eid=None, user=None, isolated_session=False):
        from models import ChatMessageRequest

        return asyncio.run(ce.run_chat_turn(
            name=AGENT,
            request=ChatMessageRequest(message="hello"),
            current_user=user or _user(),
            x_source_agent="caller",
            triggered_by="agent",
            task_execution_id=eid or row.id,
            _chat_subscription_id=None,
            chat_activity_id="act-chat",
            collaboration_activity_id="act-collab",
            session=session,
            execution=SimpleNamespace(id="q1"),
            queue_result="queued",
            is_queued=False,
            chat_timeout=900,
            idem="IDEM",
            capacity=None,
            chain_depth=2,
            isolated_session=isolated_session,
        ))

    return SimpleNamespace(
        db=db, ce=ce, sts=sts, session=session, row=row, dispatch=dispatch,
        push=push, activity=activity, idem=idem_svc, marked=marked, run=run,
    )


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def _admit(monkeypatch, pilot: bool):
    import services.dispatch_admission_service as da
    from models import ChatMessageRequest

    if pilot:
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT)
    else:
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
    idem = MagicMock(replay=False)
    isvc = MagicMock()
    isvc.begin.return_value = idem
    cap = MagicMock(acquire=AsyncMock(return_value=SimpleNamespace(
        state="admitted", queue_position=0)))
    db = MagicMock()
    db.get_execution_timeout.return_value = 900
    db.get_max_parallel_tasks.return_value = 3
    audit = MagicMock(log=AsyncMock())
    with patch.object(da, "idempotency_service", isvc), \
         patch.object(da, "dispatch_breaker_active", return_value=False), \
         patch.object(da, "get_capacity_manager", return_value=cap), \
         patch.object(da, "platform_audit_service", audit), \
         patch.object(da, "db", db):
        admission = asyncio.run(da.admit_chat_request(
            name=AGENT, request=ChatMessageRequest(message="hi"),
            current_user=_user(), x_source_agent=None, x_via_mcp="true",
            idempotency_key="k1",
        ))
    return admission, cap, isvc, audit


def test_pilot_admission_skips_acquire(monkeypatch):
    admission, cap, isvc, audit = _admit(monkeypatch, pilot=True)
    cap.acquire.assert_not_awaited()
    isvc.begin.assert_called_once()
    assert admission.capacity is None
    assert admission.capacity_result.state == "queued_persistent"
    assert admission.queue_result == "queued"
    audit.log.assert_awaited_once()


def test_pilot_admission_carries_the_gate_decision(monkeypatch):
    """ent#751/#752: the row's setup records a self-approval from
    ``admission.gate``; the pilot branch must hand it on like the push branch."""
    import services.dispatch_admission_service as da

    decision = object()
    monkeypatch.setattr(da.skill_gate_service, "enforce", AsyncMock(return_value=decision))
    admission, _, _, _ = _admit(monkeypatch, pilot=True)
    assert admission.gate is decision


def test_non_pilot_admission_acquires_in_memory(monkeypatch):
    admission, cap, _, _ = _admit(monkeypatch, pilot=False)
    cap.acquire.assert_awaited_once()
    assert cap.acquire.await_args.kwargs["overflow_policy"] == "queue_in_memory"
    assert admission.capacity is cap


def test_chat_trigger_is_shared():
    from services.chat_signals import chat_trigger

    assert chat_trigger("a", "true") == "agent"
    assert chat_trigger(None, "true") == "mcp"
    assert chat_trigger(None, None) == "chat"


# ---------------------------------------------------------------------------
# The pulled turn
# ---------------------------------------------------------------------------


def test_pilot_turn_dispatches_through_the_queue(turn_env):
    env = turn_env
    body = env.run()

    kw = env.dispatch.await_args.kwargs
    assert kw["execution_id"] == env.row.id
    assert kw["resume_session_id"] is None
    assert kw["persist_session"] is True
    assert kw["conversation_key"] == f"session:chat:{env.session.id}"
    assert kw["triggered_by"] == "agent"
    assert kw["chain_depth"] == 2
    # ent#751: admission gated with the authenticated requester; the backstop
    # must not gate again with one rebuilt from the row.
    assert kw["gate_checked"] is True
    assert kw["request_text"] == "hello"
    # The trigger is autonomous, so the claim wait is opted into explicitly,
    # and the sink closes the collaboration activity from the payload.
    assert kw["caller_waiting"] is True
    assert kw["collaboration_activity_id"] == "act-collab"
    env.push.assert_not_awaited()
    env.marked.assert_not_called()
    # The pull sink wrote the terminal; the turn writes none.
    assert _scalar("SELECT status FROM schedule_executions WHERE id = :i", i=env.row.id) != "success"

    assert body["response"] == "hi back"
    assert body["metadata"]["session_id"] == UUID1
    assert body["session"]["context_tokens"] == 1200
    assert body["execution"]["task_execution_id"] == env.row.id
    assert body["execution_log"] == [{"type": "text"}]


def test_success_persists_message_caches_id_and_completes(turn_env):
    env = turn_env
    body = env.run()

    msgs = env.db.get_chat_messages(env.session.id)
    assert [m.role for m in msgs] == ["assistant"]
    assert msgs[0].content == "hi back"
    assert env.db.get_chat_session_claude_id(env.session.id) == UUID1
    env.idem.complete.assert_called_once_with("IDEM", env.row.id, body)
    env.idem.fail.assert_not_called()
    closed = {c.kwargs["activity_id"]: c.kwargs["status"]
              for c in env.activity.complete_activity.await_args_list}
    assert closed == {"act-chat": "completed", "act-collab": "completed"}


def test_malformed_session_id_is_not_cached(turn_env):
    env = turn_env
    env.dispatch.return_value = _result(eid=env.row.id, session_id="not-a-uuid")
    env.run()
    assert env.db.get_chat_session_claude_id(env.session.id) is None


def test_turn_two_resumes_turn_one(turn_env):
    env = turn_env
    env.run()
    env.dispatch.return_value = _result(eid="e2", session_id=UUID1)
    env.run(eid="e2")
    assert env.dispatch.await_args.kwargs["resume_session_id"] == UUID1


def test_other_user_gets_their_own_conversation(turn_env):
    env = turn_env
    env.run()
    other = env.db.get_or_create_chat_session(AGENT, 2, "u2@example.com")
    assert other.id != env.session.id
    assert env.db.get_chat_session_claude_id(other.id) is None


def test_stale_id_clears_cache_and_retry_keeps_depth(turn_env):
    env = turn_env
    env.db.set_chat_session_claude_id(env.session.id, UUID1)
    seen = {}

    async def _dispatch(**kw):
        if kw.get("resume_session_id"):
            seen["cache_at_failure"] = env.db.get_chat_session_claude_id(env.session.id)
            return _result("failed", eid=env.row.id,
                           error="No conversation found with session ID: x")
        seen["cache_at_retry"] = env.db.get_chat_session_claude_id(env.session.id)
        seen["retry_kwargs"] = kw
        return _result(eid="e-retry", session_id=UUID2)

    env.dispatch.side_effect = _dispatch
    body = env.run()

    assert seen["cache_at_retry"] is None
    assert "execution_id" not in seen["retry_kwargs"]
    assert seen["retry_kwargs"]["chain_depth"] == 2
    assert env.db.get_chat_session_claude_id(env.session.id) == UUID2
    assert body["execution"]["task_execution_id"] == "e-retry"
    env.idem.complete.assert_called_once_with("IDEM", "e-retry", body)


def test_execute_task_stamps_chain_depth_on_a_row_it_creates():
    """The cold retry's row is created by execute_task (#3127 C2)."""
    from services import task_execution_service as tes

    class _Stop(Exception):
        pass

    db = MagicMock()
    db.get_execution_timeout.return_value = 900
    db.create_task_execution.return_value = SimpleNamespace(id="new")
    with patch.object(tes, "db", db), \
         patch.object(tes, "dispatch_breaker_active", return_value=False), \
         patch.object(tes, "dispatch_async_eligible", return_value=False), \
         patch.object(tes, "build_pull_queue_payload", side_effect=_Stop), \
         patch.object(tes.settings_service, "get_platform_default_model", return_value="m"):
        with pytest.raises(_Stop):
            asyncio.run(tes.TaskExecutionService().execute_task(
                agent_name=AGENT, message="m", triggered_by="agent", chain_depth=3,
            ))
    assert db.create_task_execution.call_args.kwargs["fields"].chain_depth == 3


def test_unclaimed_turn_is_429_and_releases_the_claim(turn_env):
    from services.chat_signals import ChatDispatchError
    from services.execution_envelope import TaskExecutionErrorCode

    env = turn_env
    env.dispatch.return_value = _result(
        "failed", eid=env.row.id,
        error="Agent at capacity (queued turn not claimed in 900s)",
        error_code=TaskExecutionErrorCode.CAPACITY,
    )
    with pytest.raises(ChatDispatchError) as exc:
        env.run()
    assert exc.value.status_code == 429
    env.idem.fail.assert_called_once_with("IDEM")
    env.idem.complete.assert_not_called()
    assert env.db.get_chat_messages(env.session.id) == []
    states = {c.kwargs["status"] for c in env.activity.complete_activity.await_args_list}
    assert states == {"failed"}


def test_timeout_with_row_running_returns_receipt_and_504(turn_env):
    from services.chat_signals import ChatDispatchError
    from services.execution_envelope import TaskExecutionErrorCode

    env = turn_env
    env.dispatch.return_value = _result(
        "failed", eid=env.row.id, error="Execution timed out after 1020s waiting",
        error_code=TaskExecutionErrorCode.TIMEOUT,
    )
    with pytest.raises(ChatDispatchError) as exc:
        env.run()
    assert exc.value.status_code == 504
    env.idem.fail.assert_not_called()
    (_, eid, receipt), _ = env.idem.complete.call_args
    assert eid == env.row.id
    assert receipt["status"] == "queued_timeout"


def test_isolated_turn_starts_cold_and_is_not_cached(turn_env):
    """ent#752: a self-approved gated-skill turn never resumes the session's
    conversation, and the next turn never resumes it."""
    env = turn_env
    env.db.set_chat_session_claude_id(env.session.id, UUID2)
    env.run(isolated_session=True)
    assert env.dispatch.await_args.kwargs["resume_session_id"] is None
    assert env.db.get_chat_session_claude_id(env.session.id) == UUID2


def test_lock_wait_stays_under_the_no_session_sweep(turn_env, monkeypatch):
    """A second turn of one session waits on the lock while its admission row
    is `running` with no Claude session. A wait past the #106 sweep fails that
    row, and the enqueue CAS then refuses it, so the caller gets a 429 for a
    turn that never ran."""
    from services.cleanup_service import NO_SESSION_TIMEOUT_SECONDS

    env = turn_env
    seen = {}
    real = env.sts.ResumeLock

    def _lock(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(env.sts, "ResumeLock", _lock)
    env.run()
    assert seen.get("wait_seconds") is None
    assert env.sts.LOCK_WAIT_TOTAL_SECONDS < NO_SESSION_TIMEOUT_SECONDS


def test_lock_busy_on_a_terminal_row_emits_nothing(turn_env, monkeypatch):
    from services.chat_signals import ChatDispatchError

    env = turn_env

    async def _busy(**kw):
        raise env.sts.ResumeLockBusy("session_lock:cold:x")

    monkeypatch.setattr(env.sts, "run_resumable_turn", _busy)
    monkeypatch.setattr(env.db, "update_execution_status", lambda **kw: False)
    events = MagicMock()
    monkeypatch.setattr(env.ce, "event_dispatch_service", events)
    with pytest.raises(ChatDispatchError):
        env.run()
    events.spawn_task_terminal_event.assert_not_called()
    env.activity.close_execution_activity.assert_not_awaited()


def test_backlog_full_is_429_capacity(turn_env):
    from services.chat_signals import ChatDispatchError
    from services.execution_envelope import TaskExecutionErrorCode

    env = turn_env
    env.dispatch.return_value = _result(
        "failed", eid=env.row.id,
        error="Agent backlog full (max_backlog_depth reached); queued task rejected",
        error_code=TaskExecutionErrorCode.CAPACITY,
    )
    with pytest.raises(ChatDispatchError) as exc:
        env.run()
    assert exc.value.status_code == 429
    assert exc.value.headers["X-Trinity-Error-Code"] == "capacity"


def test_lock_busy_fails_the_row_and_answers_429(turn_env, monkeypatch):
    from services.chat_signals import ChatDispatchError

    env = turn_env

    async def _busy(**kw):
        raise env.sts.ResumeLockBusy("session_lock:cold:x")

    monkeypatch.setattr(env.sts, "run_resumable_turn", _busy)
    events = MagicMock()
    monkeypatch.setattr(env.ce, "event_dispatch_service", events)
    with pytest.raises(ChatDispatchError) as exc:
        env.run()
    assert exc.value.status_code == 429
    assert exc.value.headers["X-Trinity-Error-Code"] == "capacity"
    events.spawn_task_terminal_event.assert_called_once()
    assert events.spawn_task_terminal_event.call_args.args == (AGENT, env.row.id)
    assert _scalar("SELECT status FROM schedule_executions WHERE id = :i", i=env.row.id) == "failed"
    env.idem.fail.assert_called_once_with("IDEM")
    chat_close = env.activity.close_execution_activity.await_args
    assert chat_close.args == (env.row.id, "failed")
    assert chat_close.kwargs["activity_id"] == "act-chat"
    collab = env.activity.complete_activity.await_args
    assert (collab.kwargs["activity_id"], collab.kwargs["status"]) == ("act-collab", "failed")


def test_non_pilot_turn_still_pushes(monkeypatch):
    """``capacity`` present → the push body runs and the slot is released."""
    import services.chat_execution_service as ce
    from models import ChatMessageRequest

    pulled = AsyncMock()
    monkeypatch.setattr(ce, "run_pulled_chat_turn", pulled)
    monkeypatch.setattr(ce, "build_chat_payload", lambda **kw: {})
    monkeypatch.setattr(ce, "agent_post_with_retry", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(ce, "_finalize_chat_success", AsyncMock(return_value={"ok": 1}))
    monkeypatch.setattr(ce, "idempotency_service", MagicMock())
    cap = MagicMock(release=AsyncMock())
    out = asyncio.run(ce.run_chat_turn(
        name=AGENT, request=ChatMessageRequest(message="hi"), current_user=_user(),
        x_source_agent=None, triggered_by="chat", task_execution_id="e1",
        _chat_subscription_id=None, chat_activity_id="a", collaboration_activity_id=None,
        session=SimpleNamespace(id="s"), execution=SimpleNamespace(id="q"),
        queue_result="running", is_queued=False, chat_timeout=900, idem="I",
        capacity=cap,
    ))
    assert out == {"ok": 1}
    pulled.assert_not_awaited()
    ce.agent_post_with_retry.assert_awaited_once()
    cap.release.assert_awaited_once()


# ---------------------------------------------------------------------------
# chat_sessions.cached_claude_session_id
# ---------------------------------------------------------------------------


def test_keep_set_lists_the_agents_cached_ids(tmp_db):
    from db.chat import ChatOperations

    db = _db()
    a = db.get_or_create_chat_session(AGENT, 1, "u1@example.com")
    b = db.get_or_create_chat_session(AGENT, 2, "u2@example.com")
    db.get_or_create_chat_session(AGENT, 3, "u3@example.com")
    other = db.get_or_create_chat_session("beta", 1, "u1@example.com")
    db.set_chat_session_claude_id(a.id, UUID1)
    db.set_chat_session_claude_id(b.id, UUID2)
    db.set_chat_session_claude_id(other.id, "ffffffff-0000-0000-0000-000000000000")
    assert sorted(ChatOperations().list_active_claude_session_ids(AGENT)) == sorted([UUID1, UUID2])


def test_reaper_keeps_a_chat_sessions_jsonl(tmp_db, monkeypatch):
    from services import session_cleanup_service as cleanup
    from client_portal import db as portal_db
    from shared_sessions import db as rooms_db

    db = _db()
    s = db.get_or_create_chat_session(AGENT, 1, "u1@example.com")
    db.set_chat_session_claude_id(s.id, UUID1)
    monkeypatch.setattr(cleanup, "db", db)
    monkeypatch.setattr(db, "list_active_claude_session_ids", lambda agent: [])
    for mod in (portal_db, rooms_db):
        monkeypatch.setattr(mod, "list_active_claude_session_ids", lambda agent: [])
    removed = []

    async def _exec(container, cmd, timeout=30, **kw):
        if cmd.startswith("rm -f"):
            removed.append(cmd)
            return {"exit_code": 0, "output": ""}
        if "chat-session.json" in cmd:
            return {"exit_code": 0, "output": "__NO_CHAT_SESSION__\n"}
        return {"exit_code": 0, "output": f"{UUID1}.jsonl 1000000000\n{UUID2}.jsonl 1000000000"}

    monkeypatch.setattr(cleanup, "execute_command_in_container", _exec)
    per = asyncio.run(cleanup.SessionCleanupService()._sweep_agent(AGENT))
    assert per["deleted"] == 1
    assert not any(UUID1 in c for c in removed)


def _router_container():
    return MagicMock(status="running")


def test_reset_clears_the_agents_cached_ids(tmp_db, monkeypatch):
    import routers.chat as rc

    db = _db()
    a = db.get_or_create_chat_session(AGENT, 1, "u1@example.com")
    other = db.get_or_create_chat_session("beta", 1, "u1@example.com")
    db.set_chat_session_claude_id(a.id, UUID1)
    db.set_chat_session_claude_id(other.id, UUID2)
    monkeypatch.setattr(rc, "db", db)
    monkeypatch.setattr(rc, "get_agent_container", lambda name: _router_container())
    client = MagicMock()
    client.delete = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"ok": 1}))
    cm = MagicMock(__aenter__=AsyncMock(return_value=client), __aexit__=AsyncMock(return_value=False))
    monkeypatch.setattr(rc, "agent_httpx_client", lambda name: cm)

    ui_only = db.get_or_create_chat_session(AGENT, 2, "u2@example.com")   # never used by /chat

    asyncio.run(rc.reset_agent_chat_history(name=AGENT, current_user=_user()))
    assert db.get_chat_session_claude_id(a.id) is None
    assert db.get_chat_session_claude_id(other.id) == UUID2
    # The reset session is closed, so the pilot history starts empty.
    assert db.get_agent_chat_sessions(AGENT, user_id=1, status="active") == []
    assert [x.id for x in db.get_agent_chat_sessions(AGENT, user_id=2, status="active")] == [ui_only.id]
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT)
    assert asyncio.run(rc.get_agent_chat_history(name=AGENT, current_user=_user())) == []


def test_pilot_history_comes_from_the_callers_session(tmp_db, monkeypatch):
    import routers.chat as rc

    db = _db()
    mine = db.get_or_create_chat_session(AGENT, 1, "u1@example.com")
    theirs = db.get_or_create_chat_session(AGENT, 2, "u2@example.com")
    db.add_chat_message(mine.id, AGENT, 1, "u1@example.com", "user", "q1")
    db.add_chat_message(mine.id, AGENT, 1, "u1@example.com", "assistant", "a1")
    db.add_chat_message(theirs.id, AGENT, 2, "u2@example.com", "user", "secret")
    monkeypatch.setattr(rc, "db", db)
    monkeypatch.setattr(rc, "get_agent_container", lambda name: _router_container())
    monkeypatch.setattr(rc, "agent_httpx_client", MagicMock(side_effect=AssertionError("pushed")))
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT)

    out = asyncio.run(rc.get_agent_chat_history(name=AGENT, current_user=_user(1)))
    assert [(m["role"], m["content"]) for m in out] == [("user", "q1"), ("assistant", "a1")]

    db.close_chat_session(mine.id)
    assert asyncio.run(rc.get_agent_chat_history(name=AGENT, current_user=_user(1))) == []


def test_sqlite_migration_is_idempotent(tmp_path):
    from db.migrations import MIGRATIONS, _migrate_chat_session_claude_id

    assert ("chat_session_claude_id", _migrate_chat_session_claude_id) in MIGRATIONS
    conn = sqlite3.connect(tmp_path / "m.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE chat_sessions (id TEXT PRIMARY KEY, agent_name TEXT)")
    _migrate_chat_session_claude_id(cur, conn)
    _migrate_chat_session_claude_id(cur, conn)
    cols = [r[1] for r in cur.execute("PRAGMA table_info(chat_sessions)")]
    assert cols.count("cached_claude_session_id") == 1


# ---------------------------------------------------------------------------
# Client disconnect
# ---------------------------------------------------------------------------


def test_disconnect_does_not_cancel_the_handler():
    """The app's HTTP middleware (CORS + two ``@app.middleware("http")``) does
    not cancel a handler when its client goes away. The MCP server aborts a
    sync /chat at 25s and answers with a receipt; the queued turn must keep
    running, so #3114's caller-gone cancel must not fire on that abort."""
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI()
    outcome = {}

    @app.post("/chat")
    async def chat():
        try:
            await asyncio.sleep(0.6)
            outcome["handler"] = "completed"
            return {"ok": True}
        except asyncio.CancelledError:
            outcome["handler"] = "cancelled"
            raise

    app.add_middleware(CORSMiddleware, allow_origins=["*"])

    @app.middleware("http")
    async def _a(request: Request, call_next):
        return await call_next(request)

    @app.middleware("http")
    async def _b(request: Request, call_next):
        return await call_next(request)

    async def main():
        msgs = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive():
            if msgs:
                return msgs.pop(0)
            await asyncio.sleep(0.1)
            return {"type": "http.disconnect"}

        async def send(_m):
            return None

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/chat", "raw_path": b"/chat",
            "query_string": b"", "headers": [(b"host", b"x")],
            "client": ("127.0.0.1", 1), "server": ("x", 80), "root_path": "",
        }
        try:
            await app(scope, receive, send)
        except BaseException:  # noqa: BLE001 — only the handler outcome matters
            pass
        await asyncio.sleep(0.8)

    asyncio.run(main())
    assert outcome.get("handler") == "completed"


def test_main_registers_only_the_checked_http_middleware():
    """The disconnect test above models main.py's stack. A new middleware type
    there must be re-checked against client disconnects."""
    src = (Path(_BACKEND_STR) / "main.py").read_text()
    assert src.count('@app.middleware("http")') == 2
    assert src.count("app.add_middleware(") == 2  # CORSMiddleware, HostHeaderGuard
