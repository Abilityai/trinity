"""Integration tests for the fire-and-forget result-callback endpoint (#1083).

Layer over the live server — confirms the route is reachable and the fail-closed
status codes surface over real HTTP (the exhaustive logic matrix lives in
tests/unit/test_1083_callback_endpoint.py). Mirrors tests/test_307_heartbeat_endpoint.py:
the parts reachable without an agent's injected MCP key are the auth gate (403)
and, for a valid user-scoped key, the ownership / async-marker fail-closed paths.

Critically (PR1 guarantee): no execution is ever marked ``dispatched_async`` in
production until PR2, so a callback against a real RUNNING execution is rejected.
The positive accept path (an agent POSTing its own key after a 202 dispatch) is
covered by the unit tests + the PR2 sibling-stack soak.

Issue: https://github.com/abilityai/trinity/issues/1083
"""

import pytest

from testkit.api_client import TrinityApiClient
from testkit.assertions import assert_status, assert_json_response

pytestmark = pytest.mark.smoke

_RESULT_BODY = {"status": "success", "response": "done", "metadata": {"cost_usd": 0.01}}


class TestResultCallbackAuthGate:
    """Only the agent's own agent-scoped MCP key is accepted (mirror heartbeat)."""

    def test_missing_token_returns_403(
        self, api_client: TrinityApiClient, created_agent: dict
    ):
        name = created_agent["name"]
        resp = api_client.post(
            f"/api/agents/{name}/executions/exec-does-not-exist/result",
            json=_RESULT_BODY,
            auth=False,
        )
        assert_status(resp, 403)

    def test_non_bearer_header_returns_403(
        self, api_client: TrinityApiClient, created_agent: dict
    ):
        name = created_agent["name"]
        resp = api_client.post(
            f"/api/agents/{name}/executions/exec-does-not-exist/result",
            json=_RESULT_BODY,
            auth=False,
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert_status(resp, 403)

    def test_invalid_bearer_token_returns_403(
        self, api_client: TrinityApiClient, created_agent: dict
    ):
        name = created_agent["name"]
        resp = api_client.post(
            f"/api/agents/{name}/executions/exec-does-not-exist/result",
            json=_RESULT_BODY,
            auth=False,
            headers={"Authorization": "Bearer trinity_mcp_thisisnotarealkey0000000000"},
        )
        assert_status(resp, 403)

    def test_user_scoped_key_returns_403(
        self,
        api_client: TrinityApiClient,
        created_agent: dict,
        test_mcp_key_name: str,
        resource_tracker,
    ):
        """A valid *user-scoped* MCP key cannot finalize an agent's execution —
        the auth gate (authorize_heartbeat) rejects it before ownership."""
        key_resp = api_client.post("/api/mcp/keys", json={"name": test_mcp_key_name})
        key_data = assert_json_response(key_resp)
        if "id" in key_data:
            resource_tracker.track_mcp_key(key_data["id"])
        user_key = key_data.get("key") or key_data.get("api_key") or key_data.get("access_key")
        assert user_key, "key creation must return the raw key once"

        name = created_agent["name"]
        resp = api_client.post(
            f"/api/agents/{name}/executions/exec-does-not-exist/result",
            json=_RESULT_BODY,
            auth=False,
            headers={"Authorization": f"Bearer {user_key}"},
        )
        assert_status(resp, 403)
