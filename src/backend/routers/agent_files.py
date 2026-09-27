# mcp: files.ts (share_file → /shared-files) + pipelines.ts (list_agent_pipelines, get_agent_pipeline_state → /files) + metrics.ts (refresh_metric_definitions → /metrics/definitions/refresh, ent#478; get_metrics → /metrics, /metrics/definitions — ent#479 ships that tool; get_objectives → /objectives, ent#666; the definitions READ stays deliberately unexposed until then, not forgotten)
"""Agent file management, info, and folder endpoints."""
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import DBAPIError, OperationalError

from models import User
from database import db
from dependencies import get_current_user, AuthorizedAgentByName, reject_agent_principal, assert_agent_owner, is_interactive_principal
from services.agent_auth import agent_httpx_client
from services import metric_read_service, objective_join_service, rate_limiter
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
    get_file_sharing_status_logic,
    set_file_sharing_status_logic,
)
from models import (
    CreateFolderRequest,
    FileUpdateRequest,
    ObjectiveJoinRead,
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])

# Per-agent READ rate. Four times the write limit: every consumer polls (the
# tiles, the dashboard bind, N open tabs at a 30 s cadence), so the ceiling has
# to clear normal traffic by a wide margin and still stop a runaway
# `get_metrics` loop from pinning the store with 50-metric reads (TD-6).
METRICS_READ_RATE_LIMIT = int(os.getenv("METRICS_READ_RATE_LIMIT", "240"))
METRICS_READ_RATE_WINDOW = 60  # seconds

# The objective join gets its OWN, much lower ceiling (ent#666). `/metrics` is
# store-only; this read contacts the container — a directory listing plus up to
# a hundred small file reads through the agent door — so the 240/min copied
# from a store read would let ten open cards drive an agent-server the platform
# also needs for chat. 60/min per agent clears ten cards polling at 30 s with
# room to spare.
OBJECTIVES_READ_RATE_LIMIT = int(os.getenv("OBJECTIVES_READ_RATE_LIMIT", "60"))
OBJECTIVES_READ_RATE_WINDOW = 60  # seconds


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
    agent_name: AuthorizedAgentByName,
    request: Request,
    window: str = "auto",
    since: Optional[str] = None,
    until: Optional[str] = None,
    metric: Optional[str] = None,
    include_retired: bool = False,
    series_limit: int = Query(
        metric_read_service.DEFAULT_SERIES_LIMIT,
        ge=1,
        le=metric_read_service.MAX_SERIES_LIMIT,
    ),
    current_user: User = Depends(get_current_user),
):
    """The agent's recorded business metrics, with freshness (ent#479).

    Same URL as the `metrics.json` proxy it replaces, re-backed by the ent#478
    point store: declared definitions (ent#477) joined to the points recorded
    under their names, with ONE stale rule —
    `now - last_point_at > 2 x cadence`, and never stale without a declared
    cadence. `metrics.json` is superseded; an agent still writing one gets the
    D-010 compatibility finding echoed in `findings[]` rather than having that
    file served as a current number.

    **Store-only.** No container is contacted, so a STOPPED agent answers
    exactly like a running one — the legacy proxy returned "Agent must be
    running to read metrics", which made every number disappear at the moment
    an operator most wanted to know what it had been.

    Gate order (Invariant #8): the uniform-404 dependency decides access
    first, then the agent self-gate — an agent-scoped key reads only its own
    numbers (cross-agent reads are ent#80's grant, not an oversight here).
    """
    # --- self-gate (after access, before any read: the `metric_points.py`
    # spelling, so the write and the read agree on who "itself" is) ----------
    if current_user.agent_name and current_user.agent_name != agent_name:
        raise HTTPException(
            status_code=403,
            detail="Agent-scoped key may only read its own metrics",
        )

    rate_limiter.enforce(
        f"agent_metrics_read:{agent_name}",
        METRICS_READ_RATE_LIMIT,
        METRICS_READ_RATE_WINDOW,
        detail="Metric read rate limit exceeded for this agent.",
    )

    policy = _metric_policy()
    findings, evaluated_at = _metric_findings(agent_name)

    try:
        return metric_read_service.read_agent_metrics(
            agent_name,
            window=window,
            since=since,
            until=until,
            metric=metric,
            include_retired=include_retired,
            series_limit=series_limit,
            findings=findings,
            findings_evaluated_at=evaluated_at,
            policy=policy,
            retention_days=policy.get("retention_days"),
        )
    except metric_read_service.MetricReadError as e:
        # Named 422s, never a generic 500 (Bar 6). Deliberately NOT a 404:
        # the MCP error classifier reads 404 as "not authorized" (the #186
        # uniform-404 convention), so an undeclared metric would tell the
        # agent it lacks access to its own agent.
        raise HTTPException(
            status_code=422,
            detail={"reason": e.reason, "message": e.message, **e.extra},
        )
    except (OperationalError, DBAPIError) as e:
        logger.error("[Metrics] Read failed for %s: %s", agent_name, e)
        raise HTTPException(
            status_code=503,
            detail="metric_store_unavailable",
            headers={"Retry-After": "30"},
        )


@router.get("/{agent_name}/objectives", response_model=ObjectiveJoinRead)
async def get_agent_objectives(
    agent_name: AuthorizedAgentByName,
    current_user: User = Depends(get_current_user),
):
    """What this agent is supposed to move, and where it is (ent#666).

    One read of target vs actual with freshness: the objective files in the
    agent's own canon (framework §3.4) joined to the declared-metric registry
    (ent#477) and the point store (ent#478), judged by the ONE stale rule
    (ent#479). The role card, the project hub and proactivity all consume this
    — a second join anywhere is the defect ent#476 exists to remove.

    **Not store-only.** Unlike `/metrics`, this reads the objective FILES
    through the agent door, because files are truth and they live in the
    container (framework E7/E13). A stopped agent therefore answers
    `unavailable: agent_stopped` with copy naming the fix, rather than a number
    that was true once. That container cost is why this route has its own,
    much lower rate limit.

    Gate order (Invariant #8): the uniform-404 dependency decides access first,
    then the agent self-gate — an agent-scoped key reads only its own
    objectives — then the limiter, keyed on the name the gate has already
    validated.

    Every failure below transport is a NAMED field on a 200: `unavailable`,
    `source.*`, and `findings[]` sentences an operator can act on. An objective
    naming an undeclared metric comes back with that finding, never a blank.
    """
    # --- self-gate (after access, before any read: the `/metrics` spelling,
    # so the two reads agree on who "itself" is) ----------------------------
    if current_user.agent_name and current_user.agent_name != agent_name:
        raise HTTPException(
            status_code=403,
            detail="Agent-scoped key may only read its own objectives",
        )

    rate_limiter.enforce(
        f"agent_objectives_read:{agent_name}",
        OBJECTIVES_READ_RATE_LIMIT,
        OBJECTIVES_READ_RATE_WINDOW,
        detail="Objective read rate limit exceeded for this agent.",
    )

    try:
        return await objective_join_service.read_objective_join(agent_name)
    except (OperationalError, DBAPIError) as e:
        logger.error("[Objectives] Read failed for %s: %s", agent_name, e)
        raise HTTPException(
            status_code=503,
            detail="metric_store_unavailable",
            headers={"Retry-After": "30"},
        )


def _metric_findings(agent_name: str):
    """The persisted D-010 finding, echoed onto the read (TD-1).

    Read from the compatibility row rather than probed per request: a polled
    route may not `docker exec`, and a stopped agent has nothing to exec into.
    `findings_evaluated_at` is returned alongside so that "no finding" can be
    told from "not evaluated yet" — an empty list with no timestamp means the
    compat collector has not run since the upgrade, not that the agent is
    clean.
    """
    try:
        result = db.get_compatibility_result(agent_name)
    except Exception as e:  # noqa: BLE001 — a missing report is not a failure
        logger.debug("[Metrics] No compatibility result for %s: %s",
                     agent_name, e)
        return [], None
    if not result:
        return [], None
    findings = []
    for check in result.get("checks") or []:
        if check.get("id") == "D-010" and check.get("status") == "fail":
            findings.append({
                "code": "metrics_json_superseded",
                "message": check.get("message"),
                "detail": check.get("detail"),
            })
    return findings, result.get("checked_at")


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
        # The LIVE contract, read from settings rather than restated here:
        # a consumer that hard-codes these numbers is wrong the first time an
        # operator changes one. `enforced` is True as of ent#478 — the write
        # path validates against these very values — and `source` says which
        # tier supplied each, because "365 because nobody configured it" and
        # "365 because someone chose it" are different promises.
        "policy": _metric_policy(),
    }


def _metric_policy() -> dict:
    """The live metric-point policy, resolved through the settings chain.

    One reader for the two knobs ent#478 mints, so this block, the write path
    and `GET /api/settings/retention` cannot disagree about what is in force.
    """
    from services.settings_service import settings_service

    retention, retention_source = settings_service.resolve_ops_setting(
        "metrics_retention_days")
    cap, cap_source = settings_service.resolve_ops_setting(
        "metrics_daily_point_cap")

    def _int(raw, fallback):
        try:
            return max(int(raw), 0)
        except (TypeError, ValueError):
            return fallback

    return {
        "retention_days": _int(retention, 365),
        "retention_days_source": retention_source,
        "daily_point_cap": _int(cap, 100000),
        "daily_point_cap_source": cap_source,
        "enforced": True,
        "enforced_by": "abilityai/trinity-enterprise#478",
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
            audience_email=body.audience_email,
            # ent#549: only the agent's OWN key can be a call from inside a turn.
            # An owner or a user-scoped key passes the gate above too, and
            # whatever execution id they cite, they are not an agent in a turn.
            actor_is_agent=bool(actor_agent),
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
    # ent#549 — who each file is for is the owner's to read, in the UI. An
    # agent-scoped key resolves to its OWNER and passes the gate above, so
    # without this any agent could read who every file of every sibling was for
    # — an email and, for a channel share, a phone number. Same predicate and
    # same allow-list as `routers/reports.py::_hide_audience`. `audience_source`
    # goes with them (#2955): it carries no identity, but it is a property of
    # the addressing — whether the share was for someone at all — and only the
    # UI renders it.
    show_audience = is_interactive_principal(current_user)
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
            addressed_to=row.get("addressed_to_email") if show_audience else None,
            addressed_to_channel=row.get("addressed_to_channel") if show_audience else None,
            audience_source=row.get("audience_source") if show_audience else None,
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
