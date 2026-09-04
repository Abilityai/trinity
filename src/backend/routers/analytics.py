# mcp: none — Overview chart data for the UI (#1107); agents get the same facts via get_agent_activity_summary / get_execution_result
"""Agent-scoped execution analytics (#1107).

Backs the Agent Detail "Overview" dashboard. Generalises the #868
per-schedule analytics to agent scope with a `triggered_by` type
breakdown. Read-only; `AuthorizedAgent`-gated; the window is validated
against an allow-list. All values are DB-sourced (no live container
call), so the Overview charts render even when the agent is stopped.
"""
from fastapi import APIRouter, HTTPException, Query, status

from database import db
from dependencies import AuthorizedAgent
from models import AgentAnalyticsResponse

router = APIRouter(prefix="/api/agents", tags=["analytics"])

# Selectable Overview windows → hours. 7 / 14 / 30 days (#1107).
_WINDOW_HOURS = {"7d": 168, "14d": 336, "30d": 720}


@router.get("/{name}/analytics", response_model=AgentAnalyticsResponse)
async def get_agent_analytics(
    name: AuthorizedAgent,
    window: str = Query("7d", description="One of 7d, 14d, 30d"),
):
    """Deterministic multi-day execution analytics for the Overview tab.

    Day-bucketed (UTC) execution counts grouped by user-facing type, plus
    per-day success rate, duration avg + p95, and avg context use over a
    selectable 7 / 14 / 30-day window.
    """
    hours = _WINDOW_HOURS.get(window)
    if hours is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"window must be one of {sorted(_WINDOW_HOURS)}",
        )
    return AgentAnalyticsResponse(**db.get_agent_analytics(name, hours))
