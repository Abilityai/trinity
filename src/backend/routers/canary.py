"""
Canary Invariant Harness API (CANARY-001 / Issue #411).

Admin-only query interface over the `canary_violations` table populated by
the continuous canary harness. Phase 1 ships this read endpoint plus the
`CanaryService` 5-minute background loop that posts to a Slack webhook
(`CANARY_SLACK_WEBHOOK_URL`) on green→red transitions; the table itself is
the source of truth for forensic replay and 24-hour trend tiles.

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
from models import (
    CanaryStatsResponse,
    CanaryViolation,
    CanaryViolationListResponse,
    CycleTransition,
    CycleViolation,
    RunCycleResponse,
    User,
)
from services.canary_alerts import severity_rank
from services.canary_service import canary_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/canary", tags=["canary"])


# ---------------------------------------------------------------------------
# Run-cycle request model — stays in this router (not models.py) because it
# evaluates `INVARIANTS` (from the canary library) at class-definition time,
# and the canary library imports back from models; centralizing it would
# invert the dependency direction. Allowlisted in test_models_centralized.py.
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
    no recomputation here — so the endpoint and the Slack webhook cannot
    disagree.
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
    started = time.monotonic()
    cycle = await canary_service.run_cycle(invariant_ids=requested_ids)
    duration_ms = int((time.monotonic() - started) * 1000)

    # 409 is the operator-friendly signal for "another cycle was mid-run, this
    # call did nothing." Without it the response is structurally identical to
    # a real green cycle (empty violations, empty transitions) and the caller
    # has no way to tell their request was skipped — see /review I2.
    if cycle.skipped:
        raise HTTPException(status_code=409, detail="cycle in progress")

    # Row ids come straight from the service via `persisted_violation_ids`
    # (index-aligned with `cycle.violations`); no re-query needed. A `None`
    # slot means the insert failed — we drop those from the response rather
    # than surface a stale row.
    snapshot_time = cycle.snapshot_time
    persisted: List[CycleViolation] = []
    transitions_out: List[CycleTransition] = []
    transition_set = set(cycle.transition_invariant_ids)

    for invariant_id, vlist in cycle.violations.items():
        ids = cycle.persisted_violation_ids.get(invariant_id, [])
        for v, row_id in zip(vlist, ids):
            if row_id is None:
                continue
            persisted.append(CycleViolation(
                id=row_id,
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
            worst = max(vlist, key=lambda v: severity_rank(v.severity))
            transitions_out.append(CycleTransition(
                invariant_id=invariant_id,
                severity=worst.severity,
                violations_in_cycle=len(vlist),
                # The service captured this from `previous_latest` BEFORE
                # the cycle's inserts, so it's the prior cycle's tail —
                # not the row we just wrote. `None` means first-ever
                # violation for this invariant.
                previous_violation_at=cycle.previous_violation_at.get(invariant_id),
            ))

    return RunCycleResponse(
        snapshot_time=snapshot_time,
        cycle_duration_ms=duration_ms,
        checks_run=list(requested_ids),
        sources_unavailable=cycle.sources_unavailable,
        violations=persisted,
        transitions=transitions_out,
    )
