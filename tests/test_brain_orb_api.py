"""
Brain Orb route-wiring smoke tests (#58/#61, trinity-enterprise).

The unit suite (tests/unit/test_brain_orb.py) mounts routers/agent_brain_orb.py on a
synthetic FastAPI app with dependency overrides — deliberately no live backend. That
leaves two wiring classes it structurally cannot catch (the #1069 escape class):

  * the router actually being registered in src/backend/main.py — a dropped
    include_router would 404 every brain-orb route while the unit suite stays green;
  * the real auth dependency + path-param chain (AuthorizedAgentByName /
    OwnedAgentByName) resolving through the real app.

These tests hit the running backend and need NO agent and NO Brain Orb flags:

  * an unauthenticated call must be rejected by the auth chain (401) — an
    unregistered route would instead 404 with FastAPI's literal "Not Found";
  * an authenticated call against a nonexistent agent must 404 from the real
    dependency ("Agent not found") or handler (flag gate) — never the bare
    routing "Not Found".
"""

import pytest

from utils.api_client import TrinityApiClient
from utils.assertions import assert_status, assert_status_in

# Deliberately nonexistent: these tests assert wiring, not agent behavior.
_AGENT = "smoke-brain-orb-no-such-agent"
# FastAPI's unmatched-route detail, verbatim — seeing it means the route isn't wired.
_ROUTING_404_DETAIL = "Not Found"

pytestmark = pytest.mark.smoke


class TestBrainOrbRouteWiring:
    """Real-app registration + auth-chain smoke for the brain-orb proxy routes."""

    def test_data_unauthenticated_rejected_not_unrouted(
        self, unauthenticated_client: TrinityApiClient
    ):
        """No auth → 401 from the real dependency chain. An unregistered router
        would return 404 'Not Found' instead — the exact regression this guards."""
        response = unauthenticated_client.get(
            f"/api/agents/{_AGENT}/brain-orb/data", auth=False
        )
        assert_status_in(response, [401, 403])

    def test_data_authenticated_reaches_dependency(self, api_client: TrinityApiClient):
        """Authenticated read resolves AuthorizedAgentByName + path params end-to-end:
        the nonexistent agent 404s from the dependency ('Agent not found') or, were
        the agent real with the flag off, from the handler — never from routing."""
        response = api_client.get(f"/api/agents/{_AGENT}/brain-orb/data")
        assert_status(response, 404)
        assert response.json().get("detail") != _ROUTING_404_DETAIL

    def test_action_unauthenticated_rejected_not_unrouted(
        self, unauthenticated_client: TrinityApiClient
    ):
        """Same registration proof for the write surface (POST /action)."""
        response = unauthenticated_client.post(
            f"/api/agents/{_AGENT}/brain-orb/action",
            json={"action": "capture", "body": "smoke"},
            auth=False,
        )
        assert_status_in(response, [401, 403])

    def test_action_authenticated_reaches_dependency(self, api_client: TrinityApiClient):
        """The one write route resolves OwnedAgentByName end-to-end; the 404 detail
        comes from the dependency/handler chain, never bare routing."""
        response = api_client.post(
            f"/api/agents/{_AGENT}/brain-orb/action",
            json={"action": "capture", "body": "smoke"},
        )
        assert_status(response, 404)
        assert response.json().get("detail") != _ROUTING_404_DETAIL

    def test_voice_token_route_wired(self, api_client: TrinityApiClient):
        """POST /voice-token is registered and auth-gated (it never contacts the
        agent, so with flags default-OFF the handler itself 404s; a real detail
        string proves the route + dependencies resolved)."""
        response = api_client.post(f"/api/agents/{_AGENT}/brain-orb/voice-token")
        assert_status(response, 404)
        assert response.json().get("detail") != _ROUTING_404_DETAIL
