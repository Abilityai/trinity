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


def test_the_notice_no_longer_says_the_thread_may_lack_the_reply():
    notice = _cs()._RECOVERED_NOTICE
    assert "may not show" not in notice
    assert "#3380" in notice
