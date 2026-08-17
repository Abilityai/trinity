"""
Credentials Tests (test_credentials.py)

Tests for the CRED-002 simplified credential system.
Credentials are now:
1. Injected directly into agents via /inject
2. Exported to encrypted .credentials.enc files
3. Imported from .credentials.enc files

Old Redis-based credential endpoints have been removed.
"""

import pytest
import uuid
import time
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestCredentialStatus:
    """CRED-002: Credential status endpoint tests."""

    def test_get_credential_status_running_agent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/credentials/status returns file status for running agent."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/credentials/status"
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, dict)
        # Should contain credential file status info
        assert "agent_name" in data or "env_file" in data or "files" in data

    def test_get_credential_status_stopped_agent(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """GET /api/agents/{name}/credentials/status on stopped agent returns appropriate message."""
        response = api_client.get(
            f"/api/agents/{stopped_agent['name']}/credentials/status"
        )

        # Should return 200 with status message about agent not running
        assert_status(response, 200)
        data = assert_json_response(response)
        assert data.get("status") == "agent_not_running" or "not running" in data.get("message", "").lower()

    def test_get_credential_status_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """GET /api/agents/{name}/credentials/status for nonexistent agent returns 404."""
        response = api_client.get(
            "/api/agents/nonexistent-agent-xyz/credentials/status"
        )
        assert_status(response, 404)


class TestCredentialInject:
    """CRED-002: Credential injection endpoint tests."""

    def test_inject_credentials_env_file(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/credentials/inject writes .env file."""
        unique = uuid.uuid4().hex[:8].upper()
        env_content = f"TEST_KEY_{unique}=test_value\nANOTHER_KEY_{unique}=another_value\n"

        response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/inject",
            json={"files": {".env": env_content}}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert data.get("status") == "success"
        assert ".env" in data.get("files_written", [])

    def test_inject_credentials_multiple_files(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/credentials/inject can write multiple files.

        Note: as of #590 (2026-04-30), the user-facing allowlist is
        {.env, .credentials.enc} — .mcp.json and .mcp.json.template were
        removed because raw injection of those files was an RCE-by-config
        vector (AISEC-C2). This test now exercises the still-allowed pair.
        """
        unique = uuid.uuid4().hex[:8].upper()

        response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/inject",
            json={
                "files": {
                    ".env": f"KEY_{unique}=value\n",
                    ".credentials.enc": "encrypted-blob-placeholder",
                }
            }
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert data.get("status") == "success"
        files_written = data.get("files_written", [])
        assert len(files_written) == 2

    def test_inject_credentials_empty_files_fails(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/credentials/inject with empty files returns error."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/inject",
            json={"files": {}}
        )

        assert_status(response, 400)

    def test_inject_credentials_stopped_agent_fails(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """POST /api/agents/{name}/credentials/inject on stopped agent fails."""
        response = api_client.post(
            f"/api/agents/{stopped_agent['name']}/credentials/inject",
            json={"files": {".env": "KEY=value\n"}}
        )

        assert_status(response, 400)

    def test_inject_credentials_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """POST /api/agents/{name}/credentials/inject for nonexistent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/credentials/inject",
            json={"files": {".env": "KEY=value\n"}}
        )
        assert_status(response, 404)


class TestCredentialExport:
    """CRED-002: Credential export endpoint tests."""

    def test_export_credentials_creates_encrypted_file(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/credentials/export creates .credentials.enc."""
        # First inject some credentials to export
        unique = uuid.uuid4().hex[:8].upper()
        inject_response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/inject",
            json={"files": {".env": f"EXPORT_TEST_{unique}=secret_value\n"}}
        )

        if inject_response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(inject_response, 200)

        # Now export
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/export"
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert data.get("status") == "success"
        assert data.get("files_exported", 0) >= 1
        assert ".credentials.enc" in data.get("encrypted_file", "")

    def test_export_credentials_stopped_agent_fails(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """POST /api/agents/{name}/credentials/export on stopped agent fails."""
        response = api_client.post(
            f"/api/agents/{stopped_agent['name']}/credentials/export"
        )

        assert_status(response, 400)

    def test_export_credentials_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """POST /api/agents/{name}/credentials/export for nonexistent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/credentials/export"
        )
        assert_status(response, 404)


class TestCredentialImport:
    """CRED-002: Credential import endpoint tests."""

    def test_import_credentials_restores_files(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/credentials/import restores from .credentials.enc."""
        # First inject and export
        unique = uuid.uuid4().hex[:8].upper()
        inject_response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/inject",
            json={"files": {".env": f"IMPORT_TEST_{unique}=secret_value\n"}}
        )

        if inject_response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(inject_response, 200)

        # Export to create .credentials.enc
        export_response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/export"
        )
        assert_status(export_response, 200)

        # Now import (should work since .credentials.enc exists)
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/import"
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert data.get("status") == "success"
        assert len(data.get("files_imported", [])) >= 1

    def test_import_credentials_no_enc_file_fails(
        self,
        api_client: TrinityApiClient
    ):
        """POST /api/agents/{name}/credentials/import fails when no .credentials.enc exists."""
        # Create a fresh agent without any credentials
        unique = uuid.uuid4().hex[:6]
        agent_name = f"test-no-enc-{unique}"

        create_response = api_client.post(
            "/api/agents",
            json={"name": agent_name}
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip(f"Failed to create test agent: {create_response.text}")

        try:
            # Wait for agent to be ready
            max_wait = 30
            start = time.time()
            while time.time() - start < max_wait:
                check = api_client.get(f"/api/agents/{agent_name}")
                if check.status_code == 200 and check.json().get("status") == "running":
                    time.sleep(2)
                    break
                time.sleep(1)

            # Try to import - should fail since no .credentials.enc exists
            response = api_client.post(
                f"/api/agents/{agent_name}/credentials/import"
            )

            # Should return 400 (no file) or 503 (agent not ready)
            assert_status_in(response, [400, 503])

        finally:
            # Cleanup
            api_client.delete(f"/api/agents/{agent_name}")

    def test_import_credentials_stopped_agent_fails(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """POST /api/agents/{name}/credentials/import on stopped agent fails."""
        response = api_client.post(
            f"/api/agents/{stopped_agent['name']}/credentials/import"
        )

        assert_status(response, 400)

    def test_import_credentials_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """POST /api/agents/{name}/credentials/import for nonexistent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/credentials/import"
        )
        assert_status(response, 404)


class TestEncryptionKey:
    """CRED-002: Encryption key endpoint tests."""

    def test_get_encryption_key(
        self,
        api_client: TrinityApiClient
    ):
        """GET /api/credentials/encryption-key returns platform key."""
        response = api_client.get("/api/credentials/encryption-key")

        # May return 503 if key not configured
        if response.status_code == 503:
            pytest.skip("Credential encryption key not configured")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["key", "algorithm"])
        assert data.get("algorithm") == "AES-256-GCM"
        # Key should be 64 hex characters
        key = data.get("key", "")
        assert len(key) == 64, f"Expected 64 char hex key, got {len(key)}"


class TestCredentialRoundTrip:
    """CRED-002: End-to-end credential workflow tests."""

    @pytest.mark.slow
    def test_inject_export_import_roundtrip(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Full workflow: inject -> export -> (clear) -> import -> verify."""
        unique = uuid.uuid4().hex[:8].upper()
        test_key = f"ROUNDTRIP_TEST_{unique}"
        test_value = f"secret_value_{unique}"
        env_content = f"{test_key}={test_value}\n"

        # Step 1: Inject credentials
        inject_response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/inject",
            json={"files": {".env": env_content}}
        )

        if inject_response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(inject_response, 200)

        # Step 2: Export to encrypted file
        export_response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/export"
        )
        assert_status(export_response, 200)
        export_data = export_response.json()
        assert export_data.get("files_exported", 0) >= 1

        # Step 3: Inject empty .env to clear credentials
        clear_response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/inject",
            json={"files": {".env": "# cleared\n"}}
        )
        assert_status(clear_response, 200)

        # Step 4: Import from encrypted file
        import_response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/import"
        )
        assert_status(import_response, 200)
        import_data = import_response.json()
        assert ".env" in import_data.get("files_imported", [])

        # Step 5: Verify status shows credentials restored
        status_response = api_client.get(
            f"/api/agents/{created_agent['name']}/credentials/status"
        )
        assert_status(status_response, 200)
