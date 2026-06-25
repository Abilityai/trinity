"""
Unit tests for orphaned task execution recovery on backend startup.

Tests the recover_orphaned_executions() function which checks running
schedule executions against agent containers and process registries,
marking orphaned ones as failed.

Issue: https://github.com/abilityai/trinity/issues/128
Module: src/backend/services/cleanup_service.py
"""

import asyncio
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

# Backend source path
_BACKEND = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'src', 'backend'
))

# ── Shared mocks ──────────────────────────────────────────────────────────
_mock_db = MagicMock()
_mock_capacity = AsyncMock()
_mock_capacity.release = AsyncMock()
_mock_capacity.release_if_matches = AsyncMock(return_value=True)
_mock_docker_svc = MagicMock()
_mock_agent_client = AsyncMock()
_AgentClientError = type('AgentClientError', (Exception,), {})

_SYS_MOCKS = {
    'database': Mock(db=_mock_db),
    'models': Mock(TaskExecutionStatus=Mock(RUNNING='running', FAILED='failed')),
    'services.capacity_manager': Mock(get_capacity_manager=Mock(return_value=_mock_capacity)),
    'services.docker_service': _mock_docker_svc,
    'services.agent_client': Mock(
        get_agent_client=Mock(return_value=_mock_agent_client),
        AgentClientError=_AgentClientError,
    ),
    'utils.helpers': Mock(utc_now_iso=Mock(return_value="2026-03-25T12:00:00Z")),
    'db_models': Mock(),
    'docker': Mock(),
}

# ── Load the module under test via importlib ──────────────────────────────
with patch.dict('sys.modules', _SYS_MOCKS):
    _spec = importlib.util.spec_from_file_location(
        "cleanup_service_under_test",
        os.path.join(_BACKEND, "services", "cleanup_service.py"),
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _recover_fn = _mod.recover_orphaned_executions


# ── Helpers ───────────────────────────────────────────────────────────────
def _make_execution(exec_id: str, agent_name: str) -> dict:
    return {
        "id": exec_id,
        "agent_name": agent_name,
        "started_at": "2026-03-25T10:00:00Z",
        "schedule_id": f"sched-{exec_id}",
    }


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _set_agent_registry(execution_ids: list[str]):
    """Configure mock agent client to return given execution IDs."""
    resp = Mock(status_code=200)
    resp.json.return_value = {
        "executions": [{"execution_id": eid} for eid in execution_ids]
    }
    _mock_agent_client.get.return_value = resp


# ── Tests ─────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_mocks():
    # #1103: reset return_value AND side_effect, not just call records. Plain
    # reset_mock() leaves configured return_value/side_effect in place, so a
    # side_effect set by one test (e.g. get_agent_container.side_effect in
    # test_multiple_agents_mixed, or get.side_effect=_AgentClientError) bled into
    # later tests under random ordering — side_effect wins over return_value, so
    # the recovery counts came out wrong ("assert 3 == 2"). These tests share
    # module-level mocks, so the reset must be total.
    for m in (
        _mock_db, _mock_capacity, _mock_docker_svc, _mock_agent_client,
    ):
        m.reset_mock(return_value=True, side_effect=True)


class TestRecoverOrphanedExecutions:
    pytestmark = pytest.mark.unit

    def test_no_running_executions(self):
        _mock_db.get_running_executions.return_value = []

        with patch.dict('sys.modules', _SYS_MOCKS):
            result = _run(_recover_fn())

        # Key-by-key — checks the stable per-counter contract from both
        # #748 (skipped_grace) and #749 (redis_slots_reclaimed).
        assert result["recovered"] == 0
        assert result["still_running"] == 0
        assert result["skipped_grace"] == 0
        assert result["errors"] == 0

    def test_container_down_marks_orphaned(self):
        _mock_db.get_running_executions.return_value = [
            _make_execution("exec-1", "agent-alpha")
        ]
        _mock_docker_svc.get_agent_container.return_value = None

        with patch.dict('sys.modules', _SYS_MOCKS):
            result = _run(_recover_fn())

        assert result["recovered"] == 1
        assert result["still_running"] == 0
        # RELIABILITY-005: _recover_execution now uses the CAS-guarded writer.
        kw = _mock_db.mark_execution_failed_by_watchdog.call_args[1]
        assert kw["execution_id"] == "exec-1"
        assert "orphaned" in kw["error_message"]
        _mock_capacity.release.assert_awaited_once_with("agent-alpha", "exec-1")

    def test_not_in_registry_marks_orphaned(self):
        _mock_db.get_running_executions.return_value = [
            _make_execution("exec-2", "agent-beta")
        ]
        _mock_docker_svc.get_agent_container.return_value = Mock(status="running")
        _set_agent_registry([])  # Empty registry

        with patch.dict('sys.modules', _SYS_MOCKS):
            result = _run(_recover_fn())

        assert result["recovered"] == 1
        _mock_capacity.release.assert_awaited_once_with("agent-beta", "exec-2")

    def test_in_registry_left_alone(self):
        _mock_db.get_running_executions.return_value = [
            _make_execution("exec-3", "agent-gamma")
        ]
        _mock_docker_svc.get_agent_container.return_value = Mock(status="running")
        _set_agent_registry(["exec-3"])

        with patch.dict('sys.modules', _SYS_MOCKS):
            result = _run(_recover_fn())

        assert result["recovered"] == 0
        assert result["still_running"] == 1
        _mock_db.mark_execution_failed_by_watchdog.assert_not_called()

    def test_multiple_agents_mixed(self):
        _mock_db.get_running_executions.return_value = [
            _make_execution("exec-a", "agent-up"),
            _make_execution("exec-b", "agent-up"),
            _make_execution("exec-c", "agent-down"),
        ]
        _mock_docker_svc.get_agent_container.side_effect = (
            lambda n: Mock(status="running") if n == "agent-up" else None
        )
        _set_agent_registry(["exec-a"])  # Only exec-a still running

        with patch.dict('sys.modules', _SYS_MOCKS):
            result = _run(_recover_fn())

        # exec-b not in registry + exec-c container down = 2 recovered
        assert result["recovered"] == 2
        assert result["still_running"] == 1
        assert _mock_db.mark_execution_failed_by_watchdog.call_count == 2

    def test_agent_unreachable_treats_as_orphaned(self):
        _mock_db.get_running_executions.return_value = [
            _make_execution("exec-t", "agent-slow")
        ]
        _mock_docker_svc.get_agent_container.return_value = Mock(status="running")
        _mock_agent_client.get.side_effect = _AgentClientError("Connection timeout")

        with patch.dict('sys.modules', _SYS_MOCKS):
            result = _run(_recover_fn())

        assert result["recovered"] == 1
        assert result["still_running"] == 0
