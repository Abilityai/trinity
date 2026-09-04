"""The ops/config settings model — the ONE validated write path for retention windows.

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


@router.get("/ops/config")
async def get_ops_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get all ops-related settings with their current values and defaults.

    Admin-only. Returns both stored values and defaults for ops settings.
    Useful for displaying the ops configuration panel.
    """
    assert_admin(current_user)

    try:
        # Get current values from database
        all_settings = db.get_settings_dict()

        # Build response with defaults and current values
        ops_config = {}
        for key, default_value in OPS_SETTINGS_DEFAULTS.items():
            current_value = all_settings.get(key, default_value)
            ops_config[key] = {
                "value": current_value,
                "default": default_value,
                "description": OPS_SETTINGS_DESCRIPTIONS.get(key, ""),
                "is_default": current_value == default_value
            }

        return {
            "settings": ops_config
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get ops settings: {str(e)}")
@router.put("/ops/config")
async def update_ops_settings(
    body: OpsSettingsUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Update multiple ops settings at once.

    Admin-only. Only accepts valid ops setting keys.
    Invalid keys are ignored with a warning.

    ent#297 — the OSS write path for the eight retention windows (the enterprise
    `retention` module has its own already-validated `PUT /api/enterprise/
    retention/config`, which clamps to the community floor and covers 7 of the 8
    — `agent_reminders_retention_days` is absent there). This one used to write
    `Dict[str, str]` straight through with no type or range check.
    Two things changed:

    * **Values are validated** (`config.validate_ops_setting`); the request is
      rejected 422 on the first bad one. Validation is deliberately NOT sold as
      the fix for ent#297 — a small valid integer is the dangerous input, and no
      range check can tell it apart from a legitimate short window. What it buys
      is a loud failure instead of a silent coercion to 0 ("sweep disabled"),
      which is the one thing the old shape got exactly backwards.
    * **Writes are audited.** Neither this endpoint nor `/ops/reset` logged
      anything, while the generic `PUT /{key}` directly above them does — so the
      one route that can shrink a retention window was also the one route that
      left no trace of having done so. ent#297 lists the audit surface in its
      blast radius; this closes the half of it that was self-inflicted.

    Validation is **all-or-nothing on purpose**: a partial apply would leave the
    operator with some windows moved and some not, and no way to tell which from
    the response.
    """
    assert_admin(current_user)

    from config import validate_ops_setting

    # Validate EVERYTHING before writing ANYTHING.
    to_write: list = []
    ignored: list = []
    for key, value in body.settings.items():
        if key not in OPS_SETTINGS_DEFAULTS:
            ignored.append(key)
            continue
        try:
            to_write.append((key, validate_ops_setting(key, value)))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    try:
        updated = []
        for key, value in to_write:
            db.set_setting(key, value)
            updated.append(key)

        if updated:
            from services.settings_service import RETENTION_OPS_KEYS

            touched = sorted(k for k in updated if k in RETENTION_OPS_KEYS)
            await platform_audit_service.log(
                event_type=AuditEventType.CONFIGURATION,
                event_action="ops_settings_change",
                source="api",
                actor_user=current_user,
                actor_ip=request.client.host if request.client else None,
                endpoint=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
                # Values are operator config, not secrets, and the whole point is
                # being able to answer "who shortened retention, to what, when".
                details={
                    "settings": dict(to_write),
                    "retention_windows_changed": touched or None,
                },
            )

        return {
            "success": True,
            "updated": updated,
            "ignored": ignored if ignored else None
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update ops settings: {str(e)}")
@router.post("/ops/reset")
async def reset_ops_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Reset ops settings to their default values.

    Admin-only. Removes ops settings from the database, causing them to fall
    back to defaults.

    Retention windows (`RETENTION_OPS_KEYS`) are deliberately EXCLUDED (#1638).
    Deleting one of those rows silently changes how much of the operator's data
    `cleanup_service` keeps, and a fresh install's seeded community floor would
    be widened by a button labelled "reset to defaults" — an irreversible-ish
    side effect nobody expects from a reset. Retention is changed only through
    the dedicated retention path, where it is an explicit decision.
    """
    assert_admin(current_user)

    from services.settings_service import RETENTION_OPS_KEYS

    try:
        deleted = []
        for key in OPS_SETTINGS_DEFAULTS.keys():
            if key in RETENTION_OPS_KEYS:
                continue
            if db.delete_setting(key):
                deleted.append(key)

        # #1966: ent#297 added the audit entry to `/ops/config` but not here,
        # while its own prose ("neither this route nor /ops/reset logged
        # anything before") read as though it had covered both. So the exact
        # asymmetry ent#297 objected to survived one route over: the generic
        # `PUT /{key}` audits, `/ops/config` audits, this one did not.
        #
        # Retention windows genuinely cannot be reset here (#1638 skips them),
        # but `ssh_access_enabled` can — resetting it changes whether ephemeral
        # SSH credentials may be minted at all, and that left no trace.
        #
        # Logged unconditionally, NOT gated on `deleted` the way /ops/config
        # gates on `updated`: there the empty case means nothing was asked for,
        # whereas an admin pressing reset on already-default settings is a real
        # administrative act whose absence from the log is indistinguishable
        # from it never having been attempted.
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="ops_settings_reset",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            # Keys and counts only. Unlike /ops/config there is no value worth
            # recording — every one of these is being DELETED, so the durable
            # fact is which keys reverted to their code default and which were
            # protected, not what they held on the way out.
            details={
                "reset": deleted,
                "reset_count": len(deleted),
                "skipped": sorted(RETENTION_OPS_KEYS),
            },
        )

        return {
            "success": True,
            "message": "Ops settings reset to defaults (retention windows unchanged)",
            "reset": deleted,
            "skipped": list(RETENTION_OPS_KEYS),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset ops settings: {str(e)}")
