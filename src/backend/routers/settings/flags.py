"""Platform flags, telemetry-sharing consent, operator intake, portal-session policy.

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
        # Install provenance (#2380). A STRING, not a boolean — `platform_default_model`
        # above is the precedent for a non-boolean on this surface. One of
        # do-marketplace / vultr-marketplace / script / unknown, recorded once at
        # first boot from TRINITY_INSTALL_SOURCE and read from `system_settings`
        # thereafter. Surfaced HERE rather than on a new route because this is the
        # established home for UI-gating flags and the browser already awaits it.
        "install_source": settings_service.get_install_source(),
        # The resolved gate for the first-run hardening guide. Ships beside the raw
        # value so the browser holds no second copy of WHICH sources count as a
        # marketplace (the ent#386 rule). False on every non-marketplace install —
        # including the entire managed fleet, whose plain-HTTP-over-Tailscale shape
        # is indistinguishable from an unhardened droplet by any other signal.
        "marketplace_install": settings_service.is_marketplace_install(),
        # What URL this instance ADVERTISES itself at: unconfigured | http |
        # https-ip | https-domain. Derived from `public_chat_url` (else the baked
        # FRONTEND_URL) — nothing probes a socket or reads a certificate, because
        # TLS terminates outside the backend (HOST-010) and no in-process check can
        # see it. Derived rather than returning the URL because that read is
        # admin-only and this surface is not. The guide's copy must say
        # "advertises", never assert a verified certificate.
        "install_tls_posture": settings_service.get_install_tls_posture(),
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
