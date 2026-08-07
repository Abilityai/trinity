"""Behavioral evaluations — the referee surface (ent#206).

Three-layer (Invariant #1). The load-bearing rule of the eval epic: **the graded
agent must never write its own grade.** Enforced structurally here —

* **Write** (`POST /api/agents/{name}/evaluations`) is **human-admin-only**:
  `require_admin` (role) AND `reject_agent_principal` (so an agent-scoped key —
  which resolves to its owner and inherits the owner's role — can never reach it,
  the trinity-ops-agent#232 trap). In child 3 the Tier-0 evaluator writes
  server-side via `db.create_agent_evaluation`, and a dedicated evaluator agent
  gets a narrow write grant; the *graded* agent has no write path either way.
* **Read** is access-controlled: an agent (or its owner) reads its own
  evaluations; an admin reads all. Read ≠ write — feedback is fine, self-grading
  is not.

`agent_reports` (#918) was rejected as the surface precisely because its create
is self-gated (an agent writes its own). This surface inverts that.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from database import db
from dependencies import (
    get_current_user,
    get_authorized_agent_by_name,
    reject_agent_principal,
    require_admin,
)
from models import EvaluationCreate, EvaluationResponse, User
from services.agent_service.helpers import accessible_agent_names, narrow_to_agent

router = APIRouter(prefix="/api", tags=["evaluations"])


def _to_response(row: dict) -> EvaluationResponse:
    return EvaluationResponse(
        id=row["id"],
        agent_name=row["agent_name"],
        execution_id=row.get("execution_id"),
        archetype=row.get("archetype"),
        completion=(None if row.get("completion") is None else bool(row["completion"])),
        quality=row.get("quality"),
        checks=row.get("checks"),
        judge=row.get("judge"),
        evaluator=row["evaluator"],
        created_at=row["created_at"],
    )


@router.get("/agents/{agent_name}/evaluations", response_model=List[EvaluationResponse])
async def list_agent_evaluations(
    agent_name: str = Depends(get_authorized_agent_by_name),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
):
    """An agent's own evaluations (newest first). `AuthorizedAgentByName` gates
    read to owner/admin/agent-self — reading feedback is allowed; writing is not."""
    return [_to_response(r) for r in db.list_agent_evaluations(agent_name, limit)]


@router.get("/evaluations", response_model=List[EvaluationResponse])
async def list_fleet_evaluations(
    limit: int = Query(100, ge=1, le=500),
    agent: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    """Fleet evaluations across accessible agents (admin = all)."""
    names = narrow_to_agent(accessible_agent_names(current_user), agent)
    return [_to_response(r) for r in db.list_fleet_evaluations(names, limit)]


@router.get("/evaluations/{eval_id}", response_model=EvaluationResponse)
async def get_evaluation(eval_id: str, current_user: User = Depends(get_current_user)):
    row = db.get_agent_evaluation(eval_id)
    if not row:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    # tenant boundary: non-admin may only see an evaluation of an accessible agent
    names = accessible_agent_names(current_user)
    if names is not None and row["agent_name"] not in names:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return _to_response(row)


@router.post("/agents/{agent_name}/evaluations", response_model=EvaluationResponse)
async def create_evaluation(
    agent_name: str,
    body: EvaluationCreate,
    current_user: User = Depends(require_admin),
):
    """Write an evaluation for `agent_name` — human-admin-only (the write-fence).

    `require_admin` already rejects connector keys and checks the role, but an
    agent-scoped key inherits its owner's role; `reject_agent_principal` closes
    that so no agent — graded or otherwise — can write a grade via this route.
    """
    reject_agent_principal(current_user)
    row = db.create_agent_evaluation(
        agent_name,
        evaluator=f"admin:{current_user.username}",
        execution_id=body.execution_id,
        archetype=body.archetype,
        completion=body.completion,
        quality=body.quality,
        checks=body.checks,
        judge=body.judge,
    )
    return _to_response(row)
