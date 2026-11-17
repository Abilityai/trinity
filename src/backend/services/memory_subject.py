"""Whose memory does an execution touch? (MEM-001 / trinity-enterprise#1022)

The ONE rule, read off the execution row — the agent never names the person.
Used by the `write_user_memory` endpoint (`routers/public_memory.py`, which maps
a refusal to its 422s) and by retrieval memory's capture seam
(`services/memory_service.retain_execution`, which maps a refusal to "skip").

Subjects:

* ``person``  — a verified person: the seat a scheduled run is addressed to
  (ent#637), the one-to-one speaker of a channel turn (ent#549's stamp), the
  signed-in person of a public-link / Workspace / API turn.
* ``agent``   — nobody in particular: a headless run (schedule without a seat,
  agent-to-agent, MCP, loop, fan-out, reminder, event, system). Retrieval
  memory keeps these in the agent scope, which the agent's OWNER shares
  (`memory_service.scope_for` folds the owner's own turns into it).
* ``skip``    — no one's memory: an anonymous public turn, a channel group turn
  (the unlocker's address is on the row, but other people's words are in the
  prompt), or a trigger that is not on the allowlist. The allowlist is explicit
  on purpose: "no person → agent scope" would sweep anonymous and group turns
  into the shared scope (the review's critical finding).

HTTP-free and pure (Invariant #1): the caller owns the status codes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from services import schedule_seat_memory

PERSON = "person"
AGENT = "agent"
SKIP = "skip"

# Channel turns stamp `source_channel_client` for the verified one-to-one speaker.
CHANNEL_TRIGGERS = frozenset({"slack", "telegram", "whatsapp"})
# Turns a signed-in person drives directly: public link / Workspace ("public"),
# the API and MCP chat doors ("user", "chat"). The person is `source_user_email`.
PERSON_TRIGGERS = frozenset({"public", "user", "chat"})
# Headless runs: the agent scope. Explicit allowlist — anything else is skipped.
AGENT_SCOPE_TRIGGERS = frozenset({
    "schedule", "manual", "webhook",       # a schedule's cron / Run now / webhook fire
    "agent", "mcp", "self_task",           # agent-to-agent, an MCP key, a self-dispatched task
    "loop", "fan_out", "reminder",         # orchestration primitives
    "event", "system",                     # pub/sub dispatch, platform-originated
})
# Named here so a reader can tell "reviewed" from "forgotten": voice/voip turns
# carry no durable identity we trust for memory, paid turns are anonymous
# x402 calls, rooms and sessions are multi-party, skill_gate/validation runs are
# the platform judging the agent, not the agent serving anyone.
NEVER_TRIGGERS = frozenset({"voice", "voip", "paid", "room", "session", "skill_gate", "validation"})

# No dot inside the runs either side of a domain separator: that overlap made the
# match quadratic on a long dotted domain (CodeQL py/polynomial-redos). The shape and
# the cap are the portal's (`client_portal.service.normalize_client_email`).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
_EMAIL_MAX_LEN = 320

# Reason codes (stable strings the endpoint and the logs key on).
REASON_SEAT = "seat"
REASON_CHANNEL_ONE_TO_ONE = "channel_one_to_one"
REASON_CHANNEL_NOT_ONE_TO_ONE = "channel_not_one_to_one"
REASON_PERSON = "signed_in_person"
REASON_NO_VERIFIED_EMAIL = "no_verified_email"
REASON_HEADLESS = "headless_run"
REASON_TRIGGER_NOT_ALLOWED = "trigger_not_allowlisted"


@dataclass(frozen=True)
class MemorySubject:
    kind: str                      # PERSON | AGENT | SKIP
    user_email: Optional[str]      # lower-cased; set only for PERSON
    reason: str
    triggered_by: str = ""

    @property
    def is_person(self) -> bool:
        return self.kind == PERSON


def looks_like_email(value: Optional[str]) -> bool:
    return bool(value) and len(value) <= _EMAIL_MAX_LEN and bool(_EMAIL_RE.match(value))


def resolve(execution: Any) -> MemorySubject:
    """The subject of ``execution`` — see the module docstring for the table."""
    triggered_by = (getattr(execution, "triggered_by", None) or "").lower()

    seat = schedule_seat_memory.seat_for_execution(execution)
    if seat:
        return MemorySubject(PERSON, seat.strip().lower(), REASON_SEAT, triggered_by)

    if triggered_by in CHANNEL_TRIGGERS:
        speaker = (getattr(execution, "source_channel_client", None) or "").strip().lower()
        if speaker and looks_like_email(speaker):
            return MemorySubject(PERSON, speaker, REASON_CHANNEL_ONE_TO_ONE, triggered_by)
        return MemorySubject(SKIP, None, REASON_CHANNEL_NOT_ONE_TO_ONE, triggered_by)

    if triggered_by in PERSON_TRIGGERS:
        email = (getattr(execution, "source_user_email", None) or "").strip().lower()
        if looks_like_email(email):
            return MemorySubject(PERSON, email, REASON_PERSON, triggered_by)
        if triggered_by == "public":
            # An anonymous public-link visitor — "anonymous (<ip>)" on the row.
            return MemorySubject(SKIP, None, REASON_NO_VERIFIED_EMAIL, triggered_by)
        # An API / MCP turn with no person behind it: the agent's own run.
        return MemorySubject(AGENT, None, REASON_HEADLESS, triggered_by)

    if triggered_by in AGENT_SCOPE_TRIGGERS:
        return MemorySubject(AGENT, None, REASON_HEADLESS, triggered_by)

    return MemorySubject(SKIP, None, REASON_TRIGGER_NOT_ALLOWED, triggered_by)


# #4156: triggers with no person on the row that are the agent's own work, so
# auto-memory stays on. The memory-subject allowlist plus the headless runs it
# never needed to name: a schedule's retry, the resume after an operator
# answer, and the user/chat/session/a2a doors when no person is stamped.
_AUTO_MEMORY_HEADLESS_TRIGGERS = AGENT_SCOPE_TRIGGERS | frozenset({
    "retry", "operator_response", "user", "chat", "session",
})
# Multi-party, anonymous, or carrying words from someone the row does not
# identify (a caller's speech, an x402 payer, a room): never on. `session` is
# not here — a session turn is judged by who drove it, like any other door.
_AUTO_MEMORY_NEVER_TRIGGERS = NEVER_TRIGGERS - {"session"}


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def keeps_shared_auto_memory(
    execution: Any, owner_email: Optional[str], owner_id: Optional[int] = None,
) -> bool:
    """Whether the runtime may keep Claude Code's auto-memory on for this turn
    (#4156). Accepts the row ``db.get_execution`` returns or a pull claim's dict.

    Auto-memory is ONE directory per agent, read and written by every turn, and
    its harness prompt says to save there when someone asks to be remembered.
    So it stays on only for turns that serve the owner or nobody:

    1. A Workspace destination on the row (a seat's schedule run, the resume
       after an operator answer) names the person served: on only for the owner.
    2. A channel turn: on only for the owner's one-to-one; groups are off.
    3. Multi-party / anonymous triggers (voice, voip, paid, room, ...): off.
    4. A schedule's own fire (cron, Run now, webhook, retry) is the schedule's
       run, whoever pressed the button: on.
    5. A person who drove the turn (``source_user_id``, else
       ``source_user_email``) keeps it only when they ARE the owner, compared
       by user id when both are known — a non-owner admin or a shared user's
       ``/task`` / MCP turn is someone else, even under a headless trigger.
    6. Nobody on the row: on for a known headless trigger, off otherwise.

    Fail-closed everywhere else: a wrong "off" costs one turn the agent's own
    notes, a wrong "on" is the cross-user leak.
    """
    if isinstance(execution, dict):
        from types import SimpleNamespace

        execution = SimpleNamespace(**execution)
    triggered_by = _norm(getattr(execution, "triggered_by", None))
    owner = _norm(owner_email)

    served = _norm(getattr(execution, "source_channel_client", None))
    if served and _norm(getattr(execution, "source_channel", None)) == schedule_seat_memory.PORTAL_SOURCE_CHANNEL:
        return bool(owner) and served == owner

    if triggered_by in CHANNEL_TRIGGERS:
        return bool(owner) and looks_like_email(served) and served == owner

    if triggered_by in _AUTO_MEMORY_NEVER_TRIGGERS:
        return False

    schedule_id = (getattr(execution, "schedule_id", None) or "").strip()
    if (triggered_by in schedule_seat_memory.SCHEDULE_FIRE_TRIGGERS or triggered_by == "retry") \
            and schedule_id and schedule_id != schedule_seat_memory.NO_SCHEDULE_ID:
        return True

    user_id = getattr(execution, "source_user_id", None)
    email = _norm(getattr(execution, "source_user_email", None))
    if user_id is not None and owner_id is not None:
        try:
            return int(user_id) == int(owner_id)
        except (TypeError, ValueError):
            return False
    if user_id is not None or email:
        return bool(owner) and email == owner

    # trinity-enterprise#1225: a continuation names nobody; it serves the turn it
    # continues, so it keeps auto-memory only when that turn is the owner's own.
    from services import turn_tier_service
    if (triggered_by in turn_tier_service.CONTINUATION_TRIGGERS
            and getattr(execution, "continues_execution_id", None)):
        try:
            from database import db
            return turn_tier_service.turn_actor(
                db, getattr(execution, "agent_name", None), execution=execution,
            ) == (turn_tier_service.OWNER, None, None)
        except Exception:  # noqa: BLE001 - fail closed: a wrong "on" is the leak
            return False

    return triggered_by in _AUTO_MEMORY_HEADLESS_TRIGGERS
