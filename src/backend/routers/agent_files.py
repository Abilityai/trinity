"""Agent file management, info, and folder endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Request

from models import User
from database import db
from dependencies import get_current_user, AuthorizedAgentByName
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container
from services.docker_utils import container_reload
from services.agent_service import (
    get_agent_permissions_logic,
    set_agent_permissions_logic,
    add_agent_permission_logic,
    remove_agent_permission_logic,
    get_agent_folders_logic,
    update_agent_folders_logic,
    get_available_shared_folders_logic,
    get_folder_consumers_logic,
    list_agent_files_logic,
    download_agent_file_logic,
    delete_agent_file_logic,
    preview_agent_file_logic,
    update_agent_file_logic,
    create_agent_folder_logic,
    get_agent_metrics_logic,
    get_file_sharing_status_logic,
    set_file_sharing_status_logic,
)
from models import (
    CreateFolderRequest,
    FileUpdateRequest,
    ShareFileMcpRequest,
    ShareFileResponse,
    SharedFileInfo,
    SharedFilesList,
)
from services.agent_shared_files_service import (
    create_share,
    build_download_url,
    MAX_AGENT_QUOTA_BYTES,
)
from services.idempotency_service import EffectInProgressError
from services.platform_audit_service import platform_audit_service, AuditEventType

router = APIRouter(prefix="/api/agents", tags=["agents"])


# ============================================================================
# Info Endpoints
# ============================================================================

@router.get("/{agent_name}/playbooks")
async def get_agent_playbooks_endpoint(
    agent_name: AuthorizedAgentByName,
    request: Request
):
    """
    Get available skills (playbooks) from an agent's .claude/skills/ directory.

    Returns skill metadata parsed from SKILL.md YAML frontmatter.
    """
    import httpx

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)

    if container.status != "running":
        raise HTTPException(
            status_code=503,
            detail="Agent is not running. Start the agent to view playbooks."
        )

    try:
        agent_url = f"http://agent-{agent_name}:8000/api/skills"
        async with agent_httpx_client(agent_name, timeout=10.0) as client:
            response = await client.get(agent_url)
            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Agent returned error: {response.text}"
                )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Agent is starting up, please try again")
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Could not connect to agent")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch playbooks: {str(e)}")


@router.get("/{agent_name}/info")
async def get_agent_info_endpoint(
    agent_name: AuthorizedAgentByName,
    request: Request
):
    """Get template info and metadata for an agent."""
    import httpx

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)

    if container.status != "running":
        labels = container.labels
        return {
            "has_template": bool(labels.get("trinity.template")),
            "agent_name": agent_name,
            "template_name": labels.get("trinity.template", ""),
            "type": labels.get("trinity.agent-type", ""),
            "resources": {
                "cpu": labels.get("trinity.cpu", ""),
                "memory": labels.get("trinity.memory", "")
            },
            "status": "stopped",
            "message": "Agent is stopped. Start the agent to see full template info."
        }

    try:
        agent_url = f"http://agent-{agent_name}:8000/api/template/info"
        async with agent_httpx_client(agent_name, timeout=10.0) as client:
            response = await client.get(agent_url)
            if response.status_code == 200:
                data = response.json()
                data["status"] = "running"
                return data
            else:
                labels = container.labels
                return {
                    "has_template": bool(labels.get("trinity.template")),
                    "agent_name": agent_name,
                    "template_name": labels.get("trinity.template", ""),
                    "type": labels.get("trinity.agent-type", ""),
                    "resources": {
                        "cpu": labels.get("trinity.cpu", ""),
                        "memory": labels.get("trinity.memory", "")
                    },
                    "status": "running",
                    "message": "Template info endpoint not available in this agent version"
                }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Agent is starting up, please try again")
    except Exception as e:
        labels = container.labels
        return {
            "has_template": bool(labels.get("trinity.template")),
            "agent_name": agent_name,
            "template_name": labels.get("trinity.template", ""),
            "type": labels.get("trinity.agent-type", ""),
            "resources": {
                "cpu": labels.get("trinity.cpu", ""),
                "memory": labels.get("trinity.memory", "")
            },
            "status": "running",
            "message": f"Could not fetch template info: {str(e)}"
        }


# ============================================================================
# Files Endpoints
# ============================================================================

@router.get("/{agent_name}/files")
async def list_agent_files_endpoint(
    agent_name: str,
    request: Request,
    path: str = "/home/developer",
    show_hidden: bool = False,
    current_user: User = Depends(get_current_user)
):
    """List files in the agent's workspace directory.

    Args:
        path: Directory path to list (default: /home/developer)
        show_hidden: If True, include hidden files (starting with .)
    """
    return await list_agent_files_logic(agent_name, path, current_user, request, show_hidden)


@router.get("/{agent_name}/files/download")
async def download_agent_file_endpoint(
    agent_name: str,
    request: Request,
    path: str,
    current_user: User = Depends(get_current_user)
):
    """Download a file from the agent's workspace."""
    return await download_agent_file_logic(agent_name, path, current_user, request)


@router.get("/{agent_name}/files/preview")
async def preview_agent_file_endpoint(
    agent_name: str,
    request: Request,
    path: str,
    current_user: User = Depends(get_current_user)
):
    """Get file with proper MIME type for preview (images, video, audio, etc.)."""
    return await preview_agent_file_logic(agent_name, path, current_user, request)


@router.delete("/{agent_name}/files")
async def delete_agent_file_endpoint(
    agent_name: str,
    request: Request,
    path: str,
    current_user: User = Depends(get_current_user)
):
    """Delete a file or directory from the agent's workspace."""
    return await delete_agent_file_logic(agent_name, path, current_user, request)


@router.put("/{agent_name}/files")
async def update_agent_file_endpoint(
    agent_name: str,
    request: Request,
    path: str,
    body: FileUpdateRequest,
    current_user: User = Depends(get_current_user)
):
    """Update a file's content in the agent's workspace.

    Args:
        path: File path to update
        body: Request body with content
    """
    return await update_agent_file_logic(agent_name, path, body.content, current_user, request)


@router.post("/{agent_name}/files/mkdir")
async def create_agent_folder_endpoint(
    agent_name: str,
    request: Request,
    body: CreateFolderRequest,
    current_user: User = Depends(get_current_user)
):
    """Create a new directory in the agent's workspace (#37).

    Args:
        body: Request body with the directory path to create
    """
    return await create_agent_folder_logic(agent_name, body.path, current_user, request)


# ============================================================================
# Agent Permissions Endpoints
# ============================================================================

@router.get("/{agent_name}/permissions")
async def get_agent_permissions(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get permissions for an agent."""
    return await get_agent_permissions_logic(agent_name, current_user)


@router.put("/{agent_name}/permissions")
async def set_agent_permissions(
    agent_name: str,
    request: Request,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """Set permissions for an agent (full replacement)."""
    result = await set_agent_permissions_logic(agent_name, body, current_user, request)
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHORIZATION,
        event_action="permissions_set",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"targets": body.get("permissions") or body.get("targets")},
    )
    return result


@router.post("/{agent_name}/permissions/{target_agent}")
async def add_agent_permission(
    agent_name: str,
    target_agent: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Add permission for an agent to communicate with another agent."""
    result = await add_agent_permission_logic(agent_name, target_agent, current_user, request)
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHORIZATION,
        event_action="permission_grant",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"target_agent": target_agent},
    )
    return result


@router.delete("/{agent_name}/permissions/{target_agent}")
async def remove_agent_permission(
    agent_name: str,
    target_agent: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Remove permission for an agent to communicate with another agent."""
    result = await remove_agent_permission_logic(agent_name, target_agent, current_user, request)
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHORIZATION,
        event_action="permission_revoke",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"target_agent": target_agent},
    )
    return result


# ============================================================================
# Custom Metrics Endpoints
# ============================================================================

@router.get("/{agent_name}/metrics")
async def get_agent_metrics(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get agent custom metrics."""
    return await get_agent_metrics_logic(agent_name, current_user)


# ============================================================================
# Shared Folders Endpoints
# ============================================================================

@router.get("/{agent_name}/folders")
async def get_agent_folders(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get shared folder configuration for an agent."""
    return await get_agent_folders_logic(agent_name, current_user)


@router.put("/{agent_name}/folders")
async def update_agent_folders(
    agent_name: str,
    request: Request,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """Update shared folder configuration for an agent."""
    return await update_agent_folders_logic(agent_name, body, current_user, request)


@router.get("/{agent_name}/folders/available")
async def get_available_shared_folders(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get list of shared folders available for this agent to mount."""
    return await get_available_shared_folders_logic(agent_name, current_user)


@router.get("/{agent_name}/folders/consumers")
async def get_folder_consumers(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get list of agents that can consume this agent's shared folder."""
    return await get_folder_consumers_logic(agent_name, current_user)


# ============================================================================
# File Sharing (outbound) Endpoints — FILES-001 Step 2
# ============================================================================


@router.get("/{agent_name}/file-sharing")
async def get_agent_file_sharing(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """
    Get the outbound file-sharing status for an agent.

    Returns:
    - enabled: bool — whether the toggle is on
    - volume_attached: bool — whether /home/developer/public is currently mounted
    - restart_required: bool — true when enabled != volume_attached
    - file_count / total_bytes / quota_bytes — placeholder zeros in Step 2;
      wired to agent_shared_files in Step 3
    """
    return await get_file_sharing_status_logic(agent_name, current_user)


@router.put("/{agent_name}/file-sharing")
async def set_agent_file_sharing(
    agent_name: str,
    body: dict,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """
    Enable or disable outbound file sharing for an agent (owner-only).

    Body:
    - enabled: True/False

    Flipping the flag does NOT mount/unmount immediately — it sets
    restart_required. The next stop/start cycle triggers container
    recreation with the correct volume configuration.
    """
    return await set_file_sharing_status_logic(agent_name, body, current_user)


@router.post(
    "/{agent_name}/shared-files",
    response_model=ShareFileResponse,
    status_code=201,
)
async def share_agent_file(
    agent_name: str,
    body: ShareFileMcpRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Mint a public download URL for a file the agent has written to its
    publish dir (/home/developer/public/). Called by the `share_file`
    MCP tool.

    Auth: owner/admin of the agent, OR agent-scoped MCP key whose
    agent_name matches the path. User-scoped MCP keys of non-owners
    are rejected.
    """
    # Owner gate (the agent's owner always passes)
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(
            status_code=403,
            detail="Only the owner or admin can share files from this agent.",
        )

    # Defense in depth: if this is an agent-scoped key, it must be for
    # the same agent. Prevents Agent A's key from being used to share
    # files from Agent B's volume even when both are owned by the same user.
    actor_agent = getattr(current_user, "agent_name", None)
    if actor_agent and actor_agent != agent_name:
        raise HTTPException(
            status_code=403,
            detail="Agent-scoped MCP key cannot share files for a different agent.",
        )

    try:
        result = await create_share(
            agent_name=agent_name,
            filename=body.filename,
            display_name=body.display_name,
            expires_in=body.expires_in,
            created_by=actor_agent or current_user.username,
            execution_id=body.execution_id,
            dedup_label=body.dedup_label,
        )
    except EffectInProgressError as e:
        # Concurrent duplicate share for the same (execution, file) is mid-flight
        # (#1084). Retryable — never a silent skip-and-succeed.
        raise HTTPException(status_code=409, detail=str(e))
    return ShareFileResponse(**result)


@router.get(
    "/{agent_name}/shared-files",
    response_model=SharedFilesList,
)
async def list_agent_shared_files(
    agent_name: str,
    current_user: User = Depends(get_current_user),
):
    """
    List active (non-revoked, non-expired) shared files for an agent.
    Restricted to owner + admin (C7) — the list includes full download
    URLs with tokens, so anyone who can see the list can effectively
    reuse the shares. That's a capability that belongs with `share_file`
    and `revoke` (both owner-only), not with shared-user read access.
    """
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner or admin can view shared files")

    rows = db.list_active_shared_files_for_agent(agent_name)
    files = [
        SharedFileInfo(
            file_id=row["id"],
            filename=row["filename"],
            size_bytes=row["size_bytes"],
            mime_type=row["mime_type"],
            url=build_download_url(row["id"], row["download_token"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            download_count=row["download_count"] or 0,
            last_downloaded_at=row["last_downloaded_at"],
        )
        for row in rows
    ]
    total_bytes = db.total_shared_file_bytes_for_agent(agent_name)
    return SharedFilesList(
        agent_name=agent_name,
        files=files,
        total_bytes=total_bytes,
        quota_bytes=MAX_AGENT_QUOTA_BYTES,
    )


@router.delete(
    "/{agent_name}/shared-files/{file_id}",
    status_code=204,
)
async def revoke_agent_shared_file(
    agent_name: str,
    file_id: str,
    current_user: User = Depends(get_current_user),
):
    """
    Revoke a shared file. Owner/admin only. Idempotent — revoking a
    revoked or missing file returns 204 either way.
    """
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can revoke shares")

    row = db.get_agent_shared_file(file_id)
    if row and row["agent_name"] != agent_name:
        # Preventing cross-agent revoke via URL manipulation
        raise HTTPException(status_code=404, detail="File not found for this agent")

    db.revoke_agent_shared_file(file_id)
    return None
