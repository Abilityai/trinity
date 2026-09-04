"""Optional subsystems configured from Settings — skills automation, proactive limits, Brain Orb, ElevenLabs, outbound A2A endpoints.

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


@router.get("/skills-library")
async def get_skills_library_automation_setting(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Skills-library lifecycle automation config + last-run status (ent#236).

    Admin-only. Registered before the `/{key}` catch-all (Invariant #4).

    Also reports the durable sync status and the last fleet re-inject report, so
    the panel can show a FAILING auto-sync — the AC's "never silent" half. Both
    read from `system_settings` rather than service memory because the loop runs
    on ONE leader worker and this request usually lands on a different one.
    """
    assert_admin(current_user)

    from services.settings_service import (
        get_skills_auto_sync_interval,
        is_skills_auto_reinject_enabled,
        is_skills_auto_sync_enabled,
    )
    from services.skill_service import (
        SKILLS_LAST_ERROR_KEY, SKILLS_LAST_STATUS_KEY, SKILLS_LAST_SYNC_KEY,
    )
    from services.skills_sync_service import FLEET_LAST_RUN_KEY

    last_run = None
    try:
        raw = db.get_setting_value(FLEET_LAST_RUN_KEY, None)
        if raw:
            last_run = json.loads(raw)
    except Exception:  # noqa: BLE001 — a malformed blob must not 500 the panel
        last_run = None

    return {
        "auto_sync_enabled": is_skills_auto_sync_enabled(),
        "auto_sync_interval_seconds": get_skills_auto_sync_interval(),
        "auto_reinject_enabled": is_skills_auto_reinject_enabled(),
        "interval_default": SKILLS_AUTO_SYNC_INTERVAL_DEFAULT,
        "interval_min": SKILLS_AUTO_SYNC_INTERVAL_MIN,
        "interval_max": SKILLS_AUTO_SYNC_INTERVAL_MAX,
        "last_sync": db.get_setting_value(SKILLS_LAST_SYNC_KEY, None),
        "last_sync_status": db.get_setting_value(SKILLS_LAST_STATUS_KEY, None),
        "last_sync_error": db.get_setting_value(SKILLS_LAST_ERROR_KEY, None) or None,
        "last_fleet_reinject": last_run,
    }
@router.put("/skills-library")
async def update_skills_library_automation_setting(
    body: SkillsLibraryAutomationUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Set skills-library automation flags + interval (ent#236).

    Admin **and human-only**, partial update — every field is optional, and an
    omitted field is left untouched (so toggling re-inject can't silently reset
    the interval). Interval is range-validated here with a 400 rather than being
    clamped silently: an operator who typed 30 should be told the floor exists,
    not quietly given 300.

    `reject_agent_principal` is load-bearing, not decoration. `assert_admin`
    answers "what role", never "is this a human": an agent-scoped MCP key
    resolves to its owner *carrying the owner's role*, so on a default
    admin-owned install every agent's injected `TRINITY_MCP_API_KEY` satisfies
    it. This endpoint is the ON-SWITCH for an unattended, fleet-wide write into
    every running agent's `~/.claude/skills/` — and a `SKILL.md` is instructions
    Claude executes, not data. Pre-#236 that write needed two deliberate human
    actions (click Sync, then inject per agent); automating it removed the human,
    so the gate has to put one back. Third occurrence of the
    trinity-ops-agent#232 class (see #1644, #1816), and the rule from
    learnings.md applies directly: the endpoint that USES a capability may be
    agent-callable, the endpoint that GRANTS it must be human-only.

    The GET stays role-only: it reads non-secret config, and its error string is
    PAT-scrubbed at the write.
    """
    from dependencies import reject_agent_principal

    reject_agent_principal(current_user)
    assert_admin(current_user)

    from services.settings_service import (
        SKILLS_AUTO_REINJECT_ENABLED_KEY,
        SKILLS_AUTO_SYNC_ENABLED_KEY,
        SKILLS_AUTO_SYNC_INTERVAL_KEY,
        get_skills_auto_sync_interval,
        is_skills_auto_reinject_enabled,
        is_skills_auto_sync_enabled,
    )

    changed: Dict[str, Any] = {}

    if body.auto_sync_interval_seconds is not None:
        value = body.auto_sync_interval_seconds
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < SKILLS_AUTO_SYNC_INTERVAL_MIN
            or value > SKILLS_AUTO_SYNC_INTERVAL_MAX
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"auto_sync_interval_seconds must be an integer between "
                    f"{SKILLS_AUTO_SYNC_INTERVAL_MIN} and {SKILLS_AUTO_SYNC_INTERVAL_MAX}"
                ),
            )
        db.set_setting(SKILLS_AUTO_SYNC_INTERVAL_KEY, str(value))
        changed["auto_sync_interval_seconds"] = value

    if body.auto_sync_enabled is not None:
        db.set_setting(
            SKILLS_AUTO_SYNC_ENABLED_KEY, "true" if body.auto_sync_enabled else "false"
        )
        changed["auto_sync_enabled"] = bool(body.auto_sync_enabled)

    if body.auto_reinject_enabled is not None:
        db.set_setting(
            SKILLS_AUTO_REINJECT_ENABLED_KEY,
            "true" if body.auto_reinject_enabled else "false",
        )
        changed["auto_reinject_enabled"] = bool(body.auto_reinject_enabled)

    if changed:
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"setting": "skills_library_automation", "changed": changed},
        )

    return {
        "success": True,
        "auto_sync_enabled": is_skills_auto_sync_enabled(),
        "auto_sync_interval_seconds": get_skills_auto_sync_interval(),
        "auto_reinject_enabled": is_skills_auto_reinject_enabled(),
        "changed": changed,
    }
@router.get("/proactive-rate-limits")
async def get_proactive_rate_limits_setting(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Effective proactive channel-message caps (#1609).

    Admin-only. The five agent-INITIATED anti-spam caps (Slack per-channel /
    per-agent, Telegram per-group / per-agent, proactive-DM per-recipient),
    per hour. ``0`` = unlimited. Inbound replies are never subject to these.
    """
    assert_admin(current_user)
    limits = {
        key: {
            "value": get_proactive_rate_limit(key),
            "default": default,
            "is_default": get_proactive_rate_limit(key) == default
                          and settings_service.get_setting(key) is None,
            "description": PROACTIVE_RATE_LIMIT_DESCRIPTIONS.get(key, ""),
        }
        for key, default in PROACTIVE_RATE_LIMIT_DEFAULTS.items()
    }
    return {"limits": limits, "max": PROACTIVE_RATE_LIMIT_MAX, "window_hours": 1}
@router.put("/proactive-rate-limits")
async def update_proactive_rate_limits_setting(
    body: ProactiveRateLimitsUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Set proactive channel-message caps (#1609).

    Admin-only. Only provided fields change; each must be an int in
    ``[0, MAX]`` (``0`` = unlimited, which is warned in the response). Named
    422 on bad input. Audit-logged. Runtime-resolved — no restart.
    """
    assert_admin(current_user)

    # Validate ALL provided fields BEFORE writing any — a mixed valid/invalid body
    # must be all-or-nothing (a 422 leaves every cap unchanged), not partially
    # applied.
    pending = {}
    for key in PROACTIVE_RATE_LIMIT_DEFAULTS:
        value = getattr(body, key, None)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > PROACTIVE_RATE_LIMIT_MAX:
            raise HTTPException(
                status_code=422,
                detail=f"{key} must be an integer between 0 and {PROACTIVE_RATE_LIMIT_MAX} (0 = unlimited)",
            )
        pending[key] = value

    updated, warnings = [], []
    for key, value in pending.items():
        db.set_setting(key, str(value))
        updated.append(key)
        if value == 0:
            warnings.append(f"{key} is now UNLIMITED — the anti-spam guardrail is disabled for it.")

    if updated:
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"setting": "proactive_rate_limits", "action": "update", "updated": updated},
        )

    return {
        "success": True,
        "updated": updated,
        "warnings": warnings,
        "limits": {key: get_proactive_rate_limit(key) for key in PROACTIVE_RATE_LIMIT_DEFAULTS},
    }
def _brain_orb_flag_state() -> Dict[str, Any]:
    """Per-flag effective value + source for the admin panel.

    `source` tells the UI whether a DB override is active — critical because a
    stored row makes the BRAIN_ORB_* env var silently dead until cleared:
    - "override": a system_settings row exists (env ignored)
    - "env":      no row; the env var opts the flag in
    - "default":  neither; the code default (OFF) applies
    """
    from services.settings_service import BRAIN_ORB_FLAGS

    state: Dict[str, Any] = {}
    for field, (setting_key, env_var) in BRAIN_ORB_FLAGS.items():
        stored = db.get_setting_value(setting_key, None)
        env_on = os.getenv(env_var, "").strip().lower() in ("true", "1", "yes")
        if stored is not None:
            value = str(stored).lower() in ("true", "1", "yes")
            source = "override"
        else:
            value = env_on
            source = "env" if env_on else "default"
        state[field] = {"value": value, "source": source}
    return state
@router.get("/brain-orb")
async def get_brain_orb_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the Brain Orb platform flags with per-flag source (trinity-enterprise#85).

    Admin-only. Registered before the `/{key}` catch-all (Invariant #4).
    `gemini_key_configured` reflects the env-only GEMINI_API_KEY secret the
    voice tile additionally requires (boolean only — never the key).
    """
    assert_admin(current_user)

    from config import GEMINI_API_KEY

    return {
        "flags": _brain_orb_flag_state(),
        "gemini_key_configured": bool(GEMINI_API_KEY),
    }
@router.put("/brain-orb")
async def update_brain_orb_settings(
    body: BrainOrbSettingsUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Update the Brain Orb platform flags (trinity-enterprise#85).

    Admin-only, audit-logged with per-flag old→new values. Partial update:
    only provided booleans are written. `clear: [flag,...]` deletes a flag's
    stored override, reverting it to its env/default value. Takes effect on
    the next request — route gates resolve at request time, no restart.
    """
    assert_admin(current_user)

    from config import GEMINI_API_KEY
    from services.settings_service import BRAIN_ORB_FLAGS

    clear = body.clear or []
    unknown = [f for f in clear if f not in BRAIN_ORB_FLAGS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown flag(s) in clear: {', '.join(sorted(unknown))}. "
                   f"Valid: {', '.join(BRAIN_ORB_FLAGS)}",
        )
    conflict = [f for f in clear if getattr(body, f, None) is not None]
    if conflict:
        raise HTTPException(
            status_code=400,
            detail=f"Flag(s) both set and cleared: {', '.join(sorted(conflict))}",
        )

    before = _brain_orb_flag_state()
    updated = []
    cleared = []
    # getattr default: a registry flag missing from the model must read as
    # "not provided", never AttributeError→500 (drift-guarded by test too).
    for field, (setting_key, _env_var) in BRAIN_ORB_FLAGS.items():
        value = getattr(body, field, None)
        if value is not None:
            db.set_setting(setting_key, "true" if value else "false")
            updated.append(field)
        elif field in clear:
            db.delete_setting(setting_key)
            cleared.append(field)
    after = _brain_orb_flag_state()

    if updated or cleared:
        # SEC-001: the write flag gates an exec-adjacent surface (the agent's
        # action hook), so every change must leave a per-flag old→new trace.
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={
                "setting": "brain_orb_flags",
                "action": "update",
                "changes": {
                    field: {
                        "old": before[field]["value"],
                        "new": after[field]["value"],
                    }
                    for field in updated + cleared
                },
                "cleared": cleared,
            },
        )

    return {
        "success": True,
        "updated": updated,
        "cleared": cleared,
        "flags": after,
        "gemini_key_configured": bool(GEMINI_API_KEY),
    }
def _elevenlabs_settings_state() -> dict:
    """Admin-panel view: key presence + source + default voice (never the key)."""
    return {
        "key_configured": bool(settings_service.get_elevenlabs_api_key()),
        "key_source": settings_service.elevenlabs_key_source(),
        "default_voice_id": settings_service.get_default_voice_id(),
    }
@router.get("/elevenlabs")
async def get_elevenlabs_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get ElevenLabs/voice platform settings (ent#117).

    Admin-only. Registered before the `/{key}` catch-all (Invariant #4). The API
    key is surfaced as `key_configured: bool` + `key_source` only — never echoed.
    """
    assert_admin(current_user)
    return _elevenlabs_settings_state()
@router.put("/elevenlabs")
async def update_elevenlabs_settings(
    body: ElevenLabsSettingsUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Set/clear the ElevenLabs API key + platform default voice (ent#117).

    Admin-only, audit-logged (key masked; default voice old→new). Key stored
    AES-256-GCM encrypted. Runtime-resolved — no restart. `clear: ["api_key",
    "default_voice_id"]` reverts to env/unset. A field may not be both set and cleared.
    """
    assert_admin(current_user)

    clear = body.clear or []
    valid_clear = {"api_key", "default_voice_id"}
    unknown = [f for f in clear if f not in valid_clear]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown field(s) in clear: {', '.join(sorted(unknown))}. "
                   f"Valid: {', '.join(sorted(valid_clear))}",
        )
    if "api_key" in clear and body.api_key is not None:
        raise HTTPException(status_code=400, detail="api_key both set and cleared")
    if "default_voice_id" in clear and body.default_voice_id is not None:
        raise HTTPException(status_code=400, detail="default_voice_id both set and cleared")

    before = _elevenlabs_settings_state()
    changes = {}

    if body.api_key is not None:
        key = body.api_key.strip()
        if not key:
            raise HTTPException(status_code=400, detail="api_key must not be empty (use clear instead)")
        settings_service.set_elevenlabs_api_key(key)
        changes["api_key"] = "set"
    elif "api_key" in clear:
        settings_service.clear_elevenlabs_api_key()
        changes["api_key"] = "cleared"

    if body.default_voice_id is not None:
        voice = body.default_voice_id.strip()
        if voice:
            settings_service.set_default_voice_id(voice)
        else:
            settings_service.clear_default_voice_id()
        changes["default_voice_id"] = {"old": before["default_voice_id"], "new": voice or None}
    elif "default_voice_id" in clear:
        settings_service.clear_default_voice_id()
        changes["default_voice_id"] = {"old": before["default_voice_id"], "new": None}

    if changes:
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={
                "setting": "elevenlabs",
                "action": "update",
                # Key value never logged — only whether it was set/cleared.
                "changes": changes,
            },
        )

    after = _elevenlabs_settings_state()
    return {"success": True, **after}
@router.get("/a2a-endpoints")
async def list_a2a_outbound_endpoints(
    current_user: User = Depends(get_current_user)
):
    """List the registered outbound A2A endpoints (#736).

    Credentials are never returned — each row reports `has_credentials` only,
    matching the write-only property the A2A management tools already document.

    Reads the OSS store directly rather than through the resolver seam: a
    registered enterprise provider may legitimately answer a different question
    ("what can THIS agent call?"), and an admin panel whose GET and PUT
    addressed different stores would be worse than no panel.
    """
    from dependencies import reject_agent_principal

    assert_admin(current_user)
    reject_agent_principal(current_user)

    from services import a2a_outbound, a2a_outbound_service

    return {
        "endpoints": a2a_outbound.list_oss_endpoints(),
        "enabled": a2a_outbound_service.is_outbound_enabled(),
    }
@router.put("/a2a-endpoints")
async def upsert_a2a_outbound_endpoint(
    body: A2AOutboundEndpointUpsert,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Register or update one outbound A2A endpoint by name (#736).

    The URL is SSRF-validated here so an operator finds out immediately rather
    than at first call — but that is a usability improvement, not the security
    boundary: the call path re-validates every URL on every call regardless,
    because a stored row is not trusted and a DNS record can move.

    `credentials` is write-only. Omitting it on an update leaves any existing
    secret in place; `clear_credentials` removes it. The audit row records
    whether a credential was set or cleared, never its value.
    """
    from dependencies import reject_agent_principal

    assert_admin(current_user)
    reject_agent_principal(current_user)

    from services import a2a_outbound

    credential = body.credentials.get_secret_value() if body.credentials else None
    try:
        record = a2a_outbound.upsert_endpoint(
            body.name,
            body.url,
            credential,
            clear_credential=body.clear_credentials,
        )
    except a2a_outbound.EndpointValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="settings_change",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={
            "setting": "a2a_outbound_endpoints",
            "action": "upsert",
            "endpoint": record["name"],
            "url": record["url"],
            "credential_set": bool(credential),
            "credential_cleared": bool(body.clear_credentials),
        },
    )
    return {"success": True, "endpoint": record}
@router.delete("/a2a-endpoints/{ref}")
async def remove_a2a_outbound_endpoint(
    ref: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Remove one registered outbound A2A endpoint by id or name (#736)."""
    from dependencies import reject_agent_principal

    assert_admin(current_user)
    reject_agent_principal(current_user)

    from services import a2a_outbound

    removed = a2a_outbound.remove_endpoint(ref)
    if not removed:
        raise HTTPException(status_code=404, detail=f"A2A endpoint '{ref}' not found")

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="settings_change",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={
            "setting": "a2a_outbound_endpoints",
            "action": "remove",
            "endpoint": ref,
        },
    )
    return {"success": True, "removed": ref}
