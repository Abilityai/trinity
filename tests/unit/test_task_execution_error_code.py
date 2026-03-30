"""
Unit tests for TaskExecutionErrorCode and TaskExecutionResult.

Verifies that:
- error_code field defaults to None (backward compatible)
- Timeout results carry error_code=TIMEOUT
- String comparison works (str, Enum)
- Existing results without error_code still work

Module: src/backend/services/task_execution_service.py
Issue: https://github.com/abilityai/trinity/issues/221
"""

import sys
from pathlib import Path
from unittest.mock import patch, Mock

# Add backend path for imports
_project_root = Path(__file__).resolve().parents[2]
backend_path = str(_project_root / 'src' / 'backend')
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

# Import directly from the file to avoid services/__init__.py chain
import importlib.util

_module_path = _project_root / 'src' / 'backend' / 'services' / 'task_execution_service.py'

# Mock dependencies that task_execution_service.py imports
with patch.dict('sys.modules', {
    'database': Mock(db=Mock()),
    'models': Mock(
        ActivityState=Mock(),
        ActivityType=Mock(),
        TaskExecutionStatus=Mock(FAILED="failed", SUCCESS="success", RUNNING="running", CANCELLED="cancelled"),
    ),
    'services.activity_service': Mock(),
    'services.slot_service': Mock(),
    'services.platform_prompt_service': Mock(),
    'utils.credential_sanitizer': Mock(),
    'httpx': Mock(),
}):
    spec = importlib.util.spec_from_file_location('task_execution_service', _module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    TaskExecutionResult = module.TaskExecutionResult
    TaskExecutionErrorCode = module.TaskExecutionErrorCode


class TestTaskExecutionErrorCode:
    """Test the error code enum."""

    def test_enum_values_are_strings(self):
        """Error codes should be usable as plain strings."""
        assert TaskExecutionErrorCode.TIMEOUT == "timeout"
        assert TaskExecutionErrorCode.CAPACITY == "capacity"
        assert TaskExecutionErrorCode.AUTH == "auth"
        assert TaskExecutionErrorCode.BILLING == "billing"
        assert TaskExecutionErrorCode.AGENT_ERROR == "agent_error"
        assert TaskExecutionErrorCode.NETWORK == "network"

    def test_string_comparison(self):
        """Callers should be able to compare error_code with plain strings."""
        code = TaskExecutionErrorCode.TIMEOUT
        assert code == "timeout"
        assert "timeout" == code

    def test_enum_is_truthy(self):
        """Error codes should be truthy (for if result.error_code checks)."""
        assert TaskExecutionErrorCode.TIMEOUT
        assert TaskExecutionErrorCode.CAPACITY


class TestTaskExecutionResult:
    """Test the result dataclass with error_code."""

    def test_default_error_code_is_none(self):
        """Results without error_code should default to None (backward compat)."""
        result = TaskExecutionResult(
            execution_id="test-123",
            status="failed",
            response="",
            error="Something went wrong",
        )
        assert result.error_code is None

    def test_timeout_result_has_error_code(self):
        """Timeout results should carry the TIMEOUT error code."""
        result = TaskExecutionResult(
            execution_id="test-456",
            status="failed",
            response="",
            error="Task execution timed out after 900 seconds",
            error_code=TaskExecutionErrorCode.TIMEOUT,
        )
        assert result.error_code == TaskExecutionErrorCode.TIMEOUT
        assert result.error_code == "timeout"

    def test_success_result_has_no_error_code(self):
        """Successful results should have no error code."""
        result = TaskExecutionResult(
            execution_id="test-789",
            status="success",
            response="Hello!",
        )
        assert result.error_code is None
        assert result.error is None

    def test_non_timeout_failure_has_no_error_code(self):
        """Non-timeout failures should have error_code=None until we add more codes."""
        result = TaskExecutionResult(
            execution_id="test-abc",
            status="failed",
            response="",
            error="Agent at capacity (3/3 parallel tasks running)",
        )
        assert result.error_code is None

    def test_error_code_check_in_router_pattern(self):
        """Simulate the message router's error handling pattern."""
        # Timeout case
        timeout_result = TaskExecutionResult(
            execution_id="t1",
            status="failed",
            response="",
            error="Task execution timed out after 120 seconds",
            error_code=TaskExecutionErrorCode.TIMEOUT,
        )
        if timeout_result.error_code == "timeout":
            msg = "timeout message"
        else:
            msg = "generic error"
        assert msg == "timeout message"

        # Non-timeout case
        other_result = TaskExecutionResult(
            execution_id="t2",
            status="failed",
            response="",
            error="Some other error",
        )
        if other_result.error_code == "timeout":
            msg = "timeout message"
        else:
            msg = "generic error"
        assert msg == "generic error"


class TestBackwardCompatibility:
    """Verify error_code doesn't break existing callers.

    Existing callers access: result.status, result.error, result.response, result.cost
    None of them access error_code. These tests simulate their patterns.
    """

    def test_internal_router_pattern(self):
        """routers/internal.py accesses status, response, cost — no error_code."""
        result = TaskExecutionResult(
            execution_id="exec-1",
            status="success",
            response="Task completed",
            cost=0.05,
            error_code=TaskExecutionErrorCode.TIMEOUT,  # even if set, callers ignore it
        )
        # internal.py lines 231-233
        response = {
            "status": result.status,
            "response": result.response,
            "cost": result.cost,
        }
        assert response["status"] == "success"
        assert response["response"] == "Task completed"
        assert response["cost"] == 0.05

    def test_public_router_pattern(self):
        """routers/public.py checks result.status == 'failed' and reads result.error."""
        result = TaskExecutionResult(
            execution_id="exec-2",
            status="failed",
            response="",
            error="Task execution timed out after 900 seconds",
            error_code=TaskExecutionErrorCode.TIMEOUT,
        )
        # public.py line 488-489
        if result.status == "failed":
            error = result.error or ""
        else:
            error = None
        assert error == "Task execution timed out after 900 seconds"

    def test_paid_router_pattern(self):
        """routers/paid.py accesses exec_result.response."""
        result = TaskExecutionResult(
            execution_id="exec-3",
            status="success",
            response="Payment processed",
        )
        # paid.py line 260
        assert result.response == "Payment processed"
        assert result.error_code is None

    def test_result_without_error_code_still_works(self):
        """Results created without error_code (all existing code paths except timeout)."""
        result = TaskExecutionResult(
            execution_id="exec-4",
            status="failed",
            response="",
            error="HTTP error: ConnectError",
        )
        # All existing access patterns work
        assert result.status == "failed"
        assert result.error == "HTTP error: ConnectError"
        assert result.response == ""
        assert result.cost is None
        assert result.error_code is None

    def test_error_code_accessible_as_attribute(self):
        """error_code is accessible directly and compares as string."""
        result = TaskExecutionResult(
            execution_id="exec-5",
            status="failed",
            response="",
            error="timed out",
            error_code=TaskExecutionErrorCode.TIMEOUT,
        )
        # Direct attribute access (how all callers use it)
        assert result.error_code == "timeout"
        assert result.error_code is not None

        # Without error_code
        result2 = TaskExecutionResult(
            execution_id="exec-6",
            status="success",
            response="ok",
        )
        assert result2.error_code is None
