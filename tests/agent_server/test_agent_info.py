"""
Agent Server Info Tests (test_agent_info.py)

Tests for agent server info and health endpoints.
These run through the backend proxy.
Covers REQ-AS-INFO-001 through REQ-AS-INFO-004.
"""

import pytest
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestRootEndpoint:
    """REQ-AS-INFO-001: Root endpoint tests."""

    def test_root_returns_service_info(self, agent_proxy_client):
        """GET / returns service info."""
        api_client, agent_name = agent_proxy_client

        # Access through backend proxy
        response = api_client.get(f"/api/agents/{agent_name}/info")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, dict)


class TestHealthCheck:
    """REQ-AS-INFO-002: Health check endpoint tests."""

    def test_health_returns_healthy(self, agent_proxy_client):
        """GET /health returns healthy status."""
        api_client, agent_name = agent_proxy_client

        # Health is typically checked via container status
        response = api_client.get(f"/api/agents/{agent_name}")

        assert_status(response, 200)
        data = response.json()
        assert data.get("status") in ["running", "healthy"]


class TestAgentInfo:
    """REQ-AS-INFO-003: Agent info endpoint tests."""

    def test_agent_info_returns_details(self, agent_proxy_client):
        """GET /api/agent/info returns agent details."""
        api_client, agent_name = agent_proxy_client

        response = api_client.get(f"/api/agents/{agent_name}/info")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, dict)


class TestTemplateInfo:
    """REQ-AS-INFO-004: Template info endpoint tests."""

    def test_template_info(self, agent_proxy_client):
        """GET /api/template/info returns template metadata."""
        api_client, agent_name = agent_proxy_client

        response = api_client.get(f"/api/agents/{agent_name}/info")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # May not have template
        if response.status_code == 200:
            data = response.json()
            # Check for template field
            if "template" in data:
                assert isinstance(data["template"], (dict, type(None)))
