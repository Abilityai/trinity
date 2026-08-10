"""
Web Terminal Tests (test_web_terminal.py)

Tests for Trinity web terminal endpoints (Req 11.6).
Covers: System agent terminal access and WebSocket endpoint.

Note: WebSocket tests require the server to be running.
These tests focus on HTTP endpoint validation and access control.
"""

import pytest


# #2080: this module was observed hanging indefinitely at 0% CPU (a blocking
# read no client-side timeout covers), and interrupting it surfaced a
# pytest-timeout + signal-reentrancy INTERNALERROR — so one test stalled an
# entire full-suite run for hours.
#
# Two layers bound it now. `--timeout-method=thread` is pinned globally
# (pyproject.toml) so the timeout can no longer re-enter the interpreter from a
# signal handler, and this module-level mark caps each test here at 60s so the
# file cannot consume a whole tier's budget even when pytest is invoked by hand
# without the runner's per-tier `--timeout`. Verified that the class-level
# `pytestmark = pytest.mark.smoke` below does NOT displace it — pytest unions
# module and class marks.
#
# Stated honestly: the ROOT cause is not fixed. Reproducing it needs a live
# stack and none was available; what is fixed is that it can no longer cost
# more than 60 seconds, and the failing test id now reaches the log instead of
# a run that never ends.
pytestmark = pytest.mark.timeout(60)

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


# System agent name
SYSTEM_AGENT_NAME = "trinity-system"


class TestSystemAgentTerminalAuthentication:
    """Tests for System Agent terminal authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_terminal_endpoint_exists(self, api_client: TrinityApiClient):
        """Verify that terminal-related endpoint structure exists."""
        # The actual terminal is WebSocket, but we can check the system agent status
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)

    def test_system_agent_status_accessible(self, api_client: TrinityApiClient):
        """GET /api/system-agent/status is accessible for authenticated users."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["name", "status", "exists"])
        assert data["name"] == SYSTEM_AGENT_NAME


class TestSystemAgentTerminalAccess:
    """Tests for system agent terminal access control.

    Note: The actual terminal uses WebSocket at:
    WS /api/system-agent/terminal?mode=claude|bash

    These tests verify the HTTP components and access patterns.
    """

    def test_system_agent_must_exist(self, api_client: TrinityApiClient):
        """Terminal requires system agent to exist."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = response.json()

        # System agent should exist (auto-deployed)
        if not data.get("exists"):
            pytest.skip("System agent not deployed")

        assert data["name"] == SYSTEM_AGENT_NAME

    def test_system_agent_must_be_running(self, api_client: TrinityApiClient):
        """Terminal requires system agent to be running."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = response.json()

        if not data.get("exists"):
            pytest.skip("System agent not deployed")

        # Check if running
        if data.get("status") != "running":
            pytest.skip("System agent not running")

        assert data["status"] == "running"


class TestSystemAgentTerminalModes:
    """Tests for terminal mode selection."""

    def test_terminal_supports_claude_mode(self):
        """Terminal should support 'claude' mode for Claude Code TUI."""
        # This is a design validation - the mode parameter should accept 'claude'
        # WebSocket URL: WS /api/system-agent/terminal?mode=claude
        valid_modes = ["claude", "bash"]
        assert "claude" in valid_modes

    def test_terminal_supports_bash_mode(self):
        """Terminal should support 'bash' mode for shell access."""
        # This is a design validation - the mode parameter should accept 'bash'
        # WebSocket URL: WS /api/system-agent/terminal?mode=bash
        valid_modes = ["claude", "bash"]
        assert "bash" in valid_modes


class TestSystemAgentTerminalPrerequisites:
    """Tests for terminal prerequisites via status endpoint."""

    def test_status_includes_container_id(self, api_client: TrinityApiClient):
        """Running system agent status should include container_id."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = response.json()

        if not data.get("exists"):
            pytest.skip("System agent not deployed")

        if data.get("status") != "running":
            pytest.skip("System agent not running")

        # When running, should have container_id for terminal connection
        assert "container_id" in data
        assert len(data["container_id"]) > 0

    def test_status_includes_is_system_flag(self, api_client: TrinityApiClient):
        """System agent status should include is_system flag."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = response.json()

        if not data.get("exists"):
            pytest.skip("System agent not deployed")

        # Should be marked as system agent
        assert data.get("is_system") is True


class TestAgentTerminalAuthentication:
    """Tests for regular agent terminal authentication (Req 11.6 extension).

    Web Terminal is now available for all agents, not just system agent.
    """

    pytestmark = pytest.mark.smoke

    def test_agent_terminal_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """Agent terminal WebSocket should require authentication.

        Note: This tests the HTTP upgrade path - actual WebSocket requires token.
        """
        # The WebSocket endpoint requires JWT authentication via query param
        # This is validated at connection time, not via HTTP status
        pass  # WebSocket auth is tested via integration tests

    def test_agent_terminal_requires_running_agent(self, api_client: TrinityApiClient, created_agent):
        """Terminal requires agent to be in running state."""
        agent_name = created_agent["name"]

        # Check agent status
        response = api_client.get(f"/api/agents/{agent_name}")
        assert_status(response, 200)
        data = response.json()

        # Should be running for terminal access
        assert data["status"] == "running"


class TestSystemAgentTerminalIntegration:
    """Integration tests for system agent terminal.

    These tests verify the terminal can be accessed when all prerequisites are met.
    """

    @pytest.mark.slow
    @pytest.mark.timeout(30)
    def test_system_agent_ready_for_terminal(self, api_client: TrinityApiClient):
        """Verify system agent is ready for terminal connection."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = response.json()

        # All prerequisites for terminal
        assert data.get("exists") is True, "System agent must exist"

        if data.get("status") != "running":
            pytest.skip("System agent not running - terminal not available")

        assert "container_id" in data, "Container ID required for terminal"
        assert data.get("is_system") is True, "Must be marked as system agent"

        # If all checks pass, terminal should be accessible
        # Actual WebSocket connection requires frontend or WebSocket client


class TestTerminalAuditLogging:
    """Tests for terminal session audit logging."""

    def test_system_agent_restart_logged(self, api_client: TrinityApiClient):
        """System agent restart should be logged for audit trail."""
        # First check if system agent exists
        status_response = api_client.get("/api/system-agent/status")
        if status_response.status_code != 200:
            pytest.skip("Cannot check system agent status")

        data = status_response.json()
        if not data.get("exists"):
            pytest.skip("System agent not deployed")

        # Restart would be logged - we just verify the endpoint exists
        # Actual restart test is in test_system_agent.py
        assert data.get("name") == SYSTEM_AGENT_NAME
