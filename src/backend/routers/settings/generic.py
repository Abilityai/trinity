"""The generic `/{key}` catch-all, and the blocklist that keeps validated keys off it.

Carved out of the 3,529-line `routers/settings.py` (#1028). The package
`__init__` composes every sub-router onto one `/api/settings` router, so the
mounted API is byte-identical to the single-module version.

**Registered LAST, and that ordering is load-bearing** (Invariant #4).
`/{key}` matches any single segment, so every specific route — `/ops/config`,
`/brain-orb`, `/api-keys/anthropic` — must be included before it or the
catch-all swallows them and answers 'setting not found' for a route that exists.
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


# The fleet-wide read. NOT decorated here: its path is the empty string, and a
# prefix-less sub-router cannot carry `GET ""` — FastAPI refuses "prefix and
# path cannot be both empty". The package `__init__` registers it on the parent
# router, which supplies `/api/settings`, so the mounted path is unchanged.
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

    # #2380: install provenance is a RECORDED FACT, not a setting. It is written
    # once at boot from TRINITY_INSTALL_SOURCE and gates a surface that is meant
    # to appear on marketplace installs and nowhere else. Leaving it on the
    # catch-all would make the gate self-assertable — an admin (or, on a default
    # admin-owned install, anything holding an admin's credential) could type a
    # marketplace value and summon the guide on a managed instance, or type a
    # non-marketplace one and suppress it on a droplet that needs it.
    # There is deliberately no dedicated write route to point at: the only
    # supported way to set provenance is to provision the box with the marker.
    from config import INSTALL_SOURCE_ENV_VAR, INSTALL_SOURCE_SETTING_KEY

    if key == INSTALL_SOURCE_SETTING_KEY:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{INSTALL_SOURCE_SETTING_KEY} records how this instance was "
                "installed and is not writable through the API. It is recorded "
                f"once at first boot from the {INSTALL_SOURCE_ENV_VAR} "
                "environment variable."
            ),
        )

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

    # #2380: blocked here as well as on PUT, and for a sharper reason than
    # ent#14's below. Provenance is write-once by design — `_record_install_source`
    # refuses to overwrite an existing row — so a DELETE is not "revert to a
    # default", it is the one move that UNLOCKS a rewrite: delete the row, edit
    # `.env`, restart, and the boot recorder happily records the new value.
    # Blocking the write while leaving the delete open would be no gate at all.
    from config import INSTALL_SOURCE_SETTING_KEY

    if key == INSTALL_SOURCE_SETTING_KEY:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{INSTALL_SOURCE_SETTING_KEY} records how this instance was "
                "installed and cannot be cleared through the API."
            ),
        )

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
