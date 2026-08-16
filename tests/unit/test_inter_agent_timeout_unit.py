"""
Inter-agent timeout unit tests (test_inter_agent_timeout_unit.py)

Issue #418. Verifies that the inter-agent execution path honours the target
agent's configured `execution_timeout_seconds` (TIMEOUT-001) instead of a
hardcoded 600s ceiling. Covers:

- FanOutRequest model accepts omitted / None `timeout_seconds` and validates it.
- FanOutService dispatches each sub-task with `timeout_seconds=None` so the
  TaskExecutionService resolves the per-agent config, regardless of whether
  an outer fan-out deadline is set.
- When no outer deadline is set, the asyncio.timeout() wrap is skipped.
- When an outer deadline is set, the wrap is applied but sub-tasks still
  receive `timeout_seconds=None`.

Runs as a pure unit test — no backend container required.
"""

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add backend to path so relative imports inside the target modules resolve.
_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


# Stub database and utils so fan_out_service can be imported without a
# running backend.
_fake_db = MagicMock()
sys.modules.setdefault("database", types.SimpleNamespace(db=_fake_db))

if "utils.helpers" not in sys.modules:
    _helpers = types.ModuleType("utils.helpers")
    _helpers.utc_now = lambda: datetime.utcnow()
    _helpers.utc_now_iso = lambda: datetime.utcnow().isoformat() + "Z"
    _helpers.to_utc_iso = lambda v: str(v)
    _helpers.parse_iso_timestamp = lambda s: datetime.fromisoformat(s.rstrip("Z"))
    sys.modules["utils.helpers"] = _helpers


# Load fan_out_service directly by file path to bypass services/__init__.py
# which imports unrelated modules that need a full backend env.
if "services" not in sys.modules:
    sys.modules["services"] = types.ModuleType("services")

# Stub task_execution_service before fan_out_service imports it.
_fake_tes = types.ModuleType("services.task_execution_service")
_fake_tes.get_task_execution_service = MagicMock()
_fake_tes.TaskExecutionResult = MagicMock  # sentinel, only name is used by type hint
_fake_tes.TaskExecutionErrorCode = MagicMock  # ditto
sys.modules["services.task_execution_service"] = _fake_tes

_fos_path = os.path.join(_backend_path, "services", "fan_out_service.py")
_spec = importlib.util.spec_from_file_location(
    "services.fan_out_service", _fos_path
)
fos = importlib.util.module_from_spec(_spec)
sys.modules["services.fan_out_service"] = fos
_spec.loader.exec_module(fos)  # type: ignore[union-attr]

FanOutService = fos.FanOutService
FanOutTaskInput = fos.FanOutTaskInput


# Load the FanOutRequest pydantic model. Needs `dependencies` and `models`
# stubbed because routers/fan_out.py imports them at module load.
_fake_deps = types.ModuleType("dependencies")
_fake_deps.get_authorized_agent = lambda: None
_fake_deps.get_current_user = lambda: None
sys.modules.setdefault("dependencies", _fake_deps)

_fake_models = types.ModuleType("models")
_fake_models.User = MagicMock  # only referenced as a type hint
sys.modules.setdefault("models", _fake_models)

_fo_router_path = os.path.join(_backend_path, "routers", "fan_out.py")
if "routers" not in sys.modules:
    sys.modules["routers"] = types.ModuleType("routers")
_spec2 = importlib.util.spec_from_file_location(
    "routers.fan_out", _fo_router_path
)
fo_router = importlib.util.module_from_spec(_spec2)
sys.modules["routers.fan_out"] = fo_router
_spec2.loader.exec_module(fo_router)  # type: ignore[union-attr]

FanOutRequest = fo_router.FanOutRequest


# Override the backend-requiring autouse fixtures from the package conftest.
@pytest.fixture(scope="session")
def api_client():
    yield None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    yield


# ---------------------------------------------------------------------------
# FanOutRequest model
# ---------------------------------------------------------------------------


def test_fan_out_request_allows_omitted_timeout():
    """Issue #418: timeout_seconds is optional and defaults to None."""
    req = FanOutRequest(tasks=[{"id": "t1", "message": "hi"}])
    assert req.timeout_seconds is None


def test_fan_out_request_accepts_explicit_timeout():
    req = FanOutRequest(
        tasks=[{"id": "t1", "message": "hi"}],
        timeout_seconds=300,
    )
    assert req.timeout_seconds == 300


def test_fan_out_request_rejects_out_of_range_timeout():
    with pytest.raises(Exception):
        FanOutRequest(
            tasks=[{"id": "t1", "message": "hi"}],
            timeout_seconds=5,
        )
    with pytest.raises(Exception):
        FanOutRequest(
            tasks=[{"id": "t1", "message": "hi"}],
            timeout_seconds=10_000,
        )


def test_fan_out_request_accepts_none_explicitly():
    """Explicit None round-trips — no validator error."""
    req = FanOutRequest(
        tasks=[{"id": "t1", "message": "hi"}],
        timeout_seconds=None,
    )
    assert req.timeout_seconds is None


# ---------------------------------------------------------------------------
# FanOutService — per-subtask timeout forwarding
# ---------------------------------------------------------------------------


def _make_success_result():
    """Build a minimal TaskExecutionResult-shaped object."""
    r = MagicMock()
    r.status = "success"
    r.response = "ok"
    r.execution_id = "exec_1"
    r.cost = None
    r.context_used = None
    r.error = None
    r.error_code = None
    return r


def _install_mock_task_service(call_log: list):
    """Install a mock task execution service that records each call."""
    async def _execute_task(**kwargs):
        call_log.append(kwargs)
        return _make_success_result()

    svc = MagicMock()
    svc.execute_task = AsyncMock(side_effect=_execute_task)
    fos.get_task_execution_service = lambda: svc
    return svc


def test_fan_out_service_forwards_none_per_subtask_without_outer_deadline():
    """Issue #418: each sub-task is dispatched with timeout_seconds=None so
    TaskExecutionService resolves the agent's configured timeout."""
    calls: list = []
    _install_mock_task_service(calls)

    service = FanOutService()
    tasks = [FanOutTaskInput(id=f"t{i}", message=f"task {i}") for i in range(3)]

    result = asyncio.run(
        service.execute(
            agent_name="delegate-1",
            tasks=tasks,
            max_concurrency=3,
            timeout_seconds=None,
        )
    )

    assert result.total == 3
    assert result.completed == 3
    assert len(calls) == 3
    for call in calls:
        assert call["timeout_seconds"] is None, (
            "Per-subtask timeout must be None so the agent's configured "
            "execution_timeout_seconds is used (TIMEOUT-001)"
        )
        assert call["agent_name"] == "delegate-1"


def test_fan_out_service_forwards_none_per_subtask_with_outer_deadline():
    """Outer fan-out deadline wraps the gather, but each sub-task is still
    dispatched with timeout_seconds=None (per-agent config applies)."""
    calls: list = []
    _install_mock_task_service(calls)

    service = FanOutService()
    tasks = [FanOutTaskInput(id="t1", message="one")]

    result = asyncio.run(
        service.execute(
            agent_name="delegate-2",
            tasks=tasks,
            max_concurrency=1,
            timeout_seconds=300,
        )
    )

    assert result.total == 1
    assert result.completed == 1
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] is None


def test_fan_out_service_outer_deadline_actually_applies():
    """Pre-existing behaviour: when outer deadline is set and subtasks
    exceed it, remaining tasks are marked deadline-exceeded."""
    async def _slow_task(**kwargs):
        await asyncio.sleep(5)
        return _make_success_result()

    svc = MagicMock()
    svc.execute_task = AsyncMock(side_effect=_slow_task)
    fos.get_task_execution_service = lambda: svc

    service = FanOutService()
    tasks = [FanOutTaskInput(id="t1", message="slow")]

    result = asyncio.run(
        service.execute(
            agent_name="delegate-3",
            tasks=tasks,
            max_concurrency=1,
            timeout_seconds=1,
        )
    )

    assert result.status == "deadline_exceeded"
    assert result.results[0].error_code == "timeout"
