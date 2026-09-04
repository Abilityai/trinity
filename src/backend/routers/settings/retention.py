"""Effective retention windows, the backup block, and the blast-radius prune acknowledgement.

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
