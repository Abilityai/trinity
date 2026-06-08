"""
Agent Service Files - File browser operations.

Handles file listing, download, preview, and delete for agent workspaces.
"""
import fnmatch
import logging
import posixpath

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from models import User
from database import db
from services.docker_service import get_agent_container
from services.docker_utils import container_reload
from .helpers import agent_http_request

logger = logging.getLogger(__name__)


# AISEC-C2 / #590 — backend-side deny list for PUT /api/agents/{name}/files.
# Mirrors the `path_deny` list in docker/base-image/hooks/guardrails-baseline.json
# so an authenticated owner cannot bypass the agent-server's EDIT_PROTECTED_PATHS
# check by exploiting any future router/proxy gap. Defense in depth — the
# agent-server still re-validates server-side. KEEP IN SYNC with both:
#   - docker/base-image/hooks/guardrails-baseline.json::path_deny
#   - docker/base-image/agent_server/routers/files.py::EDIT_PROTECTED_PATHS
_FILE_WRITE_DENY_PATTERNS = (
    ".env",
    ".env.*",
    ".mcp.json",
    ".mcp.json.template",
    ".credentials.enc",
    ".ssh/*",
    ".aws/*",
    ".gcp/*",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".trinity/*",
    ".git/*",
    ".gitignore",
    "/opt/trinity/*",
    "/etc/claude-code/*",
    "/etc/*",
    "/proc/*",
    "/sys/*",
)


def _normalize_user_path(raw: str) -> str:
    """Reduce a user-supplied path to a stable absolute form for deny matching.

    Resolves `..`/`.` segments lexically (no FS access — mirrors the agent-server
    Path.resolve() guard). Non-absolute paths are anchored at /home/developer to
    match how the agent-server interprets them (see agent_server/routers/files.py).
    """
    if not raw:
        return ""
    # posixpath.normpath collapses `..` and `.` lexically; fine for matching.
    if raw.startswith("/"):
        return posixpath.normpath(raw)
    return posixpath.normpath(posixpath.join("/home/developer", raw))


def _is_user_writable_path(path: str) -> bool:
    """Reject writes to credential / runtime-config / Trinity-managed paths.

    Match strategy (mirrors docker/base-image/hooks/file-guardrail.py):
    - basename match against any pattern (handles `.env`, `.mcp.json` etc.)
    - full-path glob match (handles `.ssh/*`, `/opt/trinity/*` etc.)
    - relative-form glob match against /home/developer-relative path
    """
    normalized = _normalize_user_path(path)
    if not normalized:
        return False
    basename = posixpath.basename(normalized)
    rel_to_home = ""
    if normalized.startswith("/home/developer/"):
        rel_to_home = normalized[len("/home/developer/"):]
    for pattern in _FILE_WRITE_DENY_PATTERNS:
        if fnmatch.fnmatch(basename, pattern):
            return False
        if fnmatch.fnmatch(normalized, pattern):
            return False
        if rel_to_home and fnmatch.fnmatch(rel_to_home, pattern):
            return False
    return True


async def list_agent_files_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request,
    show_hidden: bool = False
) -> dict:
    """
    List files in the agent's workspace directory.
    Returns a flat list of files with metadata (name, size, modified date).

    Args:
        agent_name: Name of the agent
        path: Directory path to list
        current_user: Current authenticated user
        request: HTTP request object
        show_hidden: If True, include hidden files (starting with .)
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to browse files")

    try:
        # Call agent's internal file listing API with retry
        response = await agent_http_request(
            agent_name,
            "GET",
            "/api/files",
            params={"path": path, "show_hidden": str(show_hidden).lower()},
            max_retries=3,
            retry_delay=1.0,
            timeout=30.0
        )
        if response.status_code == 200:
            return response.json()
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to list files: {response.text}"
            )
    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File listing timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list files: {str(e)}")


async def download_agent_file_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request
) -> PlainTextResponse:
    """
    Download a file from the agent's workspace.
    Returns the file content as plain text.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to download files")

    try:
        # Call agent's internal file download API with retry
        response = await agent_http_request(
            agent_name,
            "GET",
            "/api/files/download",
            params={"path": path},
            max_retries=3,
            retry_delay=1.0,
            timeout=60.0
        )
        if response.status_code == 200:
            return PlainTextResponse(content=response.text)
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to download file: {response.text}"
            )
    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File download timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to download file: {str(e)}")


async def delete_agent_file_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request
) -> dict:
    """
    Delete a file or directory from the agent's workspace.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to delete files")

    try:
        # Call agent's internal file delete API with retry
        response = await agent_http_request(
            agent_name,
            "DELETE",
            "/api/files",
            params={"path": path},
            max_retries=3,
            retry_delay=1.0,
            timeout=30.0
        )
        if response.status_code == 200:
            result = response.json()
            return result
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=response.json().get("detail", f"Failed to delete: {response.text}")
            )
    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File deletion timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")


async def preview_agent_file_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request
) -> StreamingResponse:
    """
    Get file with proper MIME type for preview.
    Streams the response from the agent container.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to preview files")

    try:
        # Call agent's internal file preview API with retry
        response = await agent_http_request(
            agent_name,
            "GET",
            "/api/files/preview",
            params={"path": path},
            max_retries=3,
            retry_delay=1.0,
            timeout=30.0
        )
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=response.json().get("detail", f"Failed to preview: {response.text}")
            )

        content_type = response.headers.get("content-type", "application/octet-stream")
        content_disposition = response.headers.get("content-disposition")

        # For small files, return directly
        return StreamingResponse(
            iter([response.content]),
            media_type=content_type,
            headers={"Content-Disposition": content_disposition} if content_disposition else {}
        )

    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File preview timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to preview file: {str(e)}")


async def update_agent_file_logic(
    agent_name: str,
    path: str,
    content: str,
    current_user: User,
    request: Request
) -> dict:
    """
    Update a file's content in the agent's workspace.

    Args:
        agent_name: Name of the agent
        path: File path to update
        content: New file content
        current_user: Current authenticated user
        request: HTTP request object
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # AISEC-C2 / #590: backend-side deny check before proxying to the agent.
    # Stops the .mcp.json RCE escalation at the platform boundary; the
    # agent-server still re-validates as defense in depth.
    if not _is_user_writable_path(path):
        logger.warning(
            "File write blocked at backend deny-list: agent=%s path=%s user=%s",
            agent_name, path, current_user.username,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Cannot edit protected path: {path}"
        )

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to update files")

    try:
        # Call agent's internal file update API with retry
        response = await agent_http_request(
            agent_name,
            "PUT",
            "/api/files",
            params={"path": path},
            json={"content": content},
            max_retries=3,
            retry_delay=1.0,
            timeout=60.0
        )
        if response.status_code == 200:
            result = response.json()
            return result
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=response.json().get("detail", f"Failed to update: {response.text}")
            )
    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File update timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update file: {str(e)}")


async def create_agent_folder_logic(
    agent_name: str,
    path: str,
    current_user: User,
    request: Request
) -> dict:
    """
    Create a new directory in the agent's workspace.

    Args:
        agent_name: Name of the agent
        path: Directory path to create
        current_user: Current authenticated user
        request: HTTP request object
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # AISEC-C2 / #590: backend-side deny check before proxying to the agent.
    # Same deny-list used for file writes — a folder under a credential /
    # Trinity-managed path is still a write into that path. The agent-server
    # re-validates as defense in depth.
    if not _is_user_writable_path(path):
        logger.warning(
            "Folder create blocked at backend deny-list: agent=%s path=%s user=%s",
            agent_name, path, current_user.username,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Cannot create folder in protected path: {path}"
        )

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to create folders")

    try:
        # Call agent's internal mkdir API with retry
        response = await agent_http_request(
            agent_name,
            "POST",
            "/api/files/mkdir",
            params={"path": path},
            max_retries=3,
            retry_delay=1.0,
            timeout=30.0
        )
        if response.status_code == 200:
            return response.json()
        else:
            raise HTTPException(
                status_code=response.status_code,
                detail=response.json().get("detail", f"Failed to create folder: {response.text}")
            )
    except httpx.ConnectError:
        # Agent server not ready - return 503 so tests can skip
        raise HTTPException(
            status_code=503,
            detail="Agent server not ready. The agent may still be starting up."
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Folder creation timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create folder: {str(e)}")
