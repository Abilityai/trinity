"""
Unit tests for #767: CB probe executions left open until backend restart.

Two bugs fixed:
- Fix A: execute_task now fast-fails (closes the execution record) when the
  circuit breaker is open, rather than attempting a long-running HTTP call
  that would be left dangling until cleanup_service's 120-minute stale sweep.
- Fix B: asyncio.CancelledError (Python 3.11+ BaseException) is now caught in
  both execute_task and _execute_task_internal_background so that execution
  records are closed synchronously on backend shutdown, preventing the cleanup
  service from inflating failure duration.
"""

import asyncio
import os
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirror pattern from test_watchdog_unit.py
# ---------------------------------------------------------------------------
_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

# ---------------------------------------------------------------------------
# Stub heavy dependencies before importing backend modules
# ---------------------------------------------------------------------------
_helpers_mod = types.ModuleType("utils.helpers")
_helpers_mod.utc_now = lambda: datetime.utcnow()
_helpers_mod.utc_now_iso = lambda: datetime.utcnow().isoformat() + "Z"
_helpers_mod.iso_cutoff = lambda hours: datetime.utcnow().isoformat() + "Z"
_helpers_mod.parse_iso_timestamp = lambda s: datetime.utcnow()
_helpers_mod.to_utc_iso = lambda *a, **k: datetime.utcnow().isoformat() + "Z"
sys.modules.setdefault("utils.helpers", _helpers_mod)

def _install_sanitizer_stub() -> None:
    """Install our credential_sanitizer stub even if another test (e.g.
    test_validation.py) already cached an incomplete one in sys.modules.

    Using setdefault here was a bug: test_validation.py installs a partial
    stub at module-collection time with only `sanitize_text`, so our
    setdefault is a no-op and `from utils.credential_sanitizer import
    sanitize_execution_log` (inside the real task_execution_service.py)
    fails with ImportError when our fixture re-imports the service.
    """
    sanitizer = types.ModuleType("utils.credential_sanitizer")
    sanitizer.sanitize_response = lambda x: x
    sanitizer.sanitize_execution_log = lambda x: x
    sanitizer.sanitize_text = lambda x: x
    sanitizer.sanitize_dict = lambda x: x
    sys.modules["utils.credential_sanitizer"] = sanitizer


_install_sanitizer_stub()

sys.modules.setdefault("database", MagicMock())

# Stub redis to prevent connection attempts
_redis_mod = types.ModuleType("redis")
_redis_mod.Redis = MagicMock
_redis_mod.ConnectionError = ConnectionError
sys.modules.setdefault("redis", _redis_mod)

# Stub APScheduler and other heavy deps
for _stub in (
    "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.asyncio",
    "docker", "docker.errors", "docker.types",
):
    sys.modules.setdefault(_stub, MagicMock())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_execution(status="running"):
    ex = MagicMock()
    ex.id = "exec-test-001"
    ex.status = status
    return ex


# Issue #678 cluster A: cross-file sys.modules pollution defense.
#
# test_validation.py installs an incomplete `utils.credential_sanitizer`
# stub at module-collection time:
#     sys.modules["utils.credential_sanitizer"] = _stub  # only sanitize_text
# Our file used `setdefault` (no-op once polluted), so the polluted stub
# wins and the real `services.task_execution_service` re-import fails with
#     ImportError: cannot import name 'sanitize_dict'
#       from 'utils.credential_sanitizer'
# (because #678 added sanitize_dict to the salvage path imports).
#
# In addition, some prior tests stub `services.task_execution_service`
# itself as a MagicMock; tests/conftest.py's _SYS_MODULES_BASELINE captures
# `None` for that key (not preloaded), so the autouse restore is a no-op
# and the MagicMock persists. The re-import then returns a MagicMock
# class, and `await svc.execute_task(...)` raises "MagicMock can't be used
# in await expression".
#
# Defense: before every test, overwrite the sanitizer stub with our
# complete one and evict the task_execution_service module so the test's
# import statement loads the real class against our complete stub.
@pytest.fixture(autouse=True)
def _restore_complete_stubs(monkeypatch):
    """Re-assert our complete stubs against cross-file pollution.

    Uses monkeypatch so the lint at tests/lint_sys_modules.py (#762)
    stays green and the mutations auto-revert on teardown.
    """
    monkeypatch.setitem(sys.modules, "utils.credential_sanitizer", _sanitizer_mod)
    monkeypatch.delitem(sys.modules, "services.task_execution_service", raising=False)
    yield


# ===========================================================================
# Fix A: Circuit breaker fast-fail
# ===========================================================================

class TestCircuitBreakerFastFail:
    """execute_task closes the execution record immediately when CB is open."""

    pytestmark = pytest.mark.unit

    @pytest.fixture(autouse=True)
    def _patch_env(self):
        """Ensure backend config can load without real env vars.

        Also evict any cross-file `services.task_execution_service` stub
        (test_validation.py installs a plain MagicMock at module-collection
        time). The conftest baseline-restore can't help because the baseline
        was None — the real module isn't preloadable from conftest without
        TRINITY_DB_PATH set. Force a fresh import so `TaskExecutionService`
        resolves to the real coroutine class, not a MagicMock attribute that
        would fail `await svc.execute_task(...)`.
        """
        env_patch = {
            "REDIS_URL": "redis://test:test@localhost:6379",
            "REDIS_PASSWORD": "test",
            "REDIS_BACKEND_PASSWORD": "test",
            "SECRET_KEY": "test-secret-key",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            _install_sanitizer_stub()
            sys.modules.pop("services.task_execution_service", None)
            yield

    def _make_task_service(self):
        """Return a TaskExecutionService with all external deps mocked."""
        from services.task_execution_service import TaskExecutionService, TaskExecutionStatus

        svc = TaskExecutionService.__new__(TaskExecutionService)
        return svc

    @pytest.mark.asyncio
    async def test_cb_open_fails_execution_record(self):
        """When CB is open, execute_task marks execution FAILED immediately."""
        from services.task_execution_service import (
            TaskExecutionService, TaskExecutionStatus, TaskExecutionErrorCode,
        )

        mock_db = MagicMock()
        existing_exec = _make_execution("running")
        mock_db.get_execution.return_value = existing_exec
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = existing_exec
        mock_db.mark_execution_dispatched.return_value = None

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_activity_svc = MagicMock()
        mock_activity_svc.track_activity = AsyncMock(return_value="act-001")
        mock_activity_svc.complete_activity = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = False

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", mock_activity_svc),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
        ):
            svc = TaskExecutionService()
            result = await svc.execute_task(
                agent_name="test-agent",
                message="hello",
                triggered_by="schedule",
                execution_id="exec-test-001",
            )

        assert result.status == TaskExecutionStatus.FAILED
        assert result.error_code == TaskExecutionErrorCode.CIRCUIT_OPEN
        assert "circuit breaker" in result.error.lower()

        mock_db.update_execution_status.assert_called_once()
        call_kwargs = mock_db.update_execution_status.call_args
        assert call_kwargs.kwargs.get("status") == TaskExecutionStatus.FAILED

    @pytest.mark.asyncio
    async def test_cb_open_does_not_call_agent(self):
        """When CB is open, agent_post_with_retry is never called."""
        from services.task_execution_service import TaskExecutionService, TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = False

        mock_post = AsyncMock()

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", MagicMock(
                track_activity=AsyncMock(return_value="act-001"),
                complete_activity=AsyncMock(),
            )),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.agent_post_with_retry", mock_post),
        ):
            svc = TaskExecutionService()
            await svc.execute_task(
                agent_name="test-agent",
                message="hello",
                triggered_by="schedule",
                execution_id="exec-test-001",
            )

        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_cb_open_does_not_mark_dispatched(self):
        """When CB is open, execution is NOT marked dispatched (keeps caught by short-circuit cleanup)."""
        from services.task_execution_service import TaskExecutionService

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = False

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", MagicMock(
                track_activity=AsyncMock(return_value=None),
                complete_activity=AsyncMock(),
            )),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
        ):
            svc = TaskExecutionService()
            await svc.execute_task(
                agent_name="test-agent",
                message="hello",
                triggered_by="schedule",
                execution_id="exec-test-001",
            )

        mock_db.mark_execution_dispatched.assert_not_called()

    @pytest.mark.asyncio
    async def test_cb_closed_proceeds_normally(self):
        """When CB is closed, execute_task calls agent_post_with_retry as usual."""
        from services.task_execution_service import TaskExecutionService, TaskExecutionStatus
        import httpx

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = True  # CB closed

        # Simulate a successful agent response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "response": "done",
            "session_id": "sess-001",
            "metadata": {"cost_usd": 0.01, "input_tokens": 100, "context_window": 200000},
            "execution_log": [],
        }
        mock_post = AsyncMock(return_value=mock_response)

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", MagicMock(
                track_activity=AsyncMock(return_value="act-001"),
                complete_activity=AsyncMock(),
            )),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.agent_post_with_retry", mock_post),
        ):
            svc = TaskExecutionService()
            result = await svc.execute_task(
                agent_name="test-agent",
                message="hello",
                triggered_by="schedule",
                execution_id="exec-test-001",
            )

        mock_post.assert_called_once()
        assert result.status == TaskExecutionStatus.SUCCESS


# ===========================================================================
# Fix B: asyncio.CancelledError in execute_task
# ===========================================================================

class TestCancelledErrorInExecuteTask:
    """execute_task closes the execution record when cancelled by backend shutdown."""

    pytestmark = pytest.mark.unit

    @pytest.fixture(autouse=True)
    def _patch_env(self):
        """See TestCircuitBreakerFastFail._patch_env — same stub-eviction rationale."""
        env_patch = {
            "REDIS_URL": "redis://test:test@localhost:6379",
            "REDIS_PASSWORD": "test",
            "REDIS_BACKEND_PASSWORD": "test",
            "SECRET_KEY": "test-secret-key",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            _install_sanitizer_stub()
            sys.modules.pop("services.task_execution_service", None)
            yield

    @pytest.mark.asyncio
    async def test_cancelled_error_marks_execution_failed(self):
        """When execute_task is cancelled mid-flight, the open execution is closed."""
        from services.task_execution_service import TaskExecutionService, TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = True

        # Simulate cancellation during agent HTTP call
        mock_post = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", MagicMock(
                track_activity=AsyncMock(return_value=None),
                complete_activity=AsyncMock(),
            )),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.agent_post_with_retry", mock_post),
        ):
            svc = TaskExecutionService()
            with pytest.raises(asyncio.CancelledError):
                await svc.execute_task(
                    agent_name="test-agent",
                    message="hello",
                    triggered_by="schedule",
                    execution_id="exec-test-001",
                )

        # Execution must be closed with FAILED status
        mock_db.update_execution_status.assert_called_once()
        call_kwargs = mock_db.update_execution_status.call_args
        assert call_kwargs.kwargs.get("status") == TaskExecutionStatus.FAILED
        assert "cancelled" in (call_kwargs.kwargs.get("error") or "").lower()

    @pytest.mark.asyncio
    async def test_cancelled_error_is_reraised(self):
        """CancelledError must propagate after cleanup so asyncio can cancel the task."""
        from services.task_execution_service import TaskExecutionService

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = True
        mock_post = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", MagicMock(
                track_activity=AsyncMock(return_value=None),
                complete_activity=AsyncMock(),
            )),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.agent_post_with_retry", mock_post),
        ):
            svc = TaskExecutionService()
            with pytest.raises(asyncio.CancelledError):
                await svc.execute_task(
                    agent_name="test-agent",
                    message="hello",
                    triggered_by="schedule",
                    execution_id="exec-test-001",
                )

    @pytest.mark.asyncio
    async def test_cancelled_error_skips_already_terminal_execution(self):
        """If execution is already terminal, cancel handler doesn't overwrite it."""
        from services.task_execution_service import TaskExecutionService, TaskExecutionStatus

        mock_db = MagicMock()
        # Already marked CANCELLED by another path
        mock_db.get_execution.return_value = _make_execution(TaskExecutionStatus.CANCELLED)
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = True
        mock_post = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", MagicMock(
                track_activity=AsyncMock(return_value=None),
                complete_activity=AsyncMock(),
            )),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.agent_post_with_retry", mock_post),
        ):
            svc = TaskExecutionService()
            with pytest.raises(asyncio.CancelledError):
                await svc.execute_task(
                    agent_name="test-agent",
                    message="hello",
                    triggered_by="schedule",
                    execution_id="exec-test-001",
                )

        mock_db.update_execution_status.assert_not_called()


# ===========================================================================
# Fix B2: asyncio.CancelledError in _execute_task_internal_background
# ===========================================================================

class TestCancelledErrorInBackground:
    """_execute_task_internal_background closes execution on cancel."""

    pytestmark = pytest.mark.unit

    @pytest.mark.asyncio
    async def test_background_cancelled_marks_execution_failed(self):
        """When execute_task raises CancelledError, background wrapper closes the record."""
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")

        mock_task_service = MagicMock()
        mock_task_service.execute_task = AsyncMock(side_effect=asyncio.CancelledError())

        request = MagicMock()
        request.agent_name = "test-agent"
        request.message = "hello"
        request.triggered_by = "schedule"
        request.model = None
        request.timeout_seconds = 900
        request.allowed_tools = None
        request.execution_id = "exec-bg-001"
        request.attempt = 1
        request.schedule_id = None
        request.schedule_name = None
        request.schedule_cron = None
        request.schedule_next_run = None

        with patch("routers.internal.db", mock_db):
            import routers.internal as internal_mod
            with pytest.raises(asyncio.CancelledError):
                await internal_mod._execute_task_internal_background(mock_task_service, request)

        mock_db.update_execution_status.assert_called_once()
        call_kwargs = mock_db.update_execution_status.call_args
        assert call_kwargs.kwargs.get("status") == TaskExecutionStatus.FAILED
        assert "cancelled" in (call_kwargs.kwargs.get("error") or "").lower()

    @pytest.mark.asyncio
    async def test_background_cancelled_reraises(self):
        """CancelledError must propagate from the background wrapper."""
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")

        mock_task_service = MagicMock()
        mock_task_service.execute_task = AsyncMock(side_effect=asyncio.CancelledError())

        request = MagicMock()
        request.agent_name = "test-agent"
        request.message = "hello"
        request.triggered_by = "schedule"
        request.model = None
        request.timeout_seconds = 900
        request.allowed_tools = None
        request.execution_id = "exec-bg-001"
        request.attempt = 1
        request.schedule_id = None
        request.schedule_name = None
        request.schedule_cron = None
        request.schedule_next_run = None

        with patch("routers.internal.db", mock_db):
            import routers.internal as internal_mod
            with pytest.raises(asyncio.CancelledError):
                await internal_mod._execute_task_internal_background(mock_task_service, request)

    @pytest.mark.asyncio
    async def test_background_cancelled_skips_already_terminal(self):
        """Cancel handler doesn't overwrite already-terminal execution in background."""
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution(TaskExecutionStatus.SUCCESS)

        mock_task_service = MagicMock()
        mock_task_service.execute_task = AsyncMock(side_effect=asyncio.CancelledError())

        request = MagicMock()
        request.agent_name = "test-agent"
        request.execution_id = "exec-bg-001"
        request.message = "hello"
        request.triggered_by = "schedule"
        request.model = None
        request.timeout_seconds = 900
        request.allowed_tools = None
        request.attempt = 1
        request.schedule_id = None
        request.schedule_name = None
        request.schedule_cron = None
        request.schedule_next_run = None

        with patch("routers.internal.db", mock_db):
            import routers.internal as internal_mod
            with pytest.raises(asyncio.CancelledError):
                await internal_mod._execute_task_internal_background(mock_task_service, request)

        mock_db.update_execution_status.assert_not_called()
