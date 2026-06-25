"""Unit test for #679 (Issue 7 / Codex C3): the backend terminate handler must
NOT write CANCELLED when the agent reports ``already_finished``.

``routers.chat.terminate_agent_execution`` proxies a terminate to the agent.
When the agent says ``terminated`` the turn was running and we cancelled it →
write CANCELLED. When the agent says ``already_finished`` the turn reached its
own genuine terminal a moment before the cancel landed — the agent's self-report
is authoritative, so we leave the real terminal in place (deterministic; "the
cancel was too late"). Capacity is force-released in both cases (unchanged).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeAgentResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, *a, **kw):
        return self._response


def _drive_terminate(agent_status: str):
    """Drive terminate_agent_execution with the agent returning `agent_status`.
    Returns the mock_db so the caller can inspect update_execution_status."""
    import routers.chat as chat

    mock_db = MagicMock()
    mock_db.get_execution.return_value = SimpleNamespace(status="running")  # not QUEUED

    container = MagicMock()
    container.status = "running"

    capacity = MagicMock()
    capacity.force_release = AsyncMock(
        return_value=SimpleNamespace(was_running=True, slots_cleared=1)
    )

    activity = MagicMock(track_activity=AsyncMock())
    agent_resp = _FakeAgentResponse(200, {"status": agent_status, "returncode": 0})

    with (
        patch.object(chat, "db", mock_db),
        patch.object(chat, "get_agent_container", return_value=container),
        patch.object(chat, "get_capacity_manager", return_value=capacity),
        patch.object(chat, "activity_service", activity),
        patch.object(chat.httpx, "AsyncClient", lambda *a, **kw: _FakeClient(agent_resp)),
    ):
        result = _await(
            chat.terminate_agent_execution(
                execution_id="exec-1",
                task_execution_id="exec-1",
                name="agent-a",
                current_user=SimpleNamespace(id=1, email="u@e.com", username="u"),
            )
        )
    return result, mock_db, capacity


def _cancelled_writes(mock_db):
    from services.task_execution_service import TaskExecutionStatus

    return [
        c for c in mock_db.update_execution_status.call_args_list
        if c.kwargs.get("status") == TaskExecutionStatus.CANCELLED
    ]


def test_terminated_writes_cancelled():
    result, mock_db, capacity = _drive_terminate("terminated")
    assert len(_cancelled_writes(mock_db)) == 1
    capacity.force_release.assert_awaited_once()  # capacity released


def test_already_finished_does_not_write_cancelled():
    """Issue 7: the agent's genuine terminal stands — no CANCELLED overwrite."""
    result, mock_db, capacity = _drive_terminate("already_finished")
    assert _cancelled_writes(mock_db) == []
    # Capacity is still force-released on already_finished (unchanged behavior).
    capacity.force_release.assert_awaited_once()
