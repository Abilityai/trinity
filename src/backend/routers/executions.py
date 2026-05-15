"""
Fleet-level execution endpoints (EXEC-022 / Issue #18).

Provides a unified view of all task executions across every agent the caller
can access, with filtering and aggregate stats for the Unified Executions
Dashboard at /executions.

Access control mirrors fleet.py:
- admin → sees every execution (agent_names=None, no SQL filter)
- non-admin → sees only accessible agents (owned + shared)
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from database import db
from dependencies import get_current_user
from models import FleetExecutionStats, FleetExecutionSummary, User
from services.agent_service.helpers import accessible_agent_names

router = APIRouter(prefix="/api/executions", tags=["executions"])

_VALID_STATUSES = {"running", "queued", "success", "failed", "error", "cancelled", "skipped"}
_VALID_TRIGGERS = {"schedule", "manual", "agent", "mcp", "chat", "session", "public", "webhook", "fan_out"}
_VALID_HOURS = {0, 1, 6, 24, 168, 720}  # 0 = all-time


def _narrow_to_agent(
    agent_names: Optional[List[str]], agent: Optional[str]
) -> Optional[List[str]]:
    """Narrow the accessible-agent set to a single agent if ?agent= is provided."""
    if not agent:
        return agent_names
    if agent_names is None:
        return [agent]  # admin: any single agent is fine
    return [agent] if agent in agent_names else []  # non-admin: access-gate


@router.get("/stats", response_model=FleetExecutionStats)
async def get_fleet_execution_stats(
    hours: int = Query(24, description="Time window in hours; 0 = all-time"),
    agent: Optional[str] = Query(None, description="Filter to a single agent"),
    current_user: User = Depends(get_current_user),
):
    """Aggregate stat-card data for the Unified Executions Dashboard header."""
    agent_names = _narrow_to_agent(accessible_agent_names(current_user), agent)
    effective_hours = hours if hours in _VALID_HOURS else 24
    stats = db.get_fleet_execution_stats(agent_names, hours=effective_hours)
    return FleetExecutionStats(**stats)


@router.get("", response_model=List[FleetExecutionSummary])
async def list_fleet_executions(
    status: Optional[str] = Query(None),
    triggered_by: Optional[str] = Query(None),
    hours: int = Query(24, description="Time window in hours; 0 = all-time"),
    search: Optional[str] = Query(None, max_length=200),
    agent: Optional[str] = Query(None, description="Filter to a single agent"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """List executions across all accessible agents with optional filters."""
    agent_names = _narrow_to_agent(accessible_agent_names(current_user), agent)
    rows = db.get_fleet_executions(
        agent_names,
        status=status if status in _VALID_STATUSES else None,
        triggered_by=triggered_by if triggered_by in _VALID_TRIGGERS else None,
        hours=hours if hours in _VALID_HOURS else 24,
        search=search,
        limit=limit,
        offset=offset,
    )
    return [FleetExecutionSummary(**r) for r in rows]
