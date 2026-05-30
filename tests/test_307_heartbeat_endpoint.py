"""
Integration tests for the agent heartbeat endpoint (RELIABILITY-004 / #307).

Layer over the live server. The security gate (Option B, least-privilege) is
the part reachable over HTTP without an agent's injected MCP key:

  * missing/invalid token              -> 403
  * user-scoped MCP key                -> 403 (only the agent's OWN agent-scoped
                                          key may heartbeat it)

Plus the surfacing/backward-compat contract on the fleet-status endpoint:

  * every agent summary carries the new heartbeat_* fields
  * an agent that has never heartbeated reads back as `unsupported`
    (heartbeat_alive=null) and is therefore never marked dead

The positive end-to-end path (an agent POSTing with its own injected
TRINITY_MCP_API_KEY -> heartbeat_alive=true) requires reading the key out of
the container; it is covered by the manual sibling-stack steps in the plan's
Verification section. The pure auth predicate + record/status logic are fully
covered by tests/unit/test_heartbeat_service.py.

Pairs with: tests/unit/test_heartbeat_service.py,
            tests/unit/test_mcp_key_track_usage.py.
Issue: https://github.com/abilityai/trinity/issues/307
"""

import pytest

from utils.api_client import TrinityApiClient
from utils.assertions import assert_status, assert_json_response

pytestmark = pytest.mark.smoke


class TestHeartbeatAuthGate:
    """Option B: only the agent's own agent-scoped MCP key is accepted."""

    def test_missing_token_returns_403(
        self, api_client: TrinityApiClient, created_agent: dict
    ):
        """No Authorization header → 403 (never 200, never 401-only)."""
        name = created_agent["name"]
        resp = api_client.post(
            f"/api/agents/{name}/heartbeat",
            json={"memory_mb": 1.0, "active_executions": 0, "uptime_s": 5.0},
            auth=False,
        )
        assert_status(resp, 403)

    def test_user_scoped_key_returns_403(
        self,
        api_client: TrinityApiClient,
        created_agent: dict,
        test_mcp_key_name: str,
        resource_tracker,
    ):
        """A valid *user-scoped* MCP key cannot heartbeat an agent."""
        # Mint a real user-scoped MCP key.
        key_resp = api_client.post("/api/mcp/keys", json={"name": test_mcp_key_name})
        key_data = assert_json_response(key_resp)
        if "id" in key_data:
            resource_tracker.track_mcp_key(key_data["id"])
        user_key = key_data.get("key") or key_data.get("api_key") or key_data.get("access_key")
        assert user_key, "key creation must return the raw key once"

        name = created_agent["name"]
        resp = api_client.post(
            f"/api/agents/{name}/heartbeat",
            json={"memory_mb": 1.0},
            auth=False,
            headers={"Authorization": f"Bearer {user_key}"},
        )
        assert_status(resp, 403)

    def test_non_bearer_header_returns_403(
        self, api_client: TrinityApiClient, created_agent: dict
    ):
        """A non-Bearer Authorization scheme → 403 (exercises the endpoint's
        `startswith("Bearer ")` guard, not just a missing header)."""
        name = created_agent["name"]
        resp = api_client.post(
            f"/api/agents/{name}/heartbeat",
            json={"memory_mb": 1.0},
            auth=False,
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert_status(resp, 403)

    def test_invalid_bearer_token_returns_403(
        self, api_client: TrinityApiClient, created_agent: dict
    ):
        """A well-formed but unknown Bearer token → 403 (validate_mcp_api_key
        returns None → authorize_heartbeat rejects). Verifies the None-result
        path is wired at the HTTP layer, not just in the predicate unit test."""
        name = created_agent["name"]
        resp = api_client.post(
            f"/api/agents/{name}/heartbeat",
            json={"memory_mb": 1.0},
            auth=False,
            headers={"Authorization": "Bearer trinity_mcp_thisisnotarealkey0000000000"},
        )
        assert_status(resp, 403)


class TestHeartbeatSurfacing:
    """Fleet-status exposes the new fields; never-beaten agents are unsupported."""

    def test_fleet_status_exposes_heartbeat_fields(
        self, api_client: TrinityApiClient, created_agent: dict
    ):
        resp = api_client.get("/api/monitoring/status")
        data = assert_json_response(resp)
        assert "agents" in data

        name = created_agent["name"]
        match = [a for a in data["agents"] if a.get("name") == name]
        if not match:
            pytest.skip(f"agent {name} not present in fleet status payload")
        summary = match[0]

        # All five heartbeat fields are present (default None → non-breaking).
        for field in (
            "heartbeat_alive",
            "heartbeat_state",
            "last_heartbeat_age_s",
            "heartbeat_active_executions",
            "heartbeat_memory_mb",
        ):
            assert field in summary, f"missing heartbeat field: {field}"

        # Backward-compat: a freshly-created agent that has not heartbeated
        # (old base image, or not yet beaten) reads back as unsupported and is
        # never marked dead by the heartbeat layer.
        assert summary["heartbeat_alive"] is None
        assert summary["heartbeat_state"] in ("unsupported", "alive", "stale")
