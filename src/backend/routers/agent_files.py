# mcp: files.ts (share_file → /shared-files) + pipelines.ts (list_agent_pipelines, get_agent_pipeline_state → /files) + metrics.ts (get_metrics → /metrics, /metrics/definitions — ent#479 ships the tool; ent#477 leaves the two definition routes deliberately unexposed, not forgotten)
"""Agent file management, info, and folder endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Request

from models import User
from database import db
from dependencies import get_current_user, AuthorizedAgentByName, reject_agent_principal, assert_agent_owner
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
                # #2104: the agent-server on older base images still returns a
                # `type` read from template.yaml — strip it here so the retired
                # field never reaches the UI/MCP without forcing image rebuilds.
                data.pop("type", None)
                return data
            else:
                labels = container.labels
                return {
                    "has_template": bool(labels.get("trinity.template")),
                    "agent_name": agent_name,
                    "template_name": labels.get("trinity.template", ""),
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
    # trinity-enterprise#69 Part 2: permission grants are human-only — a
    # parent agent must never delegate/re-grant its control to other agents.
    reject_agent_principal(current_user)
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
    # trinity-enterprise#69 Part 2: permission grants are human-only.
    reject_agent_principal(current_user)
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
    # trinity-enterprise#69 Part 2: permission revokes are human-only.
    reject_agent_principal(current_user)
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


@router.get("/{agent_name}/metrics/definitions")
async def get_agent_metric_definitions(
    agent_name: AuthorizedAgentByName,
    request: Request,
    include_retired: bool = False,
):
    """The agent's DECLARED metric registry (ent#477).

    What `template.yaml metrics:` declares, as the backend reconciled it — not
    what the agent has measured (that is ent#478's `record_metrics` write path
    and ent#479's read). Retired definitions are the ones the template no
    longer declares; they are kept (points recorded under their name still need
    something to interpret them) and served only on request.

    Registered BEFORE nothing it could shadow: `/{agent_name}/metrics` is a
    sibling literal, not a catch-all, so ordering is not load-bearing here
    (Invariant #4 applies to `/{name}`-style parameterized prefixes).
    """
    from services import metric_registry

    definitions = metric_registry.list_metric_definitions(
        agent_name, include_retired=include_retired
    )
    active = [d for d in definitions if d.get("status") == "active"]
    return {
        "agent_name": agent_name,
        # The empty state TEACHES the next action rather than returning a bare
        # list (Product Quality Bar 3): "no rows" and "no block" are different
        # situations and an operator cannot tell them apart from `[]`.
        "declared": bool(active),
        "definitions": definitions,
        "message": (
            None if active
            else "no metrics: block in template.yaml — declare one and pull, "
                 "restart the agent, or POST .../metrics/definitions/refresh"
        ),
        # The contract ent#478 implements, surfaced so a consumer reads the
        # numbers from the platform rather than hard-coding them. Documented in
        # requirements §47; no Settings knob is minted until the sweep that
        # enforces it ships (T2 — a control with no enforcer is a lie).
        "policy": {
            "retention_days": 365,
            "daily_point_cap": 100000,
            "enforced": False,
            "enforced_by": "abilityai/trinity-enterprise#478",
        },
    }


@router.post("/{agent_name}/metrics/definitions/refresh")
async def refresh_agent_metric_definitions(
    agent_name: AuthorizedAgentByName,
    request: Request,
):
    """Re-read the agent's `template.yaml` and reconcile its registry (ent#477).

    The third trigger, beside creation and the git hooks: an agent that edits
    its own `metrics:` block in-container and pushes is invisible to every
    backend git path, so this is how an author (or the agent itself, with its
    own scoped key) makes the registry agree with the file without a restart.

    A **use**, not a grant (Invariant #8): it re-reads the caller's own
    accessible agent's file and can reach no other agent, so `AuthorizedAgent
    ByName` is the right gate — the same principal can already `pull`.

    Running agents only (409). The stopped-agent read path spawns a throwaway
    container, and no request-triggered route may create a container as a side
    effect of a read.
    """
    from services import metric_registry

    try:
        summary = await metric_registry.refresh_from_running_agent(
            agent_name, source="refresh"
        )
    except metric_registry.RefreshUnavailable as e:
        # Named reasons, not a generic 500 (Bar 6). `agent_not_running` is a
        # 409 — the same code `pull` uses for "the agent is not in a state to
        # do this" — and an unreadable template is a 503: the registry is
        # untouched and a retry after fixing the YAML is the remedy.
        status_code = 409 if e.reason == "agent_not_running" else 503
        raise HTTPException(
            status_code=status_code,
            detail=e.message,
            headers={"X-Refresh-Unavailable": e.reason},
        )

    return {"success": True, **summary.to_dict()}


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
    assert_agent_owner(current_user, agent_name, detail="Only the owner or admin can share files from this agent.")

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
    assert_agent_owner(current_user, agent_name, detail="Only the owner or admin can view shared files")

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
    assert_agent_owner(current_user, agent_name, detail="Only the owner can revoke shares")

    row = db.get_agent_shared_file(file_id)
    if row and row["agent_name"] != agent_name:
        # Preventing cross-agent revoke via URL manipulation
        raise HTTPException(status_code=404, detail="File not found for this agent")

    db.revoke_agent_shared_file(file_id)
    return None
