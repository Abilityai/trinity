# mcp: executions.ts (get_agent_activity_summary → /api/activities/timeline)
"""
Activities Router

Cross-agent activity timeline and queries.
"""
from fastapi import APIRouter, Depends, Query
from typing import Optional, List
from database import db
from dependencies import get_current_user
from models import User

router = APIRouter(prefix="/api/activities", tags=["activities"])


@router.get("/timeline")
async def get_activity_timeline(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    activity_types: Optional[str] = Query(None, description="Comma-separated list of activity types"),
    limit: int = 100,
    current_user: User = Depends(get_current_user)
):
    """
    Get cross-agent activity timeline with access control.

    Only returns activities for agents the user can access.
    """
    # Parse activity types
    types_list = None
    if activity_types:
        types_list = [t.strip() for t in activity_types.split(',')]

    # #1265: push the per-user access filter into SQL (None = admin, no filter)
    # so we fetch exactly `limit` rows instead of over-fetching limit*2 and
    # filtering in Python.
    #
    # Admin scope note: `accessible_agent_names` returns None for admins, so the
    # admin timeline is unfiltered and surfaces the full audit history — including
    # activity rows of agents whose containers were since deleted. This is
    # intentional (admins should see complete history). The prior code filtered
    # admins to Docker-present agents only via get_accessible_agents(); that
    # incidental narrowing is dropped on purpose, not by accident.
    from services.agent_service.helpers import accessible_agent_names
    allowed = accessible_agent_names(current_user)

    activities = db.get_activities_in_range(
        start_time=start_time,
        end_time=end_time,
        activity_types=types_list,
        limit=limit,
        agent_names=allowed,
    )

    return {
        "count": len(activities),
        "activities": activities
    }
