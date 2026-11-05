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
from utils.public_url import public_base_url

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
    McpExternalAccessUpdate,
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
    MCP_EXTERNAL_ACCESS_KEY,
    get_mcp_external_access_env_value,
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
PUBLIC_CHAT_URL_SETTING_KEY = "public_chat_url"

def _get_default_mcp_url(request: Request) -> str:
    """Compute the auto-detected MCP URL from the request hostname.

    A request carrying `X-Forwarded-Proto` came through the bundled nginx
    (which always sets it) or an operator's proxy, and that proxy serves
    `/mcp` on the same origin — so the URL is `<origin>/mcp` with no `:8080`,
    whatever the scheme (#2907 for https; trinity-enterprise#1101 for http,
    where a loopback-bound raw port would otherwise be advertised to a client
    that cannot reach it). The origin comes from `public_base_url`; the
    hostname is the `Host` header, and no route trusts `X-Forwarded-Host`.

    No forwarded header (a direct `:8000` hit) → the raw port. With
    `MCP_BIND_ADDRESS` set it is published on that address only, so the honest
    URL is the host-local one; unset keeps every interface, and today's URL.
    """
    origin = public_base_url(request)
    if origin and (request.headers.get("x-forwarded-proto") or origin.startswith("https://")):
        return f"{origin}/mcp"
    if os.getenv("MCP_BIND_ADDRESS", "").strip():
        return "http://127.0.0.1:8080/mcp"
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


@router.get("/public-chat-url")
async def get_public_chat_url(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    The instance's public chat URL, or `None` when none is configured (#2202).

    A dedicated route because *unset* is the normal state for this key, not an
    error. The Settings page read it through the generic `GET /api/settings/{key}`,
    which answers 404 for a key that was never written — so every visit to every
    Settings tab logged a failed request in the browser console, and a genuine
    failure on that call was indistinguishable from the ordinary case. The store
    already treated 404 as "unset", so nothing was broken; what was lost was the
    signal, and no client-side handling can suppress the browser's own network
    log.

    Deliberately NOT a widening of the generic route: its 404 is the documented
    contract for every other key and is depended on outside this repo (the ops
    agent, scripts, the MCP surface). This follows the `/mcp-url` precedent
    instead — a named route for a named setting.

    Admin-gated to match the generic getter it replaces for this key; no caller
    outside Settings reads it. Lives in this module so it is included ABOVE the
    `generic` `/{key}` catch-all (Invariant #4 — see the package `__init__`), or
    "public-chat-url" routes as a setting key named exactly that.
    """
    assert_admin(current_user)

    return {"key": PUBLIC_CHAT_URL_SETTING_KEY, "value": db.get_setting_value(PUBLIC_CHAT_URL_SETTING_KEY)}


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


# ---------------------------------------------------------------------------
# External MCP access (trinity-enterprise#1101)
# ---------------------------------------------------------------------------

def _mcp_external_access_state(request: Request) -> Dict[str, Any]:
    enabled, source = settings_service.is_mcp_external_access_enabled()
    return {
        "enabled": enabled,
        "source": source,
        "env_value": get_mcp_external_access_env_value(),
        "advertised_url": resolve_mcp_url(request),
        "raw_port_bind_address": os.getenv("MCP_BIND_ADDRESS", "").strip() or None,
    }


async def _audit_mcp_external_access(request: Request, current_user: User, action: str, enabled=None):
    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="settings_change",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=request.scope["path"],
        request_id=getattr(request.state, "request_id", None),
        details={"setting": MCP_EXTERNAL_ACCESS_KEY, "action": action, "enabled": enabled},
    )


@router.get("/mcp-external-access")
async def get_mcp_external_access(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Whether off-box clients may reach `/mcp`, which tier decided, and the
    URL the instance advertises.

    Any authenticated user, like `GET /mcp-url`: the MCP Keys page is not
    admin-only, and the answer is on/off plus where it came from. `env_value`
    lets the page say an environment value exists even when the Settings row
    overrides it; `raw_port_bind_address` is the `MCP_BIND_ADDRESS` the raw
    `:8080` port is published on (None = every interface).
    """
    return _mcp_external_access_state(request)


@router.put("/mcp-external-access")
async def update_mcp_external_access(
    body: McpExternalAccessUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Turn external MCP access on or off for the whole instance.

    Admin-only (`assert_admin` refuses agent keys, #1890). The Settings row wins
    over `MCP_EXTERNAL_ACCESS` in both directions until cleared. Agents are
    never affected. Audit-logged: this moves the instance's perimeter.
    """
    assert_admin(current_user)

    db.set_setting(MCP_EXTERNAL_ACCESS_KEY, "true" if body.enabled else "false")
    await _audit_mcp_external_access(request, current_user, "set", body.enabled)
    return _mcp_external_access_state(request)


@router.delete("/mcp-external-access")
async def delete_mcp_external_access(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Clear the Settings row so `MCP_EXTERNAL_ACCESS`, else the default
    (off), decides. Admin-only, audit-logged when a row was removed."""
    assert_admin(current_user)

    if db.delete_setting(MCP_EXTERNAL_ACCESS_KEY):
        await _audit_mcp_external_access(request, current_user, "clear")
    return _mcp_external_access_state(request)
