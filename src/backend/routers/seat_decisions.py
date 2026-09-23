# mcp: decisions.ts (record_decision, list_seat_decisions, get_autonomy)
"""
Seat decision record — the companion's half (trinity-enterprise#638, R25).

A role companion records why a thing was approved, deferred or killed for the
seat it is serving, and reads the seat's standing decisions to reuse a
criterion (and cite it). Same security model as the MEM-001 write path:

- The caller never supplies a person. The seat is resolved from the execution
  identified by `execution_id`, after verifying the execution belongs to the
  calling agent — a scheduled run addressed to one person (ent#498/#637), or a
  user-facing channel turn with a verified email.
- The companion never sees an email back: the agent-facing shape labels the
  decider `seat` / `owner` (the assignment provider's "never an email" rule).
- `Idempotency-Key` is honoured (Invariant #18): the MCP tool derives one from
  `(execution_id, decided)`, so a retried call records once.

The person's half — read every readable seat, record, correct, close — is the
Workspace route in `client_portal/router.py`.
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from models import RecordDecisionRequest
from database import db
from dependencies import get_current_user, assert_agent_access
from db_models import User
from services import idempotency_service, rate_limiter, schedule_seat_memory, seat_decision_service
from services.assignment_provider import resolve_assignment

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agents", tags=["seat-decisions"])

_USER_FACING_TRIGGERS = {"public", "slack", "telegram", "whatsapp"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _seat_for(agent_name: str, execution_id: str) -> tuple[str, object]:
    """The seat an execution serves, or a named HTTP error. Mirrors
    `public_memory.write_user_memory` — one rule for "which person"."""
    execution = db.get_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution.agent_name != agent_name:
        raise HTTPException(status_code=403, detail="Execution does not belong to this agent")
    triggered_by = (execution.triggered_by or "").lower()
    seat = schedule_seat_memory.seat_for_execution(execution)
    if seat:
        email = seat
    elif triggered_by in _USER_FACING_TRIGGERS:
        email = execution.source_user_email
    else:
        raise HTTPException(
            status_code=422,
            detail={"code": "no_seat",
                    "message": ("A decision belongs to a seat: record it during a user-facing "
                                "session (public, slack, telegram, whatsapp) or a scheduled run that "
                                f"names a person. This execution was triggered by '{triggered_by}'.")},
        )
    if not email or not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail={"code": "no_seat",
                                                     "message": "No verified person on this execution."})
    return email.lower(), execution


@router.post("/{agent_name}/decisions")
async def record_seat_decision(
    agent_name: str,
    body: RecordDecisionRequest,
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Record a decision for the seat this execution serves."""
    assert_agent_access(current_user, agent_name, detail="Not authorized")
    rate_limiter.enforce(f"seat_decision_record:{agent_name}", 60, 60)
    seat_email, execution = _seat_for(agent_name, body.execution_id)

    scope = f"seat_decision:{agent_name}:{seat_email}"
    idem = idempotency_service.begin(scope, idempotency_key)
    if idem.replay:
        if idem.in_flight:
            raise HTTPException(status_code=409, detail="A record with this Idempotency-Key is in flight")
        return {**(idem.snapshot or {}), "replayed": True}

    role = body.decided_by_role
    if not role:
        assignment = resolve_assignment(agent_name, getattr(execution, "triggered_by", None))
        role = (assignment or {}).get("role_id") or None
    try:
        row = seat_decision_service.record(
            db,
            agent_name=agent_name, seat_email=seat_email, decided_by_person=seat_email,
            payload={**body.model_dump(exclude={"execution_id"}), "decided_by_role": role},
            source_execution_id=body.execution_id,
        )
    except seat_decision_service.DecisionRefused as e:
        idempotency_service.fail(idem)
        raise HTTPException(status_code=e.status_code, detail=e.as_detail())
    except Exception:
        idempotency_service.fail(idem)
        raise
    result = {
        "success": True,
        "decision": seat_decision_service.to_agent(row, seat_email=seat_email),
        "hint": seat_decision_service.CANON_PROPOSAL_HINT if row.get("status") == "routed" else None,
    }
    idempotency_service.complete(idem, None, result)
    logger.info("[ent#638] decision %s recorded on %s (%s, %s)", row["id"], agent_name,
                row.get("outcome"), row.get("status"))
    return result


@router.get("/{agent_name}/autonomy")
async def get_seat_autonomy(
    agent_name: str,
    execution_id: str = Query(..., min_length=1, max_length=200),
    current_user: User = Depends(get_current_user),
):
    """What this companion may do unprompted for the seat it is serving, and
    why not where it may not (trinity-enterprise#641).

    Same seat rule as the decision routes — the seat comes from the execution,
    never from a parameter — and the same email-free contract: the answer names
    ask classes and blockers, never a person. A companion that cannot read this
    cannot tell someone why it is asking first."""
    assert_agent_access(current_user, agent_name, detail="Not authorized")
    seat_email, _ = _seat_for(agent_name, execution_id)
    from services import autonomy_dial_service

    summary = autonomy_dial_service.seat_summary(db, agent_name, seat_email, persist=False)
    return {
        "agent_name": agent_name,
        **{k: summary[k] for k in ("level", "level_label", "ceiling_allows_unprompted",
                                   "agent_autonomy_enabled", "rating_window_days", "rule_version")},
        "classes": [
            {k: c[k] for k in ("ask_class", "state", "unprompted", "blocked_by",
                               "evidence_expires_at", "guard_metric", "held")}
            for c in summary["classes"]
        ],
        "blocker_text": autonomy_dial_service.BLOCKER_TEXT,
    }


@router.get("/{agent_name}/decisions")
async def list_seat_decisions(
    agent_name: str,
    execution_id: str = Query(..., min_length=1, max_length=200),
    include_history: bool = False,
    current_user: User = Depends(get_current_user),
):
    """The seat's standing decisions (criterion first), for reuse and citing.
    `include_history` adds superseded / closed / reversed / expired rows."""
    assert_agent_access(current_user, agent_name, detail="Not authorized")
    seat_email, _ = _seat_for(agent_name, execution_id)
    rows = db.list_seat_decisions(agent_name, seat_email, limit=seat_decision_service.MAX_ROWS_PER_SEAT)
    shaped = [seat_decision_service.to_agent(r, seat_email=seat_email) for r in rows]
    if not include_history:
        shaped = [d for d in shaped if d["status"] == "active"]
    return {"agent_name": agent_name, "decisions": shaped,
            "stats": seat_decision_service.stats(rows)}
