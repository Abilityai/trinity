"""
Timeout Termination Tests (test_timeout_termination.py)

Tests for Issue #61: Backend timeout triggers agent process termination.

When TaskExecutionService times out waiting for an agent response, it now
calls the agent's /api/executions/{id}/terminate endpoint to kill the
orphaned Claude process.

These are unit tests that mock the HTTP calls to test the termination logic.
The actual function is inlined here to avoid complex backend import dependencies.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx


# ---------------------------------------------------------------------------
# Inline copy of terminate_execution_on_agent for isolated testing
# (Avoids importing the full backend module graph)
# ---------------------------------------------------------------------------

TERMINATE_TIMEOUT = 5.0
logger = logging.getLogger(__name__)


async def terminate_execution_on_agent(
    agent_name: str,
    execution_id: str,
) -> bool:
    """
    Terminate an execution on an agent container (Issue #61).

    This is an inline copy of the production function for testing.
    """
    if not execution_id:
        return False

    agent_url = f"http://agent-{agent_name}:8000/api/executions/{execution_id}/terminate"

    try:
        async with httpx.AsyncClient(timeout=TERMINATE_TIMEOUT) as client:
            response = await client.post(agent_url)

            if response.status_code < 300:
                result = response.json()
                status = result.get("status", "unknown")
                if status == "terminated":
                    logger.info(
                        f"[TaskExecService] Terminated execution {execution_id} "
                        f"on agent '{agent_name}'"
                    )
                elif status == "already_finished":
                    logger.debug(
                        f"[TaskExecService] Execution {execution_id} already finished "
                        f"on agent '{agent_name}'"
                    )
                return True

            elif response.status_code == 404:
                logger.debug(
                    f"[TaskExecService] Execution {execution_id} not found on "
                    f"agent '{agent_name}' (may have finished)"
                )
                return True

            else:
                logger.warning(
                    f"[TaskExecService] Terminate returned {response.status_code} "
                    f"for execution {execution_id} on agent '{agent_name}'"
                )
                return False

    except httpx.TimeoutException:
        logger.warning(
            f"[TaskExecService] Terminate timed out for execution {execution_id} "
            f"on agent '{agent_name}' — watchdog will clean up"
        )
        return False

    except httpx.ConnectError:
        logger.warning(
            f"[TaskExecService] Could not reach agent '{agent_name}' to terminate "
            f"execution {execution_id} — watchdog will clean up"
        )
        return False

    except Exception as e:
        logger.warning(
            f"[TaskExecService] Error terminating execution {execution_id} "
            f"on agent '{agent_name}': {e}"
        )
        return False


@pytest.mark.unit
class TestTerminateExecutionOnAgent:
    """Unit tests for terminate_execution_on_agent helper function."""

    @pytest.mark.asyncio
    async def test_terminate_success_returns_true(self):
        """Successful termination returns True."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "terminated", "execution_id": "exec-123"}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await terminate_execution_on_agent("test-agent", "exec-123")

            assert result is True
            mock_client.post.assert_called_once_with(
                "http://agent-test-agent:8000/api/executions/exec-123/terminate"
            )

    @pytest.mark.asyncio
    async def test_terminate_already_finished_returns_true(self):
        """Already finished execution returns True (not an error)."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "already_finished", "execution_id": "exec-123"}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await terminate_execution_on_agent("test-agent", "exec-123")

            assert result is True

    @pytest.mark.asyncio
    async def test_terminate_not_found_returns_true(self):
        """Execution not found (404) returns True (may have finished)."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await terminate_execution_on_agent("test-agent", "exec-123")

            assert result is True

    @pytest.mark.asyncio
    async def test_terminate_server_error_returns_false(self):
        """Server error (500) returns False."""
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await terminate_execution_on_agent("test-agent", "exec-123")

            assert result is False

    @pytest.mark.asyncio
    async def test_terminate_timeout_returns_false(self):
        """HTTP timeout returns False (watchdog will clean up)."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("timeout")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await terminate_execution_on_agent("test-agent", "exec-123")

            assert result is False

    @pytest.mark.asyncio
    async def test_terminate_connect_error_returns_false(self):
        """Connection error returns False (agent unreachable)."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("connection refused")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await terminate_execution_on_agent("test-agent", "exec-123")

            assert result is False

    @pytest.mark.asyncio
    async def test_terminate_empty_execution_id_returns_false(self):
        """Empty execution_id returns False immediately."""
        result = await terminate_execution_on_agent("test-agent", "")
        assert result is False

        result = await terminate_execution_on_agent("test-agent", None)
        assert result is False

    @pytest.mark.asyncio
    async def test_terminate_generic_exception_returns_false(self):
        """Generic exception returns False (logged as warning)."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.side_effect = Exception("unexpected error")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await terminate_execution_on_agent("test-agent", "exec-123")

            assert result is False
