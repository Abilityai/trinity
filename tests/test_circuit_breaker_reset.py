"""
Circuit Breaker Reset Endpoint Tests (Issue #921)

Integration tests for POST /api/agents/{name}/circuit-breaker/reset —
the admin escape hatch added in #921 for the dormant-CB cascade. Drives
the live backend + Redis stack rather than mocking, because the value
of this test is that the route, the auth dependency, and the underlying
Redis-DEL all line up end-to-end.
"""

import os
import subprocess
import uuid

import pytest
from utils.api_client import TrinityApiClient
from utils.assertions import assert_status


_BACKEND_CONTAINER = os.getenv("TRINITY_BACKEND_CONTAINER", "trinity-backend")
_REDIS_CONTAINER = os.getenv("TRINITY_REDIS_CONTAINER", "trinity-redis")
# trinity-system is always present on every Trinity instance and stable to
# poke. We never actually issue HTTP to the agent — only Redis state for the
# CB key is touched.
_AGENT = "trinity-system"


def _exec_backend(python_code: str) -> str:
    result = subprocess.run(
        ["docker", "exec", _BACKEND_CONTAINER, "python3", "-c", python_code],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Backend exec failed: {result.stderr}")
    return result.stdout.strip()


def _redis_password() -> str:
    """Pull the operator REDIS_PASSWORD from .env for direct redis-cli access."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.path.join(here, ".env")
    if not os.path.exists(env):
        pytest.skip(".env missing — cannot derive Redis password")
    for line in open(env):
        if line.startswith("REDIS_PASSWORD="):
            return line.split("=", 1)[1].strip()
    pytest.skip("REDIS_PASSWORD not found in .env")


def _force_dormant(agent_name: str) -> None:
    """Park the CB in dormant via the existing force helper."""
    _exec_backend(
        "from services.agent_client import force_circuit_dormant\n"
        f"force_circuit_dormant('{agent_name}', reason='test-921-reset')\n"
    )


def _cb_key_exists(agent_name: str, password: str) -> bool:
    result = subprocess.run(
        ["docker", "exec", _REDIS_CONTAINER, "redis-cli",
         "-a", password, "--no-auth-warning",
         "EXISTS", f"agent:circuit:{agent_name}"],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip() == "1"


def _delete_cb_key(agent_name: str, password: str) -> None:
    subprocess.run(
        ["docker", "exec", _REDIS_CONTAINER, "redis-cli",
         "-a", password, "--no-auth-warning",
         "DEL", f"agent:circuit:{agent_name}",
         f"agent:circuit:{agent_name}:probe-lock"],
        capture_output=True, text=True, timeout=10,
    )


class TestCircuitBreakerResetEndpoint:
    """POST /api/agents/{name}/circuit-breaker/reset — #921 escape hatch."""

    @pytest.fixture
    def redis_password(self):
        return _redis_password()

    @pytest.fixture
    def parked_dormant(self, redis_password):
        """Park trinity-system's CB in dormant; tear down by clearing the
        Redis state so the agent is reachable for subsequent tests."""
        _force_dormant(_AGENT)
        yield
        _delete_cb_key(_AGENT, redis_password)

    def test_reset_returns_prior_state_and_clears_redis(
        self, api_client: TrinityApiClient, parked_dormant, redis_password
    ):
        """Happy path: dormant agent → POST reset → 200 with prior=dormant,
        new=closed, and the Redis hash deleted."""
        assert _cb_key_exists(_AGENT, redis_password), "fixture didn't park dormant"

        response = api_client.post(f"/api/agents/{_AGENT}/circuit-breaker/reset")
        assert_status(response, 200)
        body = response.json()

        assert body["agent_name"] == _AGENT
        assert body["prior_state"] == "dormant"
        assert body["new_state"] == "closed"
        assert "reset_at" in body

        # The Redis hash is gone — the next CB check will fail-open to 'closed'.
        assert not _cb_key_exists(_AGENT, redis_password)

    def test_reset_idempotent_on_closed_cb(
        self, api_client: TrinityApiClient, redis_password
    ):
        """Idempotent: calling reset when the CB is already closed (no
        Redis key present) succeeds and reports prior=closed."""
        _delete_cb_key(_AGENT, redis_password)
        assert not _cb_key_exists(_AGENT, redis_password)

        response = api_client.post(f"/api/agents/{_AGENT}/circuit-breaker/reset")
        assert_status(response, 200)
        body = response.json()
        assert body["prior_state"] == "closed"
        assert body["new_state"] == "closed"

    def test_reset_requires_admin(
        self, unauthenticated_client: TrinityApiClient, redis_password
    ):
        """Unauthenticated requests are rejected — verifies the
        require_role('admin') dependency wires correctly. Non-admin
        coverage lives in role-hierarchy tests; here we only need to
        prove the endpoint isn't anonymous."""
        response = unauthenticated_client.post(
            f"/api/agents/{_AGENT}/circuit-breaker/reset"
        )
        assert response.status_code in (401, 403)

    def test_reset_404_for_unknown_agent(self, api_client: TrinityApiClient):
        """Endpoint is per-agent — reset on an unknown agent name returns 404
        via the AuthorizedAgentByName dependency, not 200 + no-op."""
        bogus = f"does-not-exist-{uuid.uuid4().hex[:8]}"
        response = api_client.post(f"/api/agents/{bogus}/circuit-breaker/reset")
        assert response.status_code == 404
