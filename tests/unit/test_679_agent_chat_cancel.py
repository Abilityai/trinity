"""Unit tests for #679: the agent-side ``/api/task`` handler labels a cancelled
turn (``execute_task`` in ``agent_server.routers.chat``).

Two cancel shapes:
  * Graceful — Claude catches SIGINT, emits a final message, exits 0.
    ``execute_headless`` returns normally; the handler cross-checks the
    ``was_terminated`` marker (keyed off ``request.execution_id``, NEVER the
    returned ``session_id``) and labels ``status:"cancelled"``.
  * SIGKILL — Claude ignored SIGINT, terminate() escalated to SIGKILL, and
    ``execute_headless`` raised HTTPException 504. The handler swallows the
    **504 only** when terminated → 200 ``status:"cancelled"``; 503/429/etc. and
    unterminated 504s re-raise (Issue 6/C6 — keep SUB-003 + the AUTH breaker firing).

The conftest preloads the agent_server namespace package.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import agent_server.routers.chat as chat_mod  # noqa: E402

pytestmark = pytest.mark.unit


def _req(**over):
    base = dict(
        message="do the thing", model="sonnet", allowed_tools=None,
        system_prompt=None, timeout_seconds=300, max_turns=None,
        execution_id="exec-1", resume_session_id=None, persist_session=False,
        images=None, async_result=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _runtime(*, returns=None, raises=None):
    rt = MagicMock()
    if raises is not None:
        rt.execute_headless = AsyncMock(side_effect=raises)
    else:
        rt.execute_headless = AsyncMock(return_value=returns)
    return rt


def _ok_return(session_id="sess-DIFFERENT"):
    """A normal execute_headless 4-tuple: (response, raw_messages, metadata, session_id)."""
    md = MagicMock()
    md.model_dump.return_value = {"cost_usd": 0.03}
    return ("final answer", [{"type": "result"}], md, session_id)


def _drive(request, *, runtime, was_terminated):
    """Run execute_task with runtime/registry/result_callback/agent_state mocked."""
    registry = MagicMock()
    registry.was_terminated.return_value = was_terminated

    with (
        patch.object(chat_mod, "get_runtime", return_value=runtime),
        patch.object(chat_mod, "get_process_registry", return_value=registry),
        patch.object(chat_mod, "agent_state", MagicMock()),
        patch.object(chat_mod.result_callback, "try_spawn_async", return_value=False),
    ):
        return asyncio.run(chat_mod.execute_task(request)), registry


class TestGracefulPath:
    def test_graceful_cancel_labels_cancelled(self):
        # Marker is keyed off execution_id; the returned session_id differs and
        # must NOT be used for the lookup.
        reply, registry = _drive(
            _req(execution_id="exec-1"),
            runtime=_runtime(returns=_ok_return(session_id="sess-DIFFERENT")),
            was_terminated=True,
        )
        assert reply["status"] == "cancelled"
        registry.was_terminated.assert_called_once_with("exec-1")

    def test_normal_success_labels_success(self):
        reply, registry = _drive(
            _req(execution_id="exec-1"),
            runtime=_runtime(returns=_ok_return()),
            was_terminated=False,
        )
        assert reply["status"] == "success"
        assert reply["response"] == "final answer"

    def test_execution_id_none_labels_success_without_lookup(self):
        reply, registry = _drive(
            _req(execution_id=None),
            runtime=_runtime(returns=_ok_return()),
            was_terminated=True,  # would be cancelled IF it were keyed wrong
        )
        assert reply["status"] == "success"
        registry.was_terminated.assert_not_called()


class TestSigkillPath:
    def test_504_terminated_returns_200_cancelled(self):
        reply, registry = _drive(
            _req(execution_id="exec-1"),
            runtime=_runtime(raises=HTTPException(status_code=504, detail="timed out")),
            was_terminated=True,
        )
        assert reply["status"] == "cancelled"
        assert reply["response"] == "timed out"     # string detail surfaced
        assert reply["execution_log"] == []
        assert reply["metadata"] == {}
        assert reply["session_id"] is None

    def test_503_terminated_reraises(self):
        # C6: a 503-auth must propagate so SUB-003 + the AUTH dispatch breaker fire.
        with pytest.raises(HTTPException) as ei:
            _drive(
                _req(execution_id="exec-1"),
                runtime=_runtime(raises=HTTPException(status_code=503, detail="auth")),
                was_terminated=True,
            )
        assert ei.value.status_code == 503

    def test_429_terminated_reraises(self):
        with pytest.raises(HTTPException) as ei:
            _drive(
                _req(execution_id="exec-1"),
                runtime=_runtime(raises=HTTPException(status_code=429, detail="rate")),
                was_terminated=True,
            )
        assert ei.value.status_code == 429

    def test_504_not_terminated_reraises(self):
        # A genuine timeout (no cancel) must keep its 504.
        with pytest.raises(HTTPException) as ei:
            _drive(
                _req(execution_id="exec-1"),
                runtime=_runtime(raises=HTTPException(status_code=504, detail="timed out")),
                was_terminated=False,
            )
        assert ei.value.status_code == 504


class TestNonSignalTerminatedRelabel:
    """#679 F3: the sync relabel must mirror the async path's breadth — ANY
    non-auth/non-rate terminal for a terminated turn is a cancel, not a failure.
    A terminated turn that exits 0 with empty output surfaces a 502 (#678
    empty-result) or 500; before F3 the sync path only caught 504, so those wrote
    FAILED and could lose the CAS race to the terminate handler's CANCELLED write
    (leaving a user-cancel persisted as FAILED). The structured 502 detail is a
    dict, so the relabel drops it to an empty response (cancel is non-billable)."""

    def test_502_terminated_returns_200_cancelled(self):
        reply, _registry = _drive(
            _req(execution_id="exec-1"),
            runtime=_runtime(raises=HTTPException(
                status_code=502, detail={"message": "empty result", "metadata": {"cost_usd": 0.04}}
            )),
            was_terminated=True,
        )
        assert reply["status"] == "cancelled"
        assert reply["response"] == ""          # dict detail → empty (no billable salvage)
        assert reply["metadata"] == {}
        assert reply["session_id"] is None

    def test_500_terminated_returns_200_cancelled(self):
        reply, _registry = _drive(
            _req(execution_id="exec-1"),
            runtime=_runtime(raises=HTTPException(status_code=500, detail="empty response")),
            was_terminated=True,
        )
        assert reply["status"] == "cancelled"
        assert reply["response"] == "empty response"   # string detail surfaced as the label

    def test_502_not_terminated_reraises(self):
        # A genuine empty-result (no cancel) must keep its 502 so the #678
        # inline reader-race retry / FAILED accounting still fire.
        with pytest.raises(HTTPException) as ei:
            _drive(
                _req(execution_id="exec-1"),
                runtime=_runtime(raises=HTTPException(status_code=502, detail={"message": "empty"})),
                was_terminated=False,
            )
        assert ei.value.status_code == 502
