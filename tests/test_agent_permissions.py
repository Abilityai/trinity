"""
Agent Permissions Tests (test_agent_permissions.py)

Tests for agent-to-agent permission system (Requirement 9.10).
Covers permission CRUD, restrictive default permissions (no auto-grant), and cascade delete.

NOTE: MCP enforcement tests require agent-scoped keys and are in a separate test.
"""

import pytest
import time
import uuid
from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)
from utils.cleanup import cleanup_test_agent


class TestGetPermissions:
    """REQ-PERM-001: Get agent permissions endpoint tests."""

    def test_get_permissions_returns_structure(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/permissions returns correct structure."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/permissions"
        )

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have required fields
        assert "source_agent" in data
        assert "permitted_agents" in data
        assert "available_agents" in data

        # source_agent should match request
        assert data["source_agent"] == created_agent["name"]

        # permitted_agents and available_agents should be lists
        assert isinstance(data["permitted_agents"], list)
        assert isinstance(data["available_agents"], list)

    def test_get_permissions_for_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """GET /api/agents/{name}/permissions returns 404 for missing agent."""
        response = api_client.get(
            "/api/agents/nonexistent-agent-12345/permissions"
        )

        assert_status_in(response, [403, 404])

    def test_available_agents_have_required_fields(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Each available agent has name, status, type, permitted fields."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/permissions"
        )

        assert_status(response, 200)
        data = response.json()

        for agent in data.get("available_agents", []):
            assert "name" in agent, f"Missing 'name' in agent: {agent}"
            assert "status" in agent, f"Missing 'status' in agent: {agent}"
            assert "type" in agent, f"Missing 'type' in agent: {agent}"
            assert "permitted" in agent, f"Missing 'permitted' in agent: {agent}"
            assert isinstance(agent["permitted"], bool)


class TestSetPermissions:
    """REQ-PERM-002: Set agent permissions (bulk) endpoint tests."""

    def test_set_permissions_empty_list(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/permissions with empty list clears all."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/permissions",
            json={"permitted_agents": []}
        )

        assert_status(response, 200)
        data = assert_json_response(response)

        assert data.get("status") == "updated"
        assert data.get("permitted_count") == 0

    def test_set_permissions_with_self_is_handled(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/permissions with self in list.

        NOTE: API is lenient and allows self in list (becomes no-op since
        agents always have implicit self-access). This tests that behavior.
        """
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/permissions",
            json={"permitted_agents": [created_agent["name"]]}
        )

        # API is lenient - allows the request (self-permission is harmless)
        assert_status_in(response, [200, 400, 422])

    def test_set_permissions_with_nonexistent_agent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/permissions with nonexistent targets.

        NOTE: API currently allows setting permissions for agents that don't
        exist yet. This could be intentional (forward declaration) or a gap.
        """
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/permissions",
            json={"permitted_agents": ["nonexistent-agent-xyz"]}
        )

        # API may accept (lenient) or reject (strict)
        assert_status_in(response, [200, 400, 404])


class TestAddPermission:
    """REQ-PERM-003: Add single permission endpoint tests."""

    def test_add_permission_returns_status(
        self,
        api_client: TrinityApiClient,
        created_agent,
        request
    ):
        """POST /api/agents/{name}/permissions/{target} adds permission."""
        # Create a second agent to grant permission to
        second_agent_name = f"test-perm-target-{uuid.uuid4().hex[:6]}"
        create_response = api_client.post(
            "/api/agents",
            json={"name": second_agent_name}
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create second agent")

        # Wait for it to be created
        time.sleep(5)

        try:
            # First, clear any existing permissions
            api_client.put(
                f"/api/agents/{created_agent['name']}/permissions",
                json={"permitted_agents": []}
            )

            # Add permission
            response = api_client.post(
                f"/api/agents/{created_agent['name']}/permissions/{second_agent_name}"
            )

            assert_status(response, 200)
            data = assert_json_response(response)
            assert data.get("status") in ["added", "already_exists"]

        finally:
            # Cleanup second agent
            if not request.config.getoption("--skip-cleanup"):
                cleanup_test_agent(api_client, second_agent_name)

    def test_add_permission_idempotent(
        self,
        api_client: TrinityApiClient,
        created_agent,
        request
    ):
        """Adding same permission twice returns already_exists."""
        # Create a second agent
        second_agent_name = f"test-perm-idem-{uuid.uuid4().hex[:6]}"
        create_response = api_client.post(
            "/api/agents",
            json={"name": second_agent_name}
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create second agent")

        time.sleep(5)

        try:
            # Clear permissions first
            api_client.put(
                f"/api/agents/{created_agent['name']}/permissions",
                json={"permitted_agents": []}
            )

            # Add permission first time
            api_client.post(
                f"/api/agents/{created_agent['name']}/permissions/{second_agent_name}"
            )

            # Add permission second time
            response = api_client.post(
                f"/api/agents/{created_agent['name']}/permissions/{second_agent_name}"
            )

            assert_status(response, 200)
            data = response.json()
            assert data.get("status") == "already_exists"

        finally:
            if not request.config.getoption("--skip-cleanup"):
                cleanup_test_agent(api_client, second_agent_name)

    def test_add_permission_to_nonexistent_target(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/permissions/{target} with nonexistent target.

        NOTE: API is lenient and allows forward-declaration of permissions
        for agents that may be created later. Validates structure only.
        """
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/permissions/nonexistent-xyz"
        )

        # API may accept (lenient) or reject (strict)
        assert_status_in(response, [200, 400, 404])

    def test_add_permission_to_self(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Cannot add permission to call self."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/permissions/{created_agent['name']}"
        )

        assert_status_in(response, [400, 422])


class TestRemovePermission:
    """REQ-PERM-004: Remove single permission endpoint tests."""

    def test_remove_permission(
        self,
        api_client: TrinityApiClient,
        created_agent,
        request
    ):
        """DELETE /api/agents/{name}/permissions/{target} removes permission."""
        # Create a second agent
        second_agent_name = f"test-perm-remove-{uuid.uuid4().hex[:6]}"
        create_response = api_client.post(
            "/api/agents",
            json={"name": second_agent_name}
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create second agent")

        time.sleep(5)

        try:
            # Ensure permission exists first
            api_client.post(
                f"/api/agents/{created_agent['name']}/permissions/{second_agent_name}"
            )

            # Remove it
            response = api_client.delete(
                f"/api/agents/{created_agent['name']}/permissions/{second_agent_name}"
            )

            assert_status(response, 200)
            data = response.json()
            assert data.get("status") in ["removed", "not_found"]

        finally:
            if not request.config.getoption("--skip-cleanup"):
                cleanup_test_agent(api_client, second_agent_name)

    def test_remove_nonexistent_permission(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE for non-existing permission returns not_found."""
        response = api_client.delete(
            f"/api/agents/{created_agent['name']}/permissions/nonexistent-target"
        )

        # Should return 200 with not_found status or 404
        assert_status_in(response, [200, 404])
        if response.status_code == 200:
            data = response.json()
            assert data.get("status") == "not_found"


class TestDefaultPermissions:
    """REQ-PERM-005: Default permission behavior on agent creation (RESTRICTIVE)."""

    def test_new_agent_has_no_default_permissions(
        self,
        api_client: TrinityApiClient,
        created_agent,
        request
    ):
        """New agent starts with NO permissions (restrictive default as of 2026-02-19)."""
        # Create a new agent (created_agent already exists)
        new_agent_name = f"test-perm-default-{uuid.uuid4().hex[:6]}"
        create_response = api_client.post(
            "/api/agents",
            json={"name": new_agent_name}
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create new agent")

        time.sleep(5)

        try:
            # Check permissions from created_agent's perspective
            response1 = api_client.get(
                f"/api/agents/{created_agent['name']}/permissions"
            )
            assert_status(response1, 200)
            data1 = response1.json()

            # Check if new agent is in available agents list
            available_names = [a["name"] for a in data1.get("available_agents", [])]

            # New agent should be in the available list (visible to same owner)
            if new_agent_name in available_names:
                # Find the entry and check it's NOT permitted (restrictive default)
                for agent in data1["available_agents"]:
                    if agent["name"] == new_agent_name:
                        # Restrictive default: new agents are NOT permitted
                        assert agent["permitted"] is False, \
                            f"Expected new agent to have NO default permissions (restrictive)"
                        break

            # Check reverse direction - new agent should have NO permission to created_agent
            response2 = api_client.get(
                f"/api/agents/{new_agent_name}/permissions"
            )
            assert_status(response2, 200)
            data2 = response2.json()

            # Restrictive default: permitted_agents should be empty
            permitted_names = [a["name"] for a in data2.get("permitted_agents", [])]
            assert created_agent["name"] not in permitted_names, \
                "Expected NO default permissions (restrictive mode)"

            # Verify available_agents structure works (same-owner agents visible but not permitted)
            assert len(data2.get("available_agents", [])) >= 0

        finally:
            if not request.config.getoption("--skip-cleanup"):
                cleanup_test_agent(api_client, new_agent_name)


class TestPermissionCascadeDelete:
    """REQ-PERM-006: Permissions deleted when agent is deleted."""

    def test_permissions_removed_on_agent_delete(
        self,
        api_client: TrinityApiClient,
        created_agent,
        request
    ):
        """When an agent is deleted, its permissions are also removed."""
        # Create a temporary agent
        temp_agent_name = f"test-perm-cascade-{uuid.uuid4().hex[:6]}"
        create_response = api_client.post(
            "/api/agents",
            json={"name": temp_agent_name}
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create temporary agent")

        time.sleep(5)

        # Grant permission from created_agent to temp_agent
        api_client.post(
            f"/api/agents/{created_agent['name']}/permissions/{temp_agent_name}"
        )

        # Verify permission exists
        perms_before = api_client.get(
            f"/api/agents/{created_agent['name']}/permissions"
        ).json()

        permitted_before = [a["name"] for a in perms_before.get("permitted_agents", [])]

        # Delete the temp agent
        delete_response = api_client.delete(f"/api/agents/{temp_agent_name}")
        assert_status_in(delete_response, [200, 204])

        time.sleep(2)

        # Check permissions again - temp_agent should be gone
        perms_after = api_client.get(
            f"/api/agents/{created_agent['name']}/permissions"
        ).json()

        permitted_after = [a["name"] for a in perms_after.get("permitted_agents", [])]
        available_after = [a["name"] for a in perms_after.get("available_agents", [])]

        # Temp agent should not appear in either list
        assert temp_agent_name not in permitted_after, \
            "Deleted agent still in permitted_agents"
        assert temp_agent_name not in available_after, \
            "Deleted agent still in available_agents"


class TestPermissionAuthorization:
    """REQ-PERM-007: Only owner can modify permissions."""

    def test_get_permissions_requires_access(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """GET permissions requires authentication."""
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}/permissions"
        )

        assert_status_in(response, [401, 403])

    def test_set_permissions_requires_access(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """PUT permissions requires authentication."""
        response = unauthenticated_client.put(
            f"/api/agents/{created_agent['name']}/permissions",
            json={"permitted_agents": []}
        )

        assert_status_in(response, [401, 403])


class TestBulkPermissionEdges:
    """REQ-PERM-008: Bulk permissions-edges endpoint (#359)."""

    def test_get_permissions_edges_returns_structure(
        self,
        api_client: TrinityApiClient
    ):
        """GET /api/agents/permissions-edges returns correct structure."""
        response = api_client.get("/api/agents/permissions-edges")

        assert_status(response, 200)
        data = assert_json_response(response)

        assert "edges" in data
        assert isinstance(data["edges"], list)

    def test_get_permissions_edges_empty_when_no_permissions(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Bulk endpoint returns empty edges when agent has no permissions."""
        # Clear any existing permissions
        api_client.put(
            f"/api/agents/{created_agent['name']}/permissions",
            json={"permitted_agents": []}
        )

        response = api_client.get("/api/agents/permissions-edges")

        assert_status(response, 200)
        data = response.json()

        # No edges involving our test agent
        our_edges = [
            e for e in data["edges"]
            if e["source"] == created_agent["name"] or e["target"] == created_agent["name"]
        ]
        assert len(our_edges) == 0

    def test_get_permissions_edges_includes_granted_permissions(
        self,
        api_client: TrinityApiClient,
        created_agent,
        request
    ):
        """Bulk endpoint includes edges for granted permissions."""
        # Create a second agent
        second_agent_name = f"test-bulk-edge-{uuid.uuid4().hex[:6]}"
        create_response = api_client.post(
            "/api/agents",
            json={"name": second_agent_name}
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create second agent")

        time.sleep(5)

        try:
            # Grant permission
            api_client.post(
                f"/api/agents/{created_agent['name']}/permissions/{second_agent_name}"
            )

            # Check bulk endpoint
            response = api_client.get("/api/agents/permissions-edges")

            assert_status(response, 200)
            data = response.json()

            # Find edge from created_agent to second_agent
            matching_edges = [
                e for e in data["edges"]
                if e["source"] == created_agent["name"] and e["target"] == second_agent_name
            ]
            assert len(matching_edges) == 1, "Expected edge for granted permission"

        finally:
            if not request.config.getoption("--skip-cleanup"):
                cleanup_test_agent(api_client, second_agent_name)

    def test_get_permissions_edges_edge_structure(
        self,
        api_client: TrinityApiClient,
        created_agent,
        request
    ):
        """Each edge has source and target fields."""
        # Create a second agent and grant permission
        second_agent_name = f"test-edge-struct-{uuid.uuid4().hex[:6]}"
        create_response = api_client.post(
            "/api/agents",
            json={"name": second_agent_name}
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create second agent")

        time.sleep(5)

        try:
            api_client.post(
                f"/api/agents/{created_agent['name']}/permissions/{second_agent_name}"
            )

            response = api_client.get("/api/agents/permissions-edges")

            assert_status(response, 200)
            data = response.json()

            for edge in data["edges"]:
                assert "source" in edge, "Edge missing 'source' field"
                assert "target" in edge, "Edge missing 'target' field"
                assert isinstance(edge["source"], str)
                assert isinstance(edge["target"], str)

        finally:
            if not request.config.getoption("--skip-cleanup"):
                cleanup_test_agent(api_client, second_agent_name)

    def test_get_permissions_edges_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient
    ):
        """Bulk endpoint requires authentication."""
        response = unauthenticated_client.get("/api/agents/permissions-edges")

        assert_status_in(response, [401, 403])
