"""#1482 — the widest database.py writers take typed parameter objects.

`update_execution_status`, `create_task_execution`, `create_schedule_execution`
and `add_chat_message` each had 10–20 parameters, duplicated in the facade and
the db module. The facade passed `update_execution_status`'s 13 arguments to the
db layer POSITIONALLY, so a reorder in either signature would silently write one
field into another column. They now take frozen, keyword-only dataclasses from
`db/write_params.py`.

Pinned here, on a real database:
- every field of every object reaches its column (a dropped field is the one
  regression a signature refactor can introduce silently);
- `update_execution_status` keeps its exact CAS contract and bool return
  (#1082 / #1083: `apply_result` gates every side effect on it), and still
  leaves `retry_count` / `turn_integrity` untouched when they are None (#678,
  #2467);
- the facade passes the object through unchanged;
- no touched signature is wider than 8 parameters, in either layer.
"""
from __future__ import annotations

import dataclasses
import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, run as _hrun, scalar as _scalar  # noqa: E402,F401

pytestmark = pytest.mark.unit

_STUBBED_MODULE_NAMES = ["db.connection", "db.schedules", "db.chat", "database"]


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
        for mod in ("db.connection", "db.schedules", "db.chat", "database"):
            sys.modules.pop(mod, None)
    _evict()
    try:
        yield db_backend
    finally:
        _evict()


@pytest.fixture
def schedule_ops(tmp_db):
    from db.schedules import ScheduleOperations
    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


@pytest.fixture
def chat_ops(tmp_db):
    from db.chat import ChatOperations
    return ChatOperations()


def _row(execution_id):
    from db.engine import get_engine
    from db.tables import schedule_executions
    from sqlalchemy import select
    with get_engine().connect() as conn:
        return dict(conn.execute(
            select(schedule_executions).where(schedule_executions.c.id == execution_id)
        ).mappings().first())


# --------------------------------------------------------------------------- #
# The objects themselves
# --------------------------------------------------------------------------- #

class TestObjects:
    def test_all_are_frozen_and_keyword_only(self):
        from db.write_params import (
            ChatMessageFields, ExecutionResult, ExecutionSource, TaskExecutionFields)
        for cls in (ExecutionSource, TaskExecutionFields, ExecutionResult, ChatMessageFields):
            params = dataclasses.fields(cls)
            assert params, cls
            assert all(f.kw_only for f in params), f"{cls.__name__} must be keyword-only"
            with pytest.raises(TypeError):
                cls("positional")  # a positional value is exactly the bug class removed
            obj = cls()
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(obj, params[0].name, "x")

    def test_task_fields_extend_the_source(self):
        from db.write_params import ExecutionSource, TaskExecutionFields
        assert {f.name for f in dataclasses.fields(ExecutionSource)} <= {
            f.name for f in dataclasses.fields(TaskExecutionFields)}

    def test_chat_source_defaults_to_text(self):
        from db.write_params import ChatMessageFields
        assert ChatMessageFields().source == "text"


# --------------------------------------------------------------------------- #
# Signature width — the issue's acceptance bar
# --------------------------------------------------------------------------- #

WRITERS = ("update_execution_status", "create_task_execution",
           "create_schedule_execution", "add_chat_message")


@pytest.mark.parametrize("name", WRITERS)
def test_no_touched_signature_is_wider_than_eight(name, tmp_db):
    import database
    from db.chat import ChatOperations
    from db.schedules import ScheduleOperations
    owner = ChatOperations if name == "add_chat_message" else ScheduleOperations
    for fn in (getattr(database.DatabaseManager, name), getattr(owner, name)):
        params = [p for p in inspect.signature(fn).parameters if p != "self"]
        assert len(params) <= 8, f"{fn.__qualname__} has {len(params)} params: {params}"


# --------------------------------------------------------------------------- #
# create_task_execution / create_schedule_execution — every field reaches its column
# --------------------------------------------------------------------------- #

class TestCreateExecution:
    def test_every_task_field_is_persisted(self, schedule_ops):
        from db.write_params import TaskExecutionFields
        values = {
            "source_user_id": 7, "source_user_email": "u@example.com",
            "source_agent_name": "caller", "source_mcp_key_id": "key-1",
            "source_mcp_key_name": "Key One", "model_used": "claude-opus-5-5",
            "fan_out_id": "fo-1", "fan_out_task_id": "t-3", "loop_id": "loop-1",
            "subscription_id": "sub-1", "source_channel": "slack",
            "source_channel_chat_id": "C1", "source_channel_thread": "T1",
            "source_channel_agent": "binder", "source_channel_client": "client@example.com",
            "open_canvas_id": "canvas-1", "chain_depth": 2,
        }
        # Guard the guard: this test covers every field the object has.
        assert set(values) == {f.name for f in dataclasses.fields(TaskExecutionFields)}
        ex = schedule_ops.create_task_execution(
            "agent-a", "do it", "mcp", TaskExecutionFields(**values))
        row = _row(ex.id)
        for column, expected in values.items():
            assert row[column] == expected, column
        assert (row["schedule_id"], row["agent_name"], row["message"], row["triggered_by"]) == (
            "__manual__", "agent-a", "do it", "mcp")
        assert ex.chain_depth == 2 and ex.source_channel_client == "client@example.com"

    def test_no_fields_is_a_bare_manual_row(self, schedule_ops):
        ex = schedule_ops.create_task_execution("agent-a", "hi")
        row = _row(ex.id)
        assert row["triggered_by"] == "manual"
        assert row["source_user_id"] is None and row["chain_depth"] is None

    def test_every_schedule_source_field_is_persisted(self, schedule_ops):
        from db.write_params import ExecutionSource
        values = {
            "source_user_id": 3, "source_user_email": "o@example.com",
            "source_agent_name": None, "source_mcp_key_id": "k", "source_mcp_key_name": "K",
            "model_used": "claude-sonnet-5", "subscription_id": "sub-9",
        }
        assert set(values) == {f.name for f in dataclasses.fields(ExecutionSource)}
        ex = schedule_ops.create_schedule_execution(
            "sched-1", "agent-a", "brief", "manual", ExecutionSource(**values))
        row = _row(ex.id)
        for column, expected in values.items():
            assert row[column] == expected, column
        assert (row["schedule_id"], row["triggered_by"]) == ("sched-1", "manual")


# --------------------------------------------------------------------------- #
# update_execution_status — same CAS contract, same columns
# --------------------------------------------------------------------------- #

class TestUpdateExecutionStatus:
    def _fresh(self, schedule_ops):
        return schedule_ops.create_task_execution("agent-a", "go").id

    def test_every_result_field_is_persisted(self, schedule_ops):
        from db.write_params import ExecutionResult
        eid = self._fresh(schedule_ops)
        values = {
            "response": "done", "error": None, "context_used": 1200, "context_max": 200000,
            "cost": 0.42, "tool_calls": "[]", "execution_log": "[{}]",
            "claude_session_id": "sess-1", "compact_metadata": "{}", "retry_count": 1,
            "turn_integrity": "complete",
        }
        assert set(values) == {f.name for f in dataclasses.fields(ExecutionResult)}
        assert schedule_ops.update_execution_status(eid, "success", ExecutionResult(**values)) is True
        row = _row(eid)
        for column, expected in values.items():
            assert row[column] == expected, column
        assert row["status"] == "success" and row["completed_at"]

    def test_none_retry_count_and_turn_integrity_leave_the_columns_alone(self, schedule_ops):
        from db.write_params import ExecutionResult
        eid = self._fresh(schedule_ops)
        _hrun("UPDATE schedule_executions SET retry_count = 2, turn_integrity = 'partial' "
              "WHERE id = :i", i=eid)
        assert schedule_ops.update_execution_status(eid, "failed", ExecutionResult(error="boom")) is True
        row = _row(eid)
        assert (row["retry_count"], row["turn_integrity"], row["error"]) == (2, "partial", "boom")

    def test_no_result_is_a_bare_status_write(self, schedule_ops):
        eid = self._fresh(schedule_ops)
        assert schedule_ops.update_execution_status(eid, "failed") is True
        assert _row(eid)["status"] == "failed"

    def test_cas_contract_is_unchanged(self, schedule_ops):
        from db.write_params import ExecutionResult
        eid = self._fresh(schedule_ops)
        assert schedule_ops.update_execution_status(eid, "success", ExecutionResult(response="a")) is True
        # A non-success terminal write cannot clobber a completion (RELIABILITY-005).
        assert schedule_ops.update_execution_status(eid, "failed", ExecutionResult(error="late")) is False
        assert _row(eid)["status"] == "success"
        # A user cancel is authoritative over a late success (#671).
        eid2 = self._fresh(schedule_ops)
        assert schedule_ops.update_execution_status(eid2, "cancelled") is True
        assert schedule_ops.update_execution_status(eid2, "success", ExecutionResult(response="late")) is False
        # Unknown row → False, not an exception.
        assert schedule_ops.update_execution_status("missing", "success") is False

    def test_claim_token_still_gates_the_write(self, schedule_ops):
        from db.write_params import ExecutionResult
        eid = self._fresh(schedule_ops)
        _hrun("UPDATE schedule_executions SET claim_token = 'tok-1' WHERE id = :i", i=eid)
        assert schedule_ops.update_execution_status(
            eid, "success", ExecutionResult(response="x"), claim_token="wrong") is False
        assert schedule_ops.update_execution_status(
            eid, "success", ExecutionResult(response="x"), claim_token="tok-1") is True

    def test_claim_token_is_keyword_only(self, schedule_ops):
        eid = self._fresh(schedule_ops)
        with pytest.raises(TypeError):
            schedule_ops.update_execution_status(eid, "success", None, "tok")


# --------------------------------------------------------------------------- #
# add_chat_message
# --------------------------------------------------------------------------- #

class TestAddChatMessage:
    def test_every_field_is_persisted_and_session_stats_move(self, chat_ops):
        from db.write_params import ChatMessageFields
        from db.engine import get_engine
        from db.tables import chat_messages, chat_sessions
        from sqlalchemy import select
        session = chat_ops.get_or_create_chat_session("agent-a", 1, "u@example.com")
        values = {"cost": 0.5, "context_used": 900, "context_max": 200000, "tool_calls": "[]",
                  "execution_time_ms": 1234, "source": "voice", "subscription_id": "sub-1",
                  "output_tokens": 77}
        assert set(values) == {f.name for f in dataclasses.fields(ChatMessageFields)}
        msg = chat_ops.add_chat_message(session.id, "agent-a", 1, "u@example.com",
                                        "assistant", "hello", ChatMessageFields(**values))
        with get_engine().connect() as conn:
            row = dict(conn.execute(select(chat_messages).where(chat_messages.c.id == msg.id)).mappings().first())
            srow = dict(conn.execute(select(chat_sessions).where(chat_sessions.c.id == session.id)).mappings().first())
        for column, expected in values.items():
            assert row[column] == expected, column
        assert (row["role"], row["content"]) == ("assistant", "hello")
        assert srow["message_count"] == 1 and srow["total_cost"] == pytest.approx(0.5)
        assert srow["total_context_used"] == 900

    def test_no_fields_defaults_to_a_text_message(self, chat_ops):
        session = chat_ops.get_or_create_chat_session("agent-a", 1, "u@example.com")
        msg = chat_ops.add_chat_message(session.id, "agent-a", 1, "u@example.com", "user", "hi")
        assert msg.source == "text"


# --------------------------------------------------------------------------- #
# The facade passes the object through, untouched
# --------------------------------------------------------------------------- #

def test_facade_passes_each_object_through(tmp_db):
    import database
    from db.write_params import (
        ChatMessageFields, ExecutionResult, ExecutionSource, TaskExecutionFields)
    mgr = database.DatabaseManager.__new__(database.DatabaseManager)
    mgr._schedule_ops = MagicMock()
    mgr._chat_ops = MagicMock()

    result = ExecutionResult(response="r")
    mgr.update_execution_status("e1", "success", result, claim_token="t")
    mgr._schedule_ops.update_execution_status.assert_called_once_with(
        "e1", "success", result, claim_token="t")

    fields = TaskExecutionFields(chain_depth=1)
    mgr.create_task_execution("a", "m", "mcp", fields)
    # trinity-enterprise#751: the caller-chosen id rides through too (None here).
    mgr._schedule_ops.create_task_execution.assert_called_once_with(
        "a", "m", "mcp", fields, execution_id=None)
    mgr._schedule_ops.create_task_execution.reset_mock()
    mgr.create_task_execution("a", "m", "mcp", fields, execution_id="exec-chosen")
    mgr._schedule_ops.create_task_execution.assert_called_once_with(
        "a", "m", "mcp", fields, execution_id="exec-chosen")

    source = ExecutionSource(source_user_id=1)
    mgr.create_schedule_execution("s", "a", "m", "manual", source)
    mgr._schedule_ops.create_schedule_execution.assert_called_once_with("s", "a", "m", "manual", source)

    cf = ChatMessageFields(cost=1.0)
    mgr.add_chat_message("s", "a", 1, "e", "user", "c", cf)
    mgr._chat_ops.add_chat_message.assert_called_once_with("s", "a", 1, "e", "user", "c", cf)
