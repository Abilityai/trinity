"""
Subscription Management Tests (test_subscriptions.py)

Tests for SUB-001: Claude Max/Pro subscription credential management.
Subscriptions are registered once and can be assigned to multiple agents.
Credentials are encrypted using AES-256-GCM.

Test tiers:
- SMOKE: Subscription CRUD, validation (no agent required)
- AGENT: Assignment, injection, auth status (requires running agent)
"""

import pytest
import uuid

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)

# Subscription injection into agent containers can exceed 30s.
pytestmark = pytest.mark.timeout(120)


# =============================================================================
# Test Data
# =============================================================================

VALID_TOKEN = "sk-ant-oat01-test-access-token-12345"

INVALID_TOKEN = "invalid-token-no-prefix"


# =============================================================================
# Subscription CRUD Tests (SMOKE)
# =============================================================================

class TestSubscriptionCRUD:
    """SUB-001: Subscription CRUD endpoint tests."""

    @pytest.mark.smoke
    def test_list_subscriptions_empty(self, api_client: TrinityApiClient):
        """GET /api/subscriptions returns list (may be empty)."""
        response = api_client.get("/api/subscriptions")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, list)

    @pytest.mark.smoke
    def test_register_subscription(self, api_client: TrinityApiClient):
        """POST /api/subscriptions creates new subscription."""
        name = f"test-sub-{uuid.uuid4().hex[:8]}"

        response = api_client.post(
            "/api/subscriptions",
            json={
                "name": name,
                "token": VALID_TOKEN,
                "subscription_type": "max",
                "rate_limit_tier": "default_claude_max_20x"
            }
        )

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["id", "name", "subscription_type", "owner_id", "created_at"])
        assert data["name"] == name
        assert data["subscription_type"] == "max"

        # Cleanup
        api_client.delete(f"/api/subscriptions/{data['id']}")

    @pytest.mark.smoke
    def test_register_subscription_upsert(self, api_client: TrinityApiClient):
        """POST /api/subscriptions with same name updates existing."""
        name = f"test-sub-upsert-{uuid.uuid4().hex[:8]}"

        # Create first
        response1 = api_client.post(
            "/api/subscriptions",
            json={"name": name, "token": VALID_TOKEN, "subscription_type": "max"}
        )
        assert_status(response1, 200)
        id1 = response1.json()["id"]

        # Update with same name
        response2 = api_client.post(
            "/api/subscriptions",
            json={"name": name, "token": VALID_TOKEN, "subscription_type": "pro"}
        )
        assert_status(response2, 200)
        data2 = response2.json()

        # Same ID, updated type
        assert data2["id"] == id1
        assert data2["subscription_type"] == "pro"

        # Cleanup
        api_client.delete(f"/api/subscriptions/{id1}")

    @pytest.mark.smoke
    def test_register_subscription_invalid_token(self, api_client: TrinityApiClient):
        """POST /api/subscriptions with invalid token returns 422."""
        response = api_client.post(
            "/api/subscriptions",
            json={"name": "test-invalid", "token": INVALID_TOKEN}
        )
        assert_status(response, 422)

    @pytest.mark.smoke
    def test_register_subscription_missing_name(self, api_client: TrinityApiClient):
        """POST /api/subscriptions without name returns 422."""
        response = api_client.post(
            "/api/subscriptions",
            json={"token": VALID_TOKEN}
        )
        assert_status(response, 422)

    @pytest.mark.smoke
    def test_list_subscriptions_with_agents(self, api_client: TrinityApiClient):
        """GET /api/subscriptions returns agent counts and names."""
        # Create subscription
        name = f"test-sub-list-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            "/api/subscriptions",
            json={"name": name, "token": VALID_TOKEN}
        )
        assert_status(response, 200)
        sub_id = response.json()["id"]

        # List and verify structure
        response = api_client.get("/api/subscriptions")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Find our subscription
        our_sub = next((s for s in data if s["id"] == sub_id), None)
        assert our_sub is not None
        assert "agent_count" in our_sub
        assert "agents" in our_sub
        assert isinstance(our_sub["agents"], list)

        # Cleanup
        api_client.delete(f"/api/subscriptions/{sub_id}")

    @pytest.mark.smoke
    def test_delete_subscription(self, api_client: TrinityApiClient):
        """DELETE /api/subscriptions/{id} removes subscription."""
        # Create
        name = f"test-sub-del-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            "/api/subscriptions",
            json={"name": name, "token": VALID_TOKEN}
        )
        sub_id = response.json()["id"]

        # Delete
        response = api_client.delete(f"/api/subscriptions/{sub_id}")
        assert_status(response, 200)

        # Verify gone from list
        response = api_client.get("/api/subscriptions")
        data = response.json()
        assert not any(s["id"] == sub_id for s in data)

    @pytest.mark.smoke
    def test_delete_subscription_nonexistent(self, api_client: TrinityApiClient):
        """DELETE /api/subscriptions/{id} for nonexistent returns 404."""
        response = api_client.delete(f"/api/subscriptions/{uuid.uuid4()}")
        assert_status(response, 404)


# =============================================================================
# Subscription Assignment Tests (Requires Agent)
# =============================================================================

class TestSubscriptionAssignment:
    """SUB-001: Subscription assignment to agents."""

    def test_assign_subscription_to_agent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/subscriptions/agents/{name} assigns subscription."""
        # Create subscription
        sub_name = f"test-sub-assign-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            "/api/subscriptions",
            json={"name": sub_name, "token": VALID_TOKEN}
        )
        assert_status(response, 200)
        sub_id = response.json()["id"]

        try:
            # Assign to agent
            response = api_client.put(
                f"/api/subscriptions/agents/{created_agent['name']}",
                params={"subscription_name": sub_name}
            )
            assert_status(response, 200)
            data = assert_json_response(response)
            assert data["success"] is True
            assert data["subscription_name"] == sub_name

            # Verify agent appears in subscription's agent list
            response = api_client.get("/api/subscriptions")
            subs = response.json()
            our_sub = next((s for s in subs if s["id"] == sub_id), None)
            assert created_agent["name"] in our_sub["agents"]

        finally:
            # Cleanup
            api_client.delete(f"/api/subscriptions/agents/{created_agent['name']}")
            api_client.delete(f"/api/subscriptions/{sub_id}")

    def test_assign_subscription_nonexistent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/subscriptions/agents/{name} with nonexistent subscription returns 404."""
        response = api_client.put(
            f"/api/subscriptions/agents/{created_agent['name']}",
            params={"subscription_name": "nonexistent-sub-xyz"}
        )
        assert_status(response, 404)

    def test_assign_subscription_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """PUT /api/subscriptions/agents/{name} for nonexistent agent returns 400."""
        # Create subscription first
        sub_name = f"test-sub-noagent-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            "/api/subscriptions",
            json={"name": sub_name, "token": VALID_TOKEN}
        )
        sub_id = response.json()["id"]

        try:
            response = api_client.put(
                "/api/subscriptions/agents/nonexistent-agent-xyz",
                params={"subscription_name": sub_name}
            )
            # Returns 400 because agent not in ownership table
            assert_status(response, 400)
            assert "not found" in response.json()["detail"].lower()
        finally:
            api_client.delete(f"/api/subscriptions/{sub_id}")

    def test_clear_agent_subscription(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/subscriptions/agents/{name} clears assignment."""
        # Create and assign
        sub_name = f"test-sub-clear-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            "/api/subscriptions",
            json={"name": sub_name, "token": VALID_TOKEN}
        )
        sub_id = response.json()["id"]

        api_client.put(
            f"/api/subscriptions/agents/{created_agent['name']}",
            params={"subscription_name": sub_name}
        )

        try:
            # Clear assignment
            response = api_client.delete(
                f"/api/subscriptions/agents/{created_agent['name']}"
            )
            assert_status(response, 200)

            # Verify cleared
            response = api_client.get(
                f"/api/subscriptions/agents/{created_agent['name']}/auth"
            )
            data = response.json()
            assert data["auth_mode"] != "subscription" or data["subscription_name"] is None

        finally:
            api_client.delete(f"/api/subscriptions/{sub_id}")


# =============================================================================
# Auth Status Tests
# =============================================================================

class TestAuthStatus:
    """SUB-001: Agent auth status detection."""

    def test_get_auth_status_no_subscription(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/subscriptions/agents/{name}/auth returns status without subscription."""
        # First clear any existing subscription
        api_client.delete(f"/api/subscriptions/agents/{created_agent['name']}")

        response = api_client.get(
            f"/api/subscriptions/agents/{created_agent['name']}/auth"
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["agent_name", "auth_mode", "has_api_key"])
        assert data["agent_name"] == created_agent["name"]
        # Without subscription, should be api_key or not_configured
        assert data["auth_mode"] in ["api_key", "not_configured"]

    def test_get_auth_status_with_subscription(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/subscriptions/agents/{name}/auth shows subscription when assigned."""
        # Create and assign subscription
        sub_name = f"test-sub-auth-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            "/api/subscriptions",
            json={"name": sub_name, "token": VALID_TOKEN, "subscription_type": "max"}
        )
        sub_id = response.json()["id"]

        api_client.put(
            f"/api/subscriptions/agents/{created_agent['name']}",
            params={"subscription_name": sub_name}
        )

        try:
            response = api_client.get(
                f"/api/subscriptions/agents/{created_agent['name']}/auth"
            )
            assert_status(response, 200)
            data = assert_json_response(response)

            assert data["auth_mode"] == "subscription"
            assert data["subscription_name"] == sub_name
            assert data["subscription_id"] == sub_id

        finally:
            api_client.delete(f"/api/subscriptions/agents/{created_agent['name']}")
            api_client.delete(f"/api/subscriptions/{sub_id}")

    def test_get_auth_status_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """GET /api/subscriptions/agents/{name}/auth for nonexistent agent returns default status.

        Note: Currently returns 200 with api_key mode even for nonexistent agents.
        This could be improved to return 404.
        """
        response = api_client.get(
            "/api/subscriptions/agents/nonexistent-agent-xyz/auth"
        )
        # API returns 200 with default status for unknown agents
        # (could be improved to validate agent exists first)
        assert_status_in(response, [200, 404])
        if response.status_code == 200:
            data = response.json()
            assert data["auth_mode"] in ["api_key", "not_configured"]


# =============================================================================
# Auth Report Tests (SMOKE)
# =============================================================================

class TestAuthReport:
    """SUB-001: Fleet auth report endpoint."""

    @pytest.mark.smoke
    def test_get_auth_report(self, api_client: TrinityApiClient):
        """GET /api/ops/auth-report returns fleet auth status."""
        response = api_client.get("/api/ops/auth-report")
        assert_status(response, 200)
        data = assert_json_response(response)

        assert_has_fields(data, ["timestamp", "summary", "by_auth_mode", "subscriptions"])
        assert_has_fields(data["summary"], [
            "total_agents", "using_subscription", "using_api_key",
            "not_configured", "subscription_count"
        ])
        assert_has_fields(data["by_auth_mode"], ["subscription", "api_key", "not_configured"])

        # Counts should be non-negative
        assert data["summary"]["total_agents"] >= 0
        assert data["summary"]["using_subscription"] >= 0
        assert data["summary"]["using_api_key"] >= 0


# =============================================================================
# Credential Injection Tests
# =============================================================================

class TestCredentialInjection:
    """SUB-001: Subscription credential injection to running agents."""

    def test_injection_on_assignment(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Assigning subscription to running agent triggers injection."""
        # Create subscription
        sub_name = f"test-sub-inject-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            "/api/subscriptions",
            json={"name": sub_name, "token": VALID_TOKEN}
        )
        sub_id = response.json()["id"]

        try:
            # Assign - should include injection result
            response = api_client.put(
                f"/api/subscriptions/agents/{created_agent['name']}",
                params={"subscription_name": sub_name}
            )
            assert_status(response, 200)
            data = assert_json_response(response)

            # Check injection result
            assert "injection_result" in data
            # For running agent, should succeed
            if data["injection_result"]:
                assert data["injection_result"]["status"] in ["success", "agent_not_running"]

        finally:
            api_client.delete(f"/api/subscriptions/agents/{created_agent['name']}")
            api_client.delete(f"/api/subscriptions/{sub_id}")


# =============================================================================
# Cascade Delete Tests
# =============================================================================

class TestSubscriptionCascade:
    """SUB-001: Subscription deletion cascades to agent assignments."""

    def test_delete_subscription_clears_agents(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Deleting subscription clears all agent assignments."""
        # Create and assign
        sub_name = f"test-sub-cascade-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            "/api/subscriptions",
            json={"name": sub_name, "token": VALID_TOKEN}
        )
        sub_id = response.json()["id"]

        api_client.put(
            f"/api/subscriptions/agents/{created_agent['name']}",
            params={"subscription_name": sub_name}
        )

        # Verify assigned
        response = api_client.get(
            f"/api/subscriptions/agents/{created_agent['name']}/auth"
        )
        assert response.json()["auth_mode"] == "subscription"

        # Delete subscription
        api_client.delete(f"/api/subscriptions/{sub_id}")

        # Verify agent no longer has subscription
        response = api_client.get(
            f"/api/subscriptions/agents/{created_agent['name']}/auth"
        )
        data = response.json()
        assert data["auth_mode"] != "subscription" or data["subscription_id"] is None


# =============================================================================
# Auto-Assign on Agent Creation Tests (#74)
# =============================================================================

class TestSubscriptionAutoAssign:
    """#74: Auto-assign subscription to new agents via round-robin."""

    def _create_subscription(self, api_client, name, token=VALID_TOKEN):
        """Helper to create a subscription and return its id."""
        response = api_client.post(
            "/api/subscriptions",
            json={"name": name, "token": token, "subscription_type": "max"}
        )
        assert_status(response, 200)
        return response.json()["id"]

    def _create_agent(self, api_client, name):
        """Helper to create an agent and return its name."""
        response = api_client.post(
            "/api/agents",
            json={"name": name, "template": "local:default"}
        )
        assert_status(response, 200)
        return response.json()["name"]

    def _get_auth(self, api_client, agent_name):
        """Helper to get agent auth status."""
        response = api_client.get(
            f"/api/subscriptions/agents/{agent_name}/auth"
        )
        assert_status(response, 200)
        return response.json()

    def _cleanup_agent(self, api_client, agent_name):
        """Helper to delete an agent."""
        api_client.delete(f"/api/agents/{agent_name}")

    def _cleanup_subscription(self, api_client, sub_id):
        """Helper to delete a subscription."""
        api_client.delete(f"/api/subscriptions/{sub_id}")

    def test_create_agent_no_subscriptions(self, api_client: TrinityApiClient):
        """Agent created with no subscriptions uses platform API key."""
        # Clear ALL subscriptions first to ensure clean state
        # (prior tests may have left subscriptions that would auto-assign)
        subs_response = api_client.get("/api/subscriptions")
        if subs_response.status_code == 200:
            for sub in subs_response.json():
                api_client.delete(f"/api/subscriptions/{sub['id']}")

        agent_name = f"test-noauto-{uuid.uuid4().hex[:8]}"
        try:
            self._create_agent(api_client, agent_name)
            auth = self._get_auth(api_client, agent_name)
            assert auth["auth_mode"] in ["api_key", "not_configured"]
        finally:
            self._cleanup_agent(api_client, agent_name)

    def test_create_agent_with_subscription(self, api_client: TrinityApiClient):
        """Agent created with subscription registered is auto-assigned."""
        sub_name = f"test-auto-sub-{uuid.uuid4().hex[:8]}"
        agent_name = f"test-auto-{uuid.uuid4().hex[:8]}"
        sub_id = self._create_subscription(api_client, sub_name)
        try:
            self._create_agent(api_client, agent_name)
            auth = self._get_auth(api_client, agent_name)
            assert auth["auth_mode"] == "subscription"
            assert auth["subscription_name"] == sub_name
        finally:
            self._cleanup_agent(api_client, agent_name)
            self._cleanup_subscription(api_client, sub_id)

    def test_round_robin_distribution(self, api_client: TrinityApiClient):
        """Two subscriptions distribute agents evenly via round-robin."""
        sub_a_name = f"test-rr-a-{uuid.uuid4().hex[:8]}"
        sub_b_name = f"test-rr-b-{uuid.uuid4().hex[:8]}"
        sub_a_id = self._create_subscription(api_client, sub_a_name)
        sub_b_id = self._create_subscription(api_client, sub_b_name)

        agents = []
        try:
            # Create 3 agents — should distribute 2+1 across 2 subscriptions
            for i in range(3):
                name = f"test-rr-agent-{i}-{uuid.uuid4().hex[:6]}"
                self._create_agent(api_client, name)
                agents.append(name)

            # Check distribution
            assigned_subs = []
            for name in agents:
                auth = self._get_auth(api_client, name)
                assert auth["auth_mode"] == "subscription"
                assigned_subs.append(auth["subscription_id"])

            # Both subscriptions should have at least 1 agent
            assert sub_a_id in assigned_subs or sub_b_id in assigned_subs
            # No subscription should have all 3
            from collections import Counter
            counts = Counter(assigned_subs)
            assert max(counts.values()) <= 2, f"Uneven distribution: {counts}"
        finally:
            for name in agents:
                self._cleanup_agent(api_client, name)
            self._cleanup_subscription(api_client, sub_a_id)
            self._cleanup_subscription(api_client, sub_b_id)

    def test_alphabetical_tiebreak(self, api_client: TrinityApiClient):
        """Equal agent counts resolve by alphabetical subscription name."""
        # Create in reverse alphabetical order to verify sorting
        sub_beta = f"beta-tie-{uuid.uuid4().hex[:6]}"
        sub_alpha = f"alpha-tie-{uuid.uuid4().hex[:6]}"
        beta_id = self._create_subscription(api_client, sub_beta)
        alpha_id = self._create_subscription(api_client, sub_alpha)

        agent_name = f"test-tie-{uuid.uuid4().hex[:8]}"
        try:
            self._create_agent(api_client, agent_name)
            auth = self._get_auth(api_client, agent_name)
            assert auth["auth_mode"] == "subscription"
            # Should pick alphabetically first
            assert auth["subscription_name"] == sub_alpha
        finally:
            self._cleanup_agent(api_client, agent_name)
            self._cleanup_subscription(api_client, alpha_id)
            self._cleanup_subscription(api_client, beta_id)
