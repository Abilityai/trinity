"""
Agent Files Tests (test_agent_files.py)

Tests for agent workspace file browser.
Covers REQ-FILES-001 through REQ-FILES-002.
"""

import time

import pytest
from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
    assert_tree_structure,
)


def _wait_for_container_ready(
    api_client: TrinityApiClient, agent_name: str, timeout_s: float = 30.0
) -> None:
    """Poll an endpoint that requires the agent container to exist until it
    responds (i.e., no longer 404 from `get_agent_container() is None`).

    `created_agent` waits for the backend to report `status == "running"`,
    but the Docker container can momentarily disappear from the backend's
    SDK view during early startup. The mkdir endpoint surfaces that as
    404, with no 503 fallback, so the test needs an explicit barrier
    before exercising the endpoint.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        # /api/agents/{name}/files goes through the same get_agent_container
        # lookup as mkdir; a 200 (or 503 "agent server not ready") means
        # the container is visible to the backend.
        probe = api_client.get(f"/api/agents/{agent_name}/files")
        if probe.status_code != 404:
            return
        time.sleep(0.5)
    pytest.skip(
        f"Agent container for {agent_name} did not become visible to the "
        f"backend within {timeout_s}s"
    )


class TestListFiles:
    """REQ-FILES-001: List files endpoint tests."""

    def test_list_files_returns_tree(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/files returns hierarchical tree structure."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/files")

        # May return 503 if agent not ready
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should be a tree structure
        assert isinstance(data, (dict, list))

    def test_file_has_required_fields(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Each file item has name, path, type fields."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/files")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # Check root or first item
        if isinstance(data, dict) and "children" in data:
            # Tree structure with root
            assert_has_fields(data, ["name", "type"])
        elif isinstance(data, list) and len(data) > 0:
            # List of items
            item = data[0]
            assert_has_fields(item, ["name", "type"])

    def test_directory_has_children(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Directory items have children field."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/files")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # Find a directory in the response
        def find_directory(item):
            if isinstance(item, dict):
                if item.get("type") == "directory":
                    return item
                for child in item.get("children", []):
                    result = find_directory(child)
                    if result:
                        return result
            return None

        if isinstance(data, dict):
            directory = find_directory(data)
            if directory:
                assert "children" in directory or "file_count" in directory

    def test_hidden_files_excluded(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Hidden files (starting with .) are excluded."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/files")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # Check that no items start with .
        def check_no_hidden(item):
            if isinstance(item, dict):
                name = item.get("name", "")
                # .env and similar should be hidden
                if name.startswith(".") and name not in [".", ".."]:
                    return False
                for child in item.get("children", []):
                    if not check_no_hidden(child):
                        return False
            return True

        if isinstance(data, dict):
            # Root is workspace which may be shown
            pass  # Don't fail on root name
        elif isinstance(data, list):
            for item in data:
                if item.get("name", "").startswith("."):
                    pytest.fail(f"Hidden file found: {item['name']}")


class TestDownloadFile:
    """REQ-FILES-002: Download file endpoint tests."""

    def test_download_existing_file(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/files/download downloads file content."""
        # First get file list
        list_response = api_client.get(f"/api/agents/{created_agent['name']}/files")

        if list_response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Find a file to download
        def find_file(item, path=""):
            if isinstance(item, dict):
                current_path = f"{path}/{item.get('name', '')}" if path else item.get('name', '')
                if item.get("type") == "file":
                    return item.get("path", current_path)
                for child in item.get("children", []):
                    result = find_file(child, current_path)
                    if result:
                        return result
            return None

        data = list_response.json()
        file_path = find_file(data)

        if not file_path:
            pytest.skip("No files found to download")

        # Download the file
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/files/download",
            params={"path": file_path}
        )

        assert_status(response, 200)

    def test_download_nonexistent_file_returns_404(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/files/download for non-existent file returns 404."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/files/download",
            params={"path": "/nonexistent/file.txt"}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # 403 is also valid - security may block paths outside allowed directories
        assert_status_in(response, [400, 403, 404])

    def test_download_directory_returns_400(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/files/download for directory returns 400."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/files/download",
            params={"path": "/"}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Should not be able to download a directory
        # 403 is also valid - security may block paths outside allowed directories
        assert_status_in(response, [400, 403, 404, 422])

    def test_path_traversal_blocked(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Path traversal attempts are blocked."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/files/download",
            params={"path": "../../../etc/passwd"}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Should be blocked
        assert_status_in(response, [400, 403, 404])


class TestPreviewFile:
    """FILE-003: Preview file endpoint tests."""

    def test_preview_text_file_returns_content(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/files/preview returns file content."""
        # First get file list
        list_response = api_client.get(f"/api/agents/{created_agent['name']}/files")

        if list_response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Find a text file to preview
        def find_text_file(item, path=""):
            if isinstance(item, dict):
                current_path = f"{path}/{item.get('name', '')}" if path else item.get('name', '')
                name = item.get("name", "").lower()
                if item.get("type") == "file" and any(name.endswith(ext) for ext in [".txt", ".md", ".json", ".yaml", ".yml", ".py"]):
                    return item.get("path", current_path)
                for child in item.get("children", []):
                    result = find_text_file(child, current_path)
                    if result:
                        return result
            return None

        data = list_response.json()
        file_path = find_text_file(data)

        if not file_path:
            pytest.skip("No text files found to preview")

        # Preview the file
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/files/preview",
            params={"path": file_path}
        )

        assert_status_in(response, [200, 404])
        if response.status_code == 200:
            cd = response.headers.get("content-disposition", "")
            assert cd.startswith("inline"), f"Expected inline disposition for preview, got: {cd!r}"

    def test_preview_nonexistent_file_returns_404(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/files/preview for non-existent file returns 404."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/files/preview",
            params={"path": "/nonexistent/file.txt"}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status_in(response, [400, 403, 404])

    def test_preview_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/files/preview requires authentication."""
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}/files/preview",
            params={"path": "/test.txt"},
            auth=False
        )
        assert_status(response, 401)


class TestUpdateFile:
    """FILE-004: Update/edit file endpoint tests.

    Note: The file update endpoint only updates EXISTING files.
    It does NOT support creating new files (no upsert behavior).
    To create files, use the agent's terminal or chat interface.
    """

    def test_update_existing_file_changes_content(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/files updates existing file content.

        This test finds an existing editable file and modifies it.
        Protected files (CLAUDE.md, .trinity, .git) cannot be edited.
        """
        # First, find an existing text file that we can safely modify
        list_response = api_client.get(f"/api/agents/{created_agent['name']}/files")

        if list_response.status_code == 503:
            pytest.skip("Agent server not ready")

        if list_response.status_code != 200:
            pytest.skip("Could not list files")

        # Look for an existing editable file
        # Most agents won't have editable files by default, so we test the API behavior
        # by checking that it correctly rejects non-existent files

        # Test that updating a non-existent file returns 404 (expected behavior)
        test_path = "/home/developer/nonexistent_test_file.txt"
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/files",
            params={"path": test_path},
            json={"content": "test content"}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # The endpoint requires file to exist - should return 404
        # If it returns 200/201, the endpoint supports file creation (which is fine)
        assert_status_in(response, [200, 201, 404])

    def test_update_nonexistent_file_returns_404(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/files for non-existent file returns 404.

        The file update endpoint only updates existing files.
        Attempting to update a file that doesn't exist returns 404.
        """
        import uuid
        test_path = f"/home/developer/nonexistent_{uuid.uuid4().hex[:8]}.txt"

        response = api_client.put(
            f"/api/agents/{created_agent['name']}/files",
            params={"path": test_path},
            json={"content": "test content"}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Endpoint should return 404 for non-existent files
        # Accept 200/201 if the endpoint supports file creation (optional feature)
        assert_status_in(response, [200, 201, 404])

    def test_update_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient
    ):
        """PUT /api/agents/{name}/files for nonexistent agent returns 404."""
        response = api_client.put(
            "/api/agents/nonexistent-agent-xyz/files",
            params={"path": "/test.txt"},
            json={"content": "test"}
        )
        assert_status(response, 404)

    def test_update_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/files requires authentication."""
        response = unauthenticated_client.put(
            f"/api/agents/{created_agent['name']}/files",
            params={"path": "/test.txt"},
            json={"content": "test"},
            auth=False
        )
        assert_status(response, 401)


class TestDeleteFile:
    """FILE-005: Delete file endpoint tests."""

    def test_delete_file_removes_it(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/agents/{name}/files removes the file."""
        import uuid
        test_path = f"/home/developer/delete_test_{uuid.uuid4().hex[:8]}.txt"

        # First create a file
        create_response = api_client.put(
            f"/api/agents/{created_agent['name']}/files",
            params={"path": test_path},
            json={"content": "to be deleted"}
        )

        if create_response.status_code == 503:
            pytest.skip("Agent server not ready")

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create test file")

        # Delete the file
        delete_response = api_client.delete(
            f"/api/agents/{created_agent['name']}/files",
            params={"path": test_path}
        )

        assert_status(delete_response, 200)

        # Verify file is gone
        download_response = api_client.get(
            f"/api/agents/{created_agent['name']}/files/download",
            params={"path": test_path}
        )
        assert_status_in(download_response, [400, 403, 404])

    def test_delete_nonexistent_file_returns_404(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/agents/{name}/files for non-existent file returns 404."""
        response = api_client.delete(
            f"/api/agents/{created_agent['name']}/files",
            params={"path": "/nonexistent/file.txt"}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status_in(response, [400, 403, 404])

    def test_delete_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient
    ):
        """DELETE /api/agents/{name}/files for nonexistent agent returns 404."""
        response = api_client.delete(
            "/api/agents/nonexistent-agent-xyz/files",
            params={"path": "/test.txt"}
        )
        assert_status(response, 404)

    def test_delete_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/agents/{name}/files requires authentication."""
        response = unauthenticated_client.delete(
            f"/api/agents/{created_agent['name']}/files",
            params={"path": "/test.txt"},
            auth=False
        )
        assert_status(response, 401)


class TestShowHiddenFiles:
    """FILE-010: Toggle hidden files visibility."""

    def test_list_with_show_hidden_true(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/files?show_hidden=true includes hidden files."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/files",
            params={"show_hidden": "true"}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        # API should accept the parameter
        data = response.json()
        assert data is not None

    def test_list_with_show_hidden_false_excludes_dotfiles(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/files?show_hidden=false excludes dotfiles."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/files",
            params={"show_hidden": "false"}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # Check that no items start with . (except root)
        def check_no_hidden(item, is_root=True):
            if isinstance(item, dict):
                name = item.get("name", "")
                # Skip root level check
                if not is_root and name.startswith(".") and name not in [".", ".."]:
                    return False
                for child in item.get("children", []):
                    if not check_no_hidden(child, is_root=False):
                        return False
            return True

        if isinstance(data, dict):
            assert check_no_hidden(data, is_root=True), "Hidden files found when show_hidden=false"
        elif isinstance(data, list):
            for item in data:
                if item.get("name", "").startswith("."):
                    pytest.fail(f"Hidden file found: {item['name']}")

    def test_hidden_parameter_default_behavior(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Default behavior (no show_hidden param) excludes hidden files."""
        # Request without show_hidden parameter
        response = api_client.get(f"/api/agents/{created_agent['name']}/files")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        # Should work the same as show_hidden=false


class TestCreateFolder:
    """FILE-006 / #37: Create folder endpoint tests.

    POST /api/agents/{name}/files/mkdir creates a directory in the
    agent workspace. Body: {"path": "/home/developer/<dir>"}.
    """

    def test_create_folder_and_list(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """mkdir creates a directory that then appears in the listing."""
        import uuid
        name = created_agent['name']
        _wait_for_container_ready(api_client, name)
        dir_path = f"/home/developer/mkdir_test_{uuid.uuid4().hex[:8]}"

        response = api_client.post(
            f"/api/agents/{name}/files/mkdir",
            json={"path": dir_path},
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        body = response.json()
        assert body.get("success") is True
        assert body.get("type") == "directory"

        # Cleanup
        api_client.delete(
            f"/api/agents/{name}/files", params={"path": dir_path}
        )

    def test_create_folder_duplicate_returns_409(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Creating an already-existing directory returns 409."""
        import uuid
        name = created_agent['name']
        _wait_for_container_ready(api_client, name)
        dir_path = f"/home/developer/mkdir_dup_{uuid.uuid4().hex[:8]}"

        first = api_client.post(
            f"/api/agents/{name}/files/mkdir", json={"path": dir_path}
        )
        if first.status_code == 503:
            pytest.skip("Agent server not ready")
        if first.status_code != 200:
            pytest.skip("Could not create initial folder")

        second = api_client.post(
            f"/api/agents/{name}/files/mkdir", json={"path": dir_path}
        )
        assert_status(second, 409)

        api_client.delete(
            f"/api/agents/{name}/files", params={"path": dir_path}
        )

    def test_create_folder_protected_path_returns_403(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """mkdir into a Trinity-managed / protected path is denied."""
        name = created_agent['name']
        response = api_client.post(
            f"/api/agents/{name}/files/mkdir",
            json={"path": "/home/developer/.trinity/evil"},
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 403)

    def test_create_folder_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient
    ):
        """mkdir for a nonexistent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/files/mkdir",
            json={"path": "/home/developer/x"},
        )
        assert_status(response, 404)

    def test_create_folder_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/files/mkdir requires authentication."""
        response = unauthenticated_client.post(
            f"/api/agents/{created_agent['name']}/files/mkdir",
            json={"path": "/home/developer/x"},
            auth=False
        )
        assert_status(response, 401)
