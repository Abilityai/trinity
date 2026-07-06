"""Per-agent configuration endpoints: api-key, autonomy, read-only, resources, capabilities, capacity, timeout."""
from fastapi import APIRouter, Depends, HTTPException, Request

from models import User, AgentCapacityUpdate, PublicChannelModelUpdate
from database import db
from dependencies import get_current_user, AuthorizedAgentByName
from services import settings_service
from services.docker_service import get_agent_container
from services.agent_service import (
    get_agent_api_key_setting_logic,
    update_agent_api_key_setting_logic,
    get_autonomy_status_logic,
    set_autonomy_status_logic,
)
from services.platform_audit_service import platform_audit_service, AuditEventType

router = APIRouter(prefix="/api/agents", tags=["agents"])


# ============================================================================
# API Key Settings Endpoints
# ============================================================================

@router.get("/{agent_name}/api-key-setting")
async def get_agent_api_key_setting(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get the API key setting for an agent."""
    return await get_agent_api_key_setting_logic(agent_name, current_user)


@router.put("/{agent_name}/api-key-setting")
async def update_agent_api_key_setting(
    agent_name: str,
    request: Request,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """Update the API key setting for an agent."""
    return await update_agent_api_key_setting_logic(agent_name, body, current_user, request)


# ============================================================================
# Autonomy Mode Endpoints (per-agent)
# ============================================================================

@router.get("/{agent_name}/autonomy")
async def get_agent_autonomy_status(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """Get the autonomy status for an agent."""
    return await get_autonomy_status_logic(agent_name, current_user)


@router.put("/{agent_name}/autonomy")
async def set_agent_autonomy_status(
    agent_name: str,
    body: dict,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set the autonomy status for an agent.

    Body:
    - enabled: True to enable autonomy, False to disable

    When autonomy is enabled, all schedules for the agent are enabled.
    When disabled, all schedules are paused.
    """
    result = await set_autonomy_status_logic(agent_name, body, current_user)

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="autonomy_toggle",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"enabled": bool(body.get("enabled"))},
    )

    return result


# ============================================================================
# Read-Only Mode Endpoints
# ============================================================================

@router.get("/{agent_name}/read-only")
async def get_agent_read_only_status(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get the read-only mode status for an agent.

    Returns:
    - enabled: Whether read-only mode is enabled
    - config: Current configuration with blocked_patterns and allowed_patterns
    """
    from services.agent_service.read_only import get_read_only_status_logic
    return await get_read_only_status_logic(agent_name, current_user)


@router.put("/{agent_name}/read-only")
async def set_agent_read_only_status(
    agent_name: str,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """
    Set the read-only mode status for an agent.

    Body:
    - enabled: True to enable read-only mode, False to disable
    - config: Optional dict with 'blocked_patterns' and 'allowed_patterns' lists

    When enabled, the agent cannot modify files matching blocked patterns.
    Files matching allowed patterns can still be written (e.g., content/, output/).
    """
    from services.agent_service.read_only import set_read_only_status_logic
    return await set_read_only_status_logic(agent_name, body, current_user)


# ============================================================================
# Resource Limits Endpoints
# ============================================================================

@router.get("/{agent_name}/resources")
async def get_agent_resources(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get the resource limits for an agent.

    Returns:
    - memory: Memory limit (e.g., "4g", "8g", "16g") or null if using template default
    - cpu: CPU limit (e.g., "2", "4", "8") or null if using template default
    - current_memory: Current container memory limit
    - current_cpu: Current container CPU limit
    """
    # Check access
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Access denied")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get DB limits (may be None)
    db_limits = db.get_resource_limits(agent_name)

    # Get current container limits from labels
    labels = container.attrs.get("Config", {}).get("Labels", {})
    current_memory = labels.get("trinity.memory", "4g")
    current_cpu = labels.get("trinity.cpu", "2")

    return {
        "memory": db_limits.get("memory") if db_limits else None,
        "cpu": db_limits.get("cpu") if db_limits else None,
        "current_memory": current_memory,
        "current_cpu": current_cpu
    }


@router.put("/{agent_name}/resources")
async def set_agent_resources(
    agent_name: str,
    body: dict,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set the resource limits for an agent.

    Body:
    - memory: Memory limit (e.g., "4g", "8g", "16g") or null to use template default
    - cpu: CPU limit (e.g., "2", "4", "8") or null to use template default

    Changes take effect on next agent restart.

    Valid memory values: 1g, 2g, 4g, 8g, 16g, 32g
    Valid CPU values: 1, 2, 4, 8, 16
    """
    # Only owners can change resources
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only owners can change resource limits")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    memory = body.get("memory")
    cpu = body.get("cpu")

    # Validate memory format
    valid_memory = ["1g", "2g", "4g", "8g", "16g", "32g", "64g"]
    if memory and memory not in valid_memory:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid memory value. Must be one of: {', '.join(valid_memory)}"
        )

    # Validate CPU format
    valid_cpu = ["1", "2", "4", "8", "16"]
    if cpu and cpu not in valid_cpu:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid CPU value. Must be one of: {', '.join(valid_cpu)}"
        )

    # Update database
    db.set_resource_limits(agent_name, memory=memory, cpu=cpu)

    # Check if restart is needed
    labels = container.attrs.get("Config", {}).get("Labels", {})
    current_memory = labels.get("trinity.memory", "4g")
    current_cpu = labels.get("trinity.cpu", "2")

    restart_needed = (
        (memory and memory != current_memory) or
        (cpu and cpu != current_cpu)
    )

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="resource_limits",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={
            "memory": memory,
            "cpu": cpu,
            "previous_memory": current_memory,
            "previous_cpu": current_cpu,
        },
    )

    return {
        "message": "Resource limits updated",
        "memory": memory,
        "cpu": cpu,
        "restart_needed": restart_needed
    }


# ============================================================================
# Full Capabilities Endpoints (Container Security)
# ============================================================================

@router.get("/{agent_name}/capabilities")
async def get_agent_capabilities(
    agent_name: AuthorizedAgentByName,
):
    """
    Get the capabilities setting for an agent.

    Read access is enforced by AuthorizedAgentByName (uniform 404 for a
    non-existent OR inaccessible agent), closing the prior authz hole and the
    404-vs-200 existence oracle where any authenticated user could read a
    running agent's config (#186).

    Returns:
    - full_capabilities: True if agent has full Docker capabilities (apt-get works)
    - current_full_capabilities: Current container setting

    When full_capabilities=True:
    - Container runs with Docker default capabilities
    - Can install packages with apt-get
    - Can use sudo

    When full_capabilities=False (default):
    - Container runs with restricted capabilities
    - More secure but cannot install packages
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get DB setting
    db_full_caps = db.get_full_capabilities(agent_name)

    # Get current container setting from labels
    labels = container.attrs.get("Config", {}).get("Labels", {})
    current_full_caps = labels.get("trinity.full-capabilities", "false").lower() == "true"

    return {
        "full_capabilities": db_full_caps,
        "current_full_capabilities": current_full_caps
    }


@router.put("/{agent_name}/capabilities")
async def set_agent_capabilities(
    agent_name: str,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """
    Set the capabilities for an agent.

    Body:
    - full_capabilities: True for full Docker capabilities, False for restricted (secure)

    Note: Agent must be restarted for changes to take effect.
    The container label is updated on restart.
    """
    # Only owners can change capabilities
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only owners can change capabilities")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    full_caps = body.get("full_capabilities")
    if full_caps is None:
        raise HTTPException(status_code=400, detail="full_capabilities is required")

    if not isinstance(full_caps, bool):
        raise HTTPException(status_code=400, detail="full_capabilities must be a boolean")

    # Update database
    db.set_full_capabilities(agent_name, full_caps)

    # Update container label (will be applied on restart/recreate)
    labels = container.attrs.get("Config", {}).get("Labels", {})
    current_full_caps = labels.get("trinity.full-capabilities", "false").lower() == "true"

    restart_needed = full_caps != current_full_caps

    return {
        "message": "Capabilities updated",
        "full_capabilities": full_caps,
        "restart_needed": restart_needed
    }


# ============================================================================
# Parallel Capacity Endpoints (CAPACITY-001)
# ============================================================================

@router.get("/{agent_name}/capacity")
async def get_agent_capacity(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get the parallel execution capacity and current slot usage for an agent.

    Returns:
    - agent_name: Name of the agent
    - max_parallel_tasks: Stored, editable per-agent cap (1..fleet ceiling)
    - ceiling: Admin-set fleet-wide ceiling (#506)
    - effective_max_parallel_tasks: min(stored, ceiling) — the runtime limit
    - active_slots: Number of currently running executions
    - available_slots: Remaining capacity (computed from the effective cap)
    - slots: List of active slot details
    """
    from db_models import AgentCapacity, SlotInfo
    from services.capacity_manager import get_capacity_manager
    from services.settings_service import (
        get_max_parallel_tasks_ceiling,
        clamp_to_ceiling,
    )

    # Check access
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Access denied")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get max_parallel_tasks from database (stored, editable per-agent value).
    max_tasks = db.get_max_parallel_tasks(agent_name)

    # #506: two-tier model — the stored value is clamped to the fleet ceiling
    # at runtime. Surface ceiling + effective so the owner panel (which can't
    # call the admin settings GET) renders the bound and a "exceeds ceiling"
    # warning from this one call.
    ceiling = get_max_parallel_tasks_ceiling()
    effective = clamp_to_ceiling(max_tasks)

    # CAPACITY-CONSOLIDATE (#428): per-agent slot state via CapacityManager.
    # get_slot_state clamps internally, so available_slots is already computed
    # from the effective cap (available + active == effective).
    capacity = get_capacity_manager()
    slot_state = await capacity.get_slot_state(agent_name, max_tasks)

    # Convert to response model
    slots = [
        SlotInfo(
            slot_number=s.slot_number,
            execution_id=s.execution_id,
            started_at=s.started_at,
            message_preview=s.message_preview,
            duration_seconds=s.duration_seconds
        )
        for s in slot_state.slots
    ]

    return AgentCapacity(
        agent_name=agent_name,
        max_parallel_tasks=max_tasks,
        ceiling=ceiling,
        effective_max_parallel_tasks=effective,
        active_slots=slot_state.active_slots,
        available_slots=slot_state.available_slots,
        slots=slots
    )


@router.put("/{agent_name}/capacity")
async def set_agent_capacity(
    agent_name: str,
    body: AgentCapacityUpdate,
    current_user: User = Depends(get_current_user)
):
    """
    Set the parallel execution capacity for an agent.

    Body:
    - max_parallel_tasks: Maximum parallel tasks (1..fleet ceiling, #506)

    Only agent owners can modify capacity settings. The upper bound is the
    admin-set fleet ceiling (default 10), not a hardcoded 10.
    """
    from services.settings_service import get_max_parallel_tasks_ceiling

    # Only owners can change capacity
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only owners can change capacity settings")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    max_tasks = body.max_parallel_tasks

    # #506: validate against the admin-configured fleet ceiling, not a
    # hardcoded 10. Owners pick a per-agent value within the ceiling.
    ceiling = get_max_parallel_tasks_ceiling()
    if not isinstance(max_tasks, int) or max_tasks < 1 or max_tasks > ceiling:
        raise HTTPException(
            status_code=400,
            detail=f"max_parallel_tasks must be an integer between 1 and {ceiling}"
        )

    # Update database
    success = db.set_max_parallel_tasks(agent_name, max_tasks)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update capacity")

    return {
        "message": "Capacity updated",
        "agent_name": agent_name,
        "max_parallel_tasks": max_tasks
    }


# ============================================================================
# Execution Timeout Endpoints (TIMEOUT-001)
# ============================================================================

@router.get("/{agent_name}/timeout")
async def get_agent_timeout(
    agent_name: AuthorizedAgentByName,
):
    """
    Get the execution timeout setting for an agent.

    Read access enforced by AuthorizedAgentByName (uniform 404) — closes the
    open-GET authz hole + existence oracle (#186).

    Returns:
    - execution_timeout_seconds: Timeout in seconds (default 900 = 15 minutes)

    All execution paths (task API, chat, scheduler, MCP, paid endpoints) use
    this value when no explicit timeout is provided in the request.
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    timeout_seconds = db.get_execution_timeout(agent_name)

    return {
        "agent_name": agent_name,
        "execution_timeout_seconds": timeout_seconds,
        "execution_timeout_minutes": timeout_seconds // 60,
    }


@router.put("/{agent_name}/timeout")
async def set_agent_timeout(
    agent_name: str,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """
    Set the execution timeout for an agent.

    Body:
    - execution_timeout_seconds: Timeout in seconds (60-7200, i.e., 1 min to 2 hours)

    Only agent owners can modify timeout settings.
    """
    # Only owners can change timeout
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only owners can change timeout settings")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    timeout_seconds = body.get("execution_timeout_seconds")
    if timeout_seconds is None:
        raise HTTPException(status_code=400, detail="execution_timeout_seconds is required")

    # Validate range: 1 minute to 2 hours
    if not isinstance(timeout_seconds, int) or timeout_seconds < 60 or timeout_seconds > 7200:
        raise HTTPException(
            status_code=400,
            detail="execution_timeout_seconds must be an integer between 60 and 7200 (1 min to 2 hours)"
        )

    # #929 Approach A: the agent cap is the schedule ceiling, so refuse to
    # lower it below any active schedule's timeout. Caller must either raise
    # the cap above the affected schedules or shrink those schedules first.
    blocking_schedules = db.find_active_schedules_exceeding_timeout(
        agent_name, timeout_seconds
    )
    if blocking_schedules:
        max_blocking = max(s["timeout_seconds"] for s in blocking_schedules)
        raise HTTPException(
            status_code=400,
            detail={
                "error": "agent_timeout_below_active_schedules",
                "message": (
                    f"Cannot lower agent timeout to {timeout_seconds}s — "
                    f"{len(blocking_schedules)} active schedule(s) on '{agent_name}' "
                    f"have timeout_seconds up to {max_blocking}s. Edit those "
                    f"schedules first, then retry."
                ),
                "requested_seconds": timeout_seconds,
                "max_schedule_timeout_seconds": max_blocking,
                "blocking_schedules": blocking_schedules,
            },
        )

    # Update database
    success = db.set_execution_timeout(agent_name, timeout_seconds)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update timeout")

    return {
        "message": "Timeout updated",
        "agent_name": agent_name,
        "execution_timeout_seconds": timeout_seconds,
        "execution_timeout_minutes": timeout_seconds // 60,
    }


# ============================================================================
# Public-Channel Model Override (#894)
# ============================================================================

@router.get("/{agent_name}/public-channel-model")
async def get_public_channel_model(
    agent_name: AuthorizedAgentByName,
):
    """Get the per-agent model override for public-facing channels (#894).

    Read access enforced by AuthorizedAgentByName (uniform 404) — closes the
    open-GET authz hole + existence oracle (#186).

    Public-facing = public chat link, Slack/Telegram/WhatsApp channels, and x402
    paid chat. The override is owner-set; NULL means inherit the platform
    default. Returns the raw override, the resolved (effective) model, and the
    selectable list.
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    override = db.get_public_channel_model(agent_name)
    platform_default = settings_service.get_platform_default_model()
    return {
        "agent_name": agent_name,
        "public_channel_model": override,                       # None = inherit
        "resolved_model": override or platform_default,
        "is_overridden": override is not None,
        "platform_default": platform_default,
        "available_models": sorted(settings_service.PUBLIC_CHANNEL_MODELS),
    }


@router.put("/{agent_name}/public-channel-model")
async def set_public_channel_model(
    agent_name: str,
    body: PublicChannelModelUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Set or clear the per-agent public-channel model override (owner-only, #894).

    A null/empty ``model`` clears the override (inherit the platform default);
    a non-empty value must be a currently-selectable model (422 otherwise).
    """
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only owners can change the public-channel model")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    model = (body.model or "").strip() or None
    if model is not None and not settings_service.is_valid_public_channel_model(model):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_public_channel_model",
                "message": f"{model!r} is not a selectable public-channel model.",
                "available_models": sorted(settings_service.PUBLIC_CHANNEL_MODELS),
            },
        )

    if not db.set_public_channel_model(agent_name, model):
        raise HTTPException(status_code=500, detail="Failed to update public-channel model")

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="update_public_channel_model",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"public_channel_model": model},  # None = cleared
    )

    platform_default = settings_service.get_platform_default_model()
    return {
        "message": "Public-channel model updated",
        "agent_name": agent_name,
        "public_channel_model": model,
        "resolved_model": model or platform_default,
        "is_overridden": model is not None,
    }


# ============================================================================
# Guardrails (GUARD-001)
# ============================================================================

_GUARDRAILS_NUMBER_BOUNDS = {
    "max_turns_chat": (1, 500),
    "max_turns_task": (1, 500),
    "execution_timeout_sec": (60, 7200),
}
_GUARDRAILS_LIST_FIELDS = ("extra_bash_deny", "extra_path_deny", "disallowed_tools")
_GUARDRAILS_MAX_LIST = 50
_GUARDRAILS_MAX_STRING = 256


def _validate_guardrails_payload(body: dict) -> dict:
    """Whitelist + sanitise guardrails override payload.

    Per-agent overrides are intentionally narrow: numeric caps and literal
    substring lists only. Regex patterns and credential scanners stay in the
    platform baseline — agents cannot extend them. Returns the cleaned dict
    (empty dict means "clear overrides, inherit baseline").
    """
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    cleaned: dict = {}
    for field, (lo, hi) in _GUARDRAILS_NUMBER_BOUNDS.items():
        if field not in body:
            continue
        value = body[field]
        if not isinstance(value, int) or isinstance(value, bool) or not (lo <= value <= hi):
            raise HTTPException(
                status_code=400,
                detail=f"{field} must be an integer between {lo} and {hi}",
            )
        cleaned[field] = value

    for field in _GUARDRAILS_LIST_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if not isinstance(value, list):
            raise HTTPException(status_code=400, detail=f"{field} must be a list of strings")
        if len(value) > _GUARDRAILS_MAX_LIST:
            raise HTTPException(
                status_code=400,
                detail=f"{field} exceeds maximum of {_GUARDRAILS_MAX_LIST} entries",
            )
        for item in value:
            if not isinstance(item, str) or not item or len(item) > _GUARDRAILS_MAX_STRING:
                raise HTTPException(
                    status_code=400,
                    detail=f"{field} entries must be non-empty strings up to {_GUARDRAILS_MAX_STRING} chars",
                )
        cleaned[field] = value

    return cleaned


@router.get("/{agent_name}/guardrails")
async def get_agent_guardrails(
    agent_name: AuthorizedAgentByName,
):
    """Return per-agent guardrails overrides (empty object if none configured).

    Read access enforced by AuthorizedAgentByName (uniform 404) — closes the
    open-GET authz hole + existence oracle (#186).
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {
        "agent_name": agent_name,
        "guardrails": db.get_guardrails_config(agent_name),
    }


@router.put("/{agent_name}/guardrails")
async def set_agent_guardrails(
    agent_name: str,
    body: dict,
    current_user: User = Depends(get_current_user),
):
    """Set per-agent guardrails overrides. Owner-only. Requires container
    recreation to take effect (the runtime config file is written during
    startup). An empty object clears all overrides."""
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(
            status_code=403,
            detail="Only agent owners can change guardrails settings",
        )

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    cleaned = _validate_guardrails_payload(body)
    success = db.set_guardrails_config(agent_name, cleaned or None)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update guardrails")

    return {
        "message": "Guardrails updated (restart agent to apply)",
        "agent_name": agent_name,
        "guardrails": cleaned,
    }
