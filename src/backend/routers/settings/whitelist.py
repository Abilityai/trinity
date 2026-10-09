"""Email allow-list for email sign-in.

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
from pydantic import ValidationError

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


def _refusal_reason(exc: ValidationError) -> str:
    """The first validation failure as one sentence that names its field.

    A model validator's own message already names it ("email must be …");
    Pydantic prefixes those with "Value error, ". A built-in failure ("Field
    required") carries the field only in `loc`.
    """
    err = exc.errors()[0]
    msg = str(err.get("msg", "Invalid request"))
    prefix = "Value error, "
    if msg.startswith(prefix):
        return msg[len(prefix):]
    loc = err.get("loc") or ()
    return f"{loc[-1]}: {msg}" if loc else msg


@router.get("/email-whitelist")
async def list_email_whitelist(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    List all whitelisted emails.

    Admin-only endpoint.
    """
    assert_admin(current_user)

    whitelist = db.list_whitelist(limit=1000)

    return {"whitelist": whitelist}


@router.post("/email-whitelist")
async def add_email_to_whitelist(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Add an email to the whitelist.

    Admin-only endpoint.
    """
    from database import EmailWhitelistAdd

    assert_admin(current_user)

    # Parse request. The body is validated here rather than by FastAPI so a
    # refusal is one sentence the Settings field can show as-is (#3455) — and
    # so a body that is not a JSON object is a 422 too, not a 500.
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise TypeError("not a JSON object")
        add_request = EmailWhitelistAdd(**body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=_refusal_reason(e))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail='Request body must be a JSON object like {"email": "user@example.com"}',
        )
    # Trimmed and lower-cased by the model.
    email = add_request.email

    # Add to whitelist
    try:
        added = db.add_to_whitelist(
            email,
            current_user.username,
            source=add_request.source,
            default_role=add_request.default_role,
        )

        if not added:
            raise HTTPException(
                status_code=409,
                detail=f"Email {email} is already whitelisted"
            )

        return {"success": True, "email": email}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/email-whitelist/{email:path}")
async def remove_email_from_whitelist(
    email: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Remove an email from the whitelist.

    Admin-only endpoint.

    `:path` because the stored value may contain `/` (#3456): it is a legal
    local-part character, and rows that predate #3455's validation hold
    arbitrary text. The router decodes `%2F` before matching, so a plain
    `{email}` matched no route for such a row and it could never be removed.
    Nothing else is registered under `/email-whitelist/`, so the wider match
    claims no sibling (Invariant #4; pinned by
    `tests/unit/test_3455_email_whitelist_validation.py`).
    """
    assert_admin(current_user)

    # Remove from whitelist
    removed = db.remove_from_whitelist(email)

    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"Email {email} not found in whitelist"
        )

    return {"success": True, "email": email}
