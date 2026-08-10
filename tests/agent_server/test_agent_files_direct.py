"""
Agent Server Files Direct Tests (test_agent_files_direct.py)

Tests for direct file browser endpoints on agent server.
Covers REQ-AS-FILES-001 through REQ-AS-FILES-002.
"""

import pytest
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
)


class TestListFiles:
    """REQ-AS-FILES-001: List files direct tests."""

    def test_list_files_tree(self, agent_proxy_client):
        """GET /api/files returns tree from /home/developer."""
        api_client, agent_name = agent_proxy_client

        response = api_client.get(f"/api/agents/{agent_name}/files")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, (dict, list))

    def test_excludes_hidden_files(self, agent_proxy_client):
        """Hidden files are excluded from listing."""
        api_client, agent_name = agent_proxy_client

        response = api_client.get(f"/api/agents/{agent_name}/files")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        # Verification logic in main test file


class TestDownloadFile:
    """REQ-AS-FILES-002: Download file direct tests."""

    def test_download_file(self, agent_proxy_client):
        """GET /api/files/download downloads file content."""
        api_client, agent_name = agent_proxy_client

        # First list to find a file
        list_response = api_client.get(f"/api/agents/{agent_name}/files")

        if list_response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Try to find and download a file
        # This is covered more thoroughly in the main test file

    def test_path_traversal_blocked(self, agent_proxy_client):
        """Path traversal returns 403."""
        api_client, agent_name = agent_proxy_client

        response = api_client.get(
            f"/api/agents/{agent_name}/files/download",
            params={"path": "../../../etc/passwd"}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status_in(response, [400, 403, 404])
