"""
Agent Server Test Fixtures

Fixtures for testing agent container APIs directly.
Requires a running agent configured via TEST_AGENT_NAME environment variable.
"""

import os
import pytest
import httpx


@pytest.fixture(scope="session")
def agent_server_url(api_config, api_client):
    """Get the agent server URL.

    Either from TEST_AGENT_NAME or by creating a test agent.
    """
    agent_name = api_config.test_agent_name

    if not agent_name:
        pytest.skip("TEST_AGENT_NAME not set - skipping agent server tests")

    # Get agent details to find internal URL
    response = api_client.get(f"/api/agents/{agent_name}")
    if response.status_code != 200:
        pytest.skip(f"Agent {agent_name} not found")

    agent = response.json()
    if agent.get("status") != "running":
        pytest.skip(f"Agent {agent_name} is not running")

    # Agent server runs on port 8000 inside container
    # Access via Docker network: agent-{name}:8000
    # Or via backend proxy
    return f"http://agent-{agent_name}:8000"


@pytest.fixture(scope="session")
def agent_client(agent_server_url, api_config):
    """HTTP client for direct agent server access.

    #1159: a token-enabled agent (created with AGENT_AUTH_SECRET set) enforces
    the per-agent X-Trinity-Agent-Token on every non-/health route, so direct
    access must carry it. Derived from AGENT_AUTH_SECRET in the test env (which
    must match the running stack). When unset (grace / old image) we send no
    header — direct access still works because the agent isn't enforcing.
    """
    headers = {}
    if api_config.test_agent_name and os.getenv("AGENT_AUTH_SECRET"):
        from services.agent_auth import build_agent_auth_headers
        headers = build_agent_auth_headers(api_config.test_agent_name)
    client = httpx.Client(
        base_url=agent_server_url,
        timeout=httpx.Timeout(60.0, connect=10.0),
        headers=headers,
    )
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session")
def agent_proxy_client(api_client, api_config):
    """Access agent server through backend proxy.

    This is the preferred method as it handles authentication.
    """
    agent_name = api_config.test_agent_name
    if not agent_name:
        pytest.skip("TEST_AGENT_NAME not set")

    return api_client, agent_name
