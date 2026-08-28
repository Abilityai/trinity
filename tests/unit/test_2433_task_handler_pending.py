"""#2433 — the agent-server handlers register an execution as *pending* the
moment they ACCEPT it, and drop the entry only when the owning path ends.

Three producers, one registry seam:
- ``/api/task`` (sync branch): ``register_pending`` runs BEFORE
  ``runtime.execute_headless`` and ``discard_pending`` runs in ``finally`` —
  on success, on an HTTPException, and on the pre-spawn 409 cancel.
- ``/api/chat``: ``register_pending`` runs BEFORE the execution lock is
  awaited (B′ — the lock wait is a park the registry never saw).
- ``try_spawn_async`` (#1083): ``register_pending`` runs synchronously before
  the detached task is created (the 202 goes out before the task's first
  tick); ``_run_and_report`` discards in its own ``finally`` — NOT the handler,
  which returned 202 long ago.
- ``/api/executions/running`` carries ``pending_ids``.

Modules under test:
    docker/base-image/agent_server/routers/chat.py
    docker/base-image/agent_server/services/result_callback.py
The unit conftest preloads the real agent_server namespace package.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import agent_server.routers.chat as chat_mod  # noqa: E402
from agent_server.services import result_callback as rc  # noqa: E402

pytestmark = pytest.mark.unit


def _task_req(**over):
    base = dict(
        message="do the thing", model="sonnet", allowed_tools=None,
        system_prompt=None, timeout_seconds=300, max_turns=None,
        execution_id="exec-1", resume_session_id=None, persist_session=False,
        images=None, async_result=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _metadata():
    md = MagicMock()
    md.model_dump.return_value = {"cost_usd": 0.01}
    md.cost_usd = 0.0
    md.output_tokens = 0
    md.input_tokens = 0
    md.context_window = 200000
    return md


def _registry(order=None, *, was_terminated=False):
    registry = MagicMock()
    registry.was_terminated.return_value = was_terminated
    if order is not None:
        registry.register_pending.side_effect = lambda *a, **k: order.append("pending")
        registry.discard_pending.side_effect = lambda *a, **k: order.append("discard")
    return registry


# ---------------------------------------------------------------------------
# /api/executions/running
# ---------------------------------------------------------------------------

def test_running_endpoint_reports_pending_ids():
    registry = MagicMock()
    registry.list_running.return_value = []
    registry.list_recently_completed_ids.return_value = ["done-1"]
    registry.list_pending_ids.return_value = ["pend-1"]
    with patch.object(chat_mod, "get_process_registry", return_value=registry):
        payload = asyncio.run(chat_mod.list_running_executions())
    assert payload["pending_ids"] == ["pend-1"]
    assert payload["recently_completed_ids"] == ["done-1"]
    assert payload["executions"] == []


# ---------------------------------------------------------------------------
# /api/task sync branch
# ---------------------------------------------------------------------------

def _drive_task(request, *, runtime, registry):
    with (
        patch.object(chat_mod, "get_runtime", return_value=runtime),
        patch.object(chat_mod, "get_process_registry", return_value=registry),
        patch.object(chat_mod, "agent_state", MagicMock()),
        patch.object(chat_mod.result_callback, "try_spawn_async", return_value=False),
    ):
        return asyncio.run(chat_mod.execute_task(request))


def test_task_registers_pending_before_execute_and_discards_after():
    order: list = []
    registry = _registry(order)
    rt = MagicMock()

    async def _exec(**kw):
        order.append("execute")
        return ("ok", [{"type": "result"}], _metadata(), "sess")

    rt.execute_headless = _exec
    reply = _drive_task(_task_req(), runtime=rt, registry=registry)
    assert reply["status"] == "success"
    assert order == ["pending", "execute", "discard"]
    registry.register_pending.assert_called_once_with(
        "exec-1", timeout_seconds=300, metadata={"type": "task"}
    )
    registry.discard_pending.assert_called_once_with("exec-1")


def test_task_pending_window_uses_handler_default_timeout():
    registry = _registry()
    rt = MagicMock()
    rt.execute_headless = AsyncMock(return_value=("ok", [], _metadata(), "sess"))
    _drive_task(_task_req(timeout_seconds=None), runtime=rt, registry=registry)
    assert registry.register_pending.call_args.kwargs["timeout_seconds"] == 900


def test_task_discards_pending_when_execute_raises():
    order: list = []
    registry = _registry(order)
    rt = MagicMock()
    rt.execute_headless = AsyncMock(side_effect=HTTPException(status_code=503, detail="auth"))
    with pytest.raises(HTTPException):
        _drive_task(_task_req(), runtime=rt, registry=registry)
    assert order == ["pending", "discard"]


def test_task_pre_spawn_409_is_relabelled_cancelled_and_discards():
    """The thread-top check raises 409 for a cancelled-before-start run; the
    handler's #679 relabel (marker set) turns it into a `cancelled` 200."""
    order: list = []
    registry = _registry(order, was_terminated=True)
    rt = MagicMock()
    rt.execute_headless = AsyncMock(
        side_effect=HTTPException(status_code=409, detail="Execution cancelled before it started")
    )
    reply = _drive_task(_task_req(), runtime=rt, registry=registry)
    assert reply["status"] == "cancelled"
    assert order == ["pending", "discard"]


# ---------------------------------------------------------------------------
# /api/chat — register BEFORE the lock wait
# ---------------------------------------------------------------------------

def test_chat_registers_pending_before_lock_wait_and_discards_after():
    order: list = []
    registry = _registry(order)

    class _Lock:
        async def __aenter__(self):
            order.append("lock")
            return self

        async def __aexit__(self, *exc):
            return False

    rt = MagicMock()

    async def _exec(**kw):
        order.append("execute")
        return ("hi", [], _metadata(), [])

    rt.execute = _exec
    state = MagicMock(
        current_model="sonnet", conversation_history=[], session_total_cost=0.0,
        session_total_output_tokens=0, session_context_tokens=0, session_context_window=200000,
    )
    request = SimpleNamespace(
        message="hello", model=None, stream=False, system_prompt=None, execution_id="exec-chat-1"
    )
    with (
        patch.object(chat_mod, "get_execution_lock", return_value=_Lock()),
        patch.object(chat_mod, "get_runtime", return_value=rt),
        patch.object(chat_mod, "get_process_registry", return_value=registry),
        patch.object(chat_mod, "agent_state", state),
    ):
        reply = asyncio.run(chat_mod.chat(request))
    assert reply["response"] == "hi"
    assert order[:3] == ["pending", "lock", "execute"], order
    assert "discard" in order
    registry.register_pending.assert_called_once_with("exec-chat-1", metadata={"type": "chat"})


# ---------------------------------------------------------------------------
# #1083 async spawn
# ---------------------------------------------------------------------------

def test_try_spawn_async_registers_pending_synchronously_before_the_task_runs():
    order: list = []
    registry = _registry(order)

    async def fake_run_and_report(request, backend_url, mcp_key, dispatch_monotonic):
        order.append("run")

    async def main():
        with (
            patch.object(rc, "is_claude_runtime", return_value=True),
            patch.object(rc, "_callbacks_configured", return_value=True),
            patch.object(rc, "_is_safe_execution_id", return_value=True),
            patch.object(rc, "get_process_registry", return_value=registry),
            patch.object(rc, "_run_and_report", fake_run_and_report),
            patch.dict(os.environ, {"TRINITY_BACKEND_URL": "http://backend:8000", "TRINITY_MCP_API_KEY": "k"}),
        ):
            assert rc.try_spawn_async(_task_req(async_result=True)) is True
            # Registered on the calling stack — before the task had a tick.
            assert order == ["pending"]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        return list(order)

    result = asyncio.run(main())
    assert result == ["pending", "run"]
    registry.register_pending.assert_called_once_with(
        "exec-1", timeout_seconds=300, metadata={"type": "task", "async": True}
    )


def test_run_and_report_discards_pending_in_finally(monkeypatch):
    registry = _registry()
    rt = MagicMock()
    rt.execute_headless = AsyncMock(side_effect=RuntimeError("boom"))
    # The runtime is resolved through a CALL-TIME lazy import — own the
    # sys.modules key rather than patching a package attribute (learnings
    # 2026-07-07 / 2026-08-12).
    stub = types.ModuleType("agent_server.services.runtime_adapter")
    stub.get_runtime = lambda: rt
    monkeypatch.setitem(sys.modules, "agent_server.services.runtime_adapter", stub)
    with (
        patch.object(rc, "get_process_registry", return_value=registry),
        patch.object(rc, "agent_state", MagicMock(agent_name="agent-x")),
        patch.object(rc, "_persist", lambda *a, **k: None),
        patch.object(rc, "_delete", lambda *a, **k: None),
        patch.object(rc, "_deliver", AsyncMock(return_value=True)),
    ):
        asyncio.run(rc._run_and_report(_task_req(async_result=True), "http://b", "k", 0.0))
    registry.discard_pending.assert_called_once_with("exec-1")
