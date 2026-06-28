"""
Agent reports API (#918).

Agents publish structured reports (telemetry / domain reports) via the MCP
``report`` tool, which POSTs here. Reports are persisted, broadcast as a thin
WebSocket trigger, and surfaced on the Agent Detail "Reports" tab and a
fleet-wide Reports view.

Access control mirrors routers/executions.py:
- admin → every report (agent_names=None, no SQL filter)
- non-admin → only accessible agents (owned + shared)

Create is **self-gated** (review/Codex #1): ``AuthorizedAgent`` checks the key
owner can access the path agent, but does NOT stop an agent-scoped key from
reporting as a *sibling* agent the owner also accesses. So we additionally
require an agent-scoped caller's bound ``agent_name`` to equal the path agent
(mirrors heartbeat_service.authorize_heartbeat). User-scoped callers fall back to
the access check.
"""
import json
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from database import db
from dependencies import get_current_user, AuthorizedAgent, OwnedAgent
from models import (
    FleetReportStats,
    Report,
    ReportCreate,
    ReportSummary,
    REPORT_PAYLOAD_MAX_BYTES,
    User,
)
from routers.executions import _narrow_to_agent
from services import rate_limiter, report_service
from services.agent_service.helpers import accessible_agent_names

router = APIRouter(prefix="/api", tags=["reports"])

_VALID_HOURS = {0, 1, 6, 24, 168, 720}  # 0 = all-time

# Per-agent create rate limit (#918 review I3). Reports can be bursty (an agent
# may publish a few at the end of a run), so the window is generous — its job is
# to cap a runaway/looping agent flooding the table between retention sweeps, not
# to throttle normal use. Shared sliding-window limiter (services/rate_limiter.py,
# #1023); fail-open if Redis is down.
REPORT_RATE_LIMIT = int(os.getenv("REPORT_RATE_LIMIT", "30"))
REPORT_RATE_WINDOW = 60  # seconds


# ============================================================================
# Agent-scoped endpoints  (/api/agents/{name}/reports)
# ============================================================================

@router.post("/agents/{name}/reports", response_model=Report, status_code=201)
async def create_report(
    data: ReportCreate,
    name: AuthorizedAgent,
    current_user: User = Depends(get_current_user),
):
    """Publish a report for an agent (called by the agent via MCP).

    Self-gated: an agent-scoped key may only report as itself.
    """
    if current_user.agent_name and current_user.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent-scoped key may only report as itself",
        )

    # Cap report volume per agent so a runaway agent can't flood the table
    # between retention sweeps (review I3). Fail-open if Redis is down.
    rate_limiter.enforce(
        f"agent_report:{name}",
        REPORT_RATE_LIMIT,
        REPORT_RATE_WINDOW,
        detail="Report rate limit exceeded for this agent.",
    )

    # Bound the payload before it hits the DB / every list response (review A2).
    if len(json.dumps(data.payload).encode("utf-8")) > REPORT_PAYLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"payload exceeds {REPORT_PAYLOAD_MAX_BYTES} bytes",
        )

    report = await report_service.create_report(
        agent_name=name,
        user_id=current_user.id,
        report_type=data.report_type,
        title=data.title,
        payload=data.payload,
        display_hint=data.display_hint,
        schema_version=data.schema_version,
        period_start=data.period_start,
        period_end=data.period_end,
    )
    return Report(**report)


@router.get("/agents/{name}/reports", response_model=List[ReportSummary])
async def list_agent_reports(
    name: AuthorizedAgent,
    report_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List one agent's reports (metadata only, newest first)."""
    rows = db.get_reports_for_agent(name, report_type=report_type, limit=limit, offset=offset)
    return [ReportSummary(**r) for r in rows]


@router.delete("/agents/{name}/reports/{report_id}", status_code=204)
async def delete_report(name: OwnedAgent, report_id: str):
    """Delete a report (owner/admin). Scoped by (agent_name, id) → 404 on mismatch."""
    if not db.delete_report(name, report_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return None


# ============================================================================
# Fleet-wide endpoints  (/api/reports)
# `/reports/stats` is declared before `/reports/{report_id}` (Invariant #4).
# ============================================================================

@router.get("/reports/stats", response_model=FleetReportStats)
async def get_fleet_report_stats(
    report_type: Optional[str] = Query(None),
    hours: int = Query(168, description="Time window in hours; 0 = all-time"),
    agent: Optional[str] = Query(None, description="Filter to a single agent"),
    current_user: User = Depends(get_current_user),
):
    """Aggregate stat-card data for the fleet Reports view."""
    agent_names = _narrow_to_agent(accessible_agent_names(current_user), agent)
    effective_hours = hours if hours in _VALID_HOURS else 168
    stats = db.get_fleet_report_stats(
        agent_names, report_type=report_type, hours=effective_hours or None
    )
    return FleetReportStats(**stats)


@router.get("/reports", response_model=List[ReportSummary])
async def list_fleet_reports(
    report_type: Optional[str] = Query(None),
    hours: int = Query(168, description="Time window in hours; 0 = all-time"),
    search: Optional[str] = Query(None, max_length=200),
    agent: Optional[str] = Query(None, description="Filter to a single agent"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """List reports across all accessible agents (metadata only)."""
    agent_names = _narrow_to_agent(accessible_agent_names(current_user), agent)
    effective_hours = hours if hours in _VALID_HOURS else 168
    rows = db.get_fleet_reports(
        agent_names,
        report_type=report_type,
        hours=effective_hours or None,
        search=search,
        limit=limit,
        offset=offset,
    )
    return [ReportSummary(**r) for r in rows]


@router.get("/reports/{report_id}", response_model=Report)
async def get_report(report_id: str, current_user: User = Depends(get_current_user)):
    """Full report incl. payload. 404 (not 403) on no-access to avoid id leak."""
    report = db.get_report(report_id)
    if not report or not db.can_user_access_agent(current_user.username, report["agent_name"]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return Report(**report)
