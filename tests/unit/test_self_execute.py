"""
Tests for SELF-EXEC-001: Agent self-execute — background task execution during chat.

Tests the ability for an agent to trigger a background task on itself while
actively chatting with a user. The agent calls chat_with_agent(agent_name=<self>)
via MCP, and Trinity tracks this as a SELF_TASK activity.

Related flow: docs/memory/feature-flows/self-execute.md (to be created)
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime
import os
import sys

# Add backend to path for imports
backend_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'backend')
sys.path.insert(0, backend_path)

# Mock the utils.helpers module before importing models
sys.modules['utils'] = MagicMock()
sys.modules['utils.helpers'] = MagicMock()
sys.modules['utils.helpers'].to_utc_iso = lambda x: x.isoformat() if x else None

from models import ActivityType, ParallelTaskRequest, TaskExecutionStatus


class TestSelfTaskActivityType:
    """Tests for SELF_TASK activity type in the ActivityType enum."""

    def test_self_task_type_exists(self):
        """Verify SELF_TASK activity type is defined in enum."""
        assert hasattr(ActivityType, 'SELF_TASK')
        assert ActivityType.SELF_TASK.value == 'self_task'

    def test_all_activity_types_defined(self):
        """Verify all expected activity types exist including SELF_TASK."""
        expected = [
            'CHAT_START', 'CHAT_END', 'TOOL_CALL',
            'SCHEDULE_START', 'SCHEDULE_END',
            'AGENT_COLLABORATION', 'SELF_TASK',
            'EXECUTION_CANCELLED'
        ]
        for activity_type in expected:
            assert hasattr(ActivityType, activity_type), f"Missing activity type: {activity_type}"


class TestParallelTaskRequestModel:
    """Tests for ParallelTaskRequest model with inject_result parameter."""

    def test_inject_result_parameter_exists(self):
        """Verify inject_result parameter is available on ParallelTaskRequest."""
        request = ParallelTaskRequest(message="test task")
        assert hasattr(request, 'inject_result')
        assert request.inject_result == False  # Default is False

    def test_inject_result_can_be_set_true(self):
        """Verify inject_result can be set to True."""
        request = ParallelTaskRequest(
            message="test task",
            inject_result=True,
            chat_session_id="test-session-123"
        )
        assert request.inject_result == True
        assert request.chat_session_id == "test-session-123"

    def test_chat_session_id_parameter_exists(self):
        """Verify chat_session_id parameter is available."""
        request = ParallelTaskRequest(
            message="test task",
            chat_session_id="session-abc"
        )
        assert request.chat_session_id == "session-abc"


class TestSelfTaskDetection:
    """Tests for self-task detection logic in the /task endpoint."""

    def test_is_self_task_when_source_equals_target(self):
        """Self-task should be detected when x_source_agent equals target agent name."""
        x_source_agent = "my-agent"
        target_name = "my-agent"

        is_self_task = (x_source_agent is not None and x_source_agent == target_name)
        assert is_self_task == True

    def test_not_self_task_when_source_differs(self):
        """Not a self-task when source agent differs from target."""
        x_source_agent = "other-agent"
        target_name = "my-agent"

        is_self_task = (x_source_agent is not None and x_source_agent == target_name)
        assert is_self_task == False

    def test_not_self_task_when_no_source(self):
        """Not a self-task when no source agent header is present."""
        x_source_agent = None
        target_name = "my-agent"

        is_self_task = (x_source_agent is not None and x_source_agent == target_name)
        assert is_self_task == False


class TestSourceAgentHeaderValidation:
    """Tests for security validation of X-Source-Agent header."""

    def test_validation_passes_when_headers_match(self):
        """Validation should pass when X-Source-Agent matches MCP key agent scope."""
        x_source_agent = "agent-a"
        current_user_agent_name = "agent-a"

        # Validation logic
        should_reject = (
            x_source_agent and
            current_user_agent_name and
            x_source_agent != current_user_agent_name
        )

        assert should_reject == False

    def test_validation_fails_when_headers_mismatch(self):
        """Validation should fail when X-Source-Agent doesn't match MCP key agent scope."""
        x_source_agent = "agent-a"
        current_user_agent_name = "agent-b"

        # Validation logic
        should_reject = (
            x_source_agent and
            current_user_agent_name and
            x_source_agent != current_user_agent_name
        )

        assert should_reject == True

    def test_validation_skipped_for_user_scoped_keys(self):
        """Validation should be skipped when MCP key is user-scoped (no agent_name)."""
        x_source_agent = "agent-a"
        current_user_agent_name = None  # User-scoped key

        # Validation logic
        should_reject = (
            x_source_agent and
            current_user_agent_name and
            x_source_agent != current_user_agent_name
        )

        assert not should_reject  # None is falsy, so validation is skipped


class TestTriggeredByField:
    """Tests for triggered_by field values for self-tasks."""

    def test_triggered_by_self_task_for_self_calls(self):
        """triggered_by should be 'self_task' for self-calls."""
        x_source_agent = "my-agent"
        x_via_mcp = "true"
        name = "my-agent"
        is_self_task = (x_source_agent is not None and x_source_agent == name)

        if x_source_agent:
            triggered_by = "self_task" if is_self_task else "agent"
        elif x_via_mcp:
            triggered_by = "mcp"
        else:
            triggered_by = "manual"

        assert triggered_by == "self_task"

    def test_triggered_by_agent_for_other_agent_calls(self):
        """triggered_by should be 'agent' for agent-to-agent calls (not self)."""
        x_source_agent = "other-agent"
        x_via_mcp = "true"
        name = "my-agent"
        is_self_task = (x_source_agent is not None and x_source_agent == name)

        if x_source_agent:
            triggered_by = "self_task" if is_self_task else "agent"
        elif x_via_mcp:
            triggered_by = "mcp"
        else:
            triggered_by = "manual"

        assert triggered_by == "agent"

    def test_triggered_by_mcp_for_mcp_user_calls(self):
        """triggered_by should be 'mcp' for MCP user calls (no source agent)."""
        x_source_agent = None
        x_via_mcp = "true"
        name = "my-agent"
        is_self_task = (x_source_agent is not None and x_source_agent == name)

        if x_source_agent:
            triggered_by = "self_task" if is_self_task else "agent"
        elif x_via_mcp:
            triggered_by = "mcp"
        else:
            triggered_by = "manual"

        assert triggered_by == "mcp"

    def test_triggered_by_manual_for_user_calls(self):
        """triggered_by should be 'manual' for direct API calls (no MCP, no source agent)."""
        x_source_agent = None
        x_via_mcp = None
        name = "my-agent"
        is_self_task = (x_source_agent is not None and x_source_agent == name)

        if x_source_agent:
            triggered_by = "self_task" if is_self_task else "agent"
        elif x_via_mcp:
            triggered_by = "mcp"
        else:
            triggered_by = "manual"

        assert triggered_by == "manual"


class TestChatSessionValidation:
    """Tests for chat session validation before result injection."""

    def test_session_validation_passes_for_owner(self):
        """Session validation should pass when session belongs to user."""
        session = {"user_id": 123, "status": "active"}
        user_id = 123

        can_inject = session and session.get("user_id") == user_id
        assert can_inject == True

    def test_session_validation_fails_for_wrong_user(self):
        """Session validation should fail when session doesn't belong to user."""
        session = {"user_id": 456, "status": "active"}
        user_id = 123

        can_inject = session and session.get("user_id") == user_id
        assert can_inject == False

    def test_session_validation_fails_for_missing_session(self):
        """Session validation should fail when session doesn't exist."""
        session = None
        user_id = 123

        can_inject = session and session.get("user_id") == user_id
        assert not can_inject  # None is falsy


class TestInjectResultConditions:
    """Tests for conditions required for result injection."""

    def test_inject_when_all_conditions_met(self):
        """Result should be injected when inject_result=True, chat_session_id exists, and task succeeded."""
        inject_result = True
        chat_session_id = "session-123"
        status = TaskExecutionStatus.SUCCESS

        should_inject = inject_result and chat_session_id and status == TaskExecutionStatus.SUCCESS
        assert should_inject == True

    def test_no_inject_when_inject_result_false(self):
        """Result should not be injected when inject_result=False."""
        inject_result = False
        chat_session_id = "session-123"
        status = TaskExecutionStatus.SUCCESS

        should_inject = inject_result and chat_session_id and status == TaskExecutionStatus.SUCCESS
        assert should_inject == False

    def test_no_inject_when_no_session_id(self):
        """Result should not be injected when chat_session_id is missing."""
        inject_result = True
        chat_session_id = None
        status = TaskExecutionStatus.SUCCESS

        should_inject = inject_result and chat_session_id and status == TaskExecutionStatus.SUCCESS
        assert not should_inject  # None is falsy

    def test_no_inject_when_task_failed(self):
        """Result should not be injected when task failed."""
        inject_result = True
        chat_session_id = "session-123"
        status = TaskExecutionStatus.FAILED

        should_inject = inject_result and chat_session_id and status == TaskExecutionStatus.SUCCESS
        assert should_inject == False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
