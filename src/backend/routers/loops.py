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
from dependencies import get_authorized_agent, get_current_user
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
        stop_reason=loop["stop_reason"],
        last_response=loop["last_response"],
        error=loop["error"],
        runs=runs,
        created_at=loop["created_at"],
        started_at=loop["started_at"],
        completed_at=loop["completed_at"],
        max_duration_seconds=loop.get("max_duration_seconds"),
        elapsed_seconds=_elapsed_seconds(loop),
        no_progress_threshold=loop.get("no_progress_threshold"),
    )


def _check_loop_access(loop: dict, user: User) -> None:
    """Caller must be the loop initiator, agent owner, or admin."""
    if user.role == "admin":
        return
    if loop["started_by_user_id"] == user.id:
        return
    # Fall back to agent ownership/sharing check.
    if db.can_user_access_agent(user.username, loop["agent_name"]):
        return
    raise HTTPException(status_code=403, detail="Access denied")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@agent_router.post("/{name}/loops", response_model=StartLoopResponse, status_code=202)
async def start_loop(
    payload: StartLoopRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None),
    x_mcp_key_id: Optional[str] = Header(None),
    x_mcp_key_name: Optional[str] = Header(None),
):
    """Start a sequential agent loop; return loop_id immediately (202)."""
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
        no_progress_threshold=payload.no_progress_threshold,
        model=payload.model,
        allowed_tools=payload.allowed_tools,
        started_by_user_id=current_user.id,
        started_by_user_email=current_user.email,
        source_agent_name=x_source_agent,
        source_mcp_key_id=x_mcp_key_id,
        source_mcp_key_name=x_mcp_key_name,
    )
    return StartLoopResponse(
        loop_id=loop_row["id"],
        status=loop_row["status"],
        agent_name=name,
        max_runs=payload.max_runs,
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
