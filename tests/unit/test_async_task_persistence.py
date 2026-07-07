"""
Characterization tests for routers.chat._run_async_task_with_persistence (#1026).

This is the async /task background wrapper (#95): it delegates execution to
TaskExecutionService, then layers chat-endpoint post-task side effects
(chat-session persistence + broadcast, collaboration-activity completion,
self-task completion + result injection) and always signals a sync waiter in
its finally (#498).

These drive the real function with every collaborator patched and pin the
observable behavior of each guarded post-task block + the finally guarantee.
Green before and after the strategy extraction.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers.chat import _run_async_task_with_persistence
from models import ParallelTaskRequest, TaskExecutionStatus

_CHAT = sys.modules[_run_async_task_with_persistence.__module__]


def _result(status=TaskExecutionStatus.SUCCESS, response="ok"):
    r = MagicMock()
    r.status = status
    r.response = response
    r.error = None if status == TaskExecutionStatus.SUCCESS else "boom"
    r.cost = 0.01
    r.context_used = 100
    r.context_max = 200000
    return r


@contextmanager
def _env(result=None):
    """Patch every collaborator the wrapper touches; yield the mock bundle."""
    result = result or _result()
    service = MagicMock()
    service.execute_task = AsyncMock(return_value=result)

    ws = MagicMock()
    ws.broadcast = AsyncMock()

    activity = MagicMock()
    activity.complete_activity = AsyncMock()

    db = MagicMock()
    # #1457: db.get_chat_session returns a ChatSession (pydantic), NOT a dict —
    # _finalize_self_task reads session.user_id by attribute. A SimpleNamespace
    # faithfully mirrors that contract (attribute access, no .get); a dict here
    # made the fixture pass against the buggy `session.get("user_id")` code.
    db.get_chat_session.return_value = SimpleNamespace(user_id=7)

    persist = AsyncMock(return_value="cs1")
    signal = MagicMock()

    with patch.object(_CHAT, "get_task_execution_service", return_value=service), \
         patch.object(_CHAT, "_websocket_manager", ws), \
         patch.object(_CHAT, "activity_service", activity), \
         patch.object(_CHAT, "db", db), \
         patch.object(_CHAT, "_persist_chat_session", persist), \
         patch.object(_CHAT, "signal_sync_waiter", signal):
        yield {
            "service": service, "ws": ws, "activity": activity, "db": db,
            "persist": persist, "signal": signal, "result": result,
        }


def _call(request=None, **overrides):
    request = request or ParallelTaskRequest(message="hi")
    kwargs = dict(
        agent_name="agent1",
        request=request,
        execution_id="e1",
        collaboration_activity_id=None,
        x_source_agent=None,
        user_id=None,
        user_email=None,
    )
    kwargs.update(overrides)
    asyncio.run(_run_async_task_with_persistence(**kwargs))


def test_basic_executes_and_signals_waiter():
    with _env() as m:
        _call()
    m["service"].execute_task.assert_awaited_once()
    m["signal"].assert_called_once()              # finally always signals (#498)
    m["persist"].assert_not_awaited()             # no save_to_session
    m["activity"].complete_activity.assert_not_awaited()


def test_save_to_session_persists_and_broadcasts():
    req = ParallelTaskRequest(message="hi", save_to_session=True)
    with _env() as m:
        _call(req, user_id=7, user_email="u@e.com")
    m["persist"].assert_awaited_once()
    # chat_response_ready broadcast fired
    assert m["ws"].broadcast.await_count == 1


def test_save_to_session_skipped_without_user():
    req = ParallelTaskRequest(message="hi", save_to_session=True)
    with _env() as m:
        _call(req)  # no user_id/user_email
    m["persist"].assert_not_awaited()


def test_collaboration_activity_completed():
    with _env() as m:
        _call(collaboration_activity_id="collab1", x_source_agent="agentB")
    m["activity"].complete_activity.assert_awaited_once()


def test_self_task_injects_result_into_session():
    req = ParallelTaskRequest(message="hi", inject_result=True, chat_session_id="cs9")
    with _env() as m:
        _call(req, user_id=7, user_email="u@e.com",
              is_self_task=True, self_task_activity_id="st1")
    # self-task activity completed + result injected + completion broadcast
    m["activity"].complete_activity.assert_awaited_once()
    m["db"].add_chat_message.assert_called_once()
    assert m["ws"].broadcast.await_count == 1


def test_self_task_no_inject_when_session_not_owned():
    req = ParallelTaskRequest(message="hi", inject_result=True, chat_session_id="cs9")
    with _env() as m:
        m["db"].get_chat_session.return_value = SimpleNamespace(user_id=999)  # different owner
        _call(req, user_id=7, user_email="u@e.com",
              is_self_task=True, self_task_activity_id="st1")
    m["db"].add_chat_message.assert_not_called()


def test_signal_waiter_called_even_if_side_effect_raises():
    """The #498 finally guarantee: a post-task side-effect failure still signals
    the sync waiter. Current behavior: the un-wrapped _persist_chat_session call
    propagates its error *after* the finally signals — pin both facts."""
    req = ParallelTaskRequest(message="hi", save_to_session=True)
    with _env() as m:
        m["persist"].side_effect = RuntimeError("persist boom")
        with pytest.raises(RuntimeError):
            _call(req, user_id=7, user_email="u@e.com")
    m["signal"].assert_called_once()
