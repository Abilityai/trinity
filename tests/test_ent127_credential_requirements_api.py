"""Credential-requirements route-wiring smoke (trinity-enterprise#127).

The unit suite (`tests/unit/test_ent127_endpoint.py`) mounts
`routers/credentials.py` on a synthetic FastAPI app with the auth dependencies
overridden — deliberately no live backend. That leaves two wiring classes it
structurally cannot catch (the #1069 escape class):

  * the route actually resolving through `src/backend/main.py` — a path-param
    name mismatch, a prefix slip, or a sibling route registered earlier that
    shadows `credential-requirements` would 404 every call while the unit suite
    stays green;
  * the REAL `get_owned_agent_by_name` + `reject_agent_principal` chain
    resolving, rather than the overrides.

These need NO agent: a nonexistent name is enough to prove the dependency ran.

Note the self-uniformity property being exercised here (Invariant #8):
`get_owned_agent_by_name` returns the SAME 404 for an agent that does not exist
and one the caller does not own, so this test cannot distinguish them — which is
exactly the point. It asserts the detail is not FastAPI's bare routing "Not
Found", which is what an unwired route returns.
"""

import pytest

from utils.api_client import TrinityApiClient
from utils.assertions import assert_status, assert_status_in

# Deliberately nonexistent: these tests assert wiring, not agent behaviour.
_AGENT = "smoke-credreq-no-such-agent"
# FastAPI's unmatched-route detail, verbatim — seeing it means the route isn't wired.
_ROUTING_404_DETAIL = "Not Found"

pytestmark = pytest.mark.smoke


class TestCredentialRequirementsRouteWiring:
    def test_unauthenticated_rejected_not_unrouted(
        self, unauthenticated_client: TrinityApiClient
    ):
        """No auth → 401/403 from the real dependency chain. An unregistered or
        shadowed route would return 404 'Not Found' instead."""
        response = unauthenticated_client.get(
            f"/api/agents/{_AGENT}/credential-requirements", auth=False
        )
        assert_status_in(response, [401, 403])

    def test_authenticated_reaches_the_owner_dependency(self, api_client: TrinityApiClient):
        """Authenticated call resolves `get_owned_agent_by_name` + the path param
        end-to-end. The uniform 404 comes from the dependency, never from routing —
        and it is deliberately identical for "no such agent" and "not yours"."""
        response = api_client.get(f"/api/agents/{_AGENT}/credential-requirements")
        assert_status(response, 404)
        assert response.json().get("detail") != _ROUTING_404_DETAIL

    def test_sibling_status_route_is_not_shadowed(self, api_client: TrinityApiClient):
        """The new route sits beside `/credentials/status` under the same prefix;
        prove the older sibling still resolves (Invariant #4 ordering)."""
        response = api_client.get(f"/api/agents/{_AGENT}/credentials/status")
        assert_status_in(response, [403, 404])
        assert response.json().get("detail") != _ROUTING_404_DETAIL
