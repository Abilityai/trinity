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
from dependencies import agent_may_reach
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
    # It also keeps exactly two leading slashes (POSIX leaves `//` implementation-
    # defined), while Linux and the agent server's resolve() read them as one —
    # left alone, `//home/developer/.ssh/x` matched no path pattern and no fence
    # (trinity-enterprise#792).
    if raw.startswith("/"):
        return posixpath.normpath("/" + raw.lstrip("/"))
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


def _deny_anchor(pattern: str) -> str:
    """The fixed directory (or file) a path-anchored deny pattern lives under:
    its literal prefix, cut back to the last `/` before the first glob char.
    Relative patterns are anchored at /home/developer, as the matcher reads them."""
    glob_at = min((i for i, c in enumerate(pattern) if c in "*?["), default=None)
    head = pattern if glob_at is None else pattern[:pattern.rfind("/", 0, glob_at) + 1]
    if not head.startswith("/"):
        head = posixpath.join("/home/developer", head)
    return head.rstrip("/") or "/"


# trinity-enterprise#792: DELETE removes everything under a directory, so
# deleting `.ssh` (or `.claude`, or the home dir) is the same act as deleting
# `.ssh/authorized_keys`. The `/proc` anchor is load-bearing here:
# `/proc/self/root/...` and `/proc/self/cwd/...` resolve into the home dir in
# the agent container.
_DENY_ANCHORS = tuple(_deny_anchor(p) for p in _FILE_WRITE_DENY_PATTERNS if "/" in p)


def _is_user_deletable_path(path: str) -> bool:
    """Reject deleting a protected path or any directory that holds one.

    Only the path-anchored patterns can be checked that way. A basename pattern
    (`.env`, `.credentials.enc`) can sit in any directory, so deleting a
    directory that merely contains one is not refused here; the agent server's
    own by-name `PROTECTED_PATHS` block applies on its side. Fails closed: an
    empty path is refused (via the write check), and `/` holds every anchor.
    """
    if not _is_user_writable_path(path):
        return False
    normalized = _normalize_user_path(path)
    head = normalized.rstrip("/")
    return not any(a == normalized or a.startswith(head + "/") for a in _DENY_ANCHORS)


# trinity-enterprise#596: the skills directory is where a library skill lands
# (`skill_service.inject_skills`). Writing it through the generic file routes is
# the same act as assigning a skill — an executable package in the agent's head —
# so it takes the same capability. Without this the skill fence on
# `routers/skills.py` is decorative: an agent key with no grant would write
# `.claude/skills/x/SKILL.md` + `scripts/` into a sibling through `PUT /files`,
# a route that checks only ACCESS. Humans and the system agent pass the
# capability by scope, so the Files tab is unchanged for them.
_SKILLS_DIR = "/home/developer/.claude/skills"


def _touches_skills_dir(path: str, *, include_ancestors: bool = False) -> bool:
    """True for the skills dir or anything under it; with `include_ancestors`,
    also for any directory ABOVE it — deleting `.claude` (or the home dir)
    removes every skill as surely as deleting the skills dir does."""
    normalized = _normalize_user_path(path)
    if not normalized:
        return False
    if normalized == _SKILLS_DIR or normalized.startswith(_SKILLS_DIR + "/"):
        return True
    if include_ancestors:
        return _SKILLS_DIR.startswith(normalized.rstrip("/") + "/")
    return False


def _may_reach_for_path(current_user, agent_name, path, *, include_ancestors=False) -> bool:
    """trinity-enterprise#629 reach, plus the ent#596 exception: `skills.manage`
    IS the grant to change the skills of the owner's other agents, so a holder
    writing a skills path reaches the target without a permission edge (the
    capability gate below still decides the write)."""
    if agent_may_reach(current_user, agent_name):
        return True
    if not _touches_skills_dir(path, include_ancestors=include_ancestors):
        return False
    from dependencies import capability_refusal
    from db.capability_grants import CAPABILITY_SKILLS_MANAGE
    return capability_refusal(current_user, CAPABILITY_SKILLS_MANAGE) is None


async def _require_skill_capability(path, current_user, request, agent_name, *, include_ancestors=False):
    if _touches_skills_dir(path, include_ancestors=include_ancestors):
        from dependencies import enforce_agent_capability
        from db.capability_grants import CAPABILITY_SKILLS_MANAGE
        await enforce_agent_capability(
            request, current_user, CAPABILITY_SKILLS_MANAGE, target=agent_name
        )


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
    if not (db.can_user_access_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name)):
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
    if not (db.can_user_access_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name)):
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
    if not (db.can_user_access_agent(current_user.username, agent_name) and _may_reach_for_path(current_user, agent_name, path, include_ancestors=True)):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#596: a write into the skills dir needs the skill-management capability.
    await _require_skill_capability(path, current_user, request, agent_name, include_ancestors=True)

    # trinity-enterprise#792: the write deny list applies to DELETE too, and to
    # any directory that holds a protected path.
    if not _is_user_deletable_path(path):
        logger.warning(
            "File delete blocked at backend deny-list: agent=%s path=%s user=%s",
            agent_name, path, current_user.username,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Cannot delete protected path: {path}"
        )

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
            # ent#792: send the path the checks above approved, not the raw input
            params={"path": _normalize_user_path(path)},
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
    if not (db.can_user_access_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name)):
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
    if not (db.can_user_access_agent(current_user.username, agent_name) and _may_reach_for_path(current_user, agent_name, path)):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#596: a write into the skills dir needs the skill-management capability.
    await _require_skill_capability(path, current_user, request, agent_name)

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
            # ent#792: send the path the checks above approved, not the raw input
            params={"path": _normalize_user_path(path)},
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
    if not (db.can_user_access_agent(current_user.username, agent_name) and _may_reach_for_path(current_user, agent_name, path)):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # ent#596: a write into the skills dir needs the skill-management capability.
    await _require_skill_capability(path, current_user, request, agent_name)

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
            # ent#792: send the path the checks above approved, not the raw input
            params={"path": _normalize_user_path(path)},
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
