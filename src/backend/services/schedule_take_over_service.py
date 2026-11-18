"""Take over a share holder's schedule in place (#4281, decisions D4, D8).

A schedule's runs are rated by its creator (`turn_tier_service`). A share
holder's schedule therefore runs as a shared person: no shell, no tools the
owner added. Taking it over makes the taker the creator, so its runs get the
taker's owner tier from then on. That is a grant (a prompt someone else wrote
gains the owner's access), so the route is human-only and owner tier, and the
taker adopts exactly the version they reviewed:

* the request echoes `updated_at` and the prompt fields of the response the
  taker read; the database write is pinned to them (`reassign_schedule_creator`);
* refused while a run is in flight, and when the runs already have owner tier;
* the webhook is revoked in the same write, so the former creator cannot fire
  owner-tier runs through a URL they hold;
* a delivery to another person's Workspace is cleared unless the request
  repeats its address (D8): owner-tier output must not reach a third party by
  accident.
"""

import logging
from typing import NamedTuple, Optional

from services import turn_tier_service

logger = logging.getLogger(__name__)

CHANGED_SINCE_REVIEW = "schedule_changed_since_review"
RUN_IN_PROGRESS = "schedule_run_in_progress"
NOT_RESTRICTED = "schedule_not_restricted"
DELIVERS_TO_ANOTHER_PERSON = "schedule_delivers_to_another_person"

# The causes a take-over fixes: the runs are a shared person's because of who
# created the schedule. An agent with no owner on record is not one of them
# (every owner-tier route 404s there; its remedy is `repair_agent_owner`).
TAKE_OVER_CAUSES = frozenset(
    {
        turn_tier_service.CREATOR_NOT_OWNER_TIER,
        turn_tier_service.CREATOR_MISSING,
    }
)

_MESSAGES = {
    CHANGED_SINCE_REVIEW: (
        "The schedule changed since you reviewed it. Read it again, check what it "
        "will run, and take it over from that version."
    ),
    RUN_IN_PROGRESS: (
        "A run of this schedule is in progress or waiting to retry. Take it over when "
        "that run has finished."
    ),
    NOT_RESTRICTED: "This schedule's runs already have the owner's access; there is nothing to take over.",
    DELIVERS_TO_ANOTHER_PERSON: (
        "This schedule delivers to another person's Workspace. After a take-over "
        "its runs would have your access there. Choose `clear` to stop that "
        "delivery, or `keep` and repeat the address to keep it."
    ),
}


class TakeOverRefused(Exception):
    """A take-over that does not happen; `detail` is the HTTP detail."""

    def __init__(self, code: str, *, status_code: int = 409, **extra):
        message = _MESSAGES.get(code, code)
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"code": code, "message": message, **extra}


class TakeOver(NamedTuple):
    """`changed` is False when the taker already was the creator."""

    changed: bool
    previous_creator_id: Optional[int]
    webhook_revoked: bool
    delivery_cleared: bool


def _email(value) -> str:
    return (value or "").strip().lower()


def _clears_delivery(schedule, taker, body) -> bool:
    """D8: whether the take-over removes the schedule's Workspace delivery.

    `clear` always does. A delivery to anyone but the taker must be decided:
    `keep` with the address repeated keeps it; anything else is refused."""
    target = _email(getattr(schedule, "deliver_to_workspace_email", None))
    if body.delivery == "clear":
        return bool(target)
    if not target or target == _email(getattr(taker, "email", None)):
        return False
    if body.delivery == "keep" and _email(body.keep_delivery_to) == target:
        return False
    raise TakeOverRefused(
        DELIVERS_TO_ANOTHER_PERSON,
        deliver_to_workspace_email=schedule.deliver_to_workspace_email,
    )


def take_over(db, agent_name: str, schedule, taker, body) -> TakeOver:
    """Make `taker` the creator of `schedule` on `agent_name`, or raise
    `TakeOverRefused`.

    The caller has already established that `taker` is a person at the agent's
    owner tier and that `schedule` is the agent's. A rating that cannot be
    computed raises (the route fails loud rather than strip a schedule it
    cannot rate)."""
    previous = schedule.owner_id
    if previous is not None and previous == taker.id:
        return TakeOver(False, previous, False, False)
    if (
        turn_tier_service.schedule_run_tier(db, agent_name, schedule).tier
        == turn_tier_service.OWNER
    ):
        raise TakeOverRefused(NOT_RESTRICTED)
    clear = _clears_delivery(schedule, taker, body)

    expected = body.reviewed.model_dump()
    # The delivery the decision above was made about.
    expected["deliver_to_workspace_email"] = schedule.deliver_to_workspace_email
    result = db.reassign_schedule_creator(
        schedule.id,
        new_owner_id=taker.id,
        expected_owner_id=previous,
        expected_updated_at=body.expected_updated_at,
        expected=expected,
        clear_delivery=clear,
    )
    outcome = result["outcome"]
    if outcome == "taken_over":
        return TakeOver(True, previous, bool(result["webhook_revoked"]), clear)
    if outcome == "in_progress":
        raise TakeOverRefused(RUN_IN_PROGRESS)
    if outcome == "changed":
        raise TakeOverRefused(CHANGED_SINCE_REVIEW)
    if outcome == "gone":
        raise TakeOverRefused("schedule_not_found", status_code=404)
    raise RuntimeError(
        f"unexpected take-over outcome {outcome!r} for schedule {schedule.id}"
    )


def can_take_over(
    *, owner_tier_person: bool, viewer_id, schedule, read_fields: dict
) -> bool:
    """`ScheduleResponse.can_take_over`: true exactly for a person at the
    agent's owner tier who did not create the schedule, when its runs are a
    shared person's because of its creator. `read_fields` are the schedule's
    `turn_tier_service.schedule_read_fields`."""
    if not owner_tier_person:
        return False
    creator = getattr(schedule, "owner_id", None)
    if creator is not None and creator == viewer_id:
        return False
    reason = read_fields.get("runs_as_reason") or {}
    return (
        read_fields.get("runs_as") == "shared_person"
        and reason.get("code") in TAKE_OVER_CAUSES
    )
