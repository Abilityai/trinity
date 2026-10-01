"""The Workspace half of the seat decision record (trinity-enterprise#638).

Who may read which seat and who may write is decided HERE, before anything is
serialized; the grammar, lifecycle and evidence live in
`services/seat_decision_service.py`. Roster gating is the router's
(`_require_roster`) — this module assumes the principal reaches the agent.

Readers: the principal's own seat always; the agent's OWNER (creator /
infra owner — `role_card._is_owner`, never an assignment kind) every seat,
writable; a stakeholder the assignment provider recognises on the agent
(`kinds_for`) every seat read-only. Writes: own seat, or the owner on a named
seat. An external portal principal (`is_platform=False`) is never an owner.
"""
from __future__ import annotations

import logging
from typing import Optional

from database import db
from services import seat_decision_service as svc

from . import role_card

logger = logging.getLogger(__name__)


def _me(email: str) -> str:
    return (email or "").strip().lower()


def page(agent_name: str, email: str, *, is_platform: bool) -> dict:
    me = _me(email)
    owner = role_card._is_owner(agent_name, me, is_platform)
    seats = svc.readable_seats(db, agent_name, me, is_owner=owner)
    decisions: list[dict] = []
    for seat, writable in seats.items():
        rows = db.list_seat_decisions(agent_name, seat, limit=svc.MAX_ROWS_PER_SEAT)
        decisions.extend(svc.to_human(r, writable=writable) for r in rows)
    decisions.sort(key=lambda d: d["decided_at"], reverse=True)
    mine = db.list_seat_decisions(agent_name, me, limit=svc.MAX_ROWS_PER_SEAT)
    return {
        "agent_name": agent_name,
        "my_seat": me,
        "seats": list(seats.keys()),
        "decisions": decisions,
        "stats": svc.stats(mine),
        "can_record": True,
    }


def _target_seat(agent_name: str, email: str, is_platform: bool, requested: Optional[str]) -> str:
    me = _me(email)
    target = _me(requested) if requested else me
    if target != me and not role_card._is_owner(agent_name, me, is_platform):
        raise svc.DecisionRefused(
            "seat_not_yours",
            "Only the agent's owner may record for another seat.",
            status_code=403,
        )
    return target


def record(agent_name: str, email: str, *, is_platform: bool, payload: dict) -> dict:
    target = _target_seat(agent_name, email, is_platform, payload.get("seat"))
    row = svc.record(
        db,
        agent_name=agent_name, seat_email=target, decided_by_person=_me(email),
        payload={k: v for k, v in payload.items() if k != "seat"},
    )
    return {
        "decision": svc.to_human(row, writable=True),
        "hint": svc.CANON_PROPOSAL_HINT if row.get("status") == "routed" else None,
    }


def act(agent_name: str, email: str, *, is_platform: bool, decision_id: str,
        action: str, reason: Optional[str], review_by: Optional[str],
        fields: Optional[dict]) -> dict:
    me = _me(email)
    row = db.get_seat_decision(agent_name, decision_id)
    if not row:
        raise svc.DecisionRefused("decision_not_found", "No such decision.", status_code=404)
    seat = row.get("seat_email") or ""
    if seat != me and not role_card._is_owner(agent_name, me, is_platform):
        # Uniform with not-found: a non-owner learns nothing about other seats.
        raise svc.DecisionRefused("decision_not_found", "No such decision.", status_code=404)
    new_row = svc.act(
        db, agent_name=agent_name, seat_email=seat, decision_id=decision_id,
        action=action, by=me, reason=reason, review_by=review_by, fields=fields,
    )
    return {"decision": svc.to_human(new_row, writable=True), "hint": None}
