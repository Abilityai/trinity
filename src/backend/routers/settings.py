# mcp: none — platform admin settings — a grant surface, human-only (Invariant #8 grant-vs-use)
"""
System settings routes for the Trinity backend.

Provides endpoints for managing system-wide configuration like the Trinity prompt.
Admin-only access for modification, read access for all authenticated users.
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

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ============================================================================
# API Keys Management - Helper Functions
# ============================================================================


# Note: get_anthropic_api_key and get_github_pat are now imported from
# services.settings_service for proper architecture (services shouldn't import from routers)


def mask_api_key(key: str) -> str:
    """Mask an API key for display, showing only last 4 characters."""
    if not key or len(key) < 8:
        return "****"
    return f"...{key[-4:]}"


# ============================================================================
# Ops Settings Configuration
# ============================================================================

# Note: OPS_SETTINGS_DEFAULTS and OPS_SETTINGS_DESCRIPTIONS are now imported from
# services.settings_service for proper architecture


@router.get("", response_model=List[SystemSetting])
async def get_all_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get all system settings.

    Admin-only endpoint to view all configuration values.
    """
    assert_admin(current_user)

    try:
        settings = db.get_all_settings()

        return settings
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get settings: {str(e)}")


@router.get("/feature-flags")
async def get_public_feature_flags(
    current_user: User = Depends(get_current_user)
):
    """
    Public-safe feature flags for the authenticated user.

    Unlike ``/api/settings/{key}`` (admin-only), this endpoint exposes a
    curated allowlist of UI-relevant flags so the frontend can decide
    which optional surfaces to render. Currently:

    - ``session_tab_enabled`` — gates the Session tab in AgentDetail.
      Reads through ``services.settings_service.is_session_tab_enabled()``
      so the resolution order (DB → env → False default) stays in one place.
    - ``workspace_available`` — gates the Agent Workspace (voice + canvas).
      Requires both voice infrastructure (VOICE_ENABLED + GEMINI_API_KEY) AND
      ``WORKSPACE_ENABLED=true`` (or DB override). Defaults to False (#860).

    Auth required (any role) — these flags reveal nothing sensitive but we
    still keep them out of the unauthenticated surface.
    """
    from config import (
        GEMINI_API_KEY,
        VOICE_ENABLED,
        VOIP_ENABLED,
        MCP_AGENT_CHAT_PULL_ENABLED,
        MCP_INLINE_AUTH_ENABLED,
        REDELIVERY_GOVERNOR_ENABLED,
    )
    from services.entitlement_service import entitlement_service
    from services import a2a_outbound_service
    # Function-local (#2217): a top-level import would pull the whole canary
    # package into the settings-router load; the handler pattern here is
    # function-local imports.
    from services.canary_service import canary_service
    voice_available = VOICE_ENABLED and bool(GEMINI_API_KEY)
    # Brain Orb flags are RUNTIME-RESOLVED (#85): system_settings override →
    # BRAIN_ORB_* env opt-in → OFF. An admin flip via PUT /api/settings/brain-orb
    # is reflected here without a backend restart.
    brain_orb_enabled = settings_service.is_brain_orb_enabled()
    return {
        "session_tab_enabled": settings_service.is_session_tab_enabled(),
        "voice_available": voice_available,
        "workspace_available": voice_available and settings_service.is_workspace_enabled(),
        # VoIP telephony (VOIP-001, #1056) — default OFF, mirrors workspace_available.
        # Also requires a per-agent voip_bindings row to actually function.
        "voip_available": VOIP_ENABLED and bool(GEMINI_API_KEY),
        # Brain Orb (#58, trinity-enterprise) — gates the per-agent /agents/:name/brain
        # route + tab. Static render needs no Gemini; the per-agent capability gate is
        # the template.yaml `brain-orb` token, checked frontend-side.
        "brain_orb_available": brain_orb_enabled,
        # Brain Orb voice tile (#58 Phase 3, trinity-enterprise#60) — client-held
        # Gemini Live. Composes base AND voice AND a Gemini key (#85 — with base OFF
        # the orb is down, so voice must read unavailable too; mirrors the write
        # composition below). The frontend un-hides the voice tile only when this is
        # on AND the agent carries the `brain-orb` capability. Default OFF.
        "brain_orb_voice_available": brain_orb_enabled
        and settings_service.is_brain_orb_voice_enabled()
        and bool(GEMINI_API_KEY),
        # Brain Orb KB-write surface (#58 Phase 4a, trinity-enterprise#61) — owner-gated
        # capture/link. DISTINCT kill-switch from brain_orb_available so writes can be
        # disabled without downing read/voice. UI-only hint (the write routes independently
        # enforce the flag + owner gate); the orb only attempts initActions when on. The
        # per-agent gate is still owner + the agent shipping a `brain-orb/action` hook.
        # run_skill + the transcript pipeline are Phase 4b (#66). Default OFF.
        "brain_orb_write_available": settings_service.is_brain_orb_write_enabled()
        and brain_orb_enabled,
        # Pull-pilot routing for agent→agent MCP chat (#946) — default OFF.
        # Observability-only here: the routing gate is the MCP server's own read
        # of the same env var. Lets an operator confirm, via the API, whether the
        # treatment window is active during the soak. NOT a UI surface.
        "mcp_agent_chat_pull_enabled": MCP_AGENT_CHAT_PULL_ENABLED,
        # MCP inline email auth (#848) — default OFF. Observability-only here,
        # mirroring the two flags around it: the real gates are the mcp-server's
        # own read of the same env var (the keyless session tier) and
        # require_inline_auth_enabled on /api/internal/mcp-auth/*. Both processes
        # read ONE key, so this is also how an operator confirms the two halves
        # actually agree after a deploy — the failure mode that shipped this PR
        # with the flag wired into neither container. NOT a UI surface.
        "mcp_inline_auth_enabled": MCP_INLINE_AUTH_ENABLED,
        # Re-delivery governor (#1085) — default OFF. Observability-only here:
        # the actual gates live at the callback endpoint / reaper / drain read
        # points. Lets an operator confirm via the API whether the correlated-
        # failure controls are armed during a soak. NOT a UI surface.
        "redelivery_governor_enabled": REDELIVERY_GOVERNOR_ENABLED,
        # Canary run-state (#2217) — observability-only boolean, beside the
        # other observability flags; any authed user, public-safe. Whether
        # the canary harness is enabled. Last-cycle/stale/sink detail stays
        # admin-only on GET /api/canary/status.
        "canary_enabled": canary_service.is_enabled(),
        # Outbound voice replies (ent#117) — true when an ElevenLabs key resolves
        # (stored setting → ELEVENLABS_API_KEY env). Gates the agent-level Voice
        # config UI + the send_voice_reply capability. Non-sensitive boolean.
        "tts_available": bool(settings_service.get_elevenlabs_api_key()),
        # Outbound A2A (#736) — the kill switch on the platform's first
        # backend-executed, credentialed, agent-triggerable outbound fetcher.
        # Runtime-resolved (system_settings → A2A_OUTBOUND_ENABLED env → OFF);
        # both call routes 404 when off. Observability + UI gating only — the
        # routes enforce it themselves.
        "a2a_outbound_available": a2a_outbound_service.is_outbound_enabled(),
        "platform_default_model": settings_service.get_platform_default_model(),
        # Onboarding (trinity-enterprise#52) — is Claude auth configured at all?
        # Trinity agents can't think without it, so the first-run wizard uses
        # this to surface the one hard setup gate. True if a platform-wide
        # Anthropic key exists (DB or env) OR any Claude subscription is
        # registered. Non-sensitive: a boolean, never the key itself.
        "claude_auth_configured": bool(settings_service.get_anthropic_api_key())
        or db.has_any_subscription(),
        # #847 Phase 0 — enterprise entitlements. Empty list means OSS
        # build (or TRINITY_OSS_ONLY=1). UI uses this to hide
        # enterprise-only tabs cleanly without server-side conditional
        # rendering. Mirrors the deny-list pattern of the other flags.
        "enterprise_features": entitlement_service.list_entitled_features(),
        # ent#12 Tier-2 opt-in sharing — observability only (the egress gate is
        # the stored consent + config switch). Default-off; the UI reads it to
        # show the sharing state without a second round-trip. Non-sensitive bool.
        "telemetry_sharing_enabled": telemetry_sharing_service.is_consent_enabled(),
    }


@router.get("/telemetry-sharing")
async def get_telemetry_sharing(current_user: User = Depends(get_current_user)):
    """Tier-2 opt-in sharing status + an inspectable preview of the exact
    anonymized payload that would be sent (ent#12). Admin-only. Local read — no
    egress. The preview lets the operator see precisely what is shared before
    consenting (AC: payload documented and inspectable before send)."""
    assert_admin(current_user)
    status = telemetry_sharing_service.get_status()
    # Preview over the configured backfill window — what a consent-time share
    # would contain. Coarse aggregates only; never any PII.
    status["payload_preview"] = telemetry_sharing_service.build_aggregate_payload(
        window_days=status.get("backfill_days"), backfill=True
    )
    return status


@router.put("/telemetry-sharing")
async def set_telemetry_sharing(
    body: TelemetrySharingUpdate,
    current_user: User = Depends(get_current_user),
):
    """Set (or revoke) the Tier-2 sharing consent (ent#12). Admin + human-only.
    Default-off, reversible. On enable, an immediate backfill share is scheduled
    (fire-and-forget) so the first send includes the disclosed history window;
    disabling stops egress at the next heartbeat. Audit-logged."""
    from dependencies import reject_agent_principal
    assert_admin(current_user)
    reject_agent_principal(current_user)

    if telemetry_sharing_service.is_hard_disabled() and body.enabled:
        raise HTTPException(
            status_code=409,
            detail="Telemetry sharing is disabled by configuration "
            "(TELEMETRY_SHARING_ENABLED / DO_NOT_TRACK); consent cannot enable egress.",
        )

    was_enabled = telemetry_sharing_service.is_consent_enabled()
    status = telemetry_sharing_service.set_consent(
        body.enabled, backfill_days=body.backfill_days
    )

    try:
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="telemetry_sharing_consent",
            source="api",
            actor_user=current_user,
            details={"enabled": body.enabled, "backfill_days": status.get("backfill_days")},
        )
    except Exception:  # audit is best-effort
        logger.debug("[telemetry-share] audit log failed", exc_info=True)

    # Consent-time backfill: only on the off→on transition, fire-and-forget.
    if body.enabled and not was_enabled:
        asyncio.create_task(telemetry_sharing_service.share_now(backfill=True))

    return status


@router.get("/operator-intake")
async def get_operator_intake(current_user: User = Depends(get_current_user)):
    """Operator-intake Settings status (ent#463).

    Admin-only. The status is honest across the three orthogonal axes the panel
    renders: `hard_disabled` (env kill), `already_submitted` (+ `submitted_at`),
    and `enabled` (durable consent flag). A legacy install that had the marker
    set before ent#463 shipped reports `already_submitted=true` with
    `submitted_at=None`; the panel renders "date unknown" rather than lying.
    """
    assert_admin(current_user)
    return operator_intake_service.get_status()


@router.put("/operator-intake")
async def set_operator_intake(
    body: OperatorIntakeUpdate,
    current_user: User = Depends(get_current_user),
):
    """Set or revoke the operator-intake consent (ent#463). Admin + human-only.

    Three intents share the endpoint (see `OperatorIntakeUpdate`):

    * ``enabled=true`` + ``email`` on a fresh install → durable consent recorded
      AND the at-most-once intake POST is scheduled as a background task
      (converges on the same ``submit_operator_intake`` path the welcome form
      uses, AC #3).
    * ``enabled=true`` on a submitted install → consent recorded, no re-send
      (at-most-once marker preserved, AC #4 = no-op).
    * ``enabled=false`` → durable decline recorded; the submitted marker is NOT
      rolled back (AC #5: retracting the record itself requires contacting
      support — the hosted endpoint has no local delete authority).

    ``OPERATOR_INTAKE_ENABLED=false`` / ``DO_NOT_TRACK=1`` continue to win over
    this Settings control (AC #6): an attempted enable-and-submit while
    hard-disabled returns 409 rather than silently failing.

    Audit-logged with a distinct action so a Settings-driven consent change is
    distinguishable from a first-run one in the audit log.
    """
    from dependencies import reject_agent_principal
    assert_admin(current_user)
    reject_agent_principal(current_user)

    # Hard-disabled 409 mirrors the telemetry-sharing shape (ent#12). A silent
    # accept-then-drop would fail AC #2 (state shown honestly) — the panel is
    # entitled to a distinguishable error to render the disabled banner.
    submit_intent = bool(
        body.enabled
        and (body.email or "").strip()
        and not operator_intake_service.is_already_submitted()
    )
    if operator_intake_service.is_hard_disabled() and submit_intent:
        raise HTTPException(
            status_code=409,
            detail=(
                "Operator intake is disabled by configuration "
                "(OPERATOR_INTAKE_ENABLED / DO_NOT_TRACK); consent cannot submit."
            ),
        )

    was_enabled = operator_intake_service.is_consent_enabled()
    status = operator_intake_service.set_consent(body.enabled)

    # Fire the at-most-once submission only on the fresh consent-and-email path.
    # If the install has already submitted, we deliberately no-op (AC #4). If the
    # operator is opting OUT, no submission fires. If they enable without an
    # email, we record consent as intent but nothing goes out — a subsequent PUT
    # with an email will complete the submission, still at-most-once.
    submit_outcome = None
    if submit_intent:
        submit_outcome = await operator_intake_service.submit_from_settings(
            email=body.email,
            company=body.company,
            name=body.name,
            role=body.role,
            use_case=body.use_case,
        )
        # Refresh status after the fire so the response reflects the submitted
        # marker + timestamp the caller's UI needs to switch to terminal state.
        status = operator_intake_service.get_status()

    try:
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="operator_intake_consent",
            source="api",
            actor_user=current_user,
            details={
                "enabled": body.enabled,
                "was_enabled": was_enabled,
                "submit_outcome": submit_outcome,
                # Never log the email or profile fields — the local no-PII
                # invariant matches the service's rule.
                "has_email": bool((body.email or "").strip()),
            },
        )
    except Exception:  # audit is best-effort
        logger.debug("[operator-intake] audit log failed", exc_info=True)

    if submit_outcome is not None:
        status["submit_outcome"] = submit_outcome
    return status


@router.post("/retention/acknowledge")
async def acknowledge_retention_prune(
    body: RetentionAcknowledge,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Approve one over-threshold retention prune (#1644).

    THIS ENDPOINT IS THE GATE. The operator-queue alarm the guard raises is
    informational only — responding to it authorizes nothing. That split is
    deliberate: the queue item is reachable by principals that must never be able
    to approve a mass deletion of their own audit trail, and it lives in a table
    one of the guarded sweeps prunes.

    Human-only. Admin-role alone is NOT sufficient today: an agent-scoped MCP
    key resolves to its owner *carrying the owner's role*, so on an install whose
    agents are admin-owned (the default — see cornelius_agent_service.CORNELIUS_OWNER)
    an agent key passes the admin check. `reject_agent_principal` is therefore
    applied explicitly here. See abilityai/trinity-ops-agent#232 for the
    underlying fix.

    The ack is bound to `window_days`: approving a prune at 30 days does not
    approve one at 1 day. It is single-use — `cleanup_service` consumes it once the
    prune has actually run, so the guard re-arms.
    """
    # Imported in-function: several suites stub `dependencies`, and a
    # module-level import of a newer symbol breaks them (matches this file's
    # existing in-function import style).
    from dependencies import reject_agent_principal

    # #1709: was `require_admin(current_user)` — a NameError (only `assert_admin`
    # is imported here; `require_admin` is a FastAPI Depends factory, not an
    # imperative call). The #1310 auth-wiring refactor left the endpoint 500ing on
    # every request, so the guard's approval path never worked even for a caller.
    assert_admin(current_user)
    reject_agent_principal(current_user)

    from services.retention_guard import record_acknowledgement
    from services.settings_service import OPS_SETTINGS_DEFAULTS

    if body.key not in OPS_SETTINGS_DEFAULTS:
        raise HTTPException(
            status_code=422, detail=f"unknown retention setting: {body.key}"
        )

    # Bind the ack to the window actually in force right now, not to whatever the
    # caller says. Otherwise an operator could be socially-engineered into acking a
    # window that isn't the one about to run, and the guard would honour it.
    effective_raw = db.get_setting_value(
        body.key, OPS_SETTINGS_DEFAULTS.get(body.key, "0")
    )
    try:
        effective = max(int(effective_raw), 0)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=f"{body.key} currently holds a non-integer value; fix it first",
        )
    if effective != body.window_days:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{body.key} is currently {effective} days, not {body.window_days}. "
                "Re-read the alarm and acknowledge the window in force."
            ),
        )

    record_acknowledgement(body.key, effective)
    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="retention_prune_acknowledged",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"key": body.key, "window_days": effective},
    )
    logger.warning(
        "[#1644] %s acknowledged an over-threshold retention prune for %s at "
        "%d days — the next cleanup cycle will delete.",
        current_user.username, body.key, effective,
    )
    return {"success": True, "key": body.key, "window_days": effective}


@router.get("/portal-session-policy")
async def get_portal_session_policy_status(current_user: User = Depends(get_current_user)):
    """The Workspace session policy actually in force (ent#375).

    READ lives in OSS and is available in EVERY edition, mirroring
    ``GET /api/settings/retention`` (#1039): the sliding session itself is OSS —
    every install slides on the shipped defaults — so every install's operator is
    entitled to see the windows their clients are subject to. Only the *setter*
    is entitled (``PUT /api/enterprise/portal-session-policy``).

    Gating the read too, which the first cut of the enterprise module did, would
    have left a community operator unable to see a policy that is nonetheless
    enforcing on them — a security control they cannot inspect. That is worse
    than not shipping the panel.

    ``sources`` distinguishes ``db-row`` (an operator chose this) from
    ``code-default`` (the shipped value, which a future default change can move
    under them) — the #1638 distinction.

    Admin-only.
    """
    assert_admin(current_user)

    from config import (
        PORTAL_SESSION_MAX_ABSOLUTE_DAYS,
        PORTAL_SESSION_MIN_IDLE_MINUTES,
    )
    from services.entitlement_service import entitlement_service
    from services.settings_service import settings_service

    idle_s, absolute_s = settings_service.get_portal_session_policy()

    def _source(key: str) -> str:
        return "db-row" if db.get_setting_value(key, None) is not None else "code-default"

    return {
        "idle_days": round(idle_s / 86400.0, 4),
        "absolute_days": round(absolute_s / 86400.0, 4),
        "sources": {
            "portal_session_idle_days": _source("portal_session_idle_days"),
            "portal_session_absolute_days": _source("portal_session_absolute_days"),
        },
        "min_idle_minutes": PORTAL_SESSION_MIN_IDLE_MINUTES,
        "max_absolute_days": PORTAL_SESSION_MAX_ABSOLUTE_DAYS,
        "editable": "portal_session_policy" in entitlement_service.list_entitled_features(),
    }


@router.get("/retention")
async def get_retention_status(
    current_user: User = Depends(get_current_user),
):
    """Effective data-retention windows actually in use, plus the active
    edition (#1039).

    Reports the value resolved for each operator-tunable class — log archival
    (env LOG_*), execution log/row, health-check, and agent/schedule
    soft-delete (OPS settings, DB-row → code-default precedence) — and the
    audit-log window (separate 365-day integrity floor, exempt from the
    community floor).

    ``edition`` is ``enterprise`` when the ``retention`` entitlement is present
    (license-driven once #1040 lands; registry-driven today) and ``community``
    otherwise. The 5-day community floor is applied by SEEDING a fresh install's
    rows (#1638) — it is not a clamp, and OSS does not enforce it: any admin may
    widen a window via ``PUT /api/settings/ops/config``. The enterprise module is
    the managed, supported surface (audit, ``updated_by``, hot-reload).

    ``source`` per key is ``db-row`` when an explicit setting exists and
    ``code-default`` when the value is the fallback — the distinction that made
    #1638 invisible (a ``code-default`` window is one a default change can move
    under the operator's feet).

    Admin-only.
    """
    assert_admin(current_user)

    from services.entitlement_service import entitlement_service
    from services.settings_service import (
        COMMUNITY_RETENTION_FLOOR_DAYS,
        RETENTION_OPS_KEYS,
    )

    def _ops_int(key: str) -> int:
        raw = db.get_setting_value(key, OPS_SETTINGS_DEFAULTS.get(key, "0"))
        try:
            return max(int(raw), 0)
        except (TypeError, ValueError):
            return 0

    def _ops_source(key: str) -> str:
        return "db-row" if db.get_setting_value(key, None) is not None else "code-default"

    entitled = entitlement_service.is_entitled("retention")
    audit_days = max(int(os.getenv("AUDIT_LOG_RETENTION_DAYS", "365") or 365), 365)

    # #1644: the guard's threshold is a fixed constant, not an operator setting —
    # reported here for visibility only.
    from services.retention_guard import (
        MAX_ROWS_PER_SWEEP,
        FLOOR_AGENTS,
        FLOOR_SCHEDULES,
        evaluate as _guard_evaluate,
    )

    # #1709: surface the sweeps a cleanup cycle would REFUSE right now, so the
    # panel can offer an approve control. We re-run the guard live (the exact
    # logic + count fns cleanup_service uses) rather than reading stale state or
    # coupling to the operator queue — the result is always fresh and cannot
    # show a "pending" that's already been acknowledged or pruned. Only the two
    # low-floor, irreversible sweeps are ack-gated in practice; the agent purge
    # (floor 0) is the one #1581 depends on. `limit` is bounded to floor+1, so
    # each check counts at most a handful of rows.
    _ack_sweeps = (
        ("agent_soft_delete_retention_days",
         "Soft-deleted agents (this destroys each agent's workspace/public/shared Docker volumes — irreversible)",
         FLOOR_AGENTS, db.count_soft_deleted_agents_past_retention),
        ("schedule_soft_delete_retention_days",
         "Soft-deleted schedules",
         FLOOR_SCHEDULES, db.count_soft_deleted_schedules_past_retention),
    )
    pending_acknowledgements = []
    blocked_sweeps = []
    for _key, _label, _floor, _count_fn in _ack_sweeps:
        _window = _ops_int(_key)
        if _window <= 0:
            continue  # sweep disabled → nothing to prune, nothing to approve
        _verdict = _guard_evaluate(
            _key, _window,
            lambda limit, _cf=_count_fn, _w=_window: _cf(_w, limit),
            floor=_floor,
        )
        # Only "over_threshold" is a genuine pending-approval. `count_failed` /
        # `count_uninterpretable` / `count_negative` / `ack_lookup_failed` are
        # fail-closed error states, not approvable, and an already-acked sweep
        # returns allowed=True (so it drops off the list — the single-use,
        # no-stale-state guarantee the panel needs).
        if not _verdict.allowed and _verdict.reason == "over_threshold":
            pending_acknowledgements.append({
                "key": _key,
                "label": _label,
                "window_days": _window,
                "candidate_count": _verdict.candidates,
                "floor": _floor,
            })
        elif not _verdict.allowed:
            # #1833: NOT approvable is not the same as NOT worth showing. Before
            # #1833 an uninterpretable count raised out of `evaluate` and took
            # this whole endpoint down with a 500 — ugly, but loud. Now it
            # refuses, so without this the sweep is blocked forever in
            # `cleanup_service` while the panel renders a clean "nothing
            # pending" — the guard's own anti-pattern ("a guard that fails open
            # manufactures confidence") relocated from the prune to the operator
            # surface. Identifiers and reason codes ONLY, the same SECURITY rule
            # as the alarm payload: no counts of row content, no sample rows.
            blocked_sweeps.append({
                "key": _key,
                "window_days": _window,
                "reason": _verdict.reason,
            })

    return {
        "edition": "enterprise" if entitled else "community",
        "community_floor_days": COMMUNITY_RETENTION_FLOOR_DAYS,
        # #1638: the five OPS windows resolve DB-row → code-default. There is NO
        # env layer for them (the previously advertised
        # "enterprise → env → community-default" was never implemented — grep for
        # EXECUTION_ROW_RETENTION_DAYS et al: zero reads). Only log archival is
        # env-driven. Claiming an escape hatch that does not exist is what left
        # operators with no way to pre-empt #1638.
        "precedence": "db-row → code-default (OPS windows); env (log archival only)",
        "sources": {k: _ops_source(k) for k in RETENTION_OPS_KEYS},
        # #1644 blast-radius guard. Reported separately from `windows` because it
        # is not a retention window — it is the threshold above which a prune is
        # refused pending an explicit acknowledgement. Editable in EVERY edition
        # (unlike the windows, whose write path is entitlement-gated): it is a
        # safety mechanism, not a paid feature.
        "guard": {
            "max_rows": MAX_ROWS_PER_SWEEP,
            "agents_always_require_acknowledgement": True,
        },
        # #1709: sweeps a cleanup cycle would refuse right now, awaiting an admin
        # ack via POST /api/settings/retention/acknowledge. Empty ⇒ nothing pending.
        "pending_acknowledgements": pending_acknowledgements,
        # #1833: sweeps the guard is refusing for a reason this panel cannot
        # offer an approve control for (the count failed / could not be
        # interpreted / was a negative error sentinel, or the ack lookup itself
        # failed). Blocked, not pending. SCOPE: the same two ack-gated sweeps
        # `_ack_sweeps` re-runs above — the other six windows are not evaluated
        # here at all, so a refusal on those reaches an operator only through the
        # durable operator-queue alarm `cleanup_service` raises.
        "blocked_sweeps": blocked_sweeps,
        "windows": {
            # Log archival (env-driven; LOG_* escape hatch)
            "log_retention_days": int(os.getenv("LOG_RETENTION_DAYS", "5")),
            "log_archive_enabled": os.getenv("LOG_ARCHIVE_ENABLED", "true").lower() == "true",
            # Execution + health + soft-delete (OPS settings, 0 = disabled).
            # #2216: `backup_retention_days` is EXCLUDED here — _ops_int's
            # garbage→0 coercion means "sweep disabled" for row windows but
            # "keep backups forever" (the #1871 disk-fill trap) for backups,
            # so on a malformed stored row this map and the backup service
            # would disagree inside ONE response. The key is reported only in
            # the `backup` block below, through the service's own inverted
            # reader (garbage → 14). Pinned by
            # tests/unit/test_2216_backup_observability.py.
            **{
                k: _ops_int(k)
                for k in RETENTION_OPS_KEYS
                if k != "backup_retention_days"
            },
            # Audit log — exempt from the community floor (365-day integrity floor)
            "audit_log_retention_days": audit_days,
        },
        # #2216: automatic database-backup status (BKUP-014) — durable
        # system_settings keys + a live /data/backups listing, rendered by
        # the one shared reader. `scope: "same-disk"` is the machine-readable
        # boundary statement (protects against corruption/slips, not disk loss).
        "backup": await _backup_block(),
    }


async def _backup_block():
    """Backup status for GET /retention — fail-soft: a broken block must not
    take down the whole retention panel."""
    try:
        from services.db_backup_service import build_backup_status_block
        return await build_backup_status_block()
    except Exception as e:
        logger.error(f"Could not build backup status block: {e}")
        return {"error": "unavailable"}


# ============================================================================
# API Keys Management Endpoints
# NOTE: These routes MUST be defined BEFORE the /{key} catch-all route
# ============================================================================

@router.get("/api-keys")
async def get_api_keys_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get status of configured API keys.

    Admin-only. Returns masked key info for security.
    """
    assert_admin(current_user)

    try:
        # Get Anthropic key
        anthropic_key = get_anthropic_api_key()
        anthropic_configured = bool(anthropic_key)

        # Check if it's from settings or env
        key_from_settings = has_secret_setting('anthropic_api_key')

        # Get GitHub PAT
        github_pat = get_github_pat()
        github_configured = bool(github_pat)
        github_from_settings = has_secret_setting('github_pat')

        return {
            "anthropic": {
                "configured": anthropic_configured,
                "masked": mask_api_key(anthropic_key) if anthropic_configured else None,
                "source": "settings" if key_from_settings else ("env" if anthropic_configured else None)
            },
            "github": {
                "configured": github_configured,
                "masked": mask_api_key(github_pat) if github_configured else None,
                "source": "settings" if github_from_settings else ("env" if github_configured else None)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get API keys status: {str(e)}")


@router.put("/api-keys/anthropic")
async def update_anthropic_key(
    body: ApiKeyUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set or update the Anthropic API key.

    Admin-only. Key is stored in system settings.
    """
    assert_admin(current_user)

    try:
        # Validate format
        key = body.api_key.strip()
        if not key.startswith('sk-ant-'):
            raise HTTPException(
                status_code=400,
                detail="Invalid API key format. Anthropic keys start with 'sk-ant-'"
            )

        # Store in settings — AES-256-GCM encrypted at rest (ent#435)
        set_secret_setting('anthropic_api_key', key)

        # SEC-001: audit API key change
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"setting": "anthropic_api_key", "action": "update"},
        )

        return {
            "success": True,
            "masked": mask_api_key(key)
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update API key: {str(e)}")


@router.delete("/api-keys/anthropic")
async def delete_anthropic_key(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Delete the Anthropic API key from settings.

    Admin-only. Will fall back to env var if configured.
    """
    assert_admin(current_user)

    try:
        deleted = clear_secret_setting('anthropic_api_key')

        # SEC-001: audit API key deletion
        if deleted:
            await platform_audit_service.log(
                event_type=AuditEventType.CONFIGURATION,
                event_action="settings_change",
                source="api",
                actor_user=current_user,
                actor_ip=request.client.host if request.client else None,
                endpoint=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
                details={"setting": "anthropic_api_key", "action": "delete"},
            )

        # Check if env var fallback exists
        env_key = os.getenv('ANTHROPIC_API_KEY', '')

        return {
            "success": True,
            "deleted": deleted,
            "fallback_configured": bool(env_key)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete API key: {str(e)}")


@router.post("/api-keys/anthropic/test")
async def test_anthropic_key(
    body: ApiKeyTest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Test if an Anthropic API key is valid.

    Admin-only. Makes a lightweight API call to validate the key.
    """
    assert_admin(current_user)

    try:
        key = body.api_key.strip()

        # Validate format first
        if not key.startswith('sk-ant-'):
            return {
                "valid": False,
                "error": "Invalid format. Anthropic keys start with 'sk-ant-'"
            }

        # Make a lightweight API call to test the key
        # Using the models endpoint which is simple and doesn't create any resources
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01"
                },
                timeout=10.0
            )

            if response.status_code == 200:
                return {"valid": True}
            elif response.status_code == 401:
                return {
                    "valid": False,
                    "error": "Invalid API key"
                }
            else:
                return {
                    "valid": False,
                    "error": f"API returned status {response.status_code}"
                }

    except httpx.TimeoutException:
        return {
            "valid": False,
            "error": "Request timed out. Please try again."
        }
    except Exception as e:
        return {
            "valid": False,
            "error": f"Error testing key: {str(e)}"
        }


@router.put("/api-keys/github")
async def update_github_pat(
    body: ApiKeyUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set or update the GitHub Personal Access Token.

    Admin-only. Token is stored in system settings and auto-propagated to all
    running agents that currently use the global PAT (#211).
    """
    assert_admin(current_user)

    try:
        # Validate format
        key = body.api_key.strip()
        if not (key.startswith('ghp_') or key.startswith('github_pat_')):
            raise HTTPException(
                status_code=400,
                detail="Invalid token format. GitHub PATs start with 'ghp_' or 'github_pat_'"
            )

        # Store in settings
        set_secret_setting('github_pat', key)

        # Auto-propagate to running agents (#211). Never block the PAT save on
        # propagation failures — the token is already persisted.
        from services.github_pat_propagation_service import propagate_github_pat
        try:
            propagation = await propagate_github_pat(key)
            propagation_payload: Dict[str, Any] = propagation.model_dump()
        except Exception as e:
            logger.exception("GitHub PAT propagation failed")
            propagation_payload = {"error": f"Propagation failed: {str(e)}"}

        # SEC-001: audit GitHub PAT change
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"setting": "github_pat", "action": "update"},
        )

        return {
            "success": True,
            "masked": mask_api_key(key),
            "propagation": propagation_payload,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update GitHub PAT: {str(e)}")


@router.delete("/api-keys/github")
async def delete_github_pat(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Delete the GitHub PAT from settings.

    Admin-only. Will fall back to env var if configured.
    """
    assert_admin(current_user)

    try:
        deleted = clear_secret_setting('github_pat')

        # SEC-001: audit GitHub PAT deletion
        if deleted:
            await platform_audit_service.log(
                event_type=AuditEventType.CONFIGURATION,
                event_action="settings_change",
                source="api",
                actor_user=current_user,
                actor_ip=request.client.host if request.client else None,
                endpoint=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
                details={"setting": "github_pat", "action": "delete"},
            )

        # Check if env var fallback exists
        env_key = os.getenv('GITHUB_PAT', '')

        return {
            "success": True,
            "deleted": deleted,
            "fallback_configured": bool(env_key)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete GitHub PAT: {str(e)}")


@router.post("/api-keys/github/test")
async def test_github_pat(
    body: ApiKeyTest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Test if a GitHub PAT is valid.

    Admin-only. Makes a lightweight API call to validate the token.
    """
    assert_admin(current_user)

    try:
        key = body.api_key.strip()

        # Validate format first
        if not (key.startswith('ghp_') or key.startswith('github_pat_')):
            return {
                "valid": False,
                "error": "Invalid format. GitHub PATs start with 'ghp_' or 'github_pat_'"
            }

        # Make a lightweight API call to test the token
        # Using the user endpoint which is simple and doesn't create any resources
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28"
                },
                timeout=10.0
            )

            if response.status_code == 200:
                data = response.json()

                # Determine token type and check permissions
                is_fine_grained = key.startswith('github_pat_')
                scopes = []
                has_repo_access = False

                if is_fine_grained:
                    # Fine-grained PATs: Test actual permissions by trying to list repos
                    # This will succeed if the token has proper permissions
                    try:
                        repos_response = await client.get(
                            "https://api.github.com/user/repos",
                            headers={
                                "Authorization": f"Bearer {key}",
                                "Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28"
                            },
                            params={"per_page": 1},  # Just test access, don't fetch all repos
                            timeout=10.0
                        )
                        # If we can list repos, the token has sufficient permissions
                        has_repo_access = repos_response.status_code == 200
                        scopes = ["fine-grained-pat"]
                    except Exception:
                        has_repo_access = False
                        scopes = ["fine-grained-pat"]
                else:
                    # Classic PAT: Check X-OAuth-Scopes header
                    scope_header = response.headers.get("X-OAuth-Scopes", "")
                    scopes = [s.strip() for s in scope_header.split(",") if s.strip()]
                    has_repo_access = "repo" in scopes or "public_repo" in scopes

                return {
                    "valid": True,
                    "username": data.get("login"),
                    "scopes": scopes,
                    "token_type": "fine-grained" if is_fine_grained else "classic",
                    "has_repo_access": has_repo_access
                }
            elif response.status_code == 401:
                return {
                    "valid": False,
                    "error": "Invalid Personal Access Token"
                }
            else:
                return {
                    "valid": False,
                    "error": f"GitHub API returned status {response.status_code}"
                }

    except httpx.TimeoutException:
        return {
            "valid": False,
            "error": "Request timed out. Please try again."
        }
    except Exception as e:
        return {
            "valid": False,
            "error": f"Error testing token: {str(e)}"
        }


# ============================================================================
# Slack Integration Settings (SLACK-001)
# ============================================================================

@router.get("/slack")
async def get_slack_settings_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get status of Slack integration settings.

    Admin-only. Returns masked key info for security.
    """
    assert_admin(current_user)

    try:
        from services.settings_service import (
            get_slack_client_id,
            get_slack_client_secret,
            get_slack_signing_secret,
        )

        client_id = get_slack_client_id()
        client_secret = get_slack_client_secret()
        signing_secret = get_slack_signing_secret()

        # Check sources
        client_id_from_settings = bool(db.get_setting_value('slack_client_id', None))
        client_secret_from_settings = has_secret_setting('slack_client_secret')
        signing_secret_from_settings = has_secret_setting('slack_signing_secret')

        return {
            "configured": bool(client_id and client_secret and signing_secret),
            "client_id": {
                "configured": bool(client_id),
                "masked": mask_api_key(client_id) if client_id else None,
                "source": "settings" if client_id_from_settings else ("env" if client_id else None)
            },
            "client_secret": {
                "configured": bool(client_secret),
                "masked": mask_api_key(client_secret) if client_secret else None,
                "source": "settings" if client_secret_from_settings else ("env" if client_secret else None)
            },
            "signing_secret": {
                "configured": bool(signing_secret),
                "masked": mask_api_key(signing_secret) if signing_secret else None,
                "source": "settings" if signing_secret_from_settings else ("env" if signing_secret else None)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get Slack settings: {str(e)}")


@router.put("/slack")
async def update_slack_settings(
    body: SlackSettingsUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Update Slack integration settings.

    Admin-only. All fields are optional - only provided values are updated.
    """
    assert_admin(current_user)

    try:
        updated = []

        if body.client_id is not None:
            db.set_setting('slack_client_id', body.client_id.strip())
            updated.append('client_id')

        if body.client_secret is not None:
            set_secret_setting('slack_client_secret', body.client_secret)
            updated.append('client_secret')

        if body.signing_secret is not None:
            set_secret_setting('slack_signing_secret', body.signing_secret)
            updated.append('signing_secret')

        return {
            "success": True,
            "updated": updated
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update Slack settings: {str(e)}")


@router.delete("/slack")
async def delete_slack_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Delete all Slack settings from database.

    Admin-only. Will fall back to env vars if configured.
    """
    assert_admin(current_user)

    try:
        deleted = []
        # client_id is a public identifier (plain row); the two secrets are
        # encrypted, so clearing them must remove BOTH forms (ent#435).
        if db.delete_setting('slack_client_id'):
            deleted.append('slack_client_id')
        for key in ['slack_client_secret', 'slack_signing_secret']:
            if clear_secret_setting(key):
                deleted.append(key)

        # Check env var fallbacks
        import os
        fallback_configured = bool(
            os.getenv('SLACK_CLIENT_ID') and
            os.getenv('SLACK_CLIENT_SECRET') and
            os.getenv('SLACK_SIGNING_SECRET')
        )

        return {
            "success": True,
            "deleted": deleted,
            "fallback_configured": fallback_configured
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete Slack settings: {str(e)}")


# ============================================================================
# Slack Transport Management (Socket Mode / Webhook)
# ============================================================================


@router.get("/slack/status")
async def get_slack_transport_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get Slack transport connection status.

    Returns transport mode, connection state, and workspace info.
    Admin-only.
    """
    assert_admin(current_user)

    from services.settings_service import get_slack_app_token, get_slack_transport_mode

    transport = getattr(request.app.state, 'slack_transport', None)
    connected = transport is not None and transport.is_connected

    app_token = get_slack_app_token()
    transport_mode = get_slack_transport_mode()

    # Get workspace info
    workspaces_raw = db.get_all_slack_workspaces()
    workspaces = []
    for ws in workspaces_raw:
        agents = db.get_slack_agents_for_workspace(ws["team_id"])
        workspaces.append({
            "team_id": ws["team_id"],
            "team_name": ws["team_name"],
            "agent_count": len(agents),
            "agents": [a["agent_name"] for a in agents],
        })

    return {
        "connected": connected,
        "transport_mode": transport_mode,
        "app_token_configured": bool(app_token),
        "app_token_masked": mask_api_key(app_token) if app_token else None,
        "workspaces": workspaces,
    }


@router.post("/slack/connect")
async def connect_slack_transport(
    request: Request,
    body: SlackConnectRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Save Slack transport config and start the connection.

    Saves app_token and transport_mode to DB, stops any existing
    transport, and starts a new one. Admin-only.
    """
    assert_admin(current_user)

    # Save settings to DB
    if body.app_token is not None:
        set_secret_setting("slack_app_token", body.app_token)
    if body.transport_mode is not None:
        if body.transport_mode.strip() not in ("socket", "webhook"):
            raise HTTPException(status_code=400, detail="transport_mode must be 'socket' or 'webhook'")
        db.set_setting("slack_transport_mode", body.transport_mode.strip())

    # Stop existing transport before starting new one
    existing = getattr(request.app.state, 'slack_transport', None)
    if existing:
        try:
            await existing.stop()
        except Exception as e:
            logger.warning(f"Error stopping existing Slack transport: {e}")
        request.app.state.slack_transport = None

    from services.settings_service import get_slack_app_token, get_slack_transport_mode, get_slack_signing_secret
    from adapters.slack_adapter import SlackAdapter
    from adapters.message_router import message_router

    mode = get_slack_transport_mode()
    adapter = SlackAdapter()
    transport = None

    try:
        if mode == "socket":
            app_token = get_slack_app_token()
            if not app_token:
                raise HTTPException(status_code=400, detail="App token required for Socket Mode")
            from adapters.transports.slack_socket import SlackSocketTransport
            transport = SlackSocketTransport(app_token, adapter, message_router)
            await transport.start()
            if not transport.is_connected:
                raise HTTPException(status_code=400, detail="Failed to connect Socket Mode. Check app token is valid.")
        else:
            signing_secret = get_slack_signing_secret()
            if not signing_secret:
                raise HTTPException(status_code=400, detail="Signing secret required for Webhook Mode")
            from adapters.transports.slack_webhook import SlackWebhookTransport
            transport = SlackWebhookTransport(signing_secret, adapter, message_router)
            await transport.start()
            # Register webhook transport for the events endpoint
            from routers.slack import set_webhook_transport
            set_webhook_transport(transport)

        request.app.state.slack_transport = transport

        return {
            "connected": True,
            "transport_mode": mode,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start Slack transport: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to connect: {str(e)}")


@router.post("/slack/install")
async def install_slack_workspace(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Generate Slack OAuth URL to install the app to a workspace.

    Redirects user to Slack for authorization. After approval, Slack
    redirects back to the OAuth callback which stores the bot token
    and redirects to Settings page. Admin-only.
    """
    assert_admin(current_user)

    from services.settings_service import get_slack_client_id
    from services.slack_service import slack_service

    if not get_slack_client_id():
        raise HTTPException(status_code=400, detail="Slack Client ID not configured. Save OAuth credentials first.")

    try:
        state = slack_service.encode_oauth_state(
            link_id="platform",
            agent_name="platform",
            user_id=str(current_user.id),
            source="platform"
        )
        oauth_url = slack_service.get_oauth_url(state)
        return {"oauth_url": oauth_url}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/slack/disconnect")
async def disconnect_slack_transport(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Stop the Slack transport. Does not delete saved credentials.

    Admin-only.
    """
    assert_admin(current_user)

    transport = getattr(request.app.state, 'slack_transport', None)
    if not transport:
        return {"disconnected": True, "was_connected": False}

    try:
        await transport.stop()
    except Exception as e:
        logger.warning(f"Error stopping Slack transport: {e}")

    request.app.state.slack_transport = None

    return {"disconnected": True, "was_connected": True}


# ============================================================================
# Email Whitelist Management (Phase 12.4)
# ============================================================================

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

    # Parse request
    body = await request.json()
    add_request = EmailWhitelistAdd(**body)
    email = add_request.email.lower()

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


@router.delete("/email-whitelist/{email}")
async def remove_email_from_whitelist(
    email: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Remove an email from the whitelist.

    Admin-only endpoint.
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

# ============================================================================
# GitHub Templates Configuration (TMPL-001)
# ============================================================================


_REPO_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$')


@router.get("/github-templates")
async def get_github_templates(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get configured GitHub templates.

    Admin-only. Returns the configured list or the hardcoded defaults.
    Display names and descriptions are resolved from each repo's template.yaml.
    """
    assert_admin(current_user)

    from config import DEFAULT_GITHUB_TEMPLATE_REPOS
    from services.template_service import _fetch_all_metadata

    db_templates = settings_service.get_github_templates()
    if db_templates is not None:
        repos = [e["github_repo"] for e in db_templates]
        all_metadata = _fetch_all_metadata(repos)
        enriched = []
        for entry in db_templates:
            repo = entry["github_repo"]
            meta = all_metadata.get(repo, {})
            admin_name = entry.get("display_name", "")
            admin_desc = entry.get("description", "")
            enriched.append({
                "github_repo": repo,
                "display_name": admin_name,
                "description": admin_desc,
                "resolved_name": admin_name or meta.get("display_name") or meta.get("name") or repo.split("/")[-1],
                "resolved_description": admin_desc or meta.get("description", ""),
            })
        return {
            "source": "settings",
            "templates": enriched
        }
    else:
        all_metadata = _fetch_all_metadata(DEFAULT_GITHUB_TEMPLATE_REPOS)
        defaults = []
        for repo in DEFAULT_GITHUB_TEMPLATE_REPOS:
            meta = all_metadata.get(repo, {})
            defaults.append({
                "github_repo": repo,
                "display_name": "",
                "description": "",
                "resolved_name": meta.get("display_name") or meta.get("name") or repo.split("/")[-1],
                "resolved_description": meta.get("description", ""),
            })
        return {
            "source": "defaults",
            "templates": defaults
        }


@router.put("/github-templates")
async def update_github_templates(
    body: GitHubTemplatesUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set the GitHub templates list.

    Admin-only. Validates owner/repo format for each entry.
    """
    assert_admin(current_user)

    # Validate each entry
    for entry in body.templates:
        if not _REPO_PATTERN.match(entry.github_repo):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid repository format: '{entry.github_repo}'. Expected 'owner/repo'."
            )

    # Convert to list of dicts for storage
    templates_data = [entry.model_dump() for entry in body.templates]
    settings_service.set_github_templates(templates_data)

    return {
        "success": True,
        "count": len(templates_data)
    }


@router.delete("/github-templates")
async def delete_github_templates(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Reset GitHub templates to hardcoded defaults.

    Admin-only. Removes the DB override so the system falls back to config.py defaults.
    """
    assert_admin(current_user)

    deleted = settings_service.delete_github_templates()

    return {
        "success": True,
        "deleted": deleted,
        "message": "GitHub templates reset to defaults"
    }


# ============================================================================
# Remote Template Registry (TMPL-002, trinity-enterprise#14)
# ============================================================================
#
# Same shape as GitHub Templates above, deliberately — the registry is a new
# SOURCE for that seam, not a second settings idiom. Registered here, well
# before the `/{key}` catch-all (Invariant #4), like `/skills-library` and
# `/brain-orb`.


@router.get("/template-registry")
async def get_template_registry(current_user: User = Depends(get_current_user)):
    """Remote template registry configuration + live status. Admin-only.

    The `status` block is part of the contract, not a nicety: fail-open makes
    every registry failure invisible in the catalog by design, so the ONLY place
    an operator can see that their registry 404s is here. A panel that cannot
    show a failing fetch is a panel they cannot debug with (ent#236).

    Resolving the status goes through the same cache the catalog uses, so this
    fetches only when a fetch was already due — an admin opening the panel costs
    no more than a user listing templates.
    """
    assert_admin(current_user)

    from config import TEMPLATE_REGISTRY_URL
    from services.settings_service import TEMPLATE_REGISTRY_URL_KEY
    from services.template_registry_service import get_registry_status

    stored_url = settings_service.get_setting(TEMPLATE_REGISTRY_URL_KEY)
    return {
        "source": "settings" if stored_url else "default",
        "url": stored_url or "",
        "default_url": TEMPLATE_REGISTRY_URL,
        "effective_url": settings_service.get_template_registry_url(),
        "enabled": settings_service.is_template_registry_enabled(),
        # Rendered as an inert toggle rather than a working one, the
        # `TelemetrySharingPanel` shape: TEMPLATE_REGISTRY_ENABLED=false is a
        # config/air-gap decision no DB row may override, and a toggle that
        # silently does nothing is worse than one that says why.
        "hard_disabled": settings_service.is_template_registry_hard_disabled(),
        # An admin-curated GitHub list wins outright (TMPL-001), and the catalog
        # never even asks the registry in that case. Surfaced so the panel does
        # not report a healthy registry that is contributing nothing.
        "suppressed_by_github_templates": (
            settings_service.get_github_templates() is not None
        ),
        "status": get_registry_status(),
    }


@router.put("/template-registry")
async def update_template_registry(
    body: TemplateRegistryUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Set the registry URL and/or toggle. Admin **and human**-only. Audit-logged.

    `reject_agent_principal` is not optional here. `assert_admin` answers *what
    role*, never *is this a human*: `get_current_user` resolves an agent-scoped
    MCP key to its owner CARRYING THE OWNER'S ROLE, so on a default admin-owned
    install any agent's injected `TRINITY_MCP_API_KEY` satisfies a bare admin
    gate (trinity-ops-agent#232, spelled out in Invariant #8). The consequence
    here is direct and total: an agent could repoint the platform's template
    registry at a URL it controls, and every operator browsing templates would
    see its catalog. GET stays admin-only without the human gate — it reads an
    operator-set URL, same as TMPL-001's GET.
    """
    from dependencies import reject_agent_principal

    assert_admin(current_user)
    reject_agent_principal(current_user)

    if body.url is None and body.enabled is None:
        raise HTTPException(
            status_code=400, detail="Provide `url` and/or `enabled`."
        )

    if settings_service.is_template_registry_hard_disabled() and body.enabled:
        raise HTTPException(
            status_code=409,
            detail=(
                "The template registry is disabled by configuration "
                "(TEMPLATE_REGISTRY_ENABLED=false); no setting can enable it."
            ),
        )

    url = body.url
    if url is not None:
        from utils.url_validation import validate_template_registry_url

        url = url.strip()
        if not url:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Registry URL cannot be blank. Use "
                    "DELETE /api/settings/template-registry to revert to the default."
                ),
            )
        try:
            url = validate_template_registry_url(url)
        except ValueError as e:
            # `str(e)` is safe: every message in that validator is built from
            # our own literals, never from the resolved address or the URL.
            raise HTTPException(status_code=400, detail=str(e))

    previous_url = settings_service.get_template_registry_url()
    previous_enabled = settings_service.is_template_registry_enabled()

    settings_service.set_template_registry_config(url=url, enabled=body.enabled)

    # Per-process convenience; the generation counter bumped above is what
    # actually reaches the other uvicorn worker.
    from services.template_registry_service import invalidate_registry_cache

    invalidate_registry_cache()

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="template_registry_config_change",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={
            "url": {"old": previous_url, "new": settings_service.get_template_registry_url()},
            "enabled": {
                "old": previous_enabled,
                "new": settings_service.is_template_registry_enabled(),
            },
        },
    )

    return await get_template_registry(current_user=current_user)


@router.delete("/template-registry")
async def delete_template_registry(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Reset the registry to its config defaults. Admin **and human**-only.

    Also drops the durable last-known-good — it was captured under the
    overridden URL, and serving it afterwards would attribute one registry's
    catalog to another.
    """
    from dependencies import reject_agent_principal

    assert_admin(current_user)
    reject_agent_principal(current_user)

    previous_url = settings_service.get_template_registry_url()
    deleted = settings_service.delete_template_registry_config()

    from services.template_registry_service import invalidate_registry_cache

    invalidate_registry_cache()

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="template_registry_config_reset",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={
            "previous_url": previous_url,
            "new_url": settings_service.get_template_registry_url(),
            "removed_override": deleted,
        },
    )

    return {
        "success": True,
        "deleted": deleted,
        "message": "Template registry reset to defaults",
    }


# ============================================================================
# MCP Server URL Configuration (#76)
# ============================================================================

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


# ============================================================================
# Generic Settings CRUD - /{key} catch-all routes
# NOTE: These must come AFTER specific routes like /api-keys
# ============================================================================

# ============================================================================
# Agent Quota Settings (QUOTA-001)
# ============================================================================


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


# ============================================================================
# Agent Default Resources (RES-001)
# ============================================================================

# Canonical allowed values live in the container-spec module so the admin
# defaults endpoint and the agent create/recreate paths can't drift (#1197).
from services.agent_service.capabilities import VALID_CPU, VALID_MEMORY
VALID_CPU_VALUES = list(VALID_CPU)
VALID_MEMORY_VALUES = list(VALID_MEMORY)


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


# ============================================================================
# Agent Default Access Policy (#1129 — secure-by-default require_email)
# ============================================================================

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


# ============================================================================
# Max Parallel Tasks Ceiling (#506 — fleet-wide per-agent concurrency cap)
# ============================================================================

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


# ============================================================================
# Brain Orb Feature Flags (trinity-enterprise#85 — admin-configurable)
# ============================================================================

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


# ============================================================================
# ElevenLabs / outbound-voice (TTS) Settings (trinity-enterprise#117)
# NOTE: These routes MUST be defined BEFORE the /{key} catch-all (Invariant #4).
# ============================================================================

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


# ============================================================================
# Outbound A2A endpoint registry (#736 §32.5 FR-2)
# NOTE: These routes MUST be defined BEFORE the /{key} catch-all (Invariant #4).
# ============================================================================
#
# The OSS target source for `call_a2a_agent`. It lives in `system_settings` as
# one AES-256-GCM envelope rather than a new table — the shape Invariant #12
# already blesses for `elevenlabs_api_key_encrypted` — which is why #736 ships
# with no migration and no Alembic revision.
#
# Admin-only AND human-only. `assert_admin` rejects agent principals since
# ent#293, and `reject_agent_principal` is kept explicitly beside it because
# this is the GRANT half of the grant-vs-use line: registering an endpoint
# decides where a credentialed server-side request may go, and an agent-scoped
# key resolves to its owner carrying the owner's role. *Using* a registered
# endpoint is agent-callable; *creating* one is not.


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


@router.get("/{key}")
async def get_setting(
    key: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get a specific setting by key.

    Returns the setting value or 404 if not found.
    Admin-only for most settings.
    """
    assert_admin(current_user)

    try:
        setting = db.get_setting(key)

        if not setting:
            raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")

        return setting
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get setting: {str(e)}")


@router.put("/{key}", response_model=SystemSetting)
async def update_setting(
    key: str,
    body: SystemSettingUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Create or update a system setting.

    Admin-only endpoint. Creates the setting if it doesn't exist.
    """
    assert_admin(current_user)

    # #506: the fleet ceiling must go through the dedicated range-validated
    # route; block the generic PUT so it can't be written to junk/out-of-range
    # (same pattern as the skills_library_url SSRF special-case below).
    if key == MAX_PARALLEL_TASKS_CEILING_KEY:
        raise HTTPException(
            status_code=422,
            detail=(
                "max_parallel_tasks_ceiling must be set via "
                "PUT /api/settings/max-parallel-tasks-ceiling (range-validated 1–32)"
            ),
        )

    # #1609: proactive caps go through the dedicated range-validated route.
    if key in PROACTIVE_RATE_LIMIT_DEFAULTS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key} must be set via PUT /api/settings/proactive-rate-limits "
                f"(range-validated 0–{PROACTIVE_RATE_LIMIT_MAX}, 0 = unlimited)"
            ),
        )

    # ent#12: telemetry-sharing consent is a human-only decision. The dedicated
    # PUT /api/settings/telemetry-sharing enforces reject_agent_principal, the
    # hard-disabled 409, consent_at stamping, and the dedicated audit action —
    # this generic PUT has none of those, so an admin-owned agent-scoped key
    # could otherwise flip egress consent (trinity-ops-agent#232 class). Block
    # the whole key family.
    if key.startswith("telemetry_sharing_"):
        raise HTTPException(
            status_code=422,
            detail=(
                "telemetry_sharing_* must be set via "
                "PUT /api/settings/telemetry-sharing (admin + human-only, audit-logged)"
            ),
        )

    # ent#463: operator-intake consent (identified contact record — email,
    # optional company/name/role/use_case) is human-only and audit-logged, same
    # rationale as telemetry_sharing_* one block up. The dedicated PUT enforces
    # reject_agent_principal, the hard-disabled 409, at-most-once semantics, and
    # the dedicated `operator_intake_consent` audit action; none of that
    # replays here. Also cover the pre-ent#463 `operator_intake_submitted`
    # marker so a raw PUT can't be used to fake the at-most-once claim.
    if key.startswith("operator_intake_"):
        raise HTTPException(
            status_code=422,
            detail=(
                "operator_intake_* must be set via "
                "PUT /api/settings/operator-intake (admin + human-only, audit-logged)"
            ),
        )

    # #1644: the blast-radius guard's own state cannot be writable through an
    # unvalidated endpoint, or the guard is trivially disarmed by the same route
    # that causes the bug it exists to catch.
    #   - an ack row WRITTEN here would pre-approve a mass deletion;
    #   - the threshold RAISED here would disable the guard fleet-wide.
    # (DELETE of an ack is deliberately NOT blocked: removing an ack re-arms the
    # guard, which fails safe.)
    from services.retention_guard import ACK_KEY_PREFIX

    if key.startswith(ACK_KEY_PREFIX):
        raise HTTPException(
            status_code=422,
            detail=(
                "retention acknowledgements must be recorded via "
                "POST /api/settings/retention/acknowledge (#1644)"
            ),
        )

    # ent#297: the retention WINDOWS themselves. #1644 blocked the guard's ack
    # keys here but left the windows falling through to a bare `db.set_setting`
    # with no type or range check — so the generic PUT was a second, completely
    # unvalidated write path to the values that drive irreversible deletion
    # (execution history, health checks, and via agent_soft_delete_retention_days
    # the #1581 volume purge, which is unrecoverable).
    #
    # Route them to `PUT /api/settings/ops/config`, which validates and audits.
    # Same 422-with-a-pointer shape as max_parallel_tasks_ceiling (#506),
    # PROACTIVE_RATE_LIMIT_DEFAULTS (#1609) and telemetry_sharing_* (ent#12):
    # a settings key whose value has a safe range gets a route that knows the
    # range, and the catch-all refuses to be a way around it.
    # ent#14: the registry URL is an SSRF sink and the toggle is a security
    # control, so both must go through the dedicated validated + human-gated
    # route — without this block the whole SSRF gate is one generic PUT away
    # from being bypassed. `generation` and `lkg` are blocked for a different
    # reason: they ARE the cache, and a writable cache is a poisonable one.
    # Validate at the boundary AND at the sink (#1525).
    from services.settings_service import TEMPLATE_REGISTRY_KEYS

    if key in TEMPLATE_REGISTRY_KEYS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key} must be set via PUT /api/settings/template-registry "
                "(HTTPS + SSRF validated, admin + human-only, audit-logged)"
            ),
        )

    from services.settings_service import RETENTION_OPS_KEYS

    if key in RETENTION_OPS_KEYS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key} is a retention window and must be set via "
                "PUT /api/settings/ops/config (type- and range-validated, "
                "audit-logged). See GET /api/settings/retention for the "
                "effective values (ent#297)"
            ),
        )

    # ent#236: the automation keys go through the dedicated validated route.
    # The interval especially: this generic PUT takes `Dict[str, str]` with no
    # type or range check, so "10" would be accepted verbatim and the auto-sync
    # loop would fork `git fetch` six times a minute against GitHub forever.
    # (The read-side clamp in `get_skills_auto_sync_interval` is the second
    # layer; this is the first — validate at the boundary AND at the sink, #1525.)
    if key in SKILLS_AUTOMATION_KEYS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key} must be set via PUT /api/settings/skills-library "
                f"(range-validated; interval {SKILLS_AUTO_SYNC_INTERVAL_MIN}–"
                f"{SKILLS_AUTO_SYNC_INTERVAL_MAX}s)"
            ),
        )

    # ent#346: the legacy skills-library keys are a SOURCE GRANT in disguise.
    #
    # ent#237 put `reject_agent_principal` on every `/skills/sources` route and
    # states why: adding a source is the GRANT action, and a prompt-injected
    # agent that could register its own repo gets unattended, fleet-wide,
    # persistent prompt injection — skills are instructions Claude follows and
    # they ship executable `scripts/`.
    #
    # This key reaches the same room by another door. `_adopt_legacy_clone`
    # turns `skills_library_url` into a `skill_sources` row on the next sync at
    # CUSTOM priority — which outranks the bundled community catalog — then
    # deletes the setting, erasing where the row came from. This generic PUT is
    # `assert_admin`-gated but NOT `reject_agent_principal`-gated, and an
    # agent-scoped key resolves to its owner carrying the owner's role, so on
    # the default admin-owned install it passes (trinity-ops-agent#232 class).
    #
    # Validating the URL is not sufficient and never was: `github.com/attacker/skills`
    # passes `validate_skills_library_url` cleanly. The question is WHO may grant
    # a source, not what the string looks like — so block the keys and point at
    # the route that carries the gate. ent#237 already removed the UI writer, so
    # nothing supported breaks.
    # #736: the outbound A2A endpoint list is a TARGET grant carrying encrypted
    # credentials — the value is an AES-256-GCM envelope, so a plaintext write
    # here would both bypass the SSRF/shape validation and corrupt the store
    # into something the reader refuses (fail-closed, but silently and with a
    # confusing cause). It is also the answer to "where may a credentialed
    # server-side request go?", which is exactly the class this catch-all keeps
    # being a way around (#506 / #1609 / ent#12 / #1644 / ent#14 / ent#346).
    from services.a2a_outbound import A2A_ENDPOINTS_SETTING

    if key == A2A_ENDPOINTS_SETTING:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key} holds encrypted outbound A2A endpoints and must be set via "
                "PUT /api/settings/a2a-endpoints (admin + human-only, SSRF-validated, "
                "audit-logged)"
            ),
        )

    if key in LEGACY_SKILLS_LIBRARY_KEYS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key} is a skills SOURCE grant and must be set via "
                "POST /api/skills/sources (admin + human-only, validated, audited). "
                "Writing it here would register a fleet-wide skills source without "
                "the grant gate (ent#346)."
            ),
        )

    # ent#435: this catch-all is the reason a route-level block is not enough —
    # it can write ANY key, so it could put a live Anthropic/GitHub/Slack
    # credential straight back into cleartext after the migration removed it.
    # The authoritative refusal is the sink guard in `db.set_setting`
    # (`SecretSettingWriteError`, caught below); this arm exists only to answer
    # BEFORE the write with the same message the sink would give. Writing the
    # ENCRYPTED key here is refused too: the value must be an envelope this
    # platform produced, and a hand-pasted string would land as a row every
    # reader then fails to decrypt — fail-closed, but silently and confusingly
    # (the #736 A2A-endpoints rationale, exactly).
    from services.secret_settings import (
        ENCRYPTED_SETTING_KEYS,
        SecretSettingWriteError,
        assert_plaintext_write_allowed,
    )

    if key in ENCRYPTED_SETTING_KEYS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key} holds an AES-256-GCM envelope and cannot be written as a "
                "raw value. Set the credential through its own settings route, "
                "which encrypts on the way in (ent#435)."
            ),
        )
    try:
        assert_plaintext_write_allowed(key)
    except SecretSettingWriteError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # ent#434: the weekly-headroom alert threshold has a dedicated,
    # range-validated route. Blocked here for the same reason as every sibling
    # above — this catch-all takes an unvalidated string, and a small VALID
    # integer is the dangerous input, not garbage (#1644's lesson): "5" would
    # be stored verbatim and alarm on every subscription forever.
    if key == HEADROOM_ALERT_THRESHOLD_KEY:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key} must be set via "
                "PUT /api/subscriptions/settings/headroom-alert-threshold "
                f"(0 to disable, else {HEADROOM_THRESHOLD_MIN}-{HEADROOM_THRESHOLD_MAX})"
            ),
        )

    # T1 (ent#434 review): close the standing hole rather than adding a
    # twelfth `if key == ...` arm. Every key in OPS_SETTINGS_VALIDATION is
    # type- and range-checked on PUT /api/settings/ops/config and was checked
    # NOWHERE on this route, so an ops key reachable here accepted "abc" or
    # "-40" verbatim. Validating here makes the two write paths agree, and it
    # covers ops keys added in future without anyone remembering to.
    from config import OPS_SETTINGS_VALIDATION, validate_ops_setting
    if key in OPS_SETTINGS_VALIDATION:
        try:
            body.value = validate_ops_setting(key, body.value)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    try:
        setting = db.set_setting(key, body.value)

        # #831: Invalidate platform default model TTL cache on write
        if key == "platform_default_model":
            import services.settings_service as _ss
            _ss._platform_model_cache = None
            _ss._platform_model_cache_ts = 0.0

        # SEC-001: audit generic setting change
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"setting": key, "action": "update"},
        )

        # Back-fill Telegram webhooks when public_chat_url becomes available.
        # Why: bindings created before public_chat_url was set have webhook_url IS NULL
        # and receive no messages. Re-registering is idempotent (setWebhook on Telegram).
        if key == "public_chat_url" and body.value:
            await _backfill_telegram_webhooks(body.value)
            # Same back-fill for WhatsApp — refreshes the URL shown to the user
            # for pasting into Twilio Console. (Twilio doesn't have a setWebhook
            # API equivalent — users paste the URL manually.)
            try:
                from adapters.transports.twilio_webhook import backfill_webhook_urls as _wa_backfill
                _wa_backfill(body.value)
            except Exception as e:
                logger.warning(f"WhatsApp webhook URL back-fill skipped: {e}")

        return setting
    except SecretSettingWriteError as e:
        # Belt for the pre-check above: if the guard ever grows a case the
        # pre-check does not mirror, the caller still gets 422-with-a-pointer
        # rather than a 500 that reads like a platform fault.
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update setting: {str(e)}")


async def _backfill_telegram_webhooks(public_url: str) -> None:
    """Re-register Telegram webhooks for all bindings after public_chat_url changes.

    Idempotent: Telegram's setWebhook replaces any existing registration.
    Failures are logged but not raised — the setting write has already succeeded
    and a single bad binding must not block others or the response.
    """
    try:
        from adapters.transports.telegram_webhook import register_webhook
        bindings = db.get_all_telegram_bindings()
    except Exception as e:
        logger.warning(f"Telegram webhook back-fill skipped: {e}")
        return

    for binding in bindings:
        agent_name = binding.get("agent_name", "<unknown>")
        try:
            await register_webhook(agent_name, public_url)
        except Exception as e:
            logger.warning(
                f"Telegram webhook back-fill failed for agent={agent_name}: {e}"
            )


@router.delete("/{key}")
async def delete_setting(
    key: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Delete a system setting.

    Admin-only endpoint. Returns success even if setting didn't exist.
    """
    assert_admin(current_user)

    # ent#14: blocked here as well as on PUT, unlike the #1644 retention acks.
    # Deleting an ack re-arms a guard and therefore fails safe; deleting
    # `template_registry_enabled` reverts it to its default of ON, which
    # re-enables egress an operator deliberately switched off — and this route
    # carries no `reject_agent_principal`, so an admin-owned agent key could do
    # it. The dedicated DELETE has the human gate and the audit action.
    from services.settings_service import TEMPLATE_REGISTRY_KEYS

    if key in TEMPLATE_REGISTRY_KEYS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key} must be cleared via DELETE /api/settings/template-registry "
                "(admin + human-only, audit-logged)"
            ),
        )

    try:
        deleted = db.delete_setting(key)

        # SEC-001: audit setting deletion
        if deleted:
            await platform_audit_service.log(
                event_type=AuditEventType.CONFIGURATION,
                event_action="settings_change",
                source="api",
                actor_user=current_user,
                actor_ip=request.client.host if request.client else None,
                endpoint=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
                details={"setting": key, "action": "delete"},
            )

        return {"success": True, "deleted": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete setting: {str(e)}")


# ============================================================================
# Ops Settings Endpoints
# ============================================================================

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


# Note: get_ops_setting is now imported from services.settings_service
