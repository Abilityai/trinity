"""
Skills Tests (test_skills.py)

Tests for the Skills Management System:
- Skills Library endpoints (listing, getting, syncing)
- Agent Skills assignment (CRUD)
- Skills injection to running agents

SMOKE TESTS (marked with @pytest.mark.smoke):
- Library status, list skills, get skill
- No agent creation required

AGENT TESTS:
- Skill assignment, injection
- Require created_agent fixture
"""

import pytest
import uuid

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
    assert_list_response,
)


# =============================================================================
# SKILLS LIBRARY - AUTHENTICATION TESTS
# =============================================================================


class TestSkillsLibraryAuthentication:
    """Tests for Skills Library endpoints authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_list_skills_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/skills/library requires authentication."""
        response = unauthenticated_client.get("/api/skills/library", auth=False)
        assert_status(response, 401)

    def test_get_skill_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/skills/library/{name} requires authentication."""
        response = unauthenticated_client.get("/api/skills/library/commit", auth=False)
        assert_status(response, 401)

    def test_library_status_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/skills/library/status requires authentication."""
        response = unauthenticated_client.get("/api/skills/library/status", auth=False)
        assert_status(response, 401)

    def test_sync_library_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/skills/library/sync requires authentication."""
        response = unauthenticated_client.post("/api/skills/library/sync", auth=False)
        assert_status(response, 401)


# =============================================================================
# SKILLS LIBRARY - STATUS TESTS
# =============================================================================


class TestSkillsLibraryStatus:
    """Tests for GET /api/skills/library/status endpoint."""

    pytestmark = pytest.mark.smoke

    def test_library_status_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/skills/library/status returns expected structure."""
        response = api_client.get("/api/skills/library/status")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have core status fields
        assert "configured" in data
        assert isinstance(data["configured"], bool)

    def test_library_status_omits_source_url(self, api_client: TrinityApiClient):
        """The repo URL must NOT be in this payload (ent#334).

        This assertion is inverted from what it was: it used to require `url`
        (and `branch`) to be present. That was the bug — this route is
        `Depends(get_current_user)`, reachable by every authenticated caller
        and by agent-scoped MCP keys, while source repo URLs are
        admin-sensitive and served only by `GET /api/skills/sources`
        (`require_admin` + `reject_agent_principal`). The route now declares a
        `response_model` allow-list that drops the flat `url` and the
        per-source `url`. Only the URL — `branch`/`commit_sha` stay.

        Asserting KEY ABSENCE, not "no secret in the body": the org/repo half
        of the disclosure carries no secret at all.

        `cloned`/`skill_count` stay — the frontend stores derive their
        empty-state discriminator from `configured`/`cloned`.
        """
        response = api_client.get("/api/skills/library/status")
        assert_status(response, 200)
        data = response.json()

        assert "url" not in data, "ent#334: leaked flat url"
        for source in data.get("sources", []):
            assert "url" not in source, "ent#334: leaked per-source url"

        # Everything non-URL stays. `branch`/`commit_sha` are a ref name and a
        # commit hash — not credentials, not a repo identity — and the Library
        # header renders them.
        for field in ["cloned", "skill_count", "branch", "commit_sha"]:
            assert field in data, f"Missing field: {field}"


# =============================================================================
# SKILLS LIBRARY - LIST SKILLS TESTS
# =============================================================================


class TestSkillsLibraryList:
    """Tests for GET /api/skills/library endpoint."""

    pytestmark = pytest.mark.smoke

    def test_list_skills_returns_array(self, api_client: TrinityApiClient):
        """GET /api/skills/library returns array of skills."""
        response = api_client.get("/api/skills/library")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, list)

    def test_list_skills_structure_when_available(self, api_client: TrinityApiClient):
        """Skills have expected fields when library is populated."""
        response = api_client.get("/api/skills/library")
        assert_status(response, 200)
        skills = response.json()

        if len(skills) > 0:
            skill = skills[0]
            assert_has_fields(skill, ["name", "path"])
            # Description may be None
            assert "description" in skill

    def test_list_skills_empty_when_not_configured(self, api_client: TrinityApiClient):
        """GET /api/skills/library returns empty array when library not synced."""
        # First check status
        status_response = api_client.get("/api/skills/library/status")
        status = status_response.json()

        if not status.get("cloned"):
            # If not cloned, list should be empty
            response = api_client.get("/api/skills/library")
            assert_status(response, 200)
            skills = response.json()
            assert isinstance(skills, list)
            # May be empty if library not synced
            # (don't assert empty - it could have been synced)


# =============================================================================
# SKILLS LIBRARY - GET SKILL TESTS
# =============================================================================


class TestSkillsLibraryGet:
    """Tests for GET /api/skills/library/{name} endpoint."""

    pytestmark = pytest.mark.smoke

    def test_get_nonexistent_skill_returns_404(self, api_client: TrinityApiClient):
        """GET /api/skills/library/{name} returns 404 for nonexistent skill."""
        response = api_client.get("/api/skills/library/nonexistent-skill-xyz-12345")
        assert_status(response, 404)

    def test_get_skill_returns_content_when_exists(self, api_client: TrinityApiClient):
        """GET /api/skills/library/{name} returns content when skill exists."""
        # First check if there are any skills
        list_response = api_client.get("/api/skills/library")
        skills = list_response.json()

        if len(skills) == 0:
            pytest.skip("No skills available in library")

        skill_name = skills[0]["name"]
        response = api_client.get(f"/api/skills/library/{skill_name}")

        assert_status(response, 200)
        data = response.json()
        assert_has_fields(data, ["name", "path"])
        # Content should be present for individual skill fetch
        assert "content" in data
        if data["content"]:
            assert isinstance(data["content"], str)


# =============================================================================
# SKILLS LIBRARY - SYNC TESTS
# =============================================================================


class TestSkillsLibrarySync:
    """Tests for POST /api/skills/library/sync endpoint."""

    pytestmark = pytest.mark.smoke

    def test_sync_never_500s_whatever_the_configuration(self, api_client: TrinityApiClient):
        """Sync answers with a named error or a real result — never a 500 (#2242).

        Was `test_sync_requires_url_configured`, which asked
        `if status.get("url"): skip`. ent#237 replaced the single
        `skills_library_url` setting with the `skill_sources` table, so `url` reads
        `None` on every modern install — including ones that DO have sources. The
        skip stopped firing, the sync ran against the migrated source, succeeded,
        and the test failed asserting 400 against 200 while its name still claimed
        to be testing the unconfigured state.

        Swapping the key does not repair it: an API test cannot dictate what the
        target instance has configured (observed live — `/status` reported
        `configured: false` moments before `/sync` found an enabled source and
        cloned it). So the deterministic "no sources → 400, named" contract moved to
        a unit test that owns its own configuration,
        `tests/unit/test_ent237_skill_sources.py::TestSyncBookkeeping::test_sync_with_no_sources_configured_fails_by_name`,
        and what stays here is the property that holds on ANY instance and is worth
        an end-to-end check: the route degrades by name rather than crashing.
        """
        response = api_client.post("/api/skills/library/sync")

        assert response.status_code in (200, 400, 409), response.text
        data = response.json()
        if response.status_code == 200:
            # Configured: a real result, in the multi-source shape ent#237 added.
            assert data.get("success") is True, data
            assert isinstance(data.get("sources"), list)
        else:
            # 400 unconfigured / bad source; 409 a sync already running.
            assert "detail" in data
            assert data["detail"], "an error status must name its reason"

    def test_sync_returns_structure_when_configured(self, api_client: TrinityApiClient):
        """POST /api/skills/library/sync returns expected structure when configured."""
        # #2242: gate on `configured` (ent#237: "at least one source exists"), not
        # the legacy `url`, which is None on every modern install — so this test
        # was skipping unconditionally and had stopped asserting anything at all.
        status_response = api_client.get("/api/skills/library/status")
        status = status_response.json()

        if not (status.get("configured") or status.get("sources")):
            pytest.skip("No skills sources configured on this instance")

        response = api_client.post("/api/skills/library/sync")

        # May fail (bad URL, no PAT, network), but should return valid response
        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            if data["success"]:
                # Successful sync should include these fields
                assert "skill_count" in data or "action" in data


# =============================================================================
# AGENT SKILLS - AUTHENTICATION TESTS
# =============================================================================


class TestAgentSkillsAuthentication:
    """Tests for Agent Skills endpoints authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_get_agent_skills_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/skills requires authentication."""
        response = unauthenticated_client.get("/api/agents/test-agent/skills", auth=False)
        assert_status(response, 401)

    def test_update_agent_skills_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """PUT /api/agents/{name}/skills requires authentication."""
        response = unauthenticated_client.put(
            "/api/agents/test-agent/skills",
            json={"skills": ["commit"]},
            auth=False
        )
        assert_status(response, 401)

    def test_assign_skill_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/agents/{name}/skills/{skill} requires authentication."""
        response = unauthenticated_client.post(
            "/api/agents/test-agent/skills/commit",
            auth=False
        )
        assert_status(response, 401)

    def test_unassign_skill_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """DELETE /api/agents/{name}/skills/{skill} requires authentication."""
        response = unauthenticated_client.delete(
            "/api/agents/test-agent/skills/commit",
            auth=False
        )
        assert_status(response, 401)

    def test_inject_skills_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/agents/{name}/skills/inject requires authentication."""
        response = unauthenticated_client.post(
            "/api/agents/test-agent/skills/inject",
            auth=False
        )
        assert_status(response, 401)


# =============================================================================
# AGENT SKILLS - GET ASSIGNED SKILLS TESTS
# =============================================================================


class TestAgentSkillsGet:
    """Tests for GET /api/agents/{name}/skills endpoint."""

    def test_get_agent_skills_returns_array(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/skills returns array."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/skills")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, list)

    def test_get_agent_skills_initially_empty(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """New agents start with no skills assigned."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/skills")
        assert_status(response, 200)
        skills = response.json()
        # New agents should have no skills
        assert len(skills) == 0

    def test_get_agent_skills_nonexistent_agent(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/skills for nonexistent agent returns empty or 404."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz/skills")
        # Could be 404 (agent not found) or 200 with empty list
        assert_status_in(response, [200, 404])
        if response.status_code == 200:
            assert response.json() == []


# =============================================================================
# AGENT SKILLS - BULK UPDATE TESTS
# =============================================================================


class TestAgentSkillsUpdate:
    """Tests for PUT /api/agents/{name}/skills endpoint."""

    def test_update_skills_empty_list(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/skills with empty list clears skills."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/skills",
            json={"skills": []}
        )
        assert_status(response, 200)
        data = response.json()
        assert data.get("success") is True
        assert data.get("skills_assigned") == 0

    def test_update_skills_with_list(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/skills assigns multiple skills."""
        # First check if skills are available
        skills_response = api_client.get("/api/skills/library")
        available_skills = skills_response.json()

        if len(available_skills) == 0:
            pytest.skip("No skills available in library")

        skill_names = [s["name"] for s in available_skills[:2]]

        response = api_client.put(
            f"/api/agents/{created_agent['name']}/skills",
            json={"skills": skill_names}
        )
        assert_status(response, 200)
        data = response.json()
        assert data.get("success") is True
        assert data.get("skills_assigned") == len(skill_names)

        # Verify skills were assigned
        verify_response = api_client.get(f"/api/agents/{created_agent['name']}/skills")
        assigned = verify_response.json()
        assigned_names = [s["skill_name"] for s in assigned]
        for name in skill_names:
            assert name in assigned_names

        # Cleanup - clear skills
        api_client.put(
            f"/api/agents/{created_agent['name']}/skills",
            json={"skills": []}
        )

    def test_update_skills_replaces_existing(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/skills replaces existing assignments."""
        # Check if skills are available
        skills_response = api_client.get("/api/skills/library")
        available_skills = skills_response.json()

        if len(available_skills) < 2:
            pytest.skip("Need at least 2 skills in library")

        skill1 = available_skills[0]["name"]
        skill2 = available_skills[1]["name"]

        try:
            # Assign first skill
            api_client.put(
                f"/api/agents/{created_agent['name']}/skills",
                json={"skills": [skill1]}
            )

            # Replace with second skill
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/skills",
                json={"skills": [skill2]}
            )
            assert_status(response, 200)

            # Verify only second skill is assigned
            verify_response = api_client.get(f"/api/agents/{created_agent['name']}/skills")
            assigned = verify_response.json()
            assigned_names = [s["skill_name"] for s in assigned]

            assert skill2 in assigned_names
            assert skill1 not in assigned_names
        finally:
            # Cleanup
            api_client.put(
                f"/api/agents/{created_agent['name']}/skills",
                json={"skills": []}
            )


# =============================================================================
# AGENT SKILLS - SINGLE SKILL ASSIGNMENT TESTS
# =============================================================================


class TestAgentSkillsAssign:
    """Tests for POST /api/agents/{name}/skills/{skill} endpoint."""

    def test_assign_skill_success(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/skills/{skill} assigns skill."""
        # Check if skills are available
        skills_response = api_client.get("/api/skills/library")
        available_skills = skills_response.json()

        if len(available_skills) == 0:
            pytest.skip("No skills available in library")

        skill_name = available_skills[0]["name"]

        try:
            response = api_client.post(
                f"/api/agents/{created_agent['name']}/skills/{skill_name}"
            )
            assert_status(response, 200)
            data = response.json()
            assert data.get("success") is True

            # Verify skill is assigned
            verify_response = api_client.get(f"/api/agents/{created_agent['name']}/skills")
            assigned = verify_response.json()
            assigned_names = [s["skill_name"] for s in assigned]
            assert skill_name in assigned_names
        finally:
            # Cleanup
            api_client.delete(f"/api/agents/{created_agent['name']}/skills/{skill_name}")

    def test_assign_nonexistent_skill_returns_404(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/skills/{skill} returns 404 for nonexistent skill."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/skills/nonexistent-skill-xyz"
        )
        assert_status(response, 404)

    def test_assign_skill_idempotent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Assigning same skill twice is idempotent."""
        # Check if skills are available
        skills_response = api_client.get("/api/skills/library")
        available_skills = skills_response.json()

        if len(available_skills) == 0:
            pytest.skip("No skills available in library")

        skill_name = available_skills[0]["name"]

        try:
            # Assign twice
            api_client.post(f"/api/agents/{created_agent['name']}/skills/{skill_name}")
            response = api_client.post(
                f"/api/agents/{created_agent['name']}/skills/{skill_name}"
            )

            assert_status(response, 200)
            data = response.json()
            assert data.get("success") is True
            # May indicate already assigned
            assert "assigned" in data.get("message", "").lower() or data.get("skill") is None
        finally:
            api_client.delete(f"/api/agents/{created_agent['name']}/skills/{skill_name}")


# =============================================================================
# AGENT SKILLS - UNASSIGN TESTS
# =============================================================================


class TestAgentSkillsUnassign:
    """Tests for DELETE /api/agents/{name}/skills/{skill} endpoint."""

    def test_unassign_skill_success(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/agents/{name}/skills/{skill} removes assignment."""
        # Check if skills are available
        skills_response = api_client.get("/api/skills/library")
        available_skills = skills_response.json()

        if len(available_skills) == 0:
            pytest.skip("No skills available in library")

        skill_name = available_skills[0]["name"]

        # First assign
        api_client.post(f"/api/agents/{created_agent['name']}/skills/{skill_name}")

        # Then unassign
        response = api_client.delete(
            f"/api/agents/{created_agent['name']}/skills/{skill_name}"
        )
        assert_status(response, 200)
        data = response.json()
        assert data.get("success") is True

        # Verify skill is unassigned
        verify_response = api_client.get(f"/api/agents/{created_agent['name']}/skills")
        assigned = verify_response.json()
        assigned_names = [s["skill_name"] for s in assigned]
        assert skill_name not in assigned_names

    def test_unassign_nonexistent_skill_is_idempotent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/agents/{name}/skills/{skill} is idempotent."""
        response = api_client.delete(
            f"/api/agents/{created_agent['name']}/skills/nonexistent-skill-xyz"
        )
        # Should succeed even if skill not assigned
        assert_status(response, 200)
        data = response.json()
        assert data.get("success") is True


# =============================================================================
# AGENT SKILLS - INJECTION TESTS
# =============================================================================


class TestAgentSkillsInjection:
    """Tests for POST /api/agents/{name}/skills/inject endpoint."""

    def test_inject_no_skills_succeeds(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/skills/inject succeeds with no skills."""
        # Ensure no skills assigned
        api_client.put(
            f"/api/agents/{created_agent['name']}/skills",
            json={"skills": []}
        )

        response = api_client.post(
            f"/api/agents/{created_agent['name']}/skills/inject"
        )
        assert_status(response, 200)
        data = response.json()
        assert data.get("success") is True
        assert data.get("skills_injected", 0) == 0

    def test_inject_with_skills(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/skills/inject injects assigned skills."""
        # Check if skills are available
        skills_response = api_client.get("/api/skills/library")
        available_skills = skills_response.json()

        if len(available_skills) == 0:
            pytest.skip("No skills available in library")

        skill_name = available_skills[0]["name"]

        try:
            # Assign skill
            api_client.put(
                f"/api/agents/{created_agent['name']}/skills",
                json={"skills": [skill_name]}
            )

            # Inject
            response = api_client.post(
                f"/api/agents/{created_agent['name']}/skills/inject"
            )
            assert_status(response, 200)
            data = response.json()

            # Should have injected 1 skill (or failed gracefully)
            if data.get("success"):
                assert data.get("skills_injected") == 1
        finally:
            # Cleanup
            api_client.put(
                f"/api/agents/{created_agent['name']}/skills",
                json={"skills": []}
            )

    def test_inject_nonexistent_agent_graceful_response(self, api_client: TrinityApiClient):
        """POST /api/agents/{name}/skills/inject for nonexistent agent handles gracefully."""
        response = api_client.post("/api/agents/nonexistent-agent-xyz/skills/inject")
        # API handles gracefully - returns success with 0 skills injected
        # This is acceptable behavior since there are simply no skills to inject
        assert_status_in(response, [200, 400, 404])
        if response.status_code == 200:
            data = response.json()
            assert data.get("skills_injected", 0) == 0


# =============================================================================
# AGENT SKILLS - SKILL METADATA TESTS
# =============================================================================


class TestAgentSkillsMetadata:
    """Tests for skill assignment metadata."""

    def test_assigned_skill_has_metadata(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Assigned skills include assignment metadata."""
        # Check if skills are available
        skills_response = api_client.get("/api/skills/library")
        available_skills = skills_response.json()

        if len(available_skills) == 0:
            pytest.skip("No skills available in library")

        skill_name = available_skills[0]["name"]

        try:
            # Assign skill
            api_client.post(f"/api/agents/{created_agent['name']}/skills/{skill_name}")

            # Get skills
            response = api_client.get(f"/api/agents/{created_agent['name']}/skills")
            assert_status(response, 200)
            skills = response.json()

            assert len(skills) > 0
            skill = skills[0]

            # Should have metadata
            assert_has_fields(skill, ["skill_name", "assigned_by", "assigned_at"])
            assert skill["skill_name"] == skill_name
        finally:
            api_client.delete(f"/api/agents/{created_agent['name']}/skills/{skill_name}")


# =============================================================================
# CLEANUP ON AGENT DELETE TESTS
# =============================================================================


class TestSkillsCleanupOnDelete:
    """Tests for skill cleanup when agent is deleted."""

    @pytest.mark.slow
    def test_skills_deleted_with_agent(self, api_client: TrinityApiClient):
        """Skills are cleaned up when agent is deleted."""
        import time

        agent_name = f"test-skills-cleanup-{uuid.uuid4().hex[:6]}"

        try:
            # Create agent
            response = api_client.post("/api/agents", json={"name": agent_name})
            if response.status_code not in [200, 201]:
                pytest.skip("Failed to create test agent")

            # Wait for agent to start
            time.sleep(10)

            # Check if skills are available
            skills_response = api_client.get("/api/skills/library")
            available_skills = skills_response.json()

            if len(available_skills) == 0:
                pytest.skip("No skills available in library")

            skill_name = available_skills[0]["name"]

            # Assign skill
            api_client.post(f"/api/agents/{agent_name}/skills/{skill_name}")

            # Verify skill is assigned
            verify_response = api_client.get(f"/api/agents/{agent_name}/skills")
            assert len(verify_response.json()) > 0

            # Delete agent
            api_client.delete(f"/api/agents/{agent_name}")
            time.sleep(2)

            # Skills should be cleaned up (API may return 404 or empty list)
            response = api_client.get(f"/api/agents/{agent_name}/skills")
            if response.status_code == 200:
                # If agent entry still exists, skills should be empty
                assert len(response.json()) == 0

        finally:
            # Ensure cleanup
            try:
                api_client.delete(f"/api/agents/{agent_name}")
            except Exception:
                pass
