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

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status

from database import db
from dependencies import get_current_user
from models import (
    ExecutionTimeline,
    ExecutionTimelineBucket,
    FleetExecutionStats,
    FleetExecutionSummary,
    User,
)
from services.agent_service.helpers import accessible_agent_names, narrow_to_agent

router = APIRouter(prefix="/api/executions", tags=["executions"])

_VALID_STATUSES = {"running", "queued", "success", "failed", "error", "cancelled", "skipped"}
# NOTE: this is a filter ALLOW-LIST, not a DB enum — `triggered_by` is a plain
# TEXT column. An unknown value here degrades to "no filter" (see below), which
# silently returns EVERY execution instead of none, so a trigger that reaches
# this endpoint must be listed or its filter lies. `room` is ent#169, `a2a` is
# ent#157 (inbound A2A tasks reach `execute_task`, so they land in this list).
_VALID_TRIGGERS = {"schedule", "manual", "agent", "mcp", "chat", "session", "public", "webhook", "fan_out", "loop", "reminder", "room", "a2a", "operator_response"}
_VALID_HOURS = {0, 1, 6, 24, 168, 720}  # 0 = all-time

# ent#326. `hour`/`day` are gap-filled; `trigger`/`agent` are categorical and
# have no continuum to fill.
_VALID_GROUP_BY = {"hour", "day", "trigger", "agent"}
_GAP_FILLED_GROUPINGS = {"hour", "day"}
# ent#96: the optional SECOND dimension. Only over a time axis — splitting a
# `trigger` grouping by trigger is a tautology and splitting `agent` by trigger
# is a different chart nobody has asked for, so both are a named 422 rather
# than a silently ignored parameter (the #326 rule: an axis the caller did not
# request is a quietly wrong chart, and so is one they asked for and did not get).
_VALID_SPLIT = {"trigger"}
# All-time (`hours=0`) is refused for the gap-filled groupings: the x-axis would
# start at the fleet's first-ever execution and emit one bucket per hour since,
# which is a chart nobody asked for and a response nobody bounded. The scalar
# `/stats` endpoint has no such axis, which is why it can allow 0.
_MAX_GAP_FILLED_HOURS = 720


@router.get("/stats", response_model=FleetExecutionStats)
async def get_fleet_execution_stats(
    hours: int = Query(24, description="Time window in hours; 0 = all-time"),
    agent: Optional[str] = Query(None, description="Filter to a single agent"),
    current_user: User = Depends(get_current_user),
):
    """Aggregate stat-card data for the Unified Executions Dashboard header."""
    agent_names = narrow_to_agent(accessible_agent_names(current_user), agent)
    effective_hours = hours if hours in _VALID_HOURS else 24
    stats = db.get_fleet_execution_stats(agent_names, hours=effective_hours)
    return FleetExecutionStats(**stats)


@router.get("/timeline", response_model=ExecutionTimeline)
async def get_fleet_execution_timeline(
    group_by: str = Query("day", description="hour | day | trigger | agent"),
    hours: int = Query(168, description="Rolling window in hours"),
    agent: Optional[str] = Query(None, description="Filter to a single agent"),
    split: Optional[str] = Query(
        None, description="Optional second dimension over a time axis: trigger"
    ),
    current_user: User = Depends(get_current_user),
):
    """Bucketed fleet execution rollups for the grid's data tiles (ent#326).

    The time-series sibling of `/stats`: same table, same access scoping, one
    endpoint shared by the tiles so three of them don't each grow their own
    query.

    Registered BEFORE the `""` list route and any parameterized execution route
    (Invariant #4) — `/stats` already sits under this rule, and without it
    `timeline` would be readable as an execution id.

    Both parameters are validated to a NAMED 422 rather than being silently
    coerced. `/stats` and the list route degrade an unknown value to a default,
    which is right for a filter (worst case: more rows than asked for) and wrong
    for an axis — a chart drawn on a window the caller did not request is a
    quietly wrong chart, and ent#326 calls that out explicitly.
    """
    if group_by not in _VALID_GROUP_BY:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unsupported group_by '{group_by}'. "
                f"Expected one of: {', '.join(sorted(_VALID_GROUP_BY))}."
            ),
        )
    if hours not in _VALID_HOURS:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unsupported hours '{hours}'. "
                f"Expected one of: {', '.join(str(h) for h in sorted(_VALID_HOURS))}."
            ),
        )
    # `isinstance`, not `is not None`: called through FastAPI this is a `str |
    # None`, but the route is also called DIRECTLY by unit tests, where an
    # omitted parameter arrives as the `Query(...)` default OBJECT — truthy, and
    # not a member of `_VALID_SPLIT`, so a bare `is not None` turned every
    # direct call that did not mention `split` into a 422 (it broke ent#326's
    # own suite). Anything that is not a string is "no split requested".
    split = split if isinstance(split, str) else None
    if split is not None:
        if split not in _VALID_SPLIT:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Unsupported split '{split}'. "
                    f"Expected one of: {', '.join(sorted(_VALID_SPLIT))}."
                ),
            )
        if group_by not in _GAP_FILLED_GROUPINGS:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"split='{split}' needs a time grouping "
                    f"({' or '.join(sorted(_GAP_FILLED_GROUPINGS))}); "
                    f"group_by='{group_by}' is already categorical."
                ),
            )

    gap_filled = group_by in _GAP_FILLED_GROUPINGS
    if gap_filled and (hours == 0 or hours > _MAX_GAP_FILLED_HOURS):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"group_by='{group_by}' needs a bounded window "
                f"(1..{_MAX_GAP_FILLED_HOURS} hours); all-time would emit one "
                "bucket per interval since the fleet's first execution."
            ),
        )

    agent_names = narrow_to_agent(accessible_agent_names(current_user), agent)
    rows = db.get_fleet_execution_timeline(
        agent_names, group_by=group_by, hours=hours, split=split
    )

    # Folding and gap-filling live in the db layer beside the query
    # (Invariant #1, and the #1107 precedent): `_bucket_for_trigger` and
    # the continuous UTC axis already exist there, and a second copy in a
    # router is how the two drift.
    rows = db.shape_execution_timeline(
        rows, group_by=group_by, hours=hours, split=split
    )

    return ExecutionTimeline(
        group_by=group_by,
        hours=hours,
        gap_filled=gap_filled,
        buckets=[ExecutionTimelineBucket(**r) for r in rows],
        split=split,
        # Served from the db layer's own constant so the tile cannot hold a
        # stale copy of the stack order (ent#96 AC1).
        trigger_order=list(db.trigger_bucket_order()) if split == "trigger" else None,
    )


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
    agent_names = narrow_to_agent(accessible_agent_names(current_user), agent)
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
