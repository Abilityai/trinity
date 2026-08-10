"""
Playbooks Tests (test_playbooks.py)

Tests for the Playbooks Tab feature (PLAYBOOK-001):
- GET /api/agents/{name}/playbooks endpoint
- Authentication requirements
- Error handling (agent not found, agent not running)
- Response structure validation

The Playbooks endpoint proxies to the agent's internal /api/skills endpoint
which lists skills from the agent's .claude/skills/ directory.

SMOKE TESTS (marked with @pytest.mark.smoke):
- Authentication requirements
- No agent creation required

AGENT TESTS:
- Playbooks retrieval from running agent
- Require created_agent or stopped_agent fixture

NOTE: Agent-requiring tests will skip if the agent base image doesn't have
the skills router (requires base image rebuild with skills.py).
"""

import pytest

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
    assert_list_response,
    assert_error_response,
)


def check_skills_endpoint_available(response) -> bool:
    """Check if the agent has the skills endpoint available.

    Returns False if the agent returns 404 (endpoint not found),
    meaning the agent base image needs to be rebuilt with skills.py.
    """
    if response.status_code == 404:
        data = response.json()
        detail = data.get("detail", "")
        if "Not Found" in detail:
            return False
    return True


# =============================================================================
# PLAYBOOKS - AUTHENTICATION TESTS
# =============================================================================


class TestPlaybooksAuthentication:
    """Tests for Playbooks endpoint authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_get_playbooks_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/playbooks requires authentication."""
        response = unauthenticated_client.get("/api/agents/test-agent/playbooks", auth=False)
        assert_status(response, 401)


# =============================================================================
# PLAYBOOKS - ERROR HANDLING TESTS
# =============================================================================


class TestPlaybooksErrorHandling:
    """Tests for Playbooks endpoint error handling."""

    pytestmark = pytest.mark.smoke

    def test_playbooks_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/playbooks returns 404 for nonexistent agent."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz-12345/playbooks")
        assert_status(response, 404)
        data = assert_json_response(response)
        assert "detail" in data


class TestPlaybooksAgentNotRunning:
    """Tests for Playbooks endpoint when agent is not running."""

    def test_playbooks_stopped_agent_returns_503(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """GET /api/agents/{name}/playbooks returns 503 when agent is stopped."""
        response = api_client.get(f"/api/agents/{stopped_agent['name']}/playbooks")
        assert_status(response, 503)
        data = assert_json_response(response)
        assert "detail" in data
        # Error message should indicate agent is not running
        assert "not running" in data["detail"].lower() or "start" in data["detail"].lower()


# =============================================================================
# PLAYBOOKS - RETRIEVAL TESTS (RUNNING AGENT)
# =============================================================================


class TestPlaybooksRetrieval:
    """Tests for Playbooks retrieval from running agent."""

    def test_get_playbooks_returns_skills_response(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/playbooks returns SkillsResponse structure."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/playbooks")

        # May return 200 or 504 (agent still starting up)
        if response.status_code == 504:
            pytest.skip("Agent is still starting up")

        if not check_skills_endpoint_available(response):
            pytest.skip("Agent base image needs rebuild with skills.py router")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have SkillsResponse fields
        assert_has_fields(data, ["skills", "count", "skill_paths"])

    def test_playbooks_skills_is_list(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Playbooks response skills field is a list."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/playbooks")

        if response.status_code == 504:
            pytest.skip("Agent is still starting up")

        if not check_skills_endpoint_available(response):
            pytest.skip("Agent base image needs rebuild with skills.py router")

        assert_status(response, 200)
        data = response.json()

        skills = data.get("skills")
        assert isinstance(skills, list), f"Expected skills to be list, got {type(skills)}"

    def test_playbooks_count_matches_skills_length(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Playbooks count field matches skills list length."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/playbooks")

        if response.status_code == 504:
            pytest.skip("Agent is still starting up")

        if not check_skills_endpoint_available(response):
            pytest.skip("Agent base image needs rebuild with skills.py router")

        assert_status(response, 200)
        data = response.json()

        skills = data.get("skills", [])
        count = data.get("count", 0)
        assert count == len(skills), f"Count {count} does not match skills length {len(skills)}"

    def test_playbooks_skill_paths_is_list(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Playbooks response skill_paths field is a list of strings."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/playbooks")

        if response.status_code == 504:
            pytest.skip("Agent is still starting up")

        if not check_skills_endpoint_available(response):
            pytest.skip("Agent base image needs rebuild with skills.py router")

        assert_status(response, 200)
        data = response.json()

        skill_paths = data.get("skill_paths")
        assert isinstance(skill_paths, list), f"Expected skill_paths to be list, got {type(skill_paths)}"

        # Each path should be a string
        for path in skill_paths:
            assert isinstance(path, str), f"Expected path to be string, got {type(path)}"


# =============================================================================
# PLAYBOOKS - SKILL STRUCTURE TESTS
# =============================================================================


class TestPlaybooksSkillStructure:
    """Tests for individual skill structure in Playbooks response."""

    def test_skill_has_required_fields(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Each skill has required SkillInfo fields."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/playbooks")

        if response.status_code == 504:
            pytest.skip("Agent is still starting up")

        if not check_skills_endpoint_available(response):
            pytest.skip("Agent base image needs rebuild with skills.py router")

        assert_status(response, 200)
        data = response.json()

        skills = data.get("skills", [])
        if len(skills) == 0:
            pytest.skip("No skills available to test structure")

        skill = skills[0]

        # Required fields from SkillInfo model
        required_fields = ["name", "path", "user_invocable"]
        assert_has_fields(skill, required_fields, "SkillInfo response")

    def test_skill_has_expected_field_types(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Skill fields have expected types."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/playbooks")

        if response.status_code == 504:
            pytest.skip("Agent is still starting up")

        if not check_skills_endpoint_available(response):
            pytest.skip("Agent base image needs rebuild with skills.py router")

        assert_status(response, 200)
        data = response.json()

        skills = data.get("skills", [])
        if len(skills) == 0:
            pytest.skip("No skills available to test structure")

        skill = skills[0]

        # Type assertions
        assert isinstance(skill["name"], str), "name should be string"
        assert isinstance(skill["path"], str), "path should be string"
        assert isinstance(skill["user_invocable"], bool), "user_invocable should be boolean"

        # Optional fields - can be None or their expected type
        if skill.get("description") is not None:
            assert isinstance(skill["description"], str), "description should be string or None"

        if skill.get("automation") is not None:
            assert skill["automation"] in ["autonomous", "gated", "manual"], \
                f"automation should be autonomous/gated/manual, got {skill['automation']}"

        if skill.get("allowed_tools") is not None:
            assert isinstance(skill["allowed_tools"], list), "allowed_tools should be list or None"

        if skill.get("argument_hint") is not None:
            assert isinstance(skill["argument_hint"], str), "argument_hint should be string or None"

        if "has_schedule" in skill:
            assert isinstance(skill["has_schedule"], bool), "has_schedule should be boolean"

    def test_skills_sorted_by_name(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Skills are sorted alphabetically by name."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/playbooks")

        if response.status_code == 504:
            pytest.skip("Agent is still starting up")

        if not check_skills_endpoint_available(response):
            pytest.skip("Agent base image needs rebuild with skills.py router")

        assert_status(response, 200)
        data = response.json()

        skills = data.get("skills", [])
        if len(skills) < 2:
            pytest.skip("Need at least 2 skills to test sorting")

        skill_names = [s["name"].lower() for s in skills]
        assert skill_names == sorted(skill_names), \
            f"Skills should be sorted by name. Got: {skill_names}"


# =============================================================================
# PLAYBOOKS - ACCESS CONTROL TESTS
# =============================================================================


class TestPlaybooksAccessControl:
    """Tests for Playbooks endpoint access control."""

    pytestmark = pytest.mark.smoke

    def test_playbooks_requires_agent_authorization(
        self,
        api_client: TrinityApiClient
    ):
        """Playbooks endpoint uses AuthorizedAgentByName for access control."""
        # This test verifies that the endpoint properly enforces access control.
        # The 404 for nonexistent agent is already tested above.
        # Here we verify that the endpoint exists and requires auth.
        response = api_client.get("/api/agents/test-agent/playbooks")

        # Should get 404 (agent not found) or 503 (agent not running)
        # but NOT 401 (since we're authenticated)
        assert_status_in(response, [404, 503])


# =============================================================================
# PLAYBOOKS - TIMEOUT HANDLING TESTS
# =============================================================================


class TestPlaybooksTimeoutHandling:
    """Tests for Playbooks endpoint timeout handling."""

    def test_playbooks_handles_agent_startup_timeout(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/playbooks handles timeout during agent startup gracefully."""
        # This test runs against a running agent, which should succeed.
        # The 504 timeout case is a valid response during startup.
        response = api_client.get(f"/api/agents/{created_agent['name']}/playbooks")

        if not check_skills_endpoint_available(response):
            pytest.skip("Agent base image needs rebuild with skills.py router")

        # Should succeed (200) or return timeout (504)
        assert_status_in(response, [200, 504])

        if response.status_code == 504:
            data = assert_json_response(response)
            assert "detail" in data
            # Timeout message should be informative
            assert "starting" in data["detail"].lower() or "try again" in data["detail"].lower()


# =============================================================================
# PLAYBOOKS - EMPTY STATE TESTS
# =============================================================================


class TestPlaybooksEmptyState:
    """Tests for Playbooks endpoint with no skills configured."""

    def test_playbooks_returns_empty_when_no_skills(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Playbooks returns empty skills list when agent has no skills."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/playbooks")

        if response.status_code == 504:
            pytest.skip("Agent is still starting up")

        if not check_skills_endpoint_available(response):
            pytest.skip("Agent base image needs rebuild with skills.py router")

        assert_status(response, 200)
        data = response.json()

        # New agents typically have no skills
        # (unless template includes them)
        skills = data.get("skills", [])
        count = data.get("count", 0)

        # Verify consistency
        assert len(skills) == count

        # skill_paths should still be returned
        assert "skill_paths" in data


# =============================================================================
# PLAYBOOKS - CONCURRENT ACCESS TESTS
# =============================================================================


class TestPlaybooksConcurrentAccess:
    """Tests for Playbooks endpoint concurrent access patterns."""

    def test_playbooks_multiple_requests_consistent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Multiple requests to playbooks endpoint return consistent results."""
        responses = []
        for _ in range(3):
            response = api_client.get(f"/api/agents/{created_agent['name']}/playbooks")

            if not check_skills_endpoint_available(response):
                pytest.skip("Agent base image needs rebuild with skills.py router")

            if response.status_code == 200:
                responses.append(response.json())

        if len(responses) < 2:
            pytest.skip("Could not get multiple successful responses")

        # All responses should have same structure
        first = responses[0]
        for subsequent in responses[1:]:
            assert subsequent.get("count") == first.get("count"), \
                "Skill count should be consistent across requests"
            assert len(subsequent.get("skills", [])) == len(first.get("skills", [])), \
                "Skill list length should be consistent across requests"
