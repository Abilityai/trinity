"""The Workspace half of the autonomy dial (trinity-enterprise#641, P12).

What this seat's companion may do unprompted, per kind of ask, and — when it
may not — exactly why. Who may read which seat and who may write is decided
HERE, before anything is serialized; the rule lives in
`services/autonomy_dial_service.py`. Roster gating is the router's.

Readers: the same set as the decision record it is computed from
(`seat_decisions.readable_seats`) — own seat always, the agent's OWNER every
seat, a stakeholder the assignment provider recognises every seat read-only.
Without that filter a roster member would read every other seat's ask-class
slugs, which name what that person's companion does.

Writes split by direction, because they are not the same act:

* **hold** — refuse unprompted work for a class. Own seat or the owner: a
  person may always make their own companion ask first.
* **release** — let it run unprompted again. **Owner/admin only.** Releasing is
  a GRANT (Invariant #8); holding is a refusal, and a refusal is never a
  privilege. The same asymmetry as `hold`-never-`promote`: the rule promotes, a
  human can only refuse.
* **guard** — record canon's hard cap for a class (a class that moves another
  role's held metric can never graduate on this evidence alone). Owner/admin.

Nothing here promotes. Promotion is earned from the decision record and the
rating history; there is deliberately no button for it.
"""
from __future__ import annotations

import logging
from typing import Optional

from database import db
from services import autonomy_dial_service as dial
from services import seat_decision_service as svc

from . import role_card, seat_decisions

logger = logging.getLogger(__name__)

HOLD_ACTIONS = ("hold", "release")


class AutonomyRefused(Exception):
    def __init__(self, code: str, message: str, status_code: int = 403):
        super().__init__(message)
        self.code = code
        self.detail = message
        self.status_code = status_code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": self.detail}


def _me(email: str) -> str:
    return (email or "").strip().lower()


def page(agent_name: str, email: str, *, is_platform: bool) -> dict:
    """The panel: the instance ceiling, the hard off, and every readable seat's
    classes. Reads NEVER persist a verdict — an owner opening this page must not
    become a writer for every other seat (the event paths own the writes)."""
    me = _me(email)
    owner = role_card._is_owner(agent_name, me, is_platform)
    seats = svc.readable_seats(db, agent_name, me, is_owner=owner)
    summary = dial.seat_summary(db, agent_name, me, persist=False)
    others = []
    for seat, writable in seats.items():
        if seat == me:
            continue
        for row in dial.evaluate_seat(db, agent_name, seat, persist=False):
            others.append({**row, "seat": seat, "writable": bool(writable)})
    return {
        "agent_name": agent_name,
        "my_seat": me,
        "can_hold": True,
        "can_release": owner,
        **summary,
        "classes": [{**c, "seat": me, "writable": True} for c in summary["classes"]],
        "other_seats": sorted(others, key=lambda r: (r["seat"], r["ask_class"])),
    }


def _target_seat(agent_name: str, email: str, is_platform: bool,
                 requested: Optional[str]) -> tuple:
    me = _me(email)
    target = _me(requested) if requested else me
    owner = role_card._is_owner(agent_name, me, is_platform)
    if target != me and not owner:
        # Uniform with not-found: a non-owner learns nothing about other seats.
        raise AutonomyRefused("class_not_found", "No such ask class for this seat.", 404)
    return target, owner


def act(agent_name: str, email: str, *, is_platform: bool, ask_class: str,
        action: str, seat: Optional[str] = None) -> dict:
    """hold / release one ask class."""
    if action not in HOLD_ACTIONS:
        raise AutonomyRefused("unknown_action", f"action must be one of {', '.join(HOLD_ACTIONS)}", 422)
    target, owner = _target_seat(agent_name, email, is_platform, seat)
    if action == "release" and not owner:
        raise AutonomyRefused(
            "release_owner_only",
            "Only the agent's owner can let a class run unprompted again — holding is "
            "a refusal anyone may make, releasing is a grant.",
        )
    db.set_seat_ask_class_hold(agent_name=agent_name, seat_email=target,
                               ask_class=ask_class, held=(action == "hold"), by=_me(email))
    # The hold is a conjunct of the earned verdict, so recompute it here — this
    # is an event, not a read.
    dial.evaluate_seat(db, agent_name, target, persist=True)
    return _one(agent_name, target, ask_class)


def set_guard(agent_name: str, email: str, *, is_platform: bool, ask_class: str,
              guard_metric: str, seat: Optional[str] = None) -> dict:
    """Record canon's guard-metric verdict for a class. Owner/admin — it is the
    cap that decides whether the rule may ever promote this class at all."""
    if guard_metric not in dial.GUARD_STATES:
        raise AutonomyRefused("unknown_guard_state",
                              f"guard_metric must be one of {', '.join(dial.GUARD_STATES)}", 422)
    target, owner = _target_seat(agent_name, email, is_platform, seat)
    if not owner:
        raise AutonomyRefused("guard_owner_only",
                              "Only the agent's owner records whether a class moves another role's metric.")
    db.set_seat_ask_class_guard(agent_name=agent_name, seat_email=target,
                                ask_class=ask_class, guard_metric=guard_metric)
    dial.evaluate_seat(db, agent_name, target, persist=True)
    return _one(agent_name, target, ask_class)


def _one(agent_name: str, seat: str, ask_class: str) -> dict:
    for row in dial.evaluate_seat(db, agent_name, seat, persist=False):
        if row["ask_class"] == ask_class:
            return {"class": {**row, "seat": seat}}
    raise AutonomyRefused("class_not_found", "No such ask class for this seat.", 404)
