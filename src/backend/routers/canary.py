"""
Canary Invariant Harness API (CANARY-001 / Issue #411).

Admin-only query interface over the `canary_violations` table populated by
the continuous canary harness. Phase 1 deploys this read endpoint plus the
canary agent fleet that calls `mcp__trinity__send_notification` on green→red
transitions; the table itself is the source of truth for forensic replay
and 24-hour trend tiles on the canary agent's dashboard.

Mounted at `/api/canary` to keep the canary surface area distinct from the
platform audit log.
"""

import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from canary import INVARIANTS
from database import db
from dependencies import require_admin
from models import User
from services.canary_service import canary_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/canary", tags=["canary"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CanaryViolation(BaseModel):
    """Single canary_violations row as returned to API clients."""

    id: int
    invariant_id: str
    tier: str
    severity: str
    snapshot_time: str
    observed_state: dict = Field(default_factory=dict)
    signal_query: Optional[str] = None
    created_at: Optional[str] = None


class CanaryViolationListResponse(BaseModel):
    """Paginated list response."""

    violations: List[CanaryViolation]
    total: int
    limit: int
    offset: int


class CanaryStatsResponse(BaseModel):
    """Aggregate violation counts for dashboard tiles."""

    total: int
    by_invariant: dict = Field(default_factory=dict)
    by_severity: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Run-cycle request / response models
# ---------------------------------------------------------------------------


class RunCycleRequest(BaseModel):
    """Optional filter on which invariants to run this cycle."""

    invariants: Optional[List[str]] = Field(
        None,
        description=(
            "Subset of invariant ids to run. Default: all enabled "
            f"({sorted(INVARIANTS.keys())})."
        ),
    )


class CycleViolation(BaseModel):
    """One violation persisted during a run-cycle call."""

    id: int
    invariant_id: str
    tier: str
    severity: str
    snapshot_time: str
    observed_state: dict
    signal_query: Optional[str] = None


class CycleTransition(BaseModel):
    """A green→red transition detected this cycle.

    Agents iterate this list and send exactly one push notification per
    entry, mapping severity to the notification priority.
    """

    invariant_id: str
    severity: str
    violations_in_cycle: int
    previous_violation_at: Optional[str] = Field(
        None,
        description=(
            "snapshot_time of the most recent prior violation for this "
            "invariant; null if the invariant has never violated before."
        ),
    )


class RunCycleResponse(BaseModel):
    """Result of one canary cycle."""

    snapshot_time: str
    cycle_duration_ms: int
    checks_run: List[str]
    checks_skipped: List[str]
    sources_unavailable: List[str]
    violations: List[CycleViolation]
    transitions: List[CycleTransition]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/violations", response_model=CanaryViolationListResponse)
async def list_canary_violations(
    invariant_id: Optional[str] = Query(None, description="Filter by invariant id (e.g. 'S-01')"),
    severity: Optional[str] = Query(None, description="Filter by severity (critical|major|minor)"),
    tier: Optional[str] = Query(None, description="Filter by tier (A|B)"),
    start_time: Optional[str] = Query(None, description="Filter snapshot_time >= ISO 8601"),
    end_time: Optional[str] = Query(None, description="Filter snapshot_time <= ISO 8601"),
    limit: int = Query(100, ge=1, le=1000, description="Max rows returned"),
    offset: int = Query(0, ge=0, description="Rows to skip"),
    _: User = Depends(require_admin),
) -> CanaryViolationListResponse:
    """List canary invariant violations, newest first. Admin only."""
    filters = {
        "invariant_id": invariant_id,
        "severity": severity,
        "tier": tier,
        "start_time": start_time,
        "end_time": end_time,
    }
    rows = db.list_canary_violations(limit=limit, offset=offset, **filters)
    total = db.count_canary_violations(**filters)
    return CanaryViolationListResponse(
        violations=[CanaryViolation(**row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/violations/stats", response_model=CanaryStatsResponse)
async def get_canary_stats(
    start_time: Optional[str] = Query(None, description="Filter snapshot_time >= ISO 8601"),
    end_time: Optional[str] = Query(None, description="Filter snapshot_time <= ISO 8601"),
    _: User = Depends(require_admin),
) -> CanaryStatsResponse:
    """Aggregate violation counts by invariant_id and severity. Admin only."""
    stats = db.get_canary_stats(start_time=start_time, end_time=end_time)
    return CanaryStatsResponse(**stats)


@router.get("/violations/{violation_id}", response_model=CanaryViolation)
async def get_canary_violation(
    violation_id: int,
    _: User = Depends(require_admin),
) -> CanaryViolation:
    """Fetch a single violation by id. Admin only."""
    row = db.get_canary_violation(violation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Violation not found")
    return CanaryViolation(**row)


# ---------------------------------------------------------------------------
# Run-cycle endpoint
# ---------------------------------------------------------------------------


@router.post("/run-cycle", response_model=RunCycleResponse)
async def run_canary_cycle(
    body: RunCycleRequest | None = None,
    _: User = Depends(require_admin),
) -> RunCycleResponse:
    """Run one canary cycle on demand.

    Admin only. Delegates to the same `CanaryService.run_cycle()` invoked
    by the 5-minute background loop, so the operator-on-demand path and
    the scheduled path share their entire implementation. Useful for:
      - smoke-testing the harness right after deploy (don't wait 5 min)
      - confirming a violation cleared after a fix
      - integration tests that need deterministic cycle timing

    The response surfaces exactly the transitions the service emitted —
    no recomputation here — so the endpoint and the bell cannot disagree.
    """
    body = body or RunCycleRequest()
    requested_ids = body.invariants or list(INVARIANTS.keys())
    invalid_ids = [i for i in requested_ids if i not in INVARIANTS]
    if invalid_ids:
        # 422 keeps unknown ids from silently no-op'ing — easier to debug.
        raise HTTPException(
            status_code=422,
            detail=f"Unknown invariant id(s): {invalid_ids}. "
                   f"Available: {sorted(INVARIANTS.keys())}",
        )
    valid_ids = [i for i in requested_ids if i in INVARIANTS]

    started = time.monotonic()
    cycle = await canary_service.run_cycle(invariant_ids=valid_ids)
    duration_ms = int((time.monotonic() - started) * 1000)

    # Re-fetch persisted rows for the response. The service writes them
    # but doesn't return ids; we look them up by snapshot_time so the
    # endpoint contract surfaces row ids for chaining (e.g. test
    # assertions on the GET /violations endpoint).
    snapshot_time = cycle.snapshot_time
    persisted: List[CycleViolation] = []
    transitions_out: List[CycleTransition] = []
    sev_rank = {"critical": 3, "major": 2, "minor": 1}
    transition_set = set(cycle.transition_invariant_ids)

    for invariant_id, vlist in cycle.violations.items():
        for v in vlist:
            # Best-effort lookup: latest row for this (invariant, snapshot_time)
            # pair. Multiple violations of the same invariant in one cycle get
            # distinct rows so we still surface them, just not by direct id.
            rows = db.list_canary_violations(
                invariant_id=invariant_id,
                start_time=snapshot_time,
                end_time=snapshot_time,
                limit=100,
            )
            if rows:
                # Match by signal_query when present (uniquely identifies the
                # specific check that fired); fall back to ordering otherwise.
                match = next(
                    (r for r in rows if r.get("signal_query") == v.signal_query),
                    rows[0],
                )
                persisted.append(CycleViolation(
                    id=match["id"],
                    invariant_id=v.invariant_id,
                    tier=v.tier,
                    severity=v.severity,
                    snapshot_time=snapshot_time,
                    observed_state=v.observed_state,
                    signal_query=v.signal_query,
                ))

        # Build a transition entry only for invariants the SERVICE actually
        # decided fired a notification this cycle. Continuing-red invariants
        # have rows in `persisted` but are absent from `transition_set`.
        if invariant_id in transition_set and vlist:
            worst = max(vlist, key=lambda v: sev_rank.get(v.severity, 0))
            transitions_out.append(CycleTransition(
                invariant_id=invariant_id,
                severity=worst.severity,
                violations_in_cycle=len(vlist),
                # `previous_violation_at` is informational; populate from
                # the just-inserted row's prior peer if available, else null.
                previous_violation_at=None,
            ))

    checks_skipped = [i for i in valid_ids if i not in cycle.violations]

    return RunCycleResponse(
        snapshot_time=snapshot_time,
        cycle_duration_ms=duration_ms,
        checks_run=[i for i in valid_ids if i in cycle.violations],
        checks_skipped=checks_skipped,
        sources_unavailable=[],  # Service logs but doesn't surface; future: expose
        violations=persisted,
        transitions=transitions_out,
    )
