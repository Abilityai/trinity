"""#3380 — a watchdog-claimed /chat turn writes its reply into the chat thread.

Before: the #2944 watchdog claimed a finished turn whose waiting connection a
backend restart had destroyed and finalized the execution row (SUCCESS, cost,
response), but the assistant `chat_messages` row is written only by the live
path (`chat_execution_service._finalize_chat_success`). The row said SUCCESS
and the thread showed nothing, because nothing linked the row to its thread.

Now `prepare_chat_execution` stamps `schedule_executions.chat_session_id`, and
`cleanup_service._claim_retained_result` writes the reply from the persisted
row after a WON SUCCESS. A lost CAS, a failed turn and a turn with no thread
(`/task`, schedules) write nothing.

Everything runs on a real SQLite built by the platform's own `init_schema`.
`apply_result` is stood in by a fake that performs the same terminal CAS on the
real row, so the reply is read back from what the claim actually persisted.
"""
import asyncio
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


@pytest.fixture()
def real_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-3380.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.schema import init_schema
    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    from database import db
    return db


def _agent():
    # `db.engine` is cached across tests, so every test uses its own agent name.
    return f"agent-{uuid.uuid4().hex[:8]}"


def _cs():
    from services import cleanup_service as cs  # noqa: WPS433 — lazy on purpose
    return cs


def _record(*, status="success", response="the recovered answer"):
    return {
        "status": status, "response": response,
        "error": None if status == "success" else "boom", "error_code": None,
        "terminal_reason": "completed", "metadata": {"cost_usd": 0.42},
        "execution_log": [{"type": "result"}], "session_id": None,
        "retained_at": (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat(),
        "execution_time_ms": 1234,
    }


def _chat_turn(db, agent, *, linked=True):
    """A running /chat row and its thread, as `prepare_chat_execution` leaves them."""
    session = db.get_or_create_chat_session(agent_name=agent, user_id=7, user_email="u@example.com")
    db.add_chat_message(
        session_id=session.id, agent_name=agent, user_id=7, user_email="u@example.com",
        role="user", content="what is the answer?",
    )
    row = db.create_task_execution(agent_name=agent, message="what is the answer?", triggered_by="chat")
    if linked:
        assert db.set_execution_chat_session(row.id, session.id)
    return row.id, session.id


def _fake_service(db, *, win=True):
    """`apply_result` stand-in: the terminal CAS on the real row (or a lost one)."""
    from db.write_params import ExecutionResult
    from models import TaskExecutionStatus
    from services.task_execution_service import TaskExecutionErrorCode

    async def apply_result(agent_name, envelope, **_kw):
        if not win:
            return SimpleNamespace(status="success", error_code=TaskExecutionErrorCode.RECONCILED)
        status = envelope.status
        db.update_execution_status(
            execution_id=envelope.execution_id,
            status=status,
            result=ExecutionResult(
                response=envelope.response, cost=0.42,
                error=envelope.error if status != TaskExecutionStatus.SUCCESS else None,
            ),
        )
        return SimpleNamespace(status=status, error_code=None)

    return SimpleNamespace(apply_result=apply_result)


def _claim(db, agent, execution_id, record, *, win=True):
    cs = _cs()
    with (
        patch("services.task_execution_service.get_task_execution_service",
              return_value=_fake_service(db, win=win)),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.activity_service.activity_service.close_companion_activities",
              AsyncMock(return_value=0)),
    ):
        return asyncio.run(cs._claim_retained_result(agent, execution_id, record))


def _assistant(db, session_id):
    return [m for m in db.get_chat_messages(session_id) if m.role == "assistant"]


class TestTheLink:
    def test_stamp_round_trips(self, real_db):
        agent = _agent()
        row = real_db.create_task_execution(agent_name=agent, message="m", triggered_by="chat")
        assert real_db.get_execution_chat_session_id(row.id) is None
        assert real_db.set_execution_chat_session(row.id, "sess-xyz")
        assert real_db.get_execution_chat_session_id(row.id) == "sess-xyz"

    def test_unknown_execution_has_no_thread(self, real_db):
        assert real_db.get_execution_chat_session_id("no-such-exec") is None
        assert real_db.set_execution_chat_session("no-such-exec", "s") is False

    def test_prepare_chat_execution_stamps_the_thread_it_logs_into(self, real_db):
        from services import chat_execution_service as ces

        agent = _agent()
        user = SimpleNamespace(id=7, email="u@example.com", username="u")
        with patch.object(ces.activity_service, "track_activity", AsyncMock(return_value="act")):
            ctx = asyncio.run(ces.prepare_chat_execution(
                name=agent, request=SimpleNamespace(message="hi"), current_user=user,
                x_source_agent=None, x_via_mcp=None,
                idem=SimpleNamespace(enabled=False, replay=False),
                chat_execution_id="q-1", capacity_result=SimpleNamespace(state="admitted"),
                queue_result="admitted",
            ))
        assert real_db.get_execution_chat_session_id(ctx.task_execution_id) == ctx.session.id


class TestRecoveredReply:
    def test_a_claimed_chat_turn_writes_one_reply_into_its_thread(self, real_db):
        agent = _agent()
        execution_id, session_id = _chat_turn(real_db, agent)

        assert _claim(real_db, agent, execution_id, _record()) == "claimed"

        replies = _assistant(real_db, session_id)
        assert len(replies) == 1
        reply = replies[0]
        assert reply.content.startswith("> ℹ️ Recovered by the watchdog")
        assert reply.content.endswith("the recovered answer")
        assert reply.cost == pytest.approx(0.42)
        assert reply.user_id == 7 and reply.agent_name == agent
        # The reply is exactly what the row persisted.
        assert reply.content == real_db.get_execution(execution_id).response

    def test_a_lost_cas_writes_nothing(self, real_db):
        agent = _agent()
        execution_id, session_id = _chat_turn(real_db, agent)
        assert _claim(real_db, agent, execution_id, _record(), win=False) == "lost"
        assert _assistant(real_db, session_id) == []

    def test_a_failed_turn_writes_nothing(self, real_db):
        agent = _agent()
        execution_id, session_id = _chat_turn(real_db, agent)
        assert _claim(real_db, agent, execution_id, _record(status="failed")) == "claimed"
        assert _assistant(real_db, session_id) == []

    def test_a_turn_with_no_thread_writes_nothing(self, real_db):
        agent = _agent()
        execution_id, session_id = _chat_turn(real_db, agent, linked=False)
        assert _claim(real_db, agent, execution_id, _record()) == "claimed"
        assert _assistant(real_db, session_id) == []

    def test_a_thread_of_another_agent_is_never_written(self, real_db):
        agent = _agent()
        execution_id, _ = _chat_turn(real_db, agent)
        other = real_db.get_or_create_chat_session(
            agent_name=_agent(), user_id=7, user_email="u@example.com")
        real_db.set_execution_chat_session(execution_id, other.id)
        assert _claim(real_db, agent, execution_id, _record()) == "claimed"
        assert _assistant(real_db, other.id) == []

    def test_a_failing_reply_write_never_unclaims_the_turn(self, real_db):
        agent = _agent()
        execution_id, _ = _chat_turn(real_db, agent)
        with patch.object(_cs().db, "add_chat_message", side_effect=RuntimeError("db down")):
            assert _claim(real_db, agent, execution_id, _record()) == "claimed"


def _live_finalize(db, agent, execution_id, session_id):
    """The sync /chat success finalizer on the real row, as a live dispatcher
    that got the agent's answer runs it."""
    from datetime import datetime as _dt
    from unittest.mock import MagicMock
    from services import chat_execution_service as ces

    resp = MagicMock()
    resp.json.return_value = {
        "response": "the live answer", "execution_log": [], "metadata": {"cost_usd": 0.42},
        "session": {"context_tokens": 10, "context_window": 200000},
    }
    with (
        patch.object(ces, "db", db),
        patch.object(ces, "get_staged_values", lambda: []),
        patch.object(ces, "activity_service", MagicMock(complete_activity=AsyncMock())),
        patch.object(ces.idempotency_service, "complete", lambda *a, **k: None),
        patch.object(ces, "_spawn_chat_terminal_report", lambda *a, **k: None),
    ):
        asyncio.run(ces._finalize_chat_success(
            name=agent, response=resp, start_time=_dt.utcnow(),
            session=db.get_chat_session(session_id),
            current_user=SimpleNamespace(id=7, email="u@example.com", username="u"),
            chat_activity_id="ca", collaboration_activity_id=None,
            task_execution_id=execution_id, _chat_subscription_id=None,
            execution=SimpleNamespace(id="q-1"), queue_result="done", is_queued=False,
            idem="idem-1",
        ))


class TestLiveWritersAfterTheWatchdog:
    """Review follow-up: a live writer reaching a turn the watchdog already
    claimed must not write the reply a second time."""

    def test_a_dispatcher_reaching_a_watchdog_claimed_turn_writes_no_second_reply(self, real_db):
        agent = _agent()
        execution_id, session_id = _chat_turn(real_db, agent)
        assert _claim(real_db, agent, execution_id, _record()) == "claimed"
        _live_finalize(real_db, agent, execution_id, session_id)
        replies = _assistant(real_db, session_id)
        assert len(replies) == 1 and replies[0].content.startswith("> ℹ️ Recovered")

    def test_a_dispatcher_that_won_writes_its_reply(self, real_db):
        agent = _agent()
        execution_id, session_id = _chat_turn(real_db, agent)
        _live_finalize(real_db, agent, execution_id, session_id)
        assert [m.content for m in _assistant(real_db, session_id)] == ["the live answer"]
        assert real_db.get_execution(execution_id).status == "success"

    def test_recovered_reply_written_only_for_the_watchdogs_claim(self, real_db):
        cs = _cs()
        agent = _agent()
        claimed, _ = _chat_turn(real_db, agent)
        assert _claim(real_db, agent, claimed, _record()) == "claimed"
        assert cs.recovered_reply_written(claimed) is True
        live, session_id = _chat_turn(real_db, agent)
        _live_finalize(real_db, agent, live, session_id)
        assert cs.recovered_reply_written(live) is False
        unlinked, _ = _chat_turn(real_db, agent, linked=False)
        assert _claim(real_db, agent, unlinked, _record()) == "claimed"
        assert cs.recovered_reply_written(unlinked) is False
        assert cs.recovered_reply_written(None) is False


def test_the_notice_no_longer_says_the_thread_may_lack_the_reply():
    notice = _cs()._RECOVERED_NOTICE
    assert "may not show" not in notice
    assert "#3380" in notice
