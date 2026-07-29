"""Unit tests for the fire-and-forget result-callback endpoint (#1083, PR1.B).

Exercises ``routers.agents.agent_execution_result`` directly (no live server),
mocking the DB + service deps. Covers the full fail-closed matrix:

  auth (403)  ·  ownership (404)  ·  idempotent replay (200)  ·
  async-marker gate (409)  ·  body-size (413)  ·  accept → apply_result.

The marker gate is the load-bearing PR1 guarantee: in production no execution is
ever marked ``dispatched_async`` until PR2, so every live RUNNING callback hits
the 409. Here we manufacture a marked row to also exercise the accept path.
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


class _FakeRequest:
    def __init__(self, auth_header=None):
        self.headers = {}
        if auth_header is not None:
            self.headers["Authorization"] = auth_header


def _execution(*, agent_name="agent-a", status="running", claude_session_id="dispatched_async"):
    return SimpleNamespace(
        id="exec-1", agent_name=agent_name, status=status,
        claude_session_id=claude_session_id,
    )


def _payload(**over):
    from models import ExecutionResultEnvelope

    base = dict(status="success", response="ok", metadata={"cost_usd": 0.01})
    base.update(over)
    return ExecutionResultEnvelope(**base)


def _call(
    *,
    auth_header="Bearer trinity_mcp_validkey",
    validate_result={"scope": "agent", "agent_name": "agent-a"},
    authorize=True,
    execution=None,
    payload=None,
    apply_status="success",
    agent_name="agent-a",
):
    """Drive the handler with mocked deps. Returns (response_or_exc, apply_mock)."""
    from routers.agents import agent_execution_result

    if execution is None:
        execution = _execution()
    if payload is None:
        payload = _payload()

    mock_db = MagicMock()
    mock_db.validate_mcp_api_key.return_value = validate_result
    mock_db.get_execution.return_value = execution
    mock_db.get_open_activity_id_for_execution.return_value = "act-1"

    apply_mock = AsyncMock(return_value=SimpleNamespace(status=apply_status))
    svc = MagicMock(apply_result=apply_mock)

    with (
        patch("routers.agents.db", mock_db),
        patch("services.heartbeat_service.authorize_heartbeat", return_value=authorize),
        patch("services.task_execution_service.get_task_execution_service", return_value=svc),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
    ):
        try:
            resp = _await(
                agent_execution_result(agent_name, "exec-1", payload, _FakeRequest(auth_header))
            )
            return resp, apply_mock, mock_db
        except Exception as exc:  # HTTPException surfaces here
            return exc, apply_mock, mock_db


def _status(exc):
    return getattr(exc, "status_code", None)


# --------------------------------------------------------------------------
# Auth (403)
# --------------------------------------------------------------------------
class TestAuth:
    def test_missing_authorization_403(self):
        exc, apply_mock, _ = _call(auth_header=None)
        assert _status(exc) == 403
        apply_mock.assert_not_awaited()

    def test_non_bearer_403(self):
        exc, apply_mock, _ = _call(auth_header="Basic abc")
        assert _status(exc) == 403

    def test_unknown_key_403(self):
        exc, apply_mock, _ = _call(validate_result=None, authorize=False)
        assert _status(exc) == 403

    def test_user_scoped_key_403(self):
        # authorize_heartbeat returns False for a user-scoped key.
        exc, apply_mock, _ = _call(
            validate_result={"scope": "user", "agent_name": None}, authorize=False
        )
        assert _status(exc) == 403

    def test_wrong_agent_key_403(self):
        exc, apply_mock, _ = _call(
            validate_result={"scope": "agent", "agent_name": "other"}, authorize=False
        )
        assert _status(exc) == 403


# --------------------------------------------------------------------------
# Ownership (404)
# --------------------------------------------------------------------------
class TestOwnership:
    def test_missing_execution_404(self):
        exc, apply_mock, _ = _call_with_execution(None)
        assert _status(exc) == 404
        apply_mock.assert_not_awaited()

    def test_wrong_agent_execution_404(self):
        exc, apply_mock, _ = _call_with_execution(_execution(agent_name="someone-else"))
        assert _status(exc) == 404


def _call_with_execution(execution):
    """Variant of _call that passes execution through verbatim (incl. None)."""
    from routers.agents import agent_execution_result

    mock_db = MagicMock()
    mock_db.validate_mcp_api_key.return_value = {"scope": "agent", "agent_name": "agent-a"}
    mock_db.get_execution.return_value = execution
    mock_db.get_open_activity_id_for_execution.return_value = "act-1"
    apply_mock = AsyncMock(return_value=SimpleNamespace(status="success"))
    svc = MagicMock(apply_result=apply_mock)
    with (
        patch("routers.agents.db", mock_db),
        patch("services.heartbeat_service.authorize_heartbeat", return_value=True),
        patch("services.task_execution_service.get_task_execution_service", return_value=svc),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
    ):
        try:
            resp = _await(
                agent_execution_result("agent-a", "exec-1", _payload(), _FakeRequest("Bearer k"))
            )
            return resp, apply_mock, mock_db
        except Exception as exc:
            return exc, apply_mock, mock_db


# --------------------------------------------------------------------------
# Idempotent replay (200) — already-terminal row
# --------------------------------------------------------------------------
class TestReplay:
    @pytest.mark.parametrize("term", ["success", "cancelled", "skipped"])
    def test_authoritative_terminal_is_replayed_noop(self, term):
        """SUCCESS/CANCELLED/SKIPPED are final — short-circuit as a replay ACK."""
        resp, apply_mock, _ = _call_with_execution(_execution(status=term))
        assert isinstance(resp, dict)
        assert resp["ok"] is True and resp["replayed"] is True
        assert resp["status"] == term
        apply_mock.assert_not_awaited()  # no re-finalization

    def test_failed_async_row_falls_through_to_apply_result(self):
        """Codex #2: a reaper-FAILED async row keeps its marker, so a genuinely
        late SUCCESS callback must reach apply_result (CAS lets it overwrite).
        FAILED is NOT short-circuited as a replay."""
        resp, apply_mock, _ = _call_with_execution(
            _execution(status="failed", claude_session_id="dispatched_async")
        )
        assert isinstance(resp, dict)
        assert resp["ok"] is True
        apply_mock.assert_awaited_once()  # fall-through, CAS decides

    def test_failed_sync_row_without_marker_409(self):
        """A FAILED row lacking the async marker (a sync execution) is still
        rejected — the cross-path guard holds for terminal rows too."""
        exc, apply_mock, _ = _call_with_execution(
            _execution(status="failed", claude_session_id="dispatched")
        )
        assert _status(exc) == 409
        apply_mock.assert_not_awaited()


# --------------------------------------------------------------------------
# Async-marker gate (409) — RUNNING without the durable marker
# --------------------------------------------------------------------------
class TestMarkerGate:
    @pytest.mark.parametrize("csid", ["dispatched", None, "", "real-uuid-1234"])
    def test_running_without_async_marker_409(self, csid):
        exc, apply_mock, _ = _call_with_execution(
            _execution(status="running", claude_session_id=csid)
        )
        assert _status(exc) == 409
        apply_mock.assert_not_awaited()


# --------------------------------------------------------------------------
# Body-size hardening (413)
# --------------------------------------------------------------------------
class TestBodySize:
    def test_oversized_response_413(self):
        big = "x" * (4_000_001)
        resp, apply_mock, _ = _call(payload=_payload(response=big))
        assert _status(resp) == 413
        apply_mock.assert_not_awaited()

    def test_oversized_execution_log_413(self):
        # ~17MB serialized list of short strings.
        big_log = ["y" * 100 for _ in range(180_000)]
        resp, apply_mock, _ = _call(payload=_payload(execution_log=big_log))
        assert _status(resp) == 413
        apply_mock.assert_not_awaited()


# --------------------------------------------------------------------------
# Accept path — marked RUNNING row → apply_result(release_slot=True)
# --------------------------------------------------------------------------
class TestAccept:
    def test_marked_row_applies_result(self):
        resp, apply_mock, mdb = _call_with_execution(
            _execution(status="running", claude_session_id="dispatched_async")
        )
        assert isinstance(resp, dict)
        assert resp["ok"] is True and resp["replayed"] is False
        apply_mock.assert_awaited_once()
        # apply_result called with release_slot=True and the looked-up activity id.
        _, kwargs = apply_mock.await_args
        assert kwargs["release_slot"] is True
        assert kwargs["activity_id"] == "act-1"
        # #1804: include_failed=True — this endpoint is the late-SUCCESS path,
        # and a `started`-only lookup can't reach an activity a lease reaper
        # already closed FAILED.
        mdb.get_open_activity_id_for_execution.assert_called_once_with(
            "exec-1", include_failed=True
        )

    def test_failed_callback_builds_failed_envelope(self):
        from models import ExecutionResultEnvelope

        payload = ExecutionResultEnvelope(
            status="failed", error="boom", error_code="auth",
            metadata={"cost_usd": 0.02},
        )
        from routers.agents import agent_execution_result

        mock_db = MagicMock()
        mock_db.validate_mcp_api_key.return_value = {"scope": "agent", "agent_name": "agent-a"}
        mock_db.get_execution.return_value = _execution(
            status="running", claude_session_id="dispatched_async"
        )
        mock_db.get_open_activity_id_for_execution.return_value = "act-1"
        apply_mock = AsyncMock(return_value=SimpleNamespace(status="failed"))
        svc = MagicMock(apply_result=apply_mock)
        with (
            patch("routers.agents.db", mock_db),
            patch("services.heartbeat_service.authorize_heartbeat", return_value=True),
            patch("services.task_execution_service.get_task_execution_service", return_value=svc),
            patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        ):
            resp = _await(
                agent_execution_result("agent-a", "exec-1", payload, _FakeRequest("Bearer k"))
            )
        assert resp["status"] == "failed"
        envelope = apply_mock.await_args.args[1]
        from services.task_execution_service import TaskExecutionErrorCode, TaskExecutionStatus
        assert envelope.status == TaskExecutionStatus.FAILED
        assert envelope.error == "boom"
        assert envelope.error_code == TaskExecutionErrorCode.AUTH
