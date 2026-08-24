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


def _redact_for_agent_principal(row: dict, current_user: User) -> dict:
    """Strip a Workspace rating's free text when the RATED agent is reading.

    The ent#366 grooming decision, made explicitly rather than inherited: an
    agent may read its own tallies — three people finding an answer unhelpful is
    a signal worth having — but never the verbatim words. Two reasons, and the
    second is the harder one:

      * a rating the agent can read is a feedback loop it may start optimizing
        for, which is what the issue flags;
      * the comment is untrusted free text written by an annoyed stranger, so
        handing it to the rated agent verbatim is a prompt-injection path INTO
        the thing being criticised.

    Only the comment is withheld. Quality, target and evaluator stay, so the
    agent can count its ratings and an operator surface loses nothing.
    """
    if not current_user.agent_name:
        return row
    if not row.get("comment"):
        return row
    redacted = dict(row)
    redacted["comment"] = None
    redacted["comment_withheld"] = True
    return redacted


def _to_response(row: dict, current_user: User) -> EvaluationResponse:
    """Project a row for THIS caller.

    `current_user` is required rather than optional on purpose. The first
    version of ent#366 redacted inside `list_agent_evaluations` only, which left
    the fleet list and the by-id read handing the rated agent its own comments —
    an agent-scoped key resolves to its OWNER, so `accessible_agent_names`
    includes the agent itself. That is the "defense added to one of N call
    sites" class this repo keeps re-learning (#686, #1264, #1153). Threading the
    caller through the single projection makes the redaction structural: a new
    read path cannot compile without deciding.
    """
    row = _redact_for_agent_principal(row, current_user)
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
        target_kind=row.get("target_kind"),
        target_id=row.get("target_id"),
        comment=row.get("comment"),
        comment_withheld=bool(row.get("comment_withheld")),
        updated_at=row.get("updated_at"),
    )


@router.get("/agents/{agent_name}/evaluations", response_model=List[EvaluationResponse])
async def list_agent_evaluations(
    agent_name: str = Depends(get_authorized_agent_by_name),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
):
    """An agent's own evaluations (newest first). `AuthorizedAgentByName` gates
    read to owner/admin/agent-self — reading feedback is allowed; writing is not."""
    # ent#366: an agent-scoped caller gets the tallies and not the words —
    # applied inside `_to_response`, so every read path below inherits it.
    return [_to_response(r, current_user) for r in db.list_agent_evaluations(agent_name, limit)]


@router.get("/evaluations", response_model=List[EvaluationResponse])
async def list_fleet_evaluations(
    limit: int = Query(100, ge=1, le=500),
    agent: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    """Fleet evaluations across accessible agents (admin = all)."""
    names = narrow_to_agent(accessible_agent_names(current_user), agent)
    return [_to_response(r, current_user) for r in db.list_fleet_evaluations(names, limit)]


@router.get("/evaluations/{eval_id}", response_model=EvaluationResponse)
async def get_evaluation(eval_id: str, current_user: User = Depends(get_current_user)):
    row = db.get_agent_evaluation(eval_id)
    if not row:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    # tenant boundary: non-admin may only see an evaluation of an accessible agent
    names = accessible_agent_names(current_user)
    if names is not None and row["agent_name"] not in names:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return _to_response(row, current_user)


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
    return _to_response(row, current_user)
