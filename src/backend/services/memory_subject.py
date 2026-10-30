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

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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
    return bool(value) and bool(_EMAIL_RE.match(value))


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
