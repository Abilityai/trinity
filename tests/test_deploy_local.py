"""
Local Agent Deployment Tests (test_deploy_local.py)

Tests for Trinity local agent deployment endpoint.
Covers REQ-DEPLOY-LOCAL-001 (Local Agent Deployment via MCP).

Feature Flow: local-agent-deploy.md

These tests verify the deploy-local API endpoint functionality.
Note: This is the backend endpoint used by the MCP deploy_local_agent tool.
"""

import pytest
import base64
import tarfile
import io
import uuid
import time
import tempfile
import os

from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)
from utils.cleanup import cleanup_test_agent


def create_test_archive(template_yaml_content: str, files: dict = None) -> str:
    """Create a base64-encoded tar.gz archive with template.yaml and optional files.

    Args:
        template_yaml_content: Content for template.yaml
        files: Optional dict of {filename: content} for additional files

    Returns:
        Base64-encoded tar.gz archive string
    """
    buffer = io.BytesIO()

    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        # Add template.yaml
        template_data = template_yaml_content.encode('utf-8')
        tarinfo = tarfile.TarInfo(name='template.yaml')
        tarinfo.size = len(template_data)
        tar.addfile(tarinfo, io.BytesIO(template_data))

        # Add additional files
        if files:
            for filename, content in files.items():
                file_data = content.encode('utf-8') if isinstance(content, str) else content
                tarinfo = tarfile.TarInfo(name=filename)
                tarinfo.size = len(file_data)
                tar.addfile(tarinfo, io.BytesIO(file_data))

    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode('utf-8')


class TestDeployLocalAuthentication:
    """Tests for deploy-local authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_deploy_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/agents/deploy-local requires authentication."""
        response = unauthenticated_client.post(
            "/api/agents/deploy-local",
            json={"archive": "dGVzdA=="},  # Base64 of "test"
            auth=False
        )
        assert_status(response, 401)


class TestDeployLocalValidation:
    """Tests for deploy-local request validation."""

    pytestmark = pytest.mark.smoke

    def test_missing_archive_rejected(self, api_client: TrinityApiClient):
        """POST /api/agents/deploy-local rejects missing archive."""
        response = api_client.post(
            "/api/agents/deploy-local",
            json={}
        )
        assert_status(response, 422)

    def test_invalid_base64_rejected(self, api_client: TrinityApiClient):
        """POST /api/agents/deploy-local rejects invalid base64."""
        response = api_client.post(
            "/api/agents/deploy-local",
            json={"archive": "not-valid-base64!!!"}
        )
        assert_status_in(response, [400, 422])

    def test_invalid_archive_format_rejected(self, api_client: TrinityApiClient):
        """POST /api/agents/deploy-local rejects non-tarball data."""
        # Valid base64 but not a tar.gz
        invalid_archive = base64.b64encode(b"not a tarball").decode('utf-8')

        response = api_client.post(
            "/api/agents/deploy-local",
            json={"archive": invalid_archive}
        )
        assert_status(response, 400)
        data = response.json()
        # Handle both string and dict detail responses
        detail = data.get("detail", "")
        detail_str = detail.get("error", "") if isinstance(detail, dict) else str(detail)
        assert data.get("code") == "INVALID_ARCHIVE" or "invalid" in detail_str.lower()

    def test_missing_template_yaml_rejected(self, api_client: TrinityApiClient):
        """POST /api/agents/deploy-local rejects archive without template.yaml."""
        # Create archive with just CLAUDE.md
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
            content = b"# Test Agent"
            tarinfo = tarfile.TarInfo(name='CLAUDE.md')
            tarinfo.size = len(content)
            tar.addfile(tarinfo, io.BytesIO(content))

        buffer.seek(0)
        archive = base64.b64encode(buffer.read()).decode('utf-8')

        response = api_client.post(
            "/api/agents/deploy-local",
            json={"archive": archive}
        )
        assert_status(response, 400)
        data = response.json()
        # Handle both string and dict detail responses
        detail = data.get("detail", "")
        detail_str = detail.get("error", "") if isinstance(detail, dict) else str(detail)
        assert data.get("code") == "NOT_TRINITY_COMPATIBLE" or "template" in detail_str.lower()


class TestDeployLocalTemplateValidation:
    """Tests for template.yaml validation in deploy-local."""

    pytestmark = pytest.mark.smoke

    def test_template_missing_name_rejected(self, api_client: TrinityApiClient):
        """template.yaml without name field is rejected."""
        template_content = """
resources:
  cpu: "2"
  memory: "4g"
"""
        archive = create_test_archive(template_content)

        response = api_client.post(
            "/api/agents/deploy-local",
            json={"archive": archive}
        )
        # Should fail validation
        assert_status(response, 400)

    def test_template_missing_resources_rejected(self, api_client: TrinityApiClient):
        """template.yaml without resources field is rejected."""
        template_content = """
name: test-agent
display_name: Test Agent
"""
        archive = create_test_archive(template_content)

        response = api_client.post(
            "/api/agents/deploy-local",
            json={"archive": archive}
        )
        # Should fail validation
        assert_status(response, 400)


class TestDeployLocalSuccessful:
    """Tests for successful local deployment."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    @pytest.mark.timeout(120)
    def test_deploy_valid_archive(self, api_client: TrinityApiClient, request):
        """Successfully deploy a valid local agent."""
        agent_name = f"test-deploy-{uuid.uuid4().hex[:6]}"
        template_content = f"""
name: {agent_name}
display_name: Test Deploy Agent
resources:
  cpu: "1"
  memory: "2g"
"""
        archive = create_test_archive(
            template_content,
            files={"CLAUDE.md": "# Test Agent\nThis is a test deployment."}
        )

        try:
            response = api_client.post(
                "/api/agents/deploy-local",
                json={"archive": archive}
            )

            assert_status(response, 200)
            data = response.json()
            assert data.get("status") == "success"
            assert "agent" in data
            assert data["agent"]["name"] == agent_name

            # Wait for agent to start
            time.sleep(5)

            # Verify agent exists
            check = api_client.get(f"/api/agents/{agent_name}")
            assert_status(check, 200)

        finally:
            cleanup_test_agent(api_client, agent_name)

    @pytest.mark.slow
    @pytest.mark.requires_agent
    @pytest.mark.timeout(120)
    def test_deploy_with_name_override(self, api_client: TrinityApiClient, request):
        """Deploy with name override in request."""
        override_name = f"test-override-{uuid.uuid4().hex[:6]}"
        template_content = """
name: original-name
display_name: Test Agent
resources:
  cpu: "1"
  memory: "2g"
"""
        archive = create_test_archive(template_content)

        try:
            response = api_client.post(
                "/api/agents/deploy-local",
                json={
                    "archive": archive,
                    "name": override_name
                }
            )

            assert_status(response, 200)
            data = response.json()
            assert data.get("status") == "success"
            # Should use override name, not template name
            assert data["agent"]["name"] == override_name

            time.sleep(5)

            # Verify agent exists with override name
            check = api_client.get(f"/api/agents/{override_name}")
            assert_status(check, 200)

        finally:
            cleanup_test_agent(api_client, override_name)

    @pytest.mark.slow
    @pytest.mark.requires_agent
    @pytest.mark.timeout(120)
    def test_deploy_with_credentials(self, api_client: TrinityApiClient, request):
        """Deploy with credentials included."""
        agent_name = f"test-creds-{uuid.uuid4().hex[:6]}"
        template_content = f"""
name: {agent_name}
display_name: Test Agent with Creds
resources:
  cpu: "1"
  memory: "2g"
"""
        archive = create_test_archive(template_content)

        try:
            response = api_client.post(
                "/api/agents/deploy-local",
                json={
                    "archive": archive,
                    "credentials": {
                        "TEST_API_KEY": "test-value-123",
                        "ANOTHER_SECRET": "secret-value"
                    }
                }
            )

            assert_status(response, 200)
            data = response.json()
            assert data.get("status") == "success"
            # Should have credential import info
            assert "credentials_imported" in data
            assert "credentials_injected" in data

        finally:
            cleanup_test_agent(api_client, agent_name)


class TestDeployLocalVersioning:
    """Tests for local deployment versioning."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    @pytest.mark.timeout(180)
    def test_redeploy_creates_version(self, api_client: TrinityApiClient, request):
        """Redeploying same agent creates versioned name."""
        base_name = f"test-version-{uuid.uuid4().hex[:6]}"
        template_content = f"""
name: {base_name}
display_name: Versioned Agent
resources:
  cpu: "1"
  memory: "2g"
"""
        archive = create_test_archive(template_content)
        created_agents = []

        try:
            # First deployment
            response1 = api_client.post(
                "/api/agents/deploy-local",
                json={"archive": archive}
            )
            assert_status(response1, 200)
            data1 = response1.json()
            created_agents.append(data1["agent"]["name"])

            time.sleep(5)

            # Second deployment should create versioned name
            response2 = api_client.post(
                "/api/agents/deploy-local",
                json={"archive": archive}
            )
            assert_status(response2, 200)
            data2 = response2.json()
            created_agents.append(data2["agent"]["name"])

            # Second agent should have version suffix
            assert data2["agent"]["name"] != data1["agent"]["name"]
            assert base_name in data2["agent"]["name"]

            # Versioning info should be present
            if "versioning" in data2:
                # Check that versioning has correct structure
                assert "new_version" in data2["versioning"]
                assert "base_name" in data2["versioning"]
                # New version should have the base name as prefix
                assert base_name in data2["versioning"]["new_version"]

        finally:
            for name in created_agents:
                cleanup_test_agent(api_client, name)


class TestDeployLocalLimits:
    """Tests for deployment limits."""

    pytestmark = pytest.mark.smoke

    def test_too_many_credentials_rejected(self, api_client: TrinityApiClient):
        """Deployment with too many credentials is rejected."""
        agent_name = f"test-limits-{uuid.uuid4().hex[:6]}"
        template_content = f"""
name: {agent_name}
resources:
  cpu: "1"
  memory: "2g"
"""
        archive = create_test_archive(template_content)

        # Create 101 credentials (limit is 100)
        credentials = {f"CRED_{i}": f"value_{i}" for i in range(101)}

        response = api_client.post(
            "/api/agents/deploy-local",
            json={
                "archive": archive,
                "credentials": credentials
            }
        )

        assert_status(response, 400)
        data = response.json()
        # Handle both string and dict detail responses
        detail = data.get("detail", "")
        detail_str = detail.get("error", "") if isinstance(detail, dict) else str(detail)
        assert data.get("code") == "TOO_MANY_CREDENTIALS" or "credential" in detail_str.lower()


class TestDeployLocalSecurity:
    """Tests for deployment security."""

    pytestmark = pytest.mark.smoke

    def test_path_traversal_rejected(self, api_client: TrinityApiClient):
        """Archives with path traversal attempts are rejected."""
        # Create archive with path traversal
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
            # Add template.yaml
            template = b"name: test\nresources:\n  cpu: '1'\n  memory: '2g'"
            tarinfo = tarfile.TarInfo(name='template.yaml')
            tarinfo.size = len(template)
            tar.addfile(tarinfo, io.BytesIO(template))

            # Add file with path traversal
            malicious = b"malicious content"
            tarinfo = tarfile.TarInfo(name='../../../etc/passwd')
            tarinfo.size = len(malicious)
            tar.addfile(tarinfo, io.BytesIO(malicious))

        buffer.seek(0)
        archive = base64.b64encode(buffer.read()).decode('utf-8')

        response = api_client.post(
            "/api/agents/deploy-local",
            json={"archive": archive}
        )

        assert_status(response, 400)
        data = response.json()
        # Handle both string and dict detail responses
        detail = data.get("detail", "")
        detail_str = detail.get("error", "") if isinstance(detail, dict) else str(detail)
        assert data.get("code") == "INVALID_ARCHIVE" or "traversal" in detail_str.lower() \
            or "invalid" in detail_str.lower()


class TestDeployLocalResponse:
    """Tests for deploy-local response structure."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    @pytest.mark.timeout(120)
    def test_response_structure(self, api_client: TrinityApiClient, request):
        """Successful deployment returns expected response structure."""
        agent_name = f"test-resp-{uuid.uuid4().hex[:6]}"
        template_content = f"""
name: {agent_name}
display_name: Response Test Agent
resources:
  cpu: "1"
  memory: "2g"
"""
        archive = create_test_archive(template_content)

        try:
            response = api_client.post(
                "/api/agents/deploy-local",
                json={
                    "archive": archive,
                    "credentials": {"TEST_KEY": "test_value"}
                }
            )

            assert_status(response, 200)
            data = response.json()

            # Required fields
            assert_has_fields(data, ["status", "agent", "credentials_imported", "credentials_injected"])

            # Status should be success
            assert data["status"] == "success"

            # Agent info
            assert_has_fields(data["agent"], ["name", "status"])

            # Credentials info
            assert isinstance(data["credentials_imported"], dict)
            assert isinstance(data["credentials_injected"], int)

        finally:
            cleanup_test_agent(api_client, agent_name)
