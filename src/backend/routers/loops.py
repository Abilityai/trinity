"""
Loops router — sequential bounded task execution (#740).

POST /api/agents/{name}/loops           Start a loop, return immediately.
GET  /api/agents/{name}/loops           List loops for an agent.
GET  /api/loops/{loop_id}               Status + per-run summaries.
POST /api/loops/{loop_id}/stop          Graceful stop.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field, field_validator

from database import db
from dependencies import get_authorized_agent, get_current_user
from models import User
from services.loop_service import get_loop_service

logger = logging.getLogger(__name__)


# Two routers — agent-scoped + loop-scoped — sharing the same module so
# main.py only needs one import.
agent_router = APIRouter(prefix="/api/agents", tags=["loops"])
loop_router = APIRouter(prefix="/api/loops", tags=["loops"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

MAX_RUNS_LIMIT = 100
MAX_MESSAGE_LEN = 100_000
MAX_DELAY_SECONDS = 3600
MAX_TIMEOUT_PER_RUN = 7200
MAX_STOP_SIGNAL_LEN = 200


class StartLoopRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)
    max_runs: int = Field(..., ge=1, le=MAX_RUNS_LIMIT)
    stop_signal: Optional[str] = Field(default=None, max_length=MAX_STOP_SIGNAL_LEN)
    delay_seconds: int = Field(default=0, ge=0, le=MAX_DELAY_SECONDS)
    timeout_per_run: Optional[int] = Field(default=None, ge=10, le=MAX_TIMEOUT_PER_RUN)
    model: Optional[str] = None
    allowed_tools: Optional[List[str]] = None

    @field_validator("stop_signal")
    @classmethod
    def _normalize_stop_signal(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None  # empty after strip → fixed mode


class StartLoopResponse(BaseModel):
    loop_id: str
    status: str
    agent_name: str
    max_runs: int


class LoopRunResponse(BaseModel):
    run_number: int
    execution_id: Optional[str] = None
    status: str
    response_preview: Optional[str] = None
    cost: Optional[float] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    started_at: str
    completed_at: Optional[str] = None


class LoopStatusResponse(BaseModel):
    loop_id: str
    agent_name: str
    status: str
    max_runs: int
    runs_completed: int
    stop_reason: Optional[str] = None
    last_response: Optional[str] = None
    error: Optional[str] = None
    runs: List[LoopRunResponse]
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class StopLoopResponse(BaseModel):
    loop_id: str
    status: str  # "stopping" | "already_done"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RESPONSE_PREVIEW_CHARS = 500


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
    service = get_loop_service()
    loop_row = await service.start_loop(
        agent_name=name,
        message_template=payload.message,
        max_runs=payload.max_runs,
        stop_signal=payload.stop_signal,
        delay_seconds=payload.delay_seconds,
        timeout_per_run=payload.timeout_per_run,
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
