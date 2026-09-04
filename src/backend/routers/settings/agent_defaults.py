"""Fleet-wide defaults new agents inherit — quotas, resources, access policy, parallelism ceiling.

Carved out of the 3,529-line `routers/settings.py` (#1028). The package
`__init__` composes every sub-router onto one `/api/settings` router, so the
mounted API is byte-identical to the single-module version.
"""
import asyncio
import json
import logging
import os
import re
import httpx
from typing import List, Dict, Any
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger(__name__)

from models import (
    A2AOutboundEndpointUpsert,
    AgentDefaultAccessPolicyUpdate,
    AgentDefaultResourcesUpdate,
    AgentQuotaUpdate,
    ProactiveRateLimitsUpdate,
    ApiKeyTest,
    ApiKeyUpdate,
    BrainOrbSettingsUpdate,
    ElevenLabsSettingsUpdate,
    GitHubTemplatesUpdate,
    TemplateRegistryUpdate,
    MaxParallelTasksCeilingUpdate,
    McpUrlUpdate,
    OpsSettingsUpdate,
    RetentionAcknowledge,
    SkillsLibraryAutomationUpdate,
    OperatorIntakeUpdate,
    SlackConnectRequest,
    SlackSettingsUpdate,
    TelemetrySharingUpdate,
    User,
)
from database import db, SystemSetting, SystemSettingUpdate
from dependencies import get_current_user, assert_admin
from services.platform_audit_service import platform_audit_service, AuditEventType
from services import operator_intake_service, telemetry_sharing_service

# Import from settings_service (these are re-exported for backward compatibility)
from services.settings_service import (
    get_anthropic_api_key,
    get_github_pat,
    get_google_api_key,
    get_ops_setting,
    set_secret_setting,
    clear_secret_setting,
    has_secret_setting,
    settings_service,
    OPS_SETTINGS_DEFAULTS,
    OPS_SETTINGS_DESCRIPTIONS,
    AGENT_QUOTA_DEFAULTS,
    AGENT_QUOTA_DESCRIPTIONS,
    AGENT_DEFAULT_CPU_KEY,
    AGENT_DEFAULT_MEMORY_KEY,
    AGENT_DEFAULT_CPU,
    AGENT_DEFAULT_MEMORY,
    AGENT_DEFAULT_REQUIRE_EMAIL_KEY,
    AGENT_DEFAULT_REQUIRE_EMAIL,
    get_agent_default_require_email,
    MAX_PARALLEL_TASKS_CEILING_KEY,
    MAX_PARALLEL_TASKS_CEILING_DEFAULT,
    MAX_PARALLEL_TASKS_CEILING_MIN,
    MAX_PARALLEL_TASKS_CEILING_MAX,
    get_max_parallel_tasks_ceiling,
    PROACTIVE_RATE_LIMIT_DEFAULTS,
    PROACTIVE_RATE_LIMIT_DESCRIPTIONS,
    PROACTIVE_RATE_LIMIT_MAX,
    get_proactive_rate_limit,
    SKILLS_AUTO_REINJECT_ENABLED_KEY,
    SKILLS_AUTO_SYNC_ENABLED_KEY,
    SKILLS_AUTO_SYNC_INTERVAL_KEY,
    SKILLS_AUTO_SYNC_INTERVAL_DEFAULT,
    SKILLS_AUTO_SYNC_INTERVAL_MIN,
    SKILLS_AUTO_SYNC_INTERVAL_MAX,
)

# ent#236: the three keys the dedicated /skills-library route owns. Blocked on
# the generic PUT /{key} so they can only ever be written range-validated.
SKILLS_AUTOMATION_KEYS = {
    SKILLS_AUTO_SYNC_ENABLED_KEY,
    SKILLS_AUTO_SYNC_INTERVAL_KEY,
    SKILLS_AUTO_REINJECT_ENABLED_KEY,
}

# ent#346: the pre-ent#237 single-repo settings. `_adopt_legacy_clone` converts
# these into a `skill_sources` row, so writing them IS registering a skills
# source — the grant action ent#237 gates behind `reject_agent_principal` on
# every `/skills/sources` route. Blocked on the generic PUT so the gate cannot
# be walked around. `skills_library_branch` is included because a source is
# (url, ref): re-pointing the ref alone changes which commit the fleet executes.
LEGACY_SKILLS_LIBRARY_KEYS = {
    "skills_library_url",
    "skills_library_branch",
}

# ent#434 — the catch-all blocks this key in favour of the dedicated route.
from services.subscription_headroom_alerts import (
    THRESHOLD_SETTING as HEADROOM_ALERT_THRESHOLD_KEY,
    MIN_THRESHOLD_PCT as HEADROOM_THRESHOLD_MIN,
    MAX_THRESHOLD_PCT as HEADROOM_THRESHOLD_MAX,
)



router = APIRouter()

from services.agent_service.capabilities import VALID_CPU, VALID_MEMORY

VALID_CPU_VALUES = list(VALID_CPU)
VALID_MEMORY_VALUES = list(VALID_MEMORY)

@router.get("/agent-quotas")
async def get_agent_quotas(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get per-role agent quota configuration.

    Admin-only. Returns quota limits for each role with defaults.
    Admin role is always unlimited and not configurable.
    """
    assert_admin(current_user)

    all_settings = db.get_settings_dict()

    # Check for legacy setting
    legacy_value = all_settings.get("max_agents_per_user")

    quotas = {}
    for key, default_value in AGENT_QUOTA_DEFAULTS.items():
        current_value = all_settings.get(key, default_value)
        quotas[key] = {
            "value": current_value,
            "default": default_value,
            "description": AGENT_QUOTA_DESCRIPTIONS.get(key, ""),
            "is_default": key not in all_settings
        }

    return {
        "quotas": quotas,
        "admin_unlimited": True,
        "legacy_setting": legacy_value
    }
@router.put("/agent-quotas")
async def update_agent_quotas(
    body: AgentQuotaUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Update per-role agent quotas.

    Admin-only. Only updates provided fields. Values must be non-negative integers.
    Set to "0" for unlimited.
    """
    assert_admin(current_user)

    updated = []
    for key in AGENT_QUOTA_DEFAULTS:
        value = getattr(body, key, None)
        if value is not None:
            try:
                int_val = int(value)
                if int_val < 0:
                    raise HTTPException(status_code=400, detail=f"Quota value for {key} must be non-negative")
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Quota value for {key} must be an integer")
            db.set_setting(key, value)
            updated.append(key)

    return {
        "success": True,
        "updated": updated
    }
@router.get("/agent-defaults/resources")
async def get_agent_default_resources(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the system-wide default CPU and memory limits applied to every new agent container.

    Admin-only.
    """
    assert_admin(current_user)

    cpu = db.get_setting_value(AGENT_DEFAULT_CPU_KEY, AGENT_DEFAULT_CPU)
    memory = db.get_setting_value(AGENT_DEFAULT_MEMORY_KEY, AGENT_DEFAULT_MEMORY)

    return {
        "cpu": cpu,
        "memory": memory,
        "cpu_default": AGENT_DEFAULT_CPU,
        "memory_default": AGENT_DEFAULT_MEMORY,
        "valid_cpu_values": VALID_CPU_VALUES,
        "valid_memory_values": VALID_MEMORY_VALUES,
        "note": "Changes apply to new agent containers only. Restart existing agents to pick up new defaults."
    }
@router.put("/agent-defaults/resources")
async def update_agent_default_resources(
    body: AgentDefaultResourcesUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set the system-wide default CPU and memory limits for new agent containers.

    Admin-only. Only the fields provided are updated.
    Valid CPU values (number of processors): 1, 2, 4, 8, 16
    Valid memory values: 1g, 2g, 4g, 8g, 16g, 32g
    """
    assert_admin(current_user)

    updated = []

    if body.cpu is not None:
        if body.cpu not in VALID_CPU_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid CPU value. Must be one of: {', '.join(VALID_CPU_VALUES)}"
            )
        db.set_setting(AGENT_DEFAULT_CPU_KEY, body.cpu)
        updated.append("cpu")

    if body.memory is not None:
        if body.memory not in VALID_MEMORY_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid memory value. Must be one of: {', '.join(VALID_MEMORY_VALUES)}"
            )
        db.set_setting(AGENT_DEFAULT_MEMORY_KEY, body.memory)
        updated.append("memory")

    cpu = db.get_setting_value(AGENT_DEFAULT_CPU_KEY, AGENT_DEFAULT_CPU)
    memory = db.get_setting_value(AGENT_DEFAULT_MEMORY_KEY, AGENT_DEFAULT_MEMORY)

    return {
        "success": True,
        "updated": updated,
        "cpu": cpu,
        "memory": memory,
        "restart_required": True
    }
@router.get("/agent-defaults/access-policy")
async def get_agent_default_access_policy(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the fleet-wide default access policy applied to newly created agents.

    Currently scopes to `require_email` (#1129): when ON, new agents require a
    verified email on incoming DMs / public chat / shared access. Admin-only.
    """
    assert_admin(current_user)

    return {
        "require_email": get_agent_default_require_email(),
        "require_email_default": AGENT_DEFAULT_REQUIRE_EMAIL,
        "note": "Applies to newly created agents only. Existing agents keep their "
                "current per-agent value; owners can override per agent via the "
                "agent's access policy.",
    }
@router.put("/agent-defaults/access-policy")
async def update_agent_default_access_policy(
    body: AgentDefaultAccessPolicyUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set the fleet-wide default access policy for new agents (#1129).

    Admin-only. Only the fields provided are updated. Stored in system_settings;
    consumed at agent-creation time. Does NOT rewrite existing agents.
    """
    assert_admin(current_user)

    updated = []
    if body.require_email is not None:
        db.set_setting(AGENT_DEFAULT_REQUIRE_EMAIL_KEY, "1" if body.require_email else "0")
        updated.append("require_email")

        # SEC-001 / #1129: audit this security-relevant default change — flipping
        # the fleet-wide email-verification default weakens/strengthens the
        # posture for every future agent, so it must leave a trace (mirrors the
        # API-key / generic-setting audit path in this router).
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={
                "setting": "agent_default_require_email",
                "action": "update",
                "require_email": body.require_email,
            },
        )

    return {
        "success": True,
        "updated": updated,
        "require_email": get_agent_default_require_email(),
    }
@router.get("/max-parallel-tasks-ceiling")
async def get_max_parallel_tasks_ceiling_setting(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the fleet-wide ceiling on any single agent's max_parallel_tasks (#506).

    Admin-only. Owners pick a per-agent value within this ceiling.
    Registered before the `/{key}` catch-all (Invariant #4).
    """
    assert_admin(current_user)

    return {
        "value": get_max_parallel_tasks_ceiling(),
        "default": MAX_PARALLEL_TASKS_CEILING_DEFAULT,
        "min": MAX_PARALLEL_TASKS_CEILING_MIN,
        "max": MAX_PARALLEL_TASKS_CEILING_MAX,
    }
@router.put("/max-parallel-tasks-ceiling")
async def update_max_parallel_tasks_ceiling_setting(
    body: MaxParallelTasksCeilingUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set the fleet-wide ceiling on per-agent max_parallel_tasks (#506).

    Admin-only. Validates MIN ≤ value ≤ MAX (1–32). The clamp applies at
    runtime (CapacityManager facade + bypasses); stored per-agent values are
    never rewritten. Existing agents above the new ceiling are clamped on the
    next admit.
    """
    assert_admin(current_user)

    if (
        not isinstance(body.value, int)
        or body.value < MAX_PARALLEL_TASKS_CEILING_MIN
        or body.value > MAX_PARALLEL_TASKS_CEILING_MAX
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"max_parallel_tasks_ceiling must be an integer between "
                f"{MAX_PARALLEL_TASKS_CEILING_MIN} and {MAX_PARALLEL_TASKS_CEILING_MAX}"
            ),
        )

    db.set_setting(MAX_PARALLEL_TASKS_CEILING_KEY, str(body.value))

    # SEC-001: audit this fleet-wide capacity change (mirrors the access-policy
    # default audit path) — it caps concurrency for every agent on the host.
    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="settings_change",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={
            "setting": MAX_PARALLEL_TASKS_CEILING_KEY,
            "action": "update",
            "value": body.value,
        },
    )

    return {
        "success": True,
        "value": get_max_parallel_tasks_ceiling(),
        "default": MAX_PARALLEL_TASKS_CEILING_DEFAULT,
        "min": MAX_PARALLEL_TASKS_CEILING_MIN,
        "max": MAX_PARALLEL_TASKS_CEILING_MAX,
    }
