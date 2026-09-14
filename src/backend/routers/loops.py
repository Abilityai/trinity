"""
Loops router — sequential bounded task execution (#740).

POST /api/agents/{name}/loops           Start a loop, return immediately.
GET  /api/agents/{name}/loops           List loops for an agent.
GET  /api/loops/{loop_id}               Status + per-run summaries.
POST /api/loops/{loop_id}/stop          Graceful stop.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header

from database import db
from dependencies import get_authorized_agent, get_current_user, assert_agent_access
from models import (
    LoopRunResponse,
    LoopStatusResponse,
    StartLoopRequest,
    StartLoopResponse,
    StopLoopResponse,
    User,
)
from services.loop_service import get_loop_service

logger = logging.getLogger(__name__)


# Two routers — agent-scoped + loop-scoped — sharing the same module so
# main.py only needs one import.
agent_router = APIRouter(prefix="/api/agents", tags=["loops"])
loop_router = APIRouter(prefix="/api/loops", tags=["loops"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RESPONSE_PREVIEW_CHARS = 500

# Fallback per-run timeout used for deadline validation when neither
# timeout_per_run nor an agent-specific timeout is available (#1156).
DEFAULT_PER_RUN_TIMEOUT = 3600


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse a utc_now_iso() timestamp (ISO-Z) to an aware UTC datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed_seconds(loop: dict) -> Optional[int]:
    """Whole seconds from started_at to completed_at (terminal) or now.

    None until the loop has started. Powers the GET deadline/elapsed view
    (#1156) so operators can see how close a running loop is to its bound.
    """
    started = _parse_iso(loop.get("started_at"))
    if started is None:
        return None
    end = _parse_iso(loop.get("completed_at")) or datetime.now(timezone.utc)
    return max(0, int((end - started).total_seconds()))


def _build_status_response(loop: dict) -> LoopStatusResponse:
    runs_raw = db.list_loop_runs(loop["id"])
    # #1155: total_cost is computed on read (no stored column to drift) — the
    # sum of every run's cost (NULL→0). sum(()) is 0.0, so a zero-run loop
    # reports 0.0 verbatim with no None special-case.
    total_cost = sum((r["cost"] or 0.0) for r in runs_raw)
    runs: List[LoopRunResponse] = []
    for r in runs_raw:
        response_preview = None
        if r["response"]:
            response_preview = r["response"][:RESPONSE_PREVIEW_CHARS]
        runs.append(LoopRunResponse(
            run_number=r["run_number"],
            execution_id=r["execution_id"],
            status=r["status"],
            response_preview=response_preview,
            cost=r["cost"],
            duration_ms=r["duration_ms"],
            error=r["error"],
            started_at=r["started_at"],
            completed_at=r["completed_at"],
        ))
    return LoopStatusResponse(
        loop_id=loop["id"],
        agent_name=loop["agent_name"],
        status=loop["status"],
        max_runs=loop["max_runs"],
        runs_completed=loop["runs_completed"],
        failed_runs=loop.get("failed_runs", 0) or 0,
        on_failure=loop.get("on_failure") or "abort",
        max_consecutive_failures=loop.get("max_consecutive_failures") or 3,
        stop_reason=loop["stop_reason"],
        last_response=loop["last_response"],
        error=loop["error"],
        runs=runs,
        created_at=loop["created_at"],
        started_at=loop["started_at"],
        completed_at=loop["completed_at"],
        max_duration_seconds=loop.get("max_duration_seconds"),
        elapsed_seconds=_elapsed_seconds(loop),
        max_cost_usd=loop.get("max_cost_usd"),
        total_cost=total_cost,
        no_progress_threshold=loop.get("no_progress_threshold"),
    )


def _check_loop_access(loop: dict, user: User) -> None:
    """Caller must be the loop initiator, agent owner, or admin."""
    if user.role == "admin":
        return
    if loop["started_by_user_id"] == user.id:
        return
    # Fall back to agent ownership/sharing check.
    assert_agent_access(user, loop["agent_name"])


def _reject_timeout_above_cap(agent_name: str, requested_seconds: Optional[int]) -> None:
    """Refuse a per-run loop timeout above the agent's cap (ent#338).

    `agent_ownership.execution_timeout_seconds` is the per-agent ceiling, and
    nothing downstream re-applies it: `task_execution_service` reads the cap
    only when the caller passed no `timeout_seconds`, so an explicit
    `timeout_per_run` was handed straight to dispatch. A loop could therefore
    run iterations LONGER than the ceiling its owner set — a bypass, not merely
    a number displayed wrongly, and one that multiplies: `max_runs` is up to 100.

    Refuse rather than clamp, mirroring #929 for schedules (`ScheduleCreate`'s
    closest sibling — explicit, human-set config). A silent clamp would start a
    loop whose guardrails differ from the ones the caller was shown, which is
    exactly what ent#458 puts on screen before Start. `agent_cap_seconds` rides
    the structured detail so a UI can show the real bound instead of guessing.

    Reminders clamp instead (#1296); that is deliberate and different — there
    the timeout is derived from an agent's own request mid-turn, with no human
    reading a form.
    """
    if requested_seconds is None:
        return
    try:
        cap = db.get_execution_timeout(agent_name)
    except Exception:
        return                      # fail-open: never block a loop on a cap read
    if requested_seconds > cap:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "loop_timeout_exceeds_agent_cap",
                "message": (
                    f"Loop timeout_per_run {requested_seconds}s exceeds agent "
                    f"execution_timeout_seconds {cap}s. Raise the agent cap "
                    f"first via PUT /api/agents/{agent_name}/timeout."
                ),
                "agent_cap_seconds": cap,
                "requested_seconds": requested_seconds,
            },
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@agent_router.post("/{name}/loops", response_model=StartLoopResponse, status_code=202)
async def start_loop(
    payload: StartLoopRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None),
):
    """Start a sequential agent loop; return loop_id immediately (202)."""
    # ent#338: the per-agent ceiling first, so the deadline check below compares
    # against a per-run timeout that is known to be allowed.
    _reject_timeout_above_cap(name, payload.timeout_per_run)

    # #1156: a deadline shorter than a single run can never let even one
    # iteration finish — reject it. Compare against the effective per-run
    # timeout (explicit override, else the agent's configured timeout).
    if payload.max_duration_seconds is not None:
        effective_per_run = payload.timeout_per_run
        if effective_per_run is None:
            try:
                effective_per_run = db.get_execution_timeout(name)
            except Exception:
                effective_per_run = DEFAULT_PER_RUN_TIMEOUT
        if payload.max_duration_seconds < effective_per_run:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"max_duration_seconds ({payload.max_duration_seconds}s) must be "
                    f">= the per-run timeout ({effective_per_run}s); otherwise no "
                    f"iteration could complete before the deadline."
                ),
            )

    service = get_loop_service()
    loop_row = await service.start_loop(
        agent_name=name,
        message_template=payload.message,
        max_runs=payload.max_runs,
        stop_signal=payload.stop_signal,
        delay_seconds=payload.delay_seconds,
        timeout_per_run=payload.timeout_per_run,
        max_duration_seconds=payload.max_duration_seconds,
        max_cost_usd=payload.max_cost_usd,
        no_progress_threshold=payload.no_progress_threshold,
        on_failure=payload.on_failure,
        max_consecutive_failures=payload.max_consecutive_failures,
        model=payload.model,
        allowed_tools=payload.allowed_tools,
        started_by_user_id=current_user.id,
        started_by_user_email=current_user.email,
        source_agent_name=x_source_agent,
        # #2389: the credential actually presented, never the forgeable X-MCP-Key-* headers.
        source_mcp_key_id=getattr(current_user, "mcp_key_id", None),
        source_mcp_key_name=getattr(current_user, "mcp_key_name", None),
    )
    return StartLoopResponse(
        loop_id=loop_row["id"],
        status=loop_row["status"],
        agent_name=name,
        max_runs=payload.max_runs,
        on_failure=payload.on_failure,
    )


@agent_router.get("/{name}/loops", response_model=List[LoopStatusResponse])
def list_loops(
    name: str = Depends(get_authorized_agent),
    status: Optional[str] = None,
    limit: int = 50,
):
    """List loops for the agent (most recent first), optional status filter."""
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be 1–200")
    loops = db.list_loops_for_agent(name, status=status, limit=limit)
    return [_build_status_response(loop) for loop in loops]


@loop_router.get("/{loop_id}", response_model=LoopStatusResponse)
def get_loop_status(loop_id: str, current_user: User = Depends(get_current_user)):
    loop = db.get_loop(loop_id)
    if loop is None:
        raise HTTPException(status_code=404, detail="Loop not found")
    _check_loop_access(loop, current_user)
    return _build_status_response(loop)


@loop_router.post("/{loop_id}/stop", response_model=StopLoopResponse)
async def stop_loop(loop_id: str, current_user: User = Depends(get_current_user)):
    loop = db.get_loop(loop_id)
    if loop is None:
        raise HTTPException(status_code=404, detail="Loop not found")
    _check_loop_access(loop, current_user)
    service = get_loop_service()
    outcome = await service.stop_loop(loop_id)
    return StopLoopResponse(loop_id=loop_id, status=outcome)
