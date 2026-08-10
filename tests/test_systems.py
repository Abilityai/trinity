"""
System Manifest Deployment Tests (test_systems.py)

Tests for multi-agent system deployment from YAML manifests.
Covers REQ-SYSTEM-001 (10.7 System Manifest Deployment).

This test suite validates:
- Phase 1: YAML parsing, validation, agent creation, conflict resolution
- Phase 2: Permissions, folders, schedules, auto-start
- Phase 3: MCP tools and backend endpoints

Test Organization:
- Smoke tests: YAML parsing and validation (no agent creation)
- Core tests: Deployment, permissions, folders, schedules
- Slow tests: Full multi-agent system tests with all features
"""

import pytest
import time
import uuid
import yaml

from testkit.api_client import TrinityApiClient

# Multi-agent system deploys can take >30s; raise to 120s for this suite.
pytestmark = pytest.mark.timeout(120)
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
    assert_agent_fields,
    assert_list_response,
    assert_contains_agent,
)
from testkit.cleanup import cleanup_test_agent


# =============================================================================
# TEST DATA - YAML MANIFESTS
# =============================================================================

MINIMAL_MANIFEST = """
name: test-minimal
agents:
  worker:
    template: local:default
"""

FULL_MANIFEST = """
name: test-full
description: Full-featured test system
prompt: |
  Custom system-wide instructions for all agents.
  This should be injected into all agents.

agents:
  orchestrator:
    template: local:default
    resources:
      cpu: "2"
      memory: "4g"
    folders:
      expose: true
      consume: true
    schedules:
      - name: daily-task
        cron: "0 9 * * *"
        message: "Run daily analysis"
        timezone: "UTC"

  worker:
    template: local:default
    folders:
      expose: true
      consume: true

permissions:
  preset: full-mesh
"""

ORCHESTRATOR_WORKERS_MANIFEST = """
name: test-orchestrator
agents:
  orchestrator:
    template: local:default
  worker1:
    template: local:default
  worker2:
    template: local:default

permissions:
  preset: orchestrator-workers
"""

EXPLICIT_PERMISSIONS_MANIFEST = """
name: test-explicit
agents:
  manager:
    template: local:default
  analyst:
    template: local:default
  reporter:
    template: local:default

permissions:
  explicit:
    manager:
      - analyst
      - reporter
    analyst:
      - reporter
    reporter: []
"""

NONE_PERMISSIONS_MANIFEST = """
name: test-none
agents:
  agent1:
    template: local:default
  agent2:
    template: local:default

permissions:
  preset: none
"""


# =============================================================================
# PHASE 1 TESTS - YAML PARSING, VALIDATION, AGENT CREATION
# =============================================================================

class TestSystemManifestParsing:
    """REQ-SYSTEM-001.1: YAML parsing and validation tests."""

    pytestmark = pytest.mark.smoke

    def test_dry_run_minimal_manifest(self, api_client: TrinityApiClient):
        """Dry run with minimal valid manifest returns validation result."""
        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": MINIMAL_MANIFEST, "dry_run": True},
        )

        assert_status(response, 200)
        data = assert_json_response(response)

        assert data["status"] == "valid"
        assert data["system_name"] == "test-minimal"
        assert "agents_to_create" in data
        assert len(data["agents_to_create"]) == 1
        assert data["agents_to_create"][0]["short_name"] == "worker"

    def test_dry_run_invalid_yaml(self, api_client: TrinityApiClient):
        """Dry run with invalid YAML syntax returns 400."""
        invalid_yaml = "name: test\n  invalid: yaml: syntax"

        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": invalid_yaml, "dry_run": True},
        )

        assert_status(response, 400)
        data = assert_json_response(response)
        assert "YAML parse error" in data.get("detail", "").lower() or \
               "parse" in data.get("detail", "").lower()

    def test_dry_run_missing_name(self, api_client: TrinityApiClient):
        """Dry run with missing name field returns 400."""
        manifest = "agents:\n  worker:\n    template: local:default"

        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": True},
        )

        assert_status(response, 400)
        data = assert_json_response(response)
        assert "name" in data.get("detail", "").lower()

    def test_dry_run_missing_agents(self, api_client: TrinityApiClient):
        """Dry run with missing agents field returns 400."""
        manifest = "name: test-only"

        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": True},
        )

        assert_status(response, 400)
        data = assert_json_response(response)
        assert "agents" in data.get("detail", "").lower()

    def test_dry_run_invalid_system_name(self, api_client: TrinityApiClient):
        """Dry run with invalid system name format returns 400."""
        manifest = "name: INVALID_NAME\nagents:\n  worker:\n    template: local:default"

        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": True},
        )

        assert_status(response, 400)

    def test_dry_run_invalid_template_prefix(self, api_client: TrinityApiClient):
        """Dry run with invalid template prefix returns 400."""
        manifest = """
name: test-invalid-template
agents:
  worker:
    template: invalid:template
"""

        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": True},
        )

        assert_status(response, 400)
        data = assert_json_response(response)
        assert "github:" in data.get("detail", "").lower() or \
               "local:" in data.get("detail", "").lower()

    def test_dry_run_invalid_permission_preset(self, api_client: TrinityApiClient):
        """Dry run with invalid permission preset returns 400."""
        manifest = """
name: test-invalid-preset
agents:
  worker:
    template: local:default
permissions:
  preset: invalid-preset
"""

        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": True},
        )

        assert_status(response, 400)
        data = assert_json_response(response)
        assert "preset" in data.get("detail", "").lower()

    def test_dry_run_both_preset_and_explicit(self, api_client: TrinityApiClient):
        """Dry run with both preset and explicit permissions returns 400."""
        manifest = """
name: test-both-perms
agents:
  worker:
    template: local:default
permissions:
  preset: full-mesh
  explicit:
    worker: []
"""

        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": True},
        )

        assert_status(response, 400)


class TestSystemDeployment:
    """REQ-SYSTEM-001.2: System deployment tests."""

    def test_deploy_minimal_system(self, api_client: TrinityApiClient):
        """Deploy minimal system creates agent with correct naming."""
        system_name = f"test-min-{uuid.uuid4().hex[:6]}"
        manifest = f"name: {system_name}\nagents:\n  worker:\n    template: local:default"

        try:
            response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )

            assert_status(response, 200)
            data = assert_json_response(response)

            assert data["status"] == "deployed"
            assert data["system_name"] == system_name
            assert len(data["agents_created"]) == 1

            expected_name = f"{system_name}-worker"
            assert data["agents_created"][0] == expected_name

            # Verify agent exists
            time.sleep(3)
            agent_response = api_client.get(f"/api/agents/{expected_name}")
            assert_status(agent_response, 200)

        finally:
            # Cleanup
            expected_name = f"{system_name}-worker"
            cleanup_test_agent(api_client, expected_name)

    def test_deploy_conflict_resolution(self, api_client: TrinityApiClient):
        """Deploy same system twice creates agents with _2 suffix."""
        system_name = f"test-conflict-{uuid.uuid4().hex[:6]}"
        manifest = f"name: {system_name}\nagents:\n  worker:\n    template: local:default"

        try:
            # First deployment
            response1 = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )
            assert_status(response1, 200)
            data1 = response1.json()
            agent1_name = data1["agents_created"][0]

            time.sleep(2)

            # Second deployment (should create with _2 suffix)
            response2 = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )
            assert_status(response2, 200)
            data2 = response2.json()
            agent2_name = data2["agents_created"][0]

            assert agent1_name == f"{system_name}-worker"
            assert agent2_name == f"{system_name}-worker_2"
            assert "warnings" in data2

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-worker")
            cleanup_test_agent(api_client, f"{system_name}-worker_2")

    def test_deploy_updates_trinity_prompt(self, api_client: TrinityApiClient):
        """Deploy with prompt field updates trinity_prompt setting."""
        system_name = f"test-prompt-{uuid.uuid4().hex[:6]}"
        custom_prompt = f"Test custom prompt {uuid.uuid4().hex[:8]}"
        manifest = f"""
name: {system_name}
prompt: {custom_prompt}
agents:
  worker:
    template: local:default
"""

        try:
            response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )

            assert_status(response, 200)
            data = assert_json_response(response)
            assert data["prompt_updated"] is True

            # Verify prompt was updated
            prompt_response = api_client.get("/api/settings/trinity_prompt")
            if prompt_response.status_code == 200:
                prompt_data = prompt_response.json()
                assert custom_prompt in prompt_data.get("value", "")

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-worker")


# =============================================================================
# PHASE 2 TESTS - PERMISSIONS, FOLDERS, SCHEDULES
# =============================================================================

class TestSystemPermissions:
    """REQ-SYSTEM-001.3: Permission configuration tests."""

    def test_full_mesh_permissions(self, api_client: TrinityApiClient):
        """Full-mesh preset grants each agent permission to call all others."""
        system_name = f"test-mesh-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  agent1:
    template: local:default
  agent2:
    template: local:default
  agent3:
    template: local:default
permissions:
  preset: full-mesh
"""

        try:
            response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )

            assert_status(response, 200)
            data = assert_json_response(response)
            assert data["permissions_configured"] > 0

            # Wait for agents to be ready
            time.sleep(5)

            # Verify each agent has permissions to call others
            for agent_num in [1, 2, 3]:
                agent_name = f"{system_name}-agent{agent_num}"
                perm_response = api_client.get(f"/api/agents/{agent_name}/permissions")

                if perm_response.status_code == 200:
                    perm_data = perm_response.json()
                    permitted = perm_data.get("permitted_agents", [])
                    # Should have 2 other agents (not self)
                    assert len(permitted) == 2

        finally:
            # Cleanup
            for i in [1, 2, 3]:
                cleanup_test_agent(api_client, f"{system_name}-agent{i}")

    def test_orchestrator_workers_permissions(self, api_client: TrinityApiClient):
        """Orchestrator-workers preset: orchestrator can call workers, workers isolated."""
        system_name = f"test-orch-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  orchestrator:
    template: local:default
  worker1:
    template: local:default
  worker2:
    template: local:default
permissions:
  preset: orchestrator-workers
"""

        try:
            response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )

            assert_status(response, 200)
            data = assert_json_response(response)
            assert data["permissions_configured"] > 0

            time.sleep(5)

            # Verify orchestrator has permissions to workers
            orch_response = api_client.get(f"/api/agents/{system_name}-orchestrator/permissions")
            if orch_response.status_code == 200:
                orch_data = orch_response.json()
                permitted = orch_data.get("permitted_agents", [])
                assert len(permitted) == 2  # Both workers

            # Verify workers have no permissions
            worker1_response = api_client.get(f"/api/agents/{system_name}-worker1/permissions")
            if worker1_response.status_code == 200:
                worker_data = worker1_response.json()
                permitted = worker_data.get("permitted_agents", [])
                assert len(permitted) == 0

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-orchestrator")
            cleanup_test_agent(api_client, f"{system_name}-worker1")
            cleanup_test_agent(api_client, f"{system_name}-worker2")

    def test_explicit_permissions(self, api_client: TrinityApiClient):
        """Explicit permissions matrix is correctly applied."""
        system_name = f"test-explicit-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  manager:
    template: local:default
  analyst:
    template: local:default
permissions:
  explicit:
    manager:
      - analyst
    analyst: []
"""

        try:
            response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )

            assert_status(response, 200)
            data = assert_json_response(response)

            time.sleep(5)

            # Verify manager can call analyst
            mgr_response = api_client.get(f"/api/agents/{system_name}-manager/permissions")
            if mgr_response.status_code == 200:
                mgr_data = mgr_response.json()
                permitted = mgr_data.get("permitted_agents", [])
                # permitted_agents is a list of objects, extract names
                permitted_names = [p["name"] if isinstance(p, dict) else p for p in permitted]
                assert f"{system_name}-analyst" in permitted_names

            # Verify analyst has no permissions
            analyst_response = api_client.get(f"/api/agents/{system_name}-analyst/permissions")
            if analyst_response.status_code == 200:
                analyst_data = analyst_response.json()
                permitted = analyst_data.get("permitted_agents", [])
                assert len(permitted) == 0

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-manager")
            cleanup_test_agent(api_client, f"{system_name}-analyst")

    def test_none_permissions_preset(self, api_client: TrinityApiClient):
        """None preset clears all default permissions."""
        system_name = f"test-none-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  agent1:
    template: local:default
  agent2:
    template: local:default
permissions:
  preset: none
"""

        try:
            response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )

            assert_status(response, 200)

            time.sleep(5)

            # Verify both agents have no permissions
            for agent_num in [1, 2]:
                agent_name = f"{system_name}-agent{agent_num}"
                perm_response = api_client.get(f"/api/agents/{agent_name}/permissions")

                if perm_response.status_code == 200:
                    perm_data = perm_response.json()
                    permitted = perm_data.get("permitted_agents", [])
                    assert len(permitted) == 0

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-agent1")
            cleanup_test_agent(api_client, f"{system_name}-agent2")


class TestSystemFolders:
    """REQ-SYSTEM-001.4: Shared folder configuration tests."""

    def test_shared_folders_configuration(self, api_client: TrinityApiClient):
        """Agents with folders config have expose/consume flags set."""
        system_name = f"test-folders-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  producer:
    template: local:default
    folders:
      expose: true
      consume: false
  consumer:
    template: local:default
    folders:
      expose: false
      consume: true
"""

        try:
            response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
                timeout=120.0,  # Longer timeout for folder configuration
            )

            assert_status(response, 200)

            time.sleep(5)

            # Verify producer has expose=true, consume=false
            prod_response = api_client.get(f"/api/agents/{system_name}-producer/folders")
            if prod_response.status_code == 200:
                prod_data = prod_response.json()
                assert prod_data.get("expose_enabled") is True
                assert prod_data.get("consume_enabled") is False

            # Verify consumer has expose=false, consume=true
            cons_response = api_client.get(f"/api/agents/{system_name}-consumer/folders")
            if cons_response.status_code == 200:
                cons_data = cons_response.json()
                assert cons_data.get("expose_enabled") is False
                assert cons_data.get("consume_enabled") is True

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-producer")
            cleanup_test_agent(api_client, f"{system_name}-consumer")


class TestSystemSchedules:
    """REQ-SYSTEM-001.5: Schedule creation tests."""

    def test_schedules_created(self, api_client: TrinityApiClient):
        """Agents with schedules have them created in database."""
        system_name = f"test-sched-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  worker:
    template: local:default
    schedules:
      - name: daily-task
        cron: "0 9 * * *"
        message: "Run daily analysis"
        timezone: "UTC"
      - name: hourly-check
        cron: "0 * * * *"
        message: "Hourly check"
"""

        try:
            response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )

            assert_status(response, 200)
            data = assert_json_response(response)
            assert data["schedules_created"] >= 2

            time.sleep(5)

            # Verify schedules exist
            sched_response = api_client.get(f"/api/agents/{system_name}-worker/schedules")
            if sched_response.status_code == 200:
                sched_data = sched_response.json()
                # API returns list directly, not wrapped in {"schedules": [...]}
                schedules = sched_data if isinstance(sched_data, list) else sched_data.get("schedules", [])
                assert len(schedules) >= 2

                # Check schedule names
                names = [s["name"] for s in schedules]
                assert "daily-task" in names
                assert "hourly-check" in names

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-worker")


class TestSystemAutoStart:
    """REQ-SYSTEM-001.6: Agent auto-start tests."""

    def test_agents_started_after_deployment(self, api_client: TrinityApiClient):
        """All agents are started after deployment completes."""
        system_name = f"test-start-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  agent1:
    template: local:default
  agent2:
    template: local:default
"""

        try:
            response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )

            assert_status(response, 200)

            # Wait for agents to start
            time.sleep(10)

            # Verify both agents are running
            for agent_num in [1, 2]:
                agent_name = f"{system_name}-agent{agent_num}"
                agent_response = api_client.get(f"/api/agents/{agent_name}")

                if agent_response.status_code == 200:
                    agent_data = agent_response.json()
                    status = agent_data.get("status")
                    assert status in ["running", "starting"]

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-agent1")
            cleanup_test_agent(api_client, f"{system_name}-agent2")


# =============================================================================
# PHASE 3 TESTS - BACKEND ENDPOINTS
# =============================================================================

class TestSystemBackendEndpoints:
    """REQ-SYSTEM-001.7: Backend system management endpoint tests."""

    def test_list_systems_endpoint(self, api_client: TrinityApiClient):
        """GET /api/systems returns grouped agents by system prefix."""
        system_name = f"test-list-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  worker1:
    template: local:default
  worker2:
    template: local:default
"""

        try:
            # Deploy system
            deploy_response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )
            assert_status(deploy_response, 200)

            time.sleep(5)

            # List systems
            response = api_client.get("/api/systems")
            assert_status(response, 200)
            data = assert_json_response(response)

            assert "systems" in data
            systems = data["systems"]

            # Find our system
            our_system = None
            for system in systems:
                if system["name"] == system_name:
                    our_system = system
                    break

            assert our_system is not None
            assert our_system["agent_count"] == 2
            assert len(our_system["agents"]) == 2

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-worker1")
            cleanup_test_agent(api_client, f"{system_name}-worker2")

    def test_get_system_endpoint(self, api_client: TrinityApiClient):
        """GET /api/systems/{name} returns detailed system info."""
        system_name = f"test-get-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  worker:
    template: local:default
"""

        try:
            # Deploy system
            deploy_response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )
            assert_status(deploy_response, 200)

            time.sleep(5)

            # Get system details
            response = api_client.get(f"/api/systems/{system_name}")
            assert_status(response, 200)
            data = assert_json_response(response)

            assert data["name"] == system_name
            assert data["agent_count"] == 1
            assert len(data["agents"]) == 1
            assert data["agents"][0]["name"] == f"{system_name}-worker"

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-worker")

    def test_get_nonexistent_system_returns_404(self, api_client: TrinityApiClient):
        """GET /api/systems/{name} returns 404 for nonexistent system."""
        response = api_client.get("/api/systems/nonexistent-system-12345")
        assert_status(response, 404)

    def test_restart_system_endpoint(self, api_client: TrinityApiClient):
        """POST /api/systems/{name}/restart restarts all system agents."""
        system_name = f"test-restart-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  worker:
    template: local:default
"""

        try:
            # Deploy system
            deploy_response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )
            assert_status(deploy_response, 200)

            time.sleep(8)

            # Restart system
            response = api_client.post(f"/api/systems/{system_name}/restart")
            assert_status(response, 200)
            data = assert_json_response(response)

            assert "restarted" in data
            assert f"{system_name}-worker" in data["restarted"]

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-worker")

    def test_export_manifest_endpoint(self, api_client: TrinityApiClient):
        """GET /api/systems/{name}/manifest exports YAML configuration."""
        system_name = f"test-export-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  worker:
    template: local:default
"""

        try:
            # Deploy system
            deploy_response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )
            assert_status(deploy_response, 200)

            time.sleep(5)

            # Export manifest
            response = api_client.get(f"/api/systems/{system_name}/manifest")
            assert_status(response, 200)

            # Parse YAML
            exported_yaml = response.text
            exported_data = yaml.safe_load(exported_yaml)

            assert exported_data["name"] == system_name
            assert "worker" in exported_data["agents"]

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-worker")


# =============================================================================
# INTEGRATION TESTS - COMPLETE WORKFLOWS
# =============================================================================

class TestSystemCompleteWorkflows:
    """REQ-SYSTEM-001.8: End-to-end system deployment tests."""

    @pytest.mark.slow
    def test_complete_system_deployment(self, api_client: TrinityApiClient):
        """Deploy complete system with all features, verify configuration."""
        system_name = f"test-complete-{uuid.uuid4().hex[:6]}"
        custom_prompt = f"Custom instructions {uuid.uuid4().hex[:8]}"

        manifest = f"""
name: {system_name}
description: Complete test system
prompt: {custom_prompt}

agents:
  orchestrator:
    template: local:default
    folders:
      expose: true
      consume: true
    schedules:
      - name: daily-task
        cron: "0 9 * * *"
        message: "Daily work"

  worker:
    template: local:default
    folders:
      expose: true
      consume: true

permissions:
  preset: orchestrator-workers
"""

        try:
            # Deploy system
            response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
                timeout=120.0,  # Longer timeout for complex multi-agent deployment
            )

            assert_status(response, 200)
            data = assert_json_response(response)

            # Verify deployment summary
            assert data["status"] == "deployed"
            assert data["system_name"] == system_name
            assert len(data["agents_created"]) == 2
            assert data["prompt_updated"] is True
            assert data["permissions_configured"] > 0
            assert data["schedules_created"] >= 1

            # Wait for full initialization
            time.sleep(12)

            # Verify trinity_prompt updated
            prompt_response = api_client.get("/api/settings/trinity_prompt")
            if prompt_response.status_code == 200:
                prompt_data = prompt_response.json()
                assert custom_prompt in prompt_data.get("value", "")

            # Verify agents exist and are running
            for agent_short in ["orchestrator", "worker"]:
                agent_name = f"{system_name}-{agent_short}"
                agent_response = api_client.get(f"/api/agents/{agent_name}")
                assert_status(agent_response, 200)
                agent_data = agent_response.json()
                assert agent_data["status"] in ["running", "starting"]

            # Verify permissions
            orch_response = api_client.get(f"/api/agents/{system_name}-orchestrator/permissions")
            if orch_response.status_code == 200:
                orch_data = orch_response.json()
                permitted = orch_data.get("permitted_agents", [])
                # permitted_agents is a list of objects, extract names
                permitted_names = [p["name"] if isinstance(p, dict) else p for p in permitted]
                assert f"{system_name}-worker" in permitted_names

            # Verify folders
            orch_folders = api_client.get(f"/api/agents/{system_name}-orchestrator/folders")
            if orch_folders.status_code == 200:
                folder_data = orch_folders.json()
                assert folder_data.get("expose_enabled") is True
                assert folder_data.get("consume_enabled") is True

            # Verify schedules
            orch_schedules = api_client.get(f"/api/agents/{system_name}-orchestrator/schedules")
            if orch_schedules.status_code == 200:
                sched_data = orch_schedules.json()
                # API returns list directly, not wrapped in {"schedules": [...]}
                schedules = sched_data if isinstance(sched_data, list) else sched_data.get("schedules", [])
                assert len(schedules) >= 1

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-orchestrator")
            cleanup_test_agent(api_client, f"{system_name}-worker")

    @pytest.mark.slow
    def test_export_and_redeploy(self, api_client: TrinityApiClient):
        """Export manifest from deployed system, redeploy it."""
        system_name = f"test-redeploy-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  worker:
    template: local:default
"""

        try:
            # Initial deployment
            deploy1 = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )
            assert_status(deploy1, 200)

            time.sleep(8)

            # Export manifest
            export_response = api_client.get(f"/api/systems/{system_name}/manifest")
            assert_status(export_response, 200)
            exported_yaml = export_response.text

            # Redeploy from exported manifest
            deploy2 = api_client.post(
                "/api/systems/deploy",
                json={"manifest": exported_yaml, "dry_run": False},
            )
            assert_status(deploy2, 200)
            data = deploy2.json()

            # Should create with _2 suffix
            assert f"{system_name}-worker_2" in data["agents_created"]

        finally:
            # Cleanup
            cleanup_test_agent(api_client, f"{system_name}-worker")
            cleanup_test_agent(api_client, f"{system_name}-worker_2")


# =============================================================================
# EDGE CASES AND ERROR HANDLING
# =============================================================================

class TestSystemEdgeCases:
    """REQ-SYSTEM-001.9: Edge cases and error handling."""

    def test_deploy_requires_authentication(self, unauthenticated_client: TrinityApiClient):
        """POST /api/systems/deploy requires authentication."""
        response = unauthenticated_client.post(
            "/api/systems/deploy",
            json={"manifest": MINIMAL_MANIFEST, "dry_run": True},
            auth=False
        )
        assert_status(response, 401)

    def test_deploy_empty_manifest(self, api_client: TrinityApiClient):
        """Deploy with empty manifest returns 400."""
        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": "", "dry_run": True},
        )
        assert_status(response, 400)

    def test_deploy_with_unknown_agent_in_permissions(self, api_client: TrinityApiClient):
        """Deploy with unknown agent in explicit permissions returns 400."""
        manifest = """
name: test-unknown
agents:
  worker:
    template: local:default
permissions:
  explicit:
    nonexistent:
      - worker
"""

        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": True},
        )
        assert_status(response, 400)

    def test_list_systems_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/systems requires authentication."""
        response = unauthenticated_client.get("/api/systems", auth=False)
        assert_status(response, 401)

    def test_restart_nonexistent_system(self, api_client: TrinityApiClient):
        """POST /api/systems/{name}/restart returns 404 for nonexistent system."""
        response = api_client.post("/api/systems/nonexistent-12345/restart")
        assert_status(response, 404)


# =============================================================================
# RESILIENT DEPLOY TESTS (trinity-enterprise#125)
# =============================================================================

class TestResilientDeploy:
    """trinity-enterprise#125: best-effort deploy — continue-on-error + report.

    Failure vector: `resources: {cpu: "3"}` is rejected by normalize_cpu with a
    deterministic, side-effect-free HTTP 400 before any container work — an
    offline-safe way to make exactly one agent fail.

    (#1793/#1759 note: an absent `local:` template now ALSO fails, with a 404
    `UNKNOWN_LOCAL_TEMPLATE` raised even earlier — at template resolution,
    before normalize_cpu runs. It used to silently create a blank agent, which
    is why this suite picked the cpu vector. The cpu vector is kept: it is the
    only one that exercises a failure *after* the template resolved, and
    swapping it would change what this test covers. This is also why
    `config/agent-templates/default/` has to ship: every manifest here names
    `local:default`, so without a real template behind that id the 404 preempts
    the cpu 400 and this whole class fails for the wrong reason.)
    """

    def test_partial_deploy_continues_and_reports(self, api_client: TrinityApiClient):
        """One bad agent doesn't abort the deploy; the rest are created."""
        system_name = f"test-part-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  good:
    template: local:default
  bad:
    template: local:default
    resources:
      cpu: "3"
"""
        good_name = f"{system_name}-good"
        try:
            response = api_client.post(
                "/api/systems/deploy",
                json={"manifest": manifest, "dry_run": False},
            )

            assert_status(response, 200)
            data = assert_json_response(response)

            assert data["status"] == "partial"
            assert data["agents_created"] == [good_name]
            assert len(data["failed"]) == 1
            failure = data["failed"][0]
            assert failure["name"] == f"{system_name}-bad"
            assert failure["short_name"] == "bad"
            assert failure["status_code"] == 400
            assert "cpu" in failure["reason"].lower()
            # Partial responses warn about the redeploy-duplicate trap.
            assert any("duplicates" in w for w in data["warnings"])

            # The good agent really exists.
            time.sleep(3)
            agent_response = api_client.get(f"/api/agents/{good_name}")
            assert_status(agent_response, 200)
        finally:
            cleanup_test_agent(api_client, good_name)

    def test_total_failure_returns_500_with_report(self, api_client: TrinityApiClient):
        """Zero agents created: HTTP 500 with the full report as the body."""
        system_name = f"test-fail-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  only:
    template: local:default
    resources:
      cpu: "3"
"""
        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": False},
        )

        assert_status(response, 500)
        data = assert_json_response(response)
        assert data["status"] == "failed"
        assert data["agents_created"] == []
        assert len(data["failed"]) == 1
        assert data["failed"][0]["short_name"] == "only"

    def test_strict_deploy_aborts_with_original_status(self, api_client: TrinityApiClient):
        """strict=True restores abort-on-first-error, preserving the 400."""
        system_name = f"test-strict-{uuid.uuid4().hex[:6]}"
        manifest = f"""
name: {system_name}
agents:
  bad:
    template: local:default
    resources:
      cpu: "3"
"""
        response = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": False, "strict": True},
        )

        assert_status(response, 400)
        detail = response.json()["detail"]
        assert detail["error"] == "Deployment failed"
        assert detail["failed_at"] == f"{system_name}-bad"
        assert detail["created"] == []
