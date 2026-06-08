"""
Admin recovery endpoints for soft-deleted entities (Issue #834 Phase 1c).

Until this router shipped, the only way to recover a soft-deleted
agent or schedule was a direct `UPDATE ... SET deleted_at = NULL`
against the SQLite DB. That worked but required shell access, was
unauditable, and didn't surface the soft-deleted set anywhere.

This router fills both gaps:

- `GET  /api/admin/soft-deleted/agents`               list soft-deleted agents
- `POST /api/admin/soft-deleted/agents/{name}/recover` clear `deleted_at`
- `GET  /api/admin/soft-deleted/schedules`            list soft-deleted schedules
- `POST /api/admin/soft-deleted/schedules/{id}/recover` clear `deleted_at`

All endpoints are admin-only and audit-logged (every recovery emits
an `agent_lifecycle:recover` or `schedule:recover` event).

Recovery semantics: Trinity flips `deleted_at` back to NULL. Child
rows already survived the soft-delete, so the entity is immediately
usable via the regular read paths. For agents the Docker container
is NOT recreated automatically — recovery is metadata-only; the
operator must `POST /api/agents/{name}/start` to bring the container
back. The preserved workspace volume keeps the agent's files
intact across the gap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from database import db
from dependencies import require_admin
from models import User, SoftDeletedAgent, SoftDeletedSchedule
from services.platform_audit_service import (
    platform_audit_service,
    AuditEventType,
)
from services.settings_service import OPS_SETTINGS_DEFAULTS

router = APIRouter(prefix="/api/admin/soft-deleted", tags=["admin-recovery"])

# Response models SoftDeletedAgent / SoftDeletedSchedule live in
# models.py (Architectural Invariant #14 — Pydantic response models are
# centralized in models.py, not declared in router files).


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _retention_days(setting_key: str) -> int:
    raw = db.get_setting_value(setting_key, OPS_SETTINGS_DEFAULTS.get(setting_key, "0"))
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return 0


def _purge_eta(deleted_at_iso: str, retention_days: int) -> Optional[str]:
    """ISO-Z timestamp for when the sweep would hard-purge this row, or
    None if retention is disabled."""
    if retention_days <= 0:
        return None
    try:
        # ISO with trailing 'Z' isn't directly parseable by fromisoformat
        # until 3.11; strip 'Z' and treat as UTC.
        dt = datetime.fromisoformat(deleted_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt + timedelta(days=retention_days)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


# -----------------------------------------------------------------------------
# Agents
# -----------------------------------------------------------------------------


@router.get("/agents", response_model=List[SoftDeletedAgent])
async def list_soft_deleted_agents(
    limit: int = Query(200, le=500),
    _: User = Depends(require_admin),
):
    """List currently-soft-deleted agents, newest first.

    `purge_eta` is computed from `agent_soft_delete_retention_days`;
    if that's 0 (sweep disabled), the field is null.
    """
    retention = _retention_days("agent_soft_delete_retention_days")
    rows = db.list_soft_deleted_agents(limit=limit)
    return [
        SoftDeletedAgent(
            agent_name=r["agent_name"],
            owner_id=r["owner_id"],
            created_at=r["created_at"],
            deleted_at=r["deleted_at"],
            purge_eta=_purge_eta(r["deleted_at"], retention),
        )
        for r in rows
    ]


@router.post("/agents/{agent_name}/recover")
async def recover_agent(
    agent_name: str,
    request: Request,
    current_user: User = Depends(require_admin),
):
    """Clear `deleted_at` on the agent_ownership row. Audit-logged.

    404 if the agent doesn't exist or isn't currently soft-deleted
    (live agents aren't a recovery target). The container is NOT
    started automatically — operator does that explicitly.
    """
    if not db.recover_agent_ownership(agent_name):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Agent '{agent_name}' is not in the soft-deleted set "
                f"(either doesn't exist, was already recovered, or has "
                f"been hard-purged)"
            ),
        )

    await platform_audit_service.log(
        event_type=AuditEventType.AGENT_LIFECYCLE,
        event_action="recover",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
    )

    return {
        "message": (
            f"Agent {agent_name} recovered: all relational state "
            f"(chat history, schedules, sharing, permissions, "
            f"credentials config) is restored and the agent is visible "
            f"again. The Docker container was removed at soft-delete and "
            f"is NOT recreated by recovery — bringing the agent back "
            f"online (container recreate from the preserved workspace "
            f"volume) is tracked as #834 Phase 2. Until then the agent "
            f"shows status=stopped with needs_start=true."
        ),
        "agent_name": agent_name,
        "needs_container_recreate": True,
    }


# -----------------------------------------------------------------------------
# Schedules
# -----------------------------------------------------------------------------


@router.get("/schedules", response_model=List[SoftDeletedSchedule])
async def list_soft_deleted_schedules(
    agent_name: Optional[str] = None,
    limit: int = Query(200, le=500),
    _: User = Depends(require_admin),
):
    """List soft-deleted schedules across the fleet (or scoped to one
    agent via `?agent_name=`). `purge_eta` reflects
    `schedule_soft_delete_retention_days`."""
    retention = _retention_days("schedule_soft_delete_retention_days")
    rows = db.list_soft_deleted_schedules(agent_name=agent_name, limit=limit)
    return [
        SoftDeletedSchedule(
            id=r["id"],
            agent_name=r["agent_name"],
            name=r["name"],
            cron_expression=r["cron_expression"],
            message=r["message"],
            owner_id=r["owner_id"],
            enabled=bool(r["enabled"]),
            deleted_at=r["deleted_at"],
            purge_eta=_purge_eta(r["deleted_at"], retention),
        )
        for r in rows
    ]


@router.post("/schedules/{schedule_id}/recover")
async def recover_schedule(
    schedule_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
):
    """Clear `deleted_at` on the schedule row. Audit-logged.

    404 if the schedule doesn't exist or isn't currently soft-deleted.
    The schedule reappears on the scheduler firing list on its next
    poll cycle if it was enabled at the time of soft-delete.
    """
    if not db.recover_schedule(schedule_id):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Schedule '{schedule_id}' is not in the soft-deleted set "
                f"(either doesn't exist, was already recovered, or has "
                f"been hard-purged)"
            ),
        )

    await platform_audit_service.log(
        event_type=AuditEventType.AGENT_LIFECYCLE,
        event_action="schedule_recover",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="schedule",
        target_id=schedule_id,
        endpoint=str(request.url.path),
    )

    return {
        "message": f"Schedule {schedule_id} recovered.",
        "schedule_id": schedule_id,
    }
