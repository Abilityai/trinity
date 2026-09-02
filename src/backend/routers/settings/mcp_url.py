"""The externally-advertised MCP server URL, and the resolver other routers share.

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

MCP_URL_SETTING_KEY = "mcp_external_url"

def _get_default_mcp_url(request: Request) -> str:
    """Compute the auto-detected MCP URL from the request hostname."""
    host = request.headers.get("host", "localhost:8080")
    hostname = host.split(":")[0]
    if hostname in ("localhost", "127.0.0.1"):
        return "http://localhost:8080/mcp"
    return f"http://{hostname}:8080/mcp"
def resolve_mcp_url(request: Request) -> str:
    """Effective MCP URL: the operator-configured override, else auto-detected.

    Public accessor so other modules (e.g. the entitled MCP connector) don't
    reach into the private helper above.
    """
    return db.get_setting_value(MCP_URL_SETTING_KEY) or _get_default_mcp_url(request)
def _validate_mcp_url(url: str) -> str:
    """Validate and normalize MCP URL. Returns normalized URL or raises HTTPException."""
    url = url.strip().rstrip("/")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=422,
            detail="URL must start with http:// or https://"
        )
    if not parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail="Invalid URL format"
        )
    if not parsed.path.endswith("/mcp"):
        raise HTTPException(
            status_code=422,
            detail="URL must end with /mcp"
        )

    return url
@router.get("/mcp-url")
async def get_mcp_url(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the configured MCP server URL.

    Any authenticated user can read this (used by API Keys page).
    Returns both the stored custom URL (if any) and the auto-detected default.
    """
    stored_url = db.get_setting_value(MCP_URL_SETTING_KEY)
    default_url = _get_default_mcp_url(request)

    return {
        "url": stored_url,
        "default_url": default_url
    }
@router.put("/mcp-url")
async def update_mcp_url(
    body: McpUrlUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set a custom MCP server URL.

    Admin-only. Validates URL format (must be http/https, must end with /mcp).
    """
    assert_admin(current_user)

    validated_url = _validate_mcp_url(body.url)
    db.set_setting(MCP_URL_SETTING_KEY, validated_url)

    return {
        "success": True,
        "url": validated_url
    }
@router.delete("/mcp-url")
async def delete_mcp_url(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Reset MCP server URL to auto-detect.

    Admin-only. Removes the custom URL, reverting to hostname-based auto-detection.
    """
    assert_admin(current_user)

    deleted = db.delete_setting(MCP_URL_SETTING_KEY)

    return {
        "success": True,
        "deleted": deleted,
        "message": "MCP server URL reset to auto-detect"
    }
