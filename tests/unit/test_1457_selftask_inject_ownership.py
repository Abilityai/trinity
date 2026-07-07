"""Unit tests for #1457 — self-task ``inject_result`` must actually inject.

``_finalize_self_task`` (``routers/chat.py``) validated chat-session ownership with
**dict** access (``session.get("user_id")``) on the value returned by
``db.get_chat_session()``. After the #1093 SQLAlchemy-Core rewrite that accessor
returns ``Optional[ChatSession]`` — a Pydantic ``BaseModel`` with no ``.get()`` — so
the check raised ``AttributeError``, was swallowed by the surrounding
``except Exception``, and the result was **silently never injected**.

Same class as #1444: a swallowing ``except`` turns the regression into a silent
no-op, not a crash. Guards:
  (a) a SUCCESS self-task with ``inject_result=True`` and an **owned**
      ``chat_session_id`` writes an ``assistant`` message with ``source="self_task"``.
  (b) a **foreign**-owned ``chat_session_id`` does NOT inject.
  (c) ``ChatSession`` exposes ``.user_id`` (attribute) and has no ``.get`` — proving
      the old dict access was structurally wrong, so the ownership branch is really
      reached, not swallowed.

Pure unit tests — no backend. Mirrors test_1332_cancelled_activity_state.py.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _success_result():
    from models import TaskExecutionStatus

    r = MagicMock()
    r.status = TaskExecutionStatus.SUCCESS
    r.response = "task done"
    r.error = None
    r.cost = 0.02
    r.context_used = 120
    r.context_max = 200000
    return r


def _chat_session(*, owner_id: int):
    """A real ChatSession Pydantic model — has ``.user_id``, has no ``.get()``."""
    from db_models import ChatSession

    now = datetime(2026, 7, 6, 0, 0, 0)
    return ChatSession(
        id="sess-1457",
        agent_name="test-agent",
        user_id=owner_id,
        user_email="owner@example.com",
        started_at=now,
        last_message_at=now,
    )


def _run_finalize(*, session, caller_user_id):
    import routers.chat as chat

    request = MagicMock(inject_result=True, chat_session_id="sess-1457")

    mock_db = MagicMock()
    mock_db.get_chat_session.return_value = session

    mock_activity = MagicMock(complete_activity=AsyncMock())
    with (
        patch.object(chat, "db", mock_db),
        patch.object(chat, "activity_service", mock_activity),
        patch.object(chat, "_websocket_manager", None),
    ):
        _await(
            chat._finalize_self_task(
                is_self_task=True,
                self_task_activity_id="self-act",
                agent_name="test-agent",
                request=request,
                result=_success_result(),
                execution_id="exec-1457",
                user_id=caller_user_id,
                user_email="caller@example.com",
                execution_time_ms=999,
            )
        )
    return mock_db


class TestSelfTaskInjectOwnership:
    pytestmark = pytest.mark.unit

    def test_owned_session_injects_self_task_message(self):
        """SUCCESS + inject_result + owned session → assistant message, source=self_task."""
        mock_db = _run_finalize(session=_chat_session(owner_id=7), caller_user_id=7)

        mock_db.add_chat_message.assert_called_once()
        kwargs = mock_db.add_chat_message.call_args.kwargs
        assert kwargs["role"] == "assistant"
        assert kwargs["source"] == "self_task"
        assert kwargs["content"] == "task done"
        assert kwargs["session_id"] == "sess-1457"

    def test_foreign_session_does_not_inject(self):
        """Session owned by a different user → ownership check fails, no injection."""
        mock_db = _run_finalize(session=_chat_session(owner_id=7), caller_user_id=99)

        mock_db.add_chat_message.assert_not_called()

    def test_chat_session_is_attribute_accessed_not_dict(self):
        """Regression guard: the ownership branch relies on ``.user_id`` (attribute).

        The pre-#1457 ``session.get("user_id")`` raised on a Pydantic model and was
        swallowed — proving the branch was structurally unreachable.
        """
        session = _chat_session(owner_id=7)
        assert session.user_id == 7
        assert not hasattr(session, "get")
