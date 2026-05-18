"""Timeout hardening tests for TaskExecutionService.

Pins ERI-286 behavior: when the backend times out waiting for an
agent task response, the persisted failure state identifies the timeout kind
and records whether the best-effort terminate call reached the agent.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest


class _TaskExecutionStatus:
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class _ActivityState:
    COMPLETED = "completed"
    FAILED = "failed"


class _ActivityType:
    CHAT_START = "chat_start"


class _FakeDb:
    def __init__(self):
        self.status_updates = []
        self.dispatched = []

    def get_max_parallel_tasks(self, agent_name):
        return 1

    def mark_execution_dispatched(self, execution_id):
        self.dispatched.append(execution_id)
        return True

    def get_execution(self, execution_id):
        return types.SimpleNamespace(id=execution_id, status=_TaskExecutionStatus.RUNNING)

    def update_execution_status(self, **kwargs):
        self.status_updates.append(kwargs)
        return True


class _FakeActivityService:
    def __init__(self):
        self.started = []
        self.completed = []

    async def track_activity(self, **kwargs):
        self.started.append(kwargs)
        return "activity-286"

    async def complete_activity(self, **kwargs):
        self.completed.append(kwargs)


class _FakeCapacity:
    def __init__(self):
        self.acquired = []
        self.released = []

    async def acquire(self, **kwargs):
        self.acquired.append(kwargs)
        return types.SimpleNamespace(state="admitted")

    async def release(self, agent_name, execution_id):
        self.released.append((agent_name, execution_id))


def _load_task_execution_service(monkeypatch):
    """Load the service module with narrow stubs for its backend dependencies."""
    module_name = "task_execution_service_timeout_under_test"
    sys.modules.pop(module_name, None)

    fake_db = _FakeDb()
    fake_activity = _FakeActivityService()
    fake_capacity = _FakeCapacity()

    database_mod = types.ModuleType("database")
    database_mod.db = fake_db
    monkeypatch.setitem(sys.modules, "database", database_mod)

    models_mod = types.ModuleType("models")
    models_mod.ActivityState = _ActivityState
    models_mod.ActivityType = _ActivityType
    models_mod.TaskExecutionStatus = _TaskExecutionStatus
    monkeypatch.setitem(sys.modules, "models", models_mod)

    services_pkg = types.ModuleType("services")
    services_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "services", services_pkg)

    activity_mod = types.ModuleType("services.activity_service")
    activity_mod.activity_service = fake_activity
    monkeypatch.setitem(sys.modules, "services.activity_service", activity_mod)

    capacity_mod = types.ModuleType("services.capacity_manager")
    capacity_mod.CapacityFull = type("CapacityFull", (Exception,), {})
    capacity_mod.get_capacity_manager = lambda: fake_capacity
    monkeypatch.setitem(sys.modules, "services.capacity_manager", capacity_mod)

    platform_mod = types.ModuleType("services.platform_prompt_service")

    class ExecutionContext:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        @staticmethod
        def derive_mode(triggered_by):
            return triggered_by

    platform_mod.ExecutionContext = ExecutionContext
    platform_mod.compose_system_prompt = lambda **kwargs: "system prompt"
    platform_mod.get_platform_system_prompt = lambda: "platform prompt"
    platform_mod.is_execution_context_enabled = lambda: False
    monkeypatch.setitem(sys.modules, "services.platform_prompt_service", platform_mod)

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "utils", utils_pkg)

    sanitizer_mod = types.ModuleType("utils.credential_sanitizer")
    sanitizer_mod.sanitize_execution_log = lambda value: value
    sanitizer_mod.sanitize_response = lambda value: value
    monkeypatch.setitem(sys.modules, "utils.credential_sanitizer", sanitizer_mod)

    service_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "backend"
        / "services"
        / "task_execution_service.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, service_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, fake_db, fake_activity, fake_capacity


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_timeout_records_timeout_kind_and_termination_result(monkeypatch):
    module, fake_db, fake_activity, fake_capacity = _load_task_execution_service(
        monkeypatch
    )

    async def raise_read_timeout(*args, **kwargs):
        raise httpx.ReadTimeout("agent stopped responding")

    terminate = AsyncMock(return_value=False)
    monkeypatch.setattr(module, "agent_post_with_retry", raise_read_timeout)
    monkeypatch.setattr(module, "terminate_execution_on_agent", terminate)

    result = await module.TaskExecutionService().execute_task(
        agent_name="inbox",
        message="run scheduled work",
        triggered_by="schedule",
        timeout_seconds=1,
        execution_id="exec-eri-286",
    )

    assert result.status == _TaskExecutionStatus.FAILED
    assert result.error_code == module.TaskExecutionErrorCode.TIMEOUT
    assert result.raw_response == {
        "failure_kind": "timeout",
        "timeout_kind": "read_timeout",
        "termination_attempted": True,
        "termination_succeeded": False,
    }

    terminate.assert_awaited_once_with("inbox", "exec-eri-286")
    assert fake_capacity.released == [("inbox", "exec-eri-286")]

    assert fake_db.dispatched == ["exec-eri-286"]
    assert len(fake_db.status_updates) == 1
    update = fake_db.status_updates[0]
    assert update["execution_id"] == "exec-eri-286"
    assert update["status"] == _TaskExecutionStatus.FAILED
    assert "Execution timeout (read_timeout)" in update["error"]
    assert "termination_attempted=true" in update["error"]
    assert "termination_succeeded=false" in update["error"]

    assert fake_activity.completed[-1]["status"] == _ActivityState.FAILED
    assert fake_activity.completed[-1]["error"] == update["error"]
