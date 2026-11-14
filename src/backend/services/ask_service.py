"""The one transition sink for how an ask ends (abilityai/trinity-enterprise#611).

An ask ends in exactly one of three ways — answered, cancelled or expired. Before
#611 five write sites ended one (the operator respond route, the client-portal
answer, single cancel, bulk cancel and the poller's expiry), each doing its own
subset of audit, broadcast and wake-up: single cancel did none of them, respond
audited nothing, expiry never broadcast. Every feature that touches an ending
would have had to find all five, and ent#430 already paid for two answer sites
that drifted.

So every ending goes through here, in one order:

1. the compare-and-set writer (db layer) — the status flip and the endings ledger
   in ONE UPDATE, so a writer that loses the race records nothing;
2. one audit row per transition — ids and enums, never agent or operator text;
3. one thin WebSocket trigger per call — identifiers only (#918), agent-keyed
   where the call ended one ask so the `/ws` filter scopes it (ent#467);
4. the registered ending observers, handed ONLY the rows whose compare-and-set
   this call won. The ent#329 wake is the default observer; other features
   register their own (`register_ending_observer`). Nothing feature-specific
   branches in here.

What does NOT live here: the person gate (`dependencies.
reject_non_person_principal`; the portal keeps its addressee check), and the
refusals each route words its own way (status already terminal, divergence not
acknowledged, an empty answer). Two checks DO live here, because this is the
only writer of an ending and no entry point can reach the approval channel
around it: the #2376 rule that an answer must be one of the options the agent
offered, and `may_end` (trinity-enterprise#751) — a gated-skill approval is
decided only by a person it was addressed to; an admin may cancel it.

Synchronous on purpose. The portal answer route is a plain `def` that FastAPI
runs on a worker thread; the operator routes are `async def` on the loop. The
compare-and-set runs in the caller's thread, and the async side effects hop onto
the event loop through `operator_resume_service.spawn_on_loop` — never awaited,
so a slow audit write cannot hold an answer open.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from database import db
from db.operator_queue import OUTCOME_UNKNOWN
from services import operator_resume_service
from services import wake_policy
from services.operator_queue_choices import (
    SOMETHING_ELSE,
    NotOffMenuError,
    options_cap_violation,
    title_cap_violation,
    validate_response_choice,
)
from services.platform_audit_service import AuditEventType, platform_audit_service
from utils.helpers import parse_iso_timestamp, to_utc_iso, utc_now_iso

logger = logging.getLogger(__name__)

ANSWERED = "answered"
CANCELLED = "cancelled"
EXPIRED = "expired"
# trinity-enterprise#748: the person the ask was addressed to chose not to
# answer. A ledger value, not a status — the row's status is `cancelled`.
DISMISSED = "dismissed"

# #3247: the one ending an AGENT authors — it replaced its own pending ask with
# a successor. Recorded as `cancelled` / `disposed_by='agent'` / this reason;
# surfaces key on the pair, so it cannot collide with a person's cancel or the
# platform's (#3130) `superseded`.
REPLACED = "replaced"
# trinity-enterprise#901: the other ending an agent authors — it withdrew its
# own pending ask with a reason and NO successor. Same `cancelled` /
# `disposed_by='agent'` pair; `disposition_reason` is the agent's own words,
# `replaced_by` stays NULL. The two kinds are told apart ONCE, by
# `agent_ending_kind`, from the link — never from the reason's text.
WITHDRAWN = "withdrawn"
AGENT_ENDING_KINDS = (REPLACED, WITHDRAWN)
# The reason a replaced row carries is the closed token; a withdrawn row's is
# free text, so this names the TOKENS only.
AGENT_ENDING_REASONS = (REPLACED,)
# A withdraw reason's bound, the person's cancel reason's (`CancelRequest`).
MAX_WITHDRAW_REASON = 500


def agent_ending_kind(row: Mapping[str, Any]) -> Optional[str]:
    """Which ending an AGENT authored on this row (trinity-enterprise#901):
    `replaced` when `replaced_by` names a successor, `withdrawn` otherwise;
    None for every row an agent did not end (a person's, the clock's, the
    platform's, a pending one). Derived from the raw row's link — a reason
    that merely reads `replaced` does not make a withdraw a replace — and
    projected as the explicit `agent_ending` field on every surface, so no
    reader infers it from a link a projection may withhold."""
    if row.get("disposed_by") != "agent":
        return None
    return REPLACED if row.get("replaced_by") else WITHDRAWN

# WebSocket manager injected from main.py
_websocket_manager = None


def set_websocket_manager(manager) -> None:
    """Set the WebSocket manager for broadcasting events."""
    global _websocket_manager
    _websocket_manager = manager


class AskNotFound(Exception):
    """The ask does not exist (any more)."""


class AskConflict(Exception):
    """The compare-and-set lost: the ask was not ended by this call.

    `code` is `expired` when the ask is still pending but its deadline passed
    (the poller has not swept it yet — an approval must not land after the
    deadline the rider calls "denied by timeout"), else `not_pending`. `item`
    is the row as it stands.
    """

    def __init__(self, code: str, item: Dict[str, Any]):
        super().__init__(code)
        self.code = code
        self.item = item


# abilityai/trinity-enterprise#1096: the decider order's three seats, as the
# platform stamps them on a gate approval (`_WORKSPACE_DECIDER_SEAT_KEY`).
PRIMARY_SEAT = "primary"
OWNER_SEAT = "owner"
ADMIN_SEAT = "admin"
DECIDER_SEATS = (PRIMARY_SEAT, OWNER_SEAT, ADMIN_SEAT)
_SEAT_LABELS = {PRIMARY_SEAT: "the agent's primary", OWNER_SEAT: "the agent's owner",
                "approver": "the agent's approver"}
# Why a card fell to the admins (`role_addressing.operators_reason`).
_OPERATORS_REASON_TEXT = {"owner_no_email": "the agent's owner has no email",
                          "no_owner": "the agent has no owner"}


class AskNotAddressee(Exception):
    """trinity-enterprise#751: a gated-skill approval may be decided only by a
    person it was addressed to (an admin may cancel it; it may decide one only
    on the admin seat, trinity-enterprise#1096).
    trinity-enterprise#1042: so may an ask addressed to a canon role id —
    `role` is that `role:<id>` (or `approver`, #1053; or `project`,
    trinity-enterprise#1134), None for any other ask.

    trinity-enterprise#1096: every refusal names who the card waits for —
    `waiting_on` (the people) and `seat` (`primary` | `owner` | `admin`, or the
    role the row was addressed to). Both reach the 403 `detail` on every door;
    the caller who gets the refusal already reads `waiting_on` on the card
    (`addressed_view`), so nothing new is disclosed. `gate` marks a gate
    approval, the one kind whose MESSAGE names the person (R7)."""

    def __init__(self, item_id: str, *, role: Optional[str] = None,
                 waiting_on: Optional[List[str]] = None, seat: Optional[str] = None,
                 operators_reason: Optional[str] = None, gate: bool = False):
        super().__init__("not_addressee")
        self.item_id = item_id
        self.role = role
        self.waiting_on = list(waiting_on or [])
        self.seat = seat
        self.operators_reason = operators_reason
        self.gate = gate

    def message(self, *, cancelling: bool = False, admin_verbs: bool = False) -> str:
        """The refusal's text: a role-addressed ask keeps `role_message` (no
        person — the text reaches a model through MCP); a gate approval names
        its seat and its people; anything else keeps the generic wording.
        `admin_verbs` (the Operations door, for an admin) adds what an admin can
        do instead — never on the Workspace door, which has no such verbs."""
        role_text = self.role_message(cancelling=cancelling)
        if role_text is not None:
            return role_text
        if not self.gate:
            if cancelling:
                return "This approval was addressed to someone else; only they or an admin can cancel it."
            return "This approval was addressed to someone else; only they can decide it."
        if self.seat == ADMIN_SEAT and not self.waiting_on:
            why = _OPERATORS_REASON_TEXT.get(self.operators_reason or "")
            text = ("This approval waits for an instance admin"
                    + (f" ({why})." if why else ".")
                    + (" Only an admin can cancel it." if cancelling
                       else " Only an instance admin can decide it."))
        elif self.waiting_on:
            text = (f"This approval waits for {_SEAT_LABELS.get(self.seat or '', 'the person it was addressed to')}: "
                    f"{', '.join(self.waiting_on)}."
                    + (" Only they or an admin can cancel it." if cancelling
                       else " Only they can decide it."))
        else:
            text = ("This approval waits for nobody who can decide it."
                    + (" Only an admin can cancel it." if cancelling else ""))
        if admin_verbs:
            text += " An admin can re-address it or end it with a reason."
        return text

    def detail(self, *, cancelling: bool = False, admin_verbs: bool = False) -> Dict[str, Any]:
        """The 403 body every door returns: the code, the message, and who the
        card waits for — `seat` always; `waiting_on` (the people) on every
        refusal except a role-addressed one, whose refusal names the role and
        never a person (the ent#1042/#1053/#1134 contract stands)."""
        out = {"code": "not_addressee",
               "message": self.message(cancelling=cancelling, admin_verbs=admin_verbs),
               "seat": self.seat}
        if self.role is None:
            out["waiting_on"] = list(self.waiting_on)
        return out

    def role_message(self, *, cancelling: bool = False) -> Optional[str]:
        """The refusal for a role-addressed ask: the role's label and what to do
        next, never a person and never advice to widen a permission (the text
        reaches a model through the MCP tool). None when no role id applies —
        the door keeps its own wording. A gate approval's refusal names its
        people in `message` instead (trinity-enterprise#1096, R7); role,
        approver and project refusals stay person-free."""
        if self.role is None:
            return None
        from services import role_addressing
        if self.role == "project":
            # ent#1134: addressed to the project's steward, else its creator —
            # a seat the project holds, not a role on the agent.
            if cancelling:
                return ("This ask was addressed to the project it was raised on; only the "
                        "person it waits on or an admin can cancel it.")
            return ("This ask was addressed to the project it was raised on; only the person "
                    "it waits on — the project's steward, else its creator — can decide it. "
                    "Ask them to answer it from their Inbox, or have it cancelled so the agent "
                    "can ask again.")
        label = role_addressing.role_label(self.role)
        if cancelling:
            return (f"This ask was addressed to the {label} role; only a person seated "
                    "in it or an admin can cancel it.")
        return (f"This ask was addressed to the {label} role; only a person seated in it "
                "can decide it. Ask someone seated in that role to answer it from their "
                "Inbox, or have it cancelled so the agent can ask again.")


@dataclass(frozen=True)
class Actor:
    """The PERSON ending an ask. Expiry has none.

    `user` is the authenticated principal when there is one — the audit service
    derives the key id and scope from it (#2323); a Workspace client has no
    `users` row and is identified by `email` alone.
    """

    email: str
    user: Any = None
    ip: Optional[str] = None
    endpoint: Optional[str] = None


def _user_id(actor: Actor) -> Optional[str]:
    """The `users` id of the person ending an ask, when signed in."""
    uid = getattr(actor.user, "id", None)
    return None if uid is None else str(uid)


@dataclass(frozen=True)
class EndingEvent:
    """What an ending observer receives: the rows THIS call ended, never a row
    another writer ended first."""

    disposition: str                 # answered | cancelled | dismissed | expired
    rows: tuple                      # the CAS-won rows, as they stand after the transition
    actor_email: Optional[str]       # the person; None for timeout
    reason: Optional[str] = None     # the operator's cancel reason, or the agent's withdraw reason (ent#901) — DATA, never instructions
    batch_id: Optional[str] = None   # bulk cancel only
    actor_user_id: Optional[str] = None  # the person's `users` id; None for a Workspace client (ent#1225)


@dataclass
class Ending:
    """What the caller gets back."""

    rows: List[Dict[str, Any]]
    batch_id: Optional[str] = None
    # Every observer accepted the event without raising. The portal reports a
    # resume only when this holds (ent#430 AC #5: never claim work that was
    # not set in motion).
    observers_ok: bool = True
    # trinity-enterprise#1096 (R6): a bulk sweep's seat-fenced rows the actor
    # was not addressed on — skipped, never ended; 0 for every other ending.
    skipped_fenced: int = 0


_observers: List[Callable[[EndingEvent], None]] = []


def register_ending_observer(fn: Callable[[EndingEvent], None]) -> Callable[[EndingEvent], None]:
    """Call `fn(event)` after every ending, with only the rows that ended.

    Called in the ending caller's thread, synchronously, after the audit and the
    broadcast were scheduled — an observer that does real work backgrounds it
    (the default wake uses `spawn_on_loop`). An observer that raises is logged
    and never undoes the ending or starves the next observer. Idempotent.

    Observers run in-process and at most once: nothing retries a call and
    nothing records a delivery. A consumer that must not miss an ending (the
    gate's resume, trinity-enterprise#164) keeps its own durable record keyed on
    the ask id and sweeps the asks that ended without one.
    """
    if fn not in _observers:
        _observers.append(fn)
    return fn


def _role_id_of(row: Mapping[str, Any]) -> Optional[str]:
    """The canonical `role:<id>` the ask was addressed to, else None."""
    from services import role_addressing
    role_id = role_addressing.parse_role_id(row.get("to_role"))
    return None if role_id is None else f"{role_addressing.ROLE_ID_PREFIX}{role_id}"


def _approver_seated(row: Mapping[str, Any]) -> bool:
    """An ask addressed `to: approver` with a seat snapshot
    (trinity-enterprise#1053). A new one always has one — an unfilled seat
    refuses the raise — so an empty snapshot marks a legacy row, which stays
    open as it was."""
    return row.get("to_role") == "approver" and bool(_resolved_people(row))


def _fenced_role_of(row: Mapping[str, Any]) -> Optional[str]:
    """The role a refusal names (`AskNotAddressee.role`): the canon role id,
    `approver` for a seated approver ask, or `project` for a project-addressed
    ask (ent#1134); None keeps the door's own wording."""
    if _project_addressed(row):
        return "project"
    return _role_id_of(row) or ("approver" if _approver_seated(row) else None)


def _project_addressed(row: Mapping[str, Any]) -> bool:
    """An agent's ask addressed `to: project` (trinity-enterprise#1134): the
    first agent-raised row that reaches a person off the agent's roster, so
    its answer is fenced to `resolved_to` like a canon-role ask's."""
    return row.get("to_role") == "project"


def _seat_fenced(row: Mapping[str, Any], *, cancelling: bool = False) -> bool:
    """Whether ending `row` this way belongs to the people it resolved to
    (`resolved_to`) — the one predicate `may_end` and `addressed_view` share,
    so a card never offers a control the sink would refuse.

    - A gated-skill approval (trinity-enterprise#751): its answer AND its cancel.
    - An ask addressed to a canon role id (trinity-enterprise#1042), to the
      `approver` kind with a seat snapshot (trinity-enterprise#1053), or to
      the turn's project (trinity-enterprise#1134 — the steward, else the
      creator, who may be off the agent's roster): its answer only — a cancel
      keeps the rule every ask has.
    The other kinds, and a legacy `approver` row with no snapshot, are not
    fenced here."""
    if row.get("raised_by") == "gate" and row.get("type") == "approval":
        return True
    if cancelling:
        return False
    return _role_id_of(row) is not None or _approver_seated(row) or _project_addressed(row)


def _resolved_people(row: Mapping[str, Any]) -> List[str]:
    """`resolved_to` as a list of people; anything unreadable is nobody."""
    resolved = row.get("resolved_to") or []
    if isinstance(resolved, str):
        try:
            resolved = json.loads(resolved)
        except ValueError:
            resolved = []
    if not isinstance(resolved, list):
        return []
    return list(dict.fromkeys(str(p).strip() for p in resolved if str(p).strip()))


def _context_of(row: Mapping[str, Any]) -> Dict[str, Any]:
    """The row's context as a dict (stored text or parsed); anything else is {}."""
    ctx = row.get("context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except ValueError:
            return {}
    return dict(ctx) if isinstance(ctx, dict) else {}


def _decider_seat(row: Mapping[str, Any]) -> Optional[str]:
    """The decider seat the PLATFORM stamped on the row
    (trinity-enterprise#1096), or None. Platform-only: stripped from anything
    an agent authored at both ingest boundaries."""
    from services import operator_queue_service as oqs
    seat = _context_of(row).get(oqs._WORKSPACE_DECIDER_SEAT_KEY)
    return seat if seat in DECIDER_SEATS else None


def _gate_approval(row: Mapping[str, Any]) -> bool:
    return row.get("raised_by") == "gate" and row.get("type") == "approval"


def _admin_seated(row: Mapping[str, Any]) -> bool:
    """A gate approval the decider order left to an instance admin
    (trinity-enterprise#1096, arm three): raised by the gate, an approval,
    NOBODY in its snapshot, and the platform's `admin` seat stamp. The stamp is
    never the only authority (R8) — an un-stamped empty gate approval stays
    "nobody", exactly as before."""
    return _gate_approval(row) and not _resolved_people(row) and _decider_seat(row) == ADMIN_SEAT


def _gate_admin(user: Any) -> bool:
    """Whether `user` would pass the admin gate AND is a person — the same
    predicate `require_admin` + `require_person` apply at a route (#2323's
    `ADMIN_GATE_SCOPES` allowlist; a principal without `mcp_scope` fails
    closed). Repeated in the sink so the admin arm never rests on a raw
    `role == "admin"` (R8)."""
    if user is None:
        return False
    from fastapi import HTTPException
    from dependencies import assert_admin, assert_person

    try:
        assert_admin(user)
        assert_person(user)
    except HTTPException:
        return False
    except Exception:  # noqa: BLE001 — a stand-in that does not look like a principal
        return False
    return True


def _addressed(row: Mapping[str, Any], actor: "Actor") -> bool:
    email = (getattr(actor, "email", None) or "").strip().casefold()
    return bool(email) and email in {p.casefold() for p in _resolved_people(row)}


def may_end(row: Mapping[str, Any], actor: "Actor", *, cancelling: bool = False) -> bool:
    """Who may end an ask — the one rule every ending door shares
    (trinity-enterprise#751; the operator routes, the Workspace answer and a
    bulk sweep all reach it through this sink).

    A seat-fenced ending (`_seat_fenced`) belongs to the people the ask was
    addressed to (`resolved_to`, written at raise time): a gated-skill approval,
    whose decision runs a business action, and the answer to an ask addressed to
    a canon role id (trinity-enterprise#1042 — the role addresses the ask, the
    seat authorises the answer) or to the approver (#1053). An empty or
    unreadable snapshot is nobody — except on an `approver` row, which is then
    not fenced at all (a legacy row, open as it was), and on a gate approval
    the platform stamped with the `admin` decider seat
    (trinity-enterprise#1096): an instance admin who is a person decides that
    one (`_admin_seated` + `_gate_admin`). An admin may CANCEL a gate approval
    — the escalation — but never decide any other fenced ask. Every other
    ending keeps the rule it had: any person with access to the agent, checked
    by the caller. Person-only endings stay the routes' check
    (`dependencies.reject_non_person_principal`), since an agent key carries
    its owner's email."""
    if not _seat_fenced(row, cancelling=cancelling):
        return True
    if _addressed(row, actor):
        return True
    if _admin_seated(row) and _gate_admin(getattr(actor, "user", None)):
        return True
    return cancelling and getattr(getattr(actor, "user", None), "role", None) == "admin"


def addressed_view(row: Mapping[str, Any], actor: "Actor") -> Optional[Dict[str, Any]]:
    """Who a seat-fenced ask waits on, and whether THIS person may decide it —
    `may_end`'s own answer, so a card never offers a control the sink would
    refuse (trinity-enterprise#980, widened to role-id asks by #1042 and to
    seated approver asks by #1053). None for every other ask.

    The operator queue lists an ask to anyone who can use its agent, but a
    fenced ask belongs to the people it was addressed to; without this an admin
    saw Approve / Decline on someone else's decision and learned only from the
    refusal that it was not theirs. An empty snapshot waits on nobody and nobody
    may decide it — the sink's answer, never None. A row the platform stamped
    with a decider seat (trinity-enterprise#1096) also says which seat it waits
    on (`waiting_on_seat`; `admin` with nobody in `waiting_on` is the admin
    seat); an un-stamped row keeps its shape."""
    if not _seat_fenced(row):
        return None
    view = {"waiting_on": _resolved_people(row), "can_decide": may_end(row, actor)}
    seat = _decider_seat(row)
    if seat is not None:
        view["waiting_on_seat"] = seat
    return view


def not_addressee(row: Mapping[str, Any]) -> AskNotAddressee:
    """The refusal for `row`, naming who it waits for (trinity-enterprise#1096):
    the people, the seat (the decider stamp, else the role the row was
    addressed to) and, on the admin seat, why it fell there."""
    from services import operator_queue_service as oqs
    seat = _decider_seat(row) or _fenced_role_of(row) or row.get("to_role")
    reason = _context_of(row).get(oqs._WORKSPACE_OPERATORS_REASON_KEY)
    return AskNotAddressee(row.get("id"), role=_fenced_role_of(row),
                           waiting_on=_resolved_people(row), seat=seat,
                           operators_reason=reason if isinstance(reason, str) else None,
                           gate=_gate_approval(row))


def readdressed_detail(row: Mapping[str, Any], *, name_people: bool = True) -> Dict[str, Any]:
    """The 409 body for an ending that lost to a re-address
    (trinity-enterprise#1096, R5): who the card waits for now. The Operations
    door names the people (its caller reads them on the card anyway); the
    Workspace door passes `name_people=False` — the person it moved away from
    is no longer on it, and never learns who is. A role, approver or project
    row names its role and seat only, on every door (R7)."""
    e = not_addressee(row)
    if not name_people:
        return {"code": "readdressed",
                "message": "This ask was moved to someone else before your answer landed; "
                           "it no longer waits for you. Nothing was recorded."}
    if e.role is not None:
        # R7: a role, approver or project row names its role and seat, never a
        # person — the `AskNotAddressee.detail` shape (the text reaches a model
        # through MCP).
        return {"code": "readdressed",
                "message": "This card was re-addressed before your answer landed. "
                           f"{e.role_message()} Nothing was recorded.",
                "seat": e.seat}
    if e.seat == ADMIN_SEAT and not e.waiting_on:
        who = "an instance admin"
    elif e.waiting_on:
        who = (f"{_SEAT_LABELS.get(e.seat or '', 'the person it was addressed to')}: "
               f"{', '.join(e.waiting_on)}")
    else:
        who = "nobody who can decide it"
    return {"code": "readdressed",
            "message": f"This card was re-addressed and now waits for {who}. Nothing was recorded.",
            "waiting_on": e.waiting_on, "seat": e.seat}


def _ledger_email(row: Mapping[str, Any], actor: "Actor") -> str:
    """The email an ending records. An admin with no email deciding through
    the admin arm (trinity-enterprise#1096, R9) is recorded `admin:<username>`
    — never a bare username where every reader expects an email."""
    email = actor.email or ""
    if "@" in email or not _admin_seated(row):
        return email
    username = getattr(getattr(actor, "user", None), "username", None) or email
    return f"admin:{username}" if username else email


def _address_unchanged(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """trinity-enterprise#1096 (R5): the stored address of a seat-fenced row,
    raw, when it still matches the caller's read of `row`; None when a
    re-address moved it meanwhile (or the row is gone). The raw values feed the
    ending's compare-and-set, so a re-address landing after this read is also
    caught. A row that is not seat-fenced is never compared — its ending does
    not depend on who it is addressed to."""
    raw = db.read_operator_queue_address(row["id"])
    if not raw:
        return None
    if (sorted(p.casefold() for p in _resolved_people(raw))
            != sorted(p.casefold() for p in _resolved_people(row))
            or (raw.get("addressed_to_email") or "").casefold()
            != (row.get("addressed_to_email") or "").casefold()
            or _decider_seat(raw) != _decider_seat(row)):
        return None
    return raw


# ---------------------------------------------------------------------------
# The ways an ask ends
# ---------------------------------------------------------------------------

def answer(
    item: Dict[str, Any],
    *,
    response: str,
    response_text: Optional[str],
    actor: Actor,
    responded_by_id: Optional[str] = None,
    divergence_acknowledged: bool = False,
    via_credential_sink: bool = False,
) -> Ending:
    """A person answered `item` (the row the caller read and checked).

    `via_credential_sink` (trinity-enterprise#1066) is set by exactly one
    caller — the Workspace secret field's sink, after it wrote the value into
    the agent's environment — and is what lets a `credential` row end as
    answered. Any other answer to that kind raises `CredentialAnswerRefused`
    before anything is checked or written: the value must never reach
    `response`.

    Raises `ResponseNotOfferedError` (#2376), or a `ReservedAnswerError` for the
    reserved `SOMETHING_ELSE` decision (#3242), before anything is written, then
    `AskNotFound` / `AskConflict` when the compare-and-set did not land. The
    options are frozen at ingest, so validating against the caller's read is
    sound; the status is not, which is what the compare-and-set is for.
    """
    if (item or {}).get("type") == "credential" and not via_credential_sink:
        raise CredentialAnswerRefused(item["id"])
    # #3242: a platform-minted approval (a skill gate) counts only its own
    # options and no agent reads the instruction — refused, named.
    if response == SOMETHING_ELSE and decided_by_options(item):
        raise NotOffMenuError()
    validate_response_choice(item, response, response_text=response_text)
    if not may_end(item, actor):
        raise not_addressee(item)
    expect = _ending_expectation(item, actor)
    from services.operator_queue_service import is_platform_minted
    updated = db.respond_to_operator_queue_item(
        item_id=item["id"],
        response=response,
        response_text=response_text,
        responded_by_id=responded_by_id,
        responded_by_email=_ledger_email(item, actor),
        divergence_acknowledged=divergence_acknowledged,
        # #2372: a platform alert has no agent audience — nobody will ever
        # acknowledge it from a file, so the person's answer is its last event.
        terminal=is_platform_minted(item),
        # trinity-enterprise#1096 (R5): only a seat-fenced row compares its address.
        **({"expect_address": expect} if expect is not None else {}),
    )
    if not updated:
        raise AskNotFound(item["id"])
    if updated.pop("_status_conflict", False):
        raise AskConflict(_conflict_code(updated, address_compared=expect is not None), updated)
    audit = [_audit_row("answered", updated, actor,
                        {"divergence_acknowledged": bool(divergence_acknowledged)})]
    trigger = _broadcast_payload({"type": "operator_queue_responded",
                                  "data": {"id": updated["id"], "agent_name": updated["agent_name"]}})
    return _ended(EndingEvent(ANSWERED, (updated,), actor.email), audit, trigger)


def decided_by_options(item: Dict[str, Any]) -> bool:
    """An approval the platform minted (a skill gate, #751): decided only by one
    of its options, so the reserved `SOMETHING_ELSE` never applies (#3242). The
    Workspace projection exposes exactly this boolean, nothing else about it."""
    from services.operator_queue_service import is_platform_minted
    return (item or {}).get("type") == "approval" and is_platform_minted(item)


def cancel(item_id: str, *, actor: Actor, reason: Optional[str] = None) -> Ending:
    """A person cancelled one ask. Raises `AskNotFound` / `AskConflict` /
    `AskNotAddressee` (a gate approval the actor may not end, #751), or
    `AskReasonRequired` (trinity-enterprise#1096): an admin ending a
    seat-fenced card they are not addressed on — someone else's decision, or
    the admin seat's — gives a reason, recorded on the row like any cancel's."""
    current = db.get_operator_queue_item(item_id)
    if current and not may_end(current, actor, cancelling=True):
        raise not_addressee(current)
    if (current and _seat_fenced(current, cancelling=True) and not _addressed(current, actor)
            and not (reason or "").strip()):
        raise AskReasonRequired(item_id)
    expect = _ending_expectation(current, actor, cancelling=True) if current else None
    updated = db.cancel_operator_queue_item(
        item_id, disposed_by_email=_ledger_email(current or {}, actor), reason=reason,
        **({"expect_address": expect} if expect is not None else {}))
    if not updated:
        raise AskNotFound(item_id)
    if updated.pop("_status_conflict", False):
        code = ("readdressed" if expect is not None and updated.get("status") == "pending"
                else "not_pending")
        raise AskConflict(code, updated)
    audit = [_audit_row("cancelled", updated, actor, {"has_reason": bool(reason)})]
    trigger = _broadcast_payload({"type": "operator_queue_cancelled",
                                  "data": {"id": updated["id"], "agent_name": updated["agent_name"]}})
    return _ended(EndingEvent(CANCELLED, (updated,), actor.email, reason=reason,
                              actor_user_id=_user_id(actor)), audit, trigger)


def dismiss(item_id: str, *, actor: Actor) -> Ending:
    """The person an ask was addressed to dismissed it without answering
    (trinity-enterprise#748). Raises `AskNotFound` / `AskConflict`.

    The same compare-and-set as `cancel`, recorded as `dismissed`, so the
    agent's readback tells "the person chose not to answer" apart from an
    operator's cancel, an answer and an expiry. No reason: dismissing is one
    click and asks for none.
    """
    updated = db.cancel_operator_queue_item(item_id, disposed_by_email=actor.email,
                                            disposition=DISMISSED)
    if not updated:
        raise AskNotFound(item_id)
    if updated.pop("_status_conflict", False):
        raise AskConflict("not_pending", updated)
    audit = [_audit_row("dismissed", updated, actor, {})]
    trigger = _broadcast_payload({"type": "operator_queue_cancelled",
                                  "data": {"id": updated["id"], "agent_name": updated["agent_name"]}})
    return _ended(EndingEvent(DISMISSED, (updated,), actor.email,
                              actor_user_id=_user_id(actor)), audit, trigger)


def bulk_cancel(
    ids: Iterable[str],
    accessible_agent_names: Optional[Set[str]],
    *,
    actor: Actor,
    reason: Optional[str] = None,
) -> Ending:
    """A person cancelled a set of asks in one sweep.

    `ids` are the ids the operator was shown; the rows returned are the ones this
    sweep actually ended (a row answered or cancelled first is skipped, never
    re-ended). One audit row and one trigger per sweep, however many rows.
    """
    ids = list(dict.fromkeys(ids))  # dedupe, keep order — an honest skipped count
    # trinity-enterprise#751: a gate approval the actor may not end is skipped,
    # like any other row this sweep may not touch. trinity-enterprise#1096
    # (R6): so is a seat-fenced row the actor is not addressed on, an admin's
    # included — a sweep carries no per-card reason, so an admin ends someone
    # else's decision one card at a time, with a reason (`skipped_fenced`).
    permitted: List[str] = []
    skipped_fenced = 0
    for i in ids:
        row = db.get_operator_queue_item(i) or {}
        if row and _seat_fenced(row, cancelling=True) and not _addressed(row, actor):
            skipped_fenced += 1
        elif may_end(row, actor, cancelling=True):
            permitted.append(i)
    out = db.bulk_cancel_operator_queue_items(
        permitted, accessible_agent_names, disposed_by_email=actor.email, reason=reason,
    )
    rows, batch_id = out["rows"], out["batch_id"]
    if not rows:
        return Ending(rows=[], batch_id=None, skipped_fenced=skipped_fenced)
    audit = [{
        "event_action": "bulk_cancel",
        "source": "api",
        **_actor_fields(actor),
        "target_type": "operator_queue",
        "details": {
            "batch_id": batch_id,
            "cancelled": len(rows),
            "skipped": len(ids) - len(rows),
            "ids": [r["id"] for r in rows],
            "has_reason": bool(reason),
            "skipped_fenced": skipped_fenced,
        },
    }]
    # Fleet-level: a sweep can span agents, so the trigger names none and
    # carries only a count; listeners refetch the access-controlled list.
    trigger = _broadcast_payload({"type": "operator_queue_cleared",
                                  "data": {"scope": "pending", "count": len(rows)}})
    ending = _ended(
        EndingEvent(CANCELLED, tuple(rows), actor.email, reason=reason, batch_id=batch_id,
                    actor_user_id=_user_id(actor)),
        audit, trigger, batch_id=batch_id,
    )
    ending.skipped_fenced = skipped_fenced
    return ending


def expire() -> Ending:
    """The clock ended every pending ask past its deadline.

    Sends NO trigger of its own: its one caller is the poll cycle, which sends
    ONE thin trigger per cycle (#2915) and folds the expiry into it.
    """
    rows = db.mark_operator_queue_expired()
    if not rows:
        return Ending(rows=[])
    audit = [_expired_audit(r) for r in rows]
    return _ended(EndingEvent(EXPIRED, tuple(rows), None), audit, None)


def _expired_audit(r: Mapping) -> Dict[str, Any]:
    """The one `expired` audit row, shared by BOTH timeout writers — the sweep
    (`expire`) and #3247's in-transaction expiry of a replaced predecessor.

    trinity-enterprise#844: an approval whose action the platform did not hold
    expired without telling anyone whether that action went ahead. A boolean,
    never the reason text (no person's words in an audit row)."""
    details: Dict[str, Any] = {"agent_name": r["agent_name"]}
    if r.get("disposition_reason") == OUTCOME_UNKNOWN:
        details["outcome_unknown"] = True
    return {
        "event_action": "expired",
        "source": "system",
        "target_type": "operator_queue",
        "target_id": r["id"],
        "details": details,
    }


# #3246: the platform ends its own alerts for exactly these reasons — DATA on
# the ledger (`disposition_reason`), rendered by name on the card. A person's
# cancel reason is free text; the platform's is a closed vocabulary.
CONDITION_CLEARED = "condition_cleared"
SUPERSEDED = "superseded"
PLATFORM_ENDING_REASONS = (CONDITION_CLEARED, SUPERSEDED)


def clear_platform(ids: Iterable[str], *, reason: str, batch_id: Optional[str] = None) -> Ending:
    """The PLATFORM ended its own alerts: the condition cleared, or a newer
    reading superseded them (#3246). Mirrors `expire`: no Actor, the ending is
    `cancelled` / `disposed_by = 'platform'` / NULL email, and only the rows
    this call won the compare-and-set for are returned and handed to the
    observers — a row a person ended first is skipped, never re-ended. One
    audit row (`platform_cleared`) and one thin `operator_queue_cancelled`
    trigger per agent; listeners refetch through the access-controlled list.
    """
    if reason not in PLATFORM_ENDING_REASONS:
        raise ValueError(f"platform ending reason must be one of {PLATFORM_ENDING_REASONS}, got {reason!r}")
    ids = list(dict.fromkeys(ids))
    out = db.end_operator_queue_items_by_platform(ids, reason=reason, batch_id=batch_id)
    rows, batch_id = out["rows"], out["batch_id"]
    if not rows:
        return Ending(rows=[], batch_id=None)
    audit = [{
        "event_action": "platform_cleared",
        "source": "system",
        "target_type": "operator_queue",
        "details": {
            "batch_id": batch_id,
            "reason": reason,
            "cancelled": len(rows),
            "skipped": len(ids) - len(rows),
            "ids": [r["id"] for r in rows],
            "agent_names": sorted({r["agent_name"] for r in rows}),
        },
    }]
    agents = list(dict.fromkeys(r["agent_name"] for r in rows))
    triggers = [_broadcast_payload({"type": "operator_queue_cancelled",
                                    "data": {"agent_name": agent, "batch_id": batch_id}})
                for agent in agents]
    for extra in triggers[1:]:
        try:
            operator_resume_service.spawn_on_loop(lambda extra=extra: _announce([], extra))
        except Exception:  # noqa: BLE001 — the ending is committed; it must stand
            logger.warning("[AskService] could not schedule a platform-clear trigger", exc_info=True)
    return _ended(
        EndingEvent(CANCELLED, tuple(rows), None, reason=reason, batch_id=batch_id),
        audit, triggers[0], batch_id=batch_id,
    )


# ---------------------------------------------------------------------------
# The one way an ask begins (PR B): an agent — or, later, a gate — raises it
# ---------------------------------------------------------------------------

class CredentialAnswerRefused(Exception):
    """trinity-enterprise#1066: a `credential` ask is answered ONLY by the
    Workspace secret field's sink — the one caller that wrote the value into
    the agent's environment and answers by name. An operator's respond box, a
    Workspace typed answer or a direct `answer(...)` would put the secret into
    `response`, the row, the write-back and the wake prompt; refused before
    anything is written. `code` is what both doors answer with."""

    code = "credential_answer_via_field"

    def __init__(self, item_id: str):
        super().__init__(self.code)
        self.item_id = item_id


class AskRejected(Exception):
    """A create the sink refused: the HTTP status and the NAMED code the caller
    answers with (422 for a malformed ask, 429 for the caps). `extra` carries
    what the caller can act on — the field and its limit, the roles on offer,
    the expired ask to link. Never agent text echoed back."""

    def __init__(self, status_code: int, code: str, message: str, **extra):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


# trinity-enterprise#1066: `credential` — the agent asks for a secret BY NAME
# (`context.credential_name`); the person fills it through the Workspace secret
# field, whose sink is the only thing that may answer it (`CredentialAnswerRefused`).
ASK_TYPES = ("approval", "question", "alert", "credential")
ASK_ROLES = ("primary", "approver", "viewer", "operator")
# trinity-enterprise#1007: beside the four kinds, an agent's `to` may name a
# canon role by id. This is the form the `invalid_to` refusal teaches; the id
# itself is validated by `role_addressing.parse_role_id` (ROLE_ID_RE).
ASK_ROLE_ID_FORM = "role:<id>"
# Who raised a native ask, and the channel its row records. The column also holds
# `file`, which only the file poller writes (`create_item_with_outcome`), never
# this sink.
ASK_RAISERS = ("agent", "gate")
ASK_CHANNELS = ("mcp", "gate")
# C5: the soonest an agent-raised ask may expire. An ask that times out before a
# person could plausibly read it is a spend loop (every expiry can wake the
# agent), not a question.
MIN_DEADLINE_MINUTES = 15
# C6: how many of the agent's expired proposals the re-ask guard compares.
_REASK_SCAN = 200


def drop_addressee(ask_id: str, email: str, *, reason: str) -> bool:
    """The PLATFORM takes one person off a row it addressed
    (trinity-enterprise#981): removed from `resolved_to`, and the single
    addressee cleared when it named them. The row is NOT ended — it stays, as
    it is, for the people it was also delivered to; a row that named nobody
    else is the caller's to end (`clear_platform`). The Workspace list and its
    doors match the address (`_is_addressed_to` and its SQL twin), so from
    this write on that person no longer reads the row. `reason` is one of
    `PLATFORM_ENDING_REASONS`, like `clear_platform`'s. Returns whether
    anything changed; no audit row carries the person's address — ids and the
    reason only.
    """
    if reason not in PLATFORM_ENDING_REASONS:
        raise ValueError(f"platform ending reason must be one of {PLATFORM_ENDING_REASONS}, got {reason!r}")
    changed = bool(db.drop_operator_queue_addressee(ask_id, email))
    if not changed:
        return False
    audit = [{
        "event_action": "platform_addressee_dropped",
        "source": "system",
        "target_type": "operator_queue",
        "target_id": ask_id,
        "details": {"reason": reason},
    }]
    try:
        operator_resume_service.spawn_on_loop(lambda: _announce(audit, None))
    except Exception:  # noqa: BLE001 — the write is committed; it must stand
        logger.warning("[AskService] could not schedule the addressee-dropped audit", exc_info=True)
    return True


# trinity-enterprise#1095: the platform re-addresses a pending row it addressed
# BY ROLE, for exactly these reasons — a closed vocabulary like
# `PLATFORM_ENDING_REASONS`, carried on the audit row. The assignments router's
# write is the one trigger today; ent#961's unshare adds its own reason here.
ASSIGNMENT_CHANGED = "assignment_changed"
# trinity-enterprise#1134: a project's steward was set, changed, cleared or
# removed from the project — its pending `to: project` rows move (per row, by
# the stamped project id; the enterprise projects module is the one caller).
PROJECT_STEWARD_CHANGED = "project_steward_changed"
# trinity-enterprise#1096: an instance admin moved a stranded card — back to the
# rule that raised it, or to the admin seat (`POST /api/operator-queue/{id}/
# readdress`). The one reason that carries a person's actor on its audit row.
ADMIN_READDRESSED = "admin_readdressed"
PLATFORM_READDRESS_REASONS = (ASSIGNMENT_CHANGED, PROJECT_STEWARD_CHANGED, ADMIN_READDRESSED)

# trinity-enterprise#1096: who a platform-family row is re-addressed to, by the
# rule that raised it — registered by the module that raises the family (the
# `role_addressing.register_target_resolver` shape), keyed on a literal
# request-id prefix under the reserved `gate-` family. The resolver takes the
# agent name and returns the decider: `people`, `single`, `seat` (one of
# `DECIDER_SEATS`) and `operators_reason`, as attributes or keys. In-process
# and registered at boot only; an unregistered family has no rule to re-run.
_readdress_resolvers: Dict[str, Callable[[str], Any]] = {}


def register_readdress_resolver(prefix: str, fn: Callable[[str], Any]) -> Callable[[str], Any]:
    """Register the rule that re-addresses the platform family whose request
    ids start with `prefix`. A prefix outside the reserved `gate-` family is a
    programming error: only the platform mints those ids (R13). So is a prefix
    of hex digits only: it could prefix a skill-gate id (`gate-` + 40 hex),
    and that approval must never reach the admin seat (T3)."""
    from services import operator_queue_service as oqs
    if not prefix or not prefix.startswith(oqs.GATE_ASK_ID_PREFIX) or prefix == oqs.GATE_ASK_ID_PREFIX:
        raise ValueError(f"readdress resolver prefix must extend {oqs.GATE_ASK_ID_PREFIX!r}, got {prefix!r}")
    if _SKILL_GATE_PREFIX.fullmatch(prefix):
        raise ValueError(f"readdress resolver prefix {prefix!r} could prefix a skill-gate id")
    _readdress_resolvers[prefix] = fn
    return fn


def readdress_resolver_for(request_id: Optional[str]) -> Optional[Callable[[str], Any]]:
    """The registered rule for this row's family (longest prefix wins), or None."""
    if not request_id:
        return None
    hits = [p for p in _readdress_resolvers if request_id.startswith(p)]
    return _readdress_resolvers[max(hits, key=len)] if hits else None


def _people_list(values: Optional[Iterable[Any]]) -> List[str]:
    """Lower-cased, de-duplicated, in order; nothing blank."""
    out: List[str] = []
    for value in values or []:
        email = str(value or "").strip().lower()
        if email and email not in out:
            out.append(email)
    return out


def readdress_item(ask_id: str, people: Iterable[str], single: Optional[str], *,
                   reason: str, trigger: bool = True, attach_chat: bool = True,
                   platform_context: Optional[Mapping[str, Any]] = None,
                   actor: Optional[Actor] = None, has_reason: Optional[bool] = None) -> bool:
    """The PLATFORM rewrites ONE pending row's audience
    (trinity-enterprise#1095): `resolved_to` becomes `people`, the single
    Workspace addressee becomes `single`. The row is not ended and its
    question is not touched — the same ask now waits on the people the role
    reaches today, and every ending door (`may_end`, the gate's
    `_approved_by_addressee`, the Inbox's `resolved_to` membership) reads the
    rewritten columns unchanged.

    The Workspace chat keys follow the single addressee: when it changes, the
    thread, turn and discussion keys are dropped — a stale key would link the
    new person to a chat they cannot open — and, when there is a new single
    addressee, their Main is attached as a fresh raise would attach it
    (`operator_queue_service._workspace_attachment`, Main only: the raising
    turn is long over). Attaching may create Main; the row is then written
    under a compare-and-set on the three address columns as read plus
    `status = 'pending'`, so a row answered, cancelled or dropped meanwhile is
    never overwritten.

    `attach_chat=False` (trinity-enterprise#1134) drops the stale keys but
    attaches no Main for the new addressee — a project-addressed row moves to
    a steward or creator who may be off the agent's roster, and a Main is a
    roster relationship (the ent#981 reason the gate skips it).

    `platform_context` (trinity-enterprise#1134) re-stamps platform keys
    (`_PLATFORM_CONTEXT_KEYS` only; anything else is ignored) in the same
    compare-and-set — a project row moved from its steward to its creator
    carries the seat that picked the new reader, never the old one's.
    trinity-enterprise#1096 (R4): a stamp whose value is None REMOVES the key,
    and a stamp that differs from the stored one counts as a change — so a
    card moved off the admin seat (or to another seat with the same people)
    never keeps a stale seat.

    `actor` (trinity-enterprise#1096) is the admin who moved the row by hand
    (`ADMIN_READDRESSED`): the audit row then carries that person and
    `has_reason`, never the reason's text.

    `reason` is one of `PLATFORM_READDRESS_REASONS`. An unchanged audience
    and unchanged stamps write, audit and announce nothing. Per changed row: one audit row (ids
    and enums only, never an address — ent#715) and, when `trigger`, the thin
    agent-keyed `/ws` trigger the Operations store refetches on. Returns
    whether the row changed; no row, or a row no longer pending, is False.
    `single` must be one of `people` — anything else is a programming error.
    """
    from services import operator_queue_service as oqs

    if reason not in PLATFORM_READDRESS_REASONS:
        raise ValueError(f"platform readdress reason must be one of {PLATFORM_READDRESS_REASONS}, got {reason!r}")
    new_people = _people_list(people)
    new_single = (single or "").strip().lower() or None
    if new_single and new_single not in new_people:
        raise ValueError("readdress_item: single must be one of people")
    row = db.read_operator_queue_address(ask_id)
    if not row or row.get("status") != "pending":
        return False
    old_people = _people_list(_resolved_people(row))
    old_single = (row.get("addressed_to_email") or "").strip().lower() or None
    raw_context = row.get("context")
    stamps = {k: v for k, v in (platform_context or {}).items() if k in oqs._PLATFORM_CONTEXT_KEYS}
    stored = _context_of(row)
    stamps_differ = any(stored.get(k) != v for k, v in stamps.items())
    if set(old_people) == set(new_people) and old_single == new_single and not stamps_differ:
        return False

    context: Optional[Dict[str, Any]] = None
    if old_single != new_single or stamps_differ:
        context = dict(stored)
    if old_single != new_single:
        for key in (oqs._WORKSPACE_THREAD_KEY, oqs._WORKSPACE_TURN_KEY, oqs._WORKSPACE_DISCUSSION_KEY):
            context.pop(key, None)
        if new_single and attach_chat:
            thread, _ = oqs._workspace_attachment(row["agent_name"], new_single)
            if thread:
                context[oqs._WORKSPACE_THREAD_KEY] = thread
    if context is not None:
        for key, value in stamps.items():
            if value is None:
                context.pop(key, None)
            else:
                context[key] = value

    changed = bool(db.readdress_operator_queue_item(
        ask_id, new_people, new_single,
        expect_single=row.get("addressed_to_email"), expect_resolved_raw=row.get("resolved_to"),
        context=context, expect_context_raw=raw_context))
    if not changed:
        return False
    audit = [{
        "event_action": "platform_readdressed",
        "source": "system",
        "target_type": "operator_queue",
        "target_id": ask_id,
        "details": {"reason": reason, "to_role": row.get("to_role"), "agent_name": row["agent_name"]},
    }]
    if actor is not None:
        audit[0].update(source="api", **_actor_fields(actor))
        audit[0]["details"]["has_reason"] = bool(has_reason)
    payload = _broadcast_payload({"type": "operator_queue_new",
                                  "data": {"id": ask_id, "agent_name": row["agent_name"]}})
    try:
        operator_resume_service.spawn_on_loop(lambda: _announce(audit, payload if trigger else None))
    except Exception:  # noqa: BLE001 — the write is committed; it must stand
        logger.warning("[AskService] could not schedule the readdressed audit", exc_info=True)
    return True


def readdress_role(agent_name: str, role: str, *, reason: str) -> Dict[str, int]:
    """Who fills `role` on `agent_name` changed: re-resolve it and move this
    agent's pending rows that were addressed to it BY ROLE
    (trinity-enterprise#1095) — the agent's own raises, and gated-skill
    approvals (`db.list_pending_role_addressed`'s allowlist). A row the
    platform addressed to a named person (a gate note, a project consent card,
    a reach row — ent#661) is never moved HERE: the role was never its fence.
    A project consent card is moved by the module that raised it, through
    `readdress_item` with its own decider (trinity-enterprise#1096) — this
    pass stays unaware of that family.

    The fresh audience is what a fresh raise would get: an agent's row through
    `_address` (the one resolution rule), a gate approval through the gate's
    own `_approvers` (ent#881 reachability applied), so a moved approval
    reaches exactly who `enforce` would ask today. Nobody filling `primary`
    leaves the row with NO audience — it falls to the operators exactly as a
    fresh no-primary raise does, and is re-addressed when a primary is set
    again. A refused role (`approver`/`viewer` unfilled, nobody seated who can
    open the agent) leaves the rows AS THEY WERE: an empty snapshot on an
    approver row would un-fence it (ent#1053). A credential row keeps exactly
    one reader (ent#1066): it moves only to a single person, and an agent's
    credential row only to one who can open its secret field (#4579,
    `_fresh_credential_audience`).

    `role` is one of `ASK_ROLES`; anything else is a programming error. One
    audit row per changed row (`readdress_item`), and ONE agent-keyed `/ws`
    trigger per call when anything changed — a replace is two writes
    (delete, then create) and each announces once. Returns the counts.
    """
    if role not in ASK_ROLES:
        raise ValueError(f"readdress_role: role must be one of {ASK_ROLES}, got {role!r}")
    if reason not in PLATFORM_READDRESS_REASONS:
        raise ValueError(f"platform readdress reason must be one of {PLATFORM_READDRESS_REASONS}, got {reason!r}")
    rows = db.list_pending_role_addressed_operator_queue_items(agent_name, role)
    out = {"readdressed": 0, "unchanged": 0}
    if not rows:
        return out
    fresh: Dict[Tuple[bool, bool], Optional[Tuple[List[str], Optional[str]]]] = {}
    first_changed: Optional[str] = None
    for row in rows:
        gate = row.get("raised_by") == "gate"
        # #4579: an agent's credential row follows the rule its raise used —
        # the reader must be able to open the secret field.
        own_credential = row.get("type") == "credential" and role == "primary" and not gate
        key = (gate, own_credential)
        if key not in fresh:
            fresh[key] = (_fresh_credential_audience(agent_name) if own_credential
                          else _fresh_audience(agent_name, role, gate=gate))
        audience = fresh[key]
        if audience is None:
            out["unchanged"] += 1
            continue
        people, single = audience
        if row.get("type") == "credential" and not single:
            logger.info("[AskService] credential ask %s on %s keeps its reader: %s now reaches %d people",
                        row["id"], agent_name, role, len(people))
            out["unchanged"] += 1
            continue
        if readdress_item(row["id"], people, single, reason=reason, trigger=False):
            out["readdressed"] += 1
            first_changed = first_changed or row["id"]
        else:
            out["unchanged"] += 1
    if first_changed:
        trigger = _broadcast_payload({"type": "operator_queue_new",
                                      "data": {"id": first_changed, "agent_name": agent_name}})
        try:
            operator_resume_service.spawn_on_loop(lambda: _announce([], trigger))
        except Exception:  # noqa: BLE001 — the writes are committed; they must stand
            logger.warning("[AskService] could not schedule the readdressed trigger", exc_info=True)
    return out


def _fresh_credential_audience(agent_name: str) -> Optional[Tuple[List[str], Optional[str]]]:
    """`_fresh_audience` for an agent's `credential` row (#4579): the reader a
    fresh raise would record NOW (`role_addressing.resolve_credential_reader`),
    or None when nobody can open the secret field, so the row stays as it was."""
    from services import role_addressing

    try:
        r = role_addressing.resolve_credential_reader(agent_name, owner_lookup=_owner_email)
    except role_addressing.RoleRefused as e:
        logger.info("[AskService] the credential asks on %s are not re-addressed: %s",
                    agent_name, e.code)
        return None
    people = _people_list(r.people)
    return people, (people[0] if len(people) == 1 else None)


def _fresh_audience(agent_name: str, role: str, *, gate: bool) -> Optional[Tuple[List[str], Optional[str]]]:
    """`(people, single)` the role reaches NOW, by the rule the row's raiser
    uses; None when the role refuses and the rows must stay as they were.
    Nobody filling `primary` is `([], None)` — the operators — for both."""
    if gate:
        from services import skill_gate_service as sgs
        try:
            people = sgs._approvers(agent_name, role)
        except sgs.SkillGateRefused as e:
            if role == "primary" and e.code == "role_unassigned":
                people = []
            else:
                logger.info("[AskService] %s on %s not re-addressed for gate approvals: %s",
                            role, agent_name, e.code)
                return None
    else:
        try:
            people, _, _ = _address(agent_name, role)
        except AskRejected as e:
            logger.info("[AskService] %s on %s not re-addressed for the agent's asks: %s",
                        role, agent_name, e.code)
            return None
    people = _people_list(people)
    return people, (people[0] if len(people) == 1 else None)


# ---------------------------------------------------------------------------
# An admin moves a stranded card (trinity-enterprise#1096)
# ---------------------------------------------------------------------------

# The skill gate's id shape (`skill_gate_service._request_id`) — the gate
# approvals `readdress_role` moves (`db.list_pending_role_addressed`'s twin).
_SKILL_GATE_ID = re.compile(r"gate-[0-9a-f]{40}")
# trinity-enterprise#1096 (L1): a resolver prefix that could start a skill-gate id.
_SKILL_GATE_PREFIX = re.compile(r"gate-[0-9a-f]*")
READDRESS_TARGETS = ("rule", "admin")


class ReaddressRefused(Exception):
    """An admin re-address that did not happen, named: `status` and `code`
    for the route, `message` for the person, `data` the structured extras
    (who the card waits for now, on `audience_unchanged`)."""

    def __init__(self, status: int, code: str, message: str, **data: Any):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.data = data


class AskReasonRequired(Exception):
    """trinity-enterprise#1096: an admin ending a seat-fenced card they are
    not addressed on — someone else's decision — must say why."""

    def __init__(self, item_id: str):
        super().__init__("reason_required")
        self.item_id = item_id


def _decider_value(decider: Any, key: str) -> Any:
    if isinstance(decider, Mapping):
        return decider.get(key)
    return getattr(decider, key, None)


def _role_family(row: Mapping[str, Any]) -> bool:
    """A row addressed BY ROLE, whose rule `_fresh_audience` re-runs: the
    agent's own raise to an `ASK_ROLES` role, or a skill-gate approval —
    `readdress_role`'s allowlist."""
    if row.get("to_role") not in ASK_ROLES:
        return False
    if row.get("raised_by") == "agent":
        return True
    return _gate_approval(row) and bool(_SKILL_GATE_ID.fullmatch(row.get("request_id") or ""))


def admin_readdress(item_id: str, *, to: str, reason: str, actor: Actor) -> Dict[str, Any]:
    """An instance admin re-addresses one pending card
    (trinity-enterprise#1096, R2/T2) — never to a person they pick:

    - `to="rule"` re-runs the rule that raised it: a platform family's
      registered resolver (`register_readdress_resolver`; the decider and its
      seat stamp), or, for a row addressed by role, the role as
      `readdress_role` would resolve it now;
    - `to="admin"` moves a registered family's gate approval to the admin seat
      (no audience, the `admin` stamp), which an admin then decides. A skill
      gate approval cannot go there (T3): its resume reads `resolved_to`, so an
      admin's answer would be refused by the gate.

    Written through `readdress_item` with `ADMIN_READDRESSED`, the admin on
    the audit row and `has_reason` (the reason's text is never stored there).
    Raises `AskNotFound`, or `ReaddressRefused`: 409 `not_pending`, 422
    `unknown_family` (no rule to re-run), 422 `admin_seat_unsupported`, 409
    `rule_refused` (the role refuses today, the family's rule raises, or it
    names a person seat with nobody on it), 409 `audience_unchanged` naming
    who the card waits for. The caller (the route) has already applied the
    admin + person gate and the row's visibility.
    """
    from services import operator_queue_service as oqs

    if to not in READDRESS_TARGETS:
        raise ValueError(f"admin_readdress: to must be one of {READDRESS_TARGETS}, got {to!r}")
    row = db.read_operator_queue_address(item_id)
    if not row:
        raise AskNotFound(item_id)
    if row.get("status") != "pending" or _past_deadline(row):
        raise ReaddressRefused(409, "not_pending", "This card has already ended; there is nothing to move.")
    resolver = readdress_resolver_for(row.get("request_id")) if _gate_approval(row) else None
    agent_name = row["agent_name"]
    stamps: Dict[str, Any] = {}
    attach_chat = True
    if resolver is not None:
        attach_chat = False
        if to == ADMIN_SEAT:
            people, single, seat = [], None, ADMIN_SEAT
            stamps = {oqs._WORKSPACE_DECIDER_SEAT_KEY: ADMIN_SEAT,
                      oqs._WORKSPACE_OPERATORS_REASON_KEY: None}
        else:
            try:
                decider = resolver(agent_name)
            except Exception:
                # L3: a rule that fails is a refusal the admin can act on, not a 500.
                logger.warning("[AskService] readdress resolver failed for %s", item_id, exc_info=True)
                raise ReaddressRefused(409, "rule_refused",
                                       "The rule that raised this card could not be run right now, "
                                       "so the card stays as it is; end it with a reason instead.")
            seat = _decider_value(decider, "seat")
            if seat not in DECIDER_SEATS:
                raise ValueError(f"readdress resolver returned seat {seat!r}")
            people = _people_list(_decider_value(decider, "people"))
            if not people and seat != ADMIN_SEAT:
                # L3: only the admin seat may be empty — a person seat with
                # nobody on it would strand the card the admin is fixing.
                raise ReaddressRefused(409, "rule_refused",
                                       "The rule that raised this card finds nobody to decide it "
                                       "right now, so the card stays as it is; move it to the admin "
                                       "seat (to: admin) or end it with a reason.")
            single = (_decider_value(decider, "single") or "").strip().lower() or None
            if single is None and len(people) == 1:
                single = people[0]
            reason_code = _decider_value(decider, "operators_reason") if seat == ADMIN_SEAT else None
            stamps = {oqs._WORKSPACE_DECIDER_SEAT_KEY: seat,
                      oqs._WORKSPACE_OPERATORS_REASON_KEY: reason_code}
    elif to == ADMIN_SEAT:
        if _role_family(row) or _gate_approval(row):
            raise ReaddressRefused(
                422, "admin_seat_unsupported",
                "This card cannot be moved to the admin seat: who decides it is fixed by the rule "
                "that raised it. Re-address it by its rule, or end it with a reason.")
        raise ReaddressRefused(422, "unknown_family",
                               "This card has no rule to re-address it by; end it with a reason instead.")
    elif _role_family(row):
        gate = row.get("raised_by") == "gate"
        # #4579: an agent's credential row is re-addressed by the rule its
        # raise used, so "by its rule" cannot hand the secret field to a
        # primary who cannot open it.
        audience = (_fresh_credential_audience(agent_name)
                    if row.get("type") == "credential" and row["to_role"] == "primary" and not gate
                    else _fresh_audience(agent_name, row["to_role"], gate=gate))
        if audience is None:
            raise ReaddressRefused(409, "rule_refused",
                                   f"Nobody can be found for the {row['to_role']} role on this agent "
                                   "right now, so the card stays as it is; end it with a reason instead.")
        people, single = audience
        seat = row["to_role"]
        if row.get("type") == "credential" and not single:
            raise ReaddressRefused(409, "rule_refused",
                                   "A credential ask must reach exactly one person, and its role "
                                   "reaches more or fewer today; end it with a reason instead.")
    else:
        raise ReaddressRefused(422, "unknown_family",
                               "This card has no rule to re-address it by; end it with a reason instead.")

    changed = readdress_item(item_id, people, single, reason=ADMIN_READDRESSED,
                             attach_chat=attach_chat, platform_context=stamps or None,
                             actor=actor, has_reason=bool((reason or "").strip()))
    if not changed:
        fresh = db.read_operator_queue_address(item_id) or {}
        if fresh.get("status") != "pending":
            raise ReaddressRefused(409, "not_pending", "This card ended while it was being moved.")
        waiting = _resolved_people(fresh)
        now_seat = _decider_seat(fresh) or fresh.get("to_role")
        who = (f"{_SEAT_LABELS.get(now_seat or '', 'the person it was addressed to')}: "
               f"{', '.join(waiting)}" if waiting else "an instance admin")
        raise ReaddressRefused(
            409, "audience_unchanged",
            f"This card already waits for {who}; re-addressing by its rule changes nothing. "
            "Move it to the admin seat (to: admin) or end it with a reason.",
            waiting_on=waiting, seat=now_seat)
    return {"id": item_id, "waiting_on": people, "seat": seat}


def _raising_room(agent_name: str, turn: Optional[str], ask_type: str) -> Optional[str]:
    """The room whose RUNNING turn raised this ask, or None
    (abilityai/trinity-enterprise#1135). An agent's raise only — a gate's ask is
    a background ask (the ent#610 amendment) — and never a credential ask: a
    secret has exactly one reader (ent#1066), and a room has many. Fail-soft,
    the #1632 clamp contract: a lookup that fails leaves the ask where ent#734
    puts it (its chat, or Main) and never refuses it."""
    if not turn or ask_type == "credential":
        return None
    try:
        from shared_sessions import service as rooms

        return rooms.room_for_execution(agent_name, turn)
    except Exception:  # noqa: BLE001
        logger.warning("[AskService] could not resolve the room of the turn that raised an "
                       "ask from %s; it is not attached to a room (ent#1135)",
                       agent_name, exc_info=True)
        return None


def raise_ask(
    agent_name: str,
    ask: Mapping[str, Any],
    *,
    raised_by: str,
    channel: str,
    actor_user: Any = None,
    addressee: Optional[str] = None,
    platform_execution_id: Optional[str] = None,
    addressees: Optional[List[str]] = None,
    to_role_override: Optional[str] = None,
    attach_chat: bool = True,
    platform_context: Optional[Mapping[str, Any]] = None,
    destination_key_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Raise an ask through the platform and return its receipt
    (trinity-enterprise#611).

    `destination_key_state` (trinity-enterprise#1158 §2) is what the raise
    route read from the asking agent's own `.env` for a `key_env` destination
    (`credential_sink.key_env_state`): `missing` → 422
    `destination_key_env_missing`, `invalid` → 422 `destination_key_invalid`,
    `ok` or None (could not tell — delivery re-checks) → no effect. Applied
    LAST, after every other refusal and the replay, and only to a stored
    `key_env` destination; gate and file callers pass nothing.

    `attach_chat` and `platform_context` (trinity-enterprise#981) are the
    gate's too — an agent's raise passing either is a programming error.
    `attach_chat=False` skips the Workspace attachment: an addressed ask
    normally resolves (and may create) the pair's Main, which a row the
    platform raises about a person under a platform agent must not do. The
    row then carries no chat. `platform_context` is what the PLATFORM says
    about the row — merged into `context` AFTER the agent-content strip and
    only for keys in `_PLATFORM_CONTEXT_KEYS`, so the same keys are stripped
    from anything an agent authored and written only here.

    `addressees` + `to_role_override` (trinity-enterprise#848) are the gate's
    pre-resolved audience: the very people `skill_gate_service.enforce` decided
    self-approval with, and the approver value the ask is recorded `to_role`
    as — a kind, or a canon role `role:<id>` (an agent's own `to` may name one
    too since trinity-enterprise#1007, resolved by `_address` on its own
    roster; the gate's value is pre-resolved and skips that step). Given, role
    resolution (`_address`) is skipped, so the gate's audience and the ask's
    cannot diverge (no second resolve between the two). Gate raises only, like
    `addressee`; an agent's raise passing either is a programming error.

    `platform_execution_id` (ent#661 v3) is the raising turn as the platform
    saw it — the MCP request's `X-Trinity-Execution-Id` (#2392). When it is one
    of this agent's own executions it is recorded as the ask's execution,
    winning over an agent-written `context.execution_id`; `manual`, an unknown
    id or another agent's changes nothing. Consumers (a project's asks) read
    the turn from it, and ent#734 attaches an addressed ask to that turn's
    Workspace chat. The header is platform-set, but the agent's own process can
    send any of its executions' ids, so the guarantee is "one of this agent's
    own turns" — never another agent's, and never (ent#734) another person's
    chat or a finished turn's.

    `addressee` (ent#661) names exactly who is asked, bypassing role
    resolution. Only a `gate` raise may pass it (a platform decision such as an
    agent owner's consent must reach that owner, not whoever a provider maps
    `primary` to); for an agent's raise it is a programming error, so an agent
    can never choose who is asked.

    The seam the agent's MCP tool calls today, and the one a gate calls later
    with `raised_by="gate"`, `channel="gate"` and a `request_id` derived from the
    paused call under the platform-reserved `gate-` prefix (required for a gate
    raise, refused for an agent's). In order:

    1. validate the shape — `AskRejected(422)` with a named code, never a
       silent truncation (that is the file path's compatibility behaviour);
    2. REPLAY: `(agent, request_id)` exists → the first ask's receipt,
       `status: "replayed"`, plus `differs` (what this call changed). Before
       every time- or state-dependent check, so a retry minutes later gets its
       receipt back rather than a refusal it did not earn the first time;
    2b. the authoring caps (#3243) — option count and length (gate raises
       too), a lookalike of the reserved `SOMETHING_ELSE`, and an agent's
       title length. After the replay, deliberately: an ask raised before the
       caps existed still gets its receipt back on retry. Before the rate cap,
       like every other 422, so a refusal spends no token;
    3. the #1632 rate caps — the SAME buckets as the file poller, so the two
       channels share one budget. Before every check that reads the database,
       so a refusal below spends a token and cannot be repeated for free (the
       re-ask scan reads up to `_REASK_SCAN` stored proposals). An AGENT's
       raise only: a gate raise neither spends nor is refused by the agent's
       buckets (trinity-enterprise#751) — shared, any requester could park the
       agent's queue with gated requests and block its own asks, and an agent
       flooding its own queue would block every gated request to it. The gate
       (`services/skill_gate_service.py`) caps its own raises, per requester
       and per executor, before it calls this;
    4. the deadline floor (C5), the re-ask link (`supersedes_expired` must name
       the agent's own expired ask) and its guard (C6), the replace target
       (#3247: `replaces` must name the agent's OWN pending ask — a row with
       `raised_by == 'agent'` under its name, checked on the column itself,
       never inferred — one uniform 422 `invalid_replaces` otherwise) and the
       then the role (`to:`) resolved to a person;
    5. the create: replay, the replace's compare-and-set, the pending-proposal
       guard (T8: an AGENT's approval whose non-empty `proposal` one of the
       agent's own pending agent-raised approvals already carries, not
       replaced here → 409 `already_pending`; a question, an alert, an empty
       proposal and a gate raise are neither refused nor counted), depth cap
       and insert in one per-agent serialized step (`queue_full` → 429); the
       guard sits inside that lock so two concurrent raises with one
       proposal have exactly one winner. The depth cap counts the agent's own asks
       and applies to them only — gate rows are neither counted nor capped
       here (#751, same reason as step 3). With `replaces`, the predecessor
       is ended in that same transaction by compare-and-set (#3247): if it
       already ended, 409 `replaces_ended` names its real state (ids and
       enums only — a person's answer stands and is read with `get_my_ask`)
       and nothing is created; still pending past its deadline, it is expired
       in-transaction (the normal expiry event fires) and then refused;
    6. one audit row (`raised`, ids and enums only) and one thin broadcast;
       a replace adds the predecessor's ending — an agent-keyed `replaced`
       audit row, `operator_queue_cancelled`, and the observers (the default
       wake skips an ending the agent authored itself).

    The receipt names the ROLE an ask went to, never the resolved email, and
    who raised it. An unknown `raised_by` or `channel` is a programming error
    (`ValueError`), never a refusal an agent earned.
    """
    from services import operator_queue_service as oqs
    from services import role_addressing

    if raised_by not in ASK_RAISERS:
        raise ValueError(f"raise_ask: unknown raised_by {raised_by!r}")
    if channel not in ASK_CHANNELS:
        raise ValueError(f"raise_ask: unknown channel {channel!r}")
    if (raised_by == "gate") != (channel == "gate"):
        raise ValueError(f"raise_ask: channel {channel!r} does not go with raised_by {raised_by!r}")
    named = _named_addressee(addressee, raised_by)
    pre = _named_addressees(addressees, to_role_override, raised_by, named, platform_context)
    if raised_by != "gate" and (attach_chat is not True or platform_context is not None):
        raise ValueError("raise_ask: only a gate raise may skip the chat or attach platform context")
    norm = _validated_ask(ask, oqs, raised_by=raised_by, agent_name=agent_name)
    existing = db.get_operator_queue_item_for_agent_by_request_id(agent_name, norm["request_id"])
    if existing:
        return _replay(existing, norm, oqs, raised_by)
    _refuse_over_caps(norm, oqs, raised_by)

    if raised_by == "agent" and not _rate_allowed(agent_name, oqs):
        raise AskRejected(429, "rate_limited",
                          "Too many asks in a short time; try again in a minute.")

    deadline = _deadline(norm["expires_at"], floor=True)
    predecessor = _predecessor(agent_name, norm["supersedes_expired"], raised_by)
    if norm["proposal"] is not None and predecessor is None:
        _refuse_unlinked_reask(agent_name, norm["proposal"], raised_by)
    target = _replace_target(agent_name, norm["replaces"])
    to_role = to_role_override or norm["to"]
    turn = _platform_turn(agent_name, platform_execution_id)
    project_id: Optional[str] = None
    seat: Optional[str] = None
    if named:
        people, addressee, resolved = [named], named, True
    elif pre is not None:
        people, addressee, resolved = pre, (pre[0] if len(pre) == 1 else None), bool(pre)
    elif norm["to"] in role_addressing.ASK_TARGETS:
        # trinity-enterprise#1134: the platform resolves the target from the
        # turn it knows — the agent supplied the word, nothing else.
        people, addressee, resolved, project_id, seat = _address_target(
            agent_name, norm["to"], turn)
    elif norm["type"] == "credential":
        # #4579: the secret field is this kind's only door, so its reader is
        # resolved by the rule that asks who can open it.
        people, addressee, resolved = _address_credential(agent_name)
    else:
        people, addressee, resolved = _address(agent_name, norm["to"])
    if norm["type"] == "credential" and not addressee:
        # trinity-enterprise#1066: a secret field has exactly ONE reader. No
        # resolved person (the operator role, an agent with no owner and no
        # primary) or several (a role two people fill) is refused before
        # anything is written — the value must not be typed by whoever
        # answers first and recorded against everyone it was delivered to.
        raise AskRejected(
            422, "no_person",
            "A credential ask must reach exactly one person; ask it from a conversation "
            "with the person who holds the credential, addressed to primary.")
    _refuse_key_state(norm, destination_key_state)

    context = dict(norm["context"])
    if turn:
        context["execution_id"] = turn
    if addressee and attach_chat and project_id is not None:
        # trinity-enterprise#1134: a project-addressed ask reaches a person who
        # may be off the agent's roster, so it attaches ONLY to the chat the
        # addressee's own running turn serves — never their Main (a roster
        # relationship, which the ent#981 reach rows skip for the same reason).
        thread = _in_turn_chat(agent_name, addressee, turn)
        if thread:
            context[oqs._WORKSPACE_THREAD_KEY] = thread
            context[oqs._WORKSPACE_TURN_KEY] = True
    elif addressee and attach_chat:
        # The chat the raising turn serves (ent#734), else the addressee's Main
        # (ent#429/#523), resolved at raise time. Only after the caps passed:
        # attaching may create Main. Only an AGENT's raise reads the turn: a
        # gate's ask is a background ask, and belongs to the Inbox only
        # (the ent#610 amendment of 2026-09-30). A gate may skip this
        # (trinity-enterprise#981): a row about a person under a platform
        # agent opens no chat.
        thread, in_turn = oqs._workspace_attachment(
            agent_name, addressee, execution_id=turn if raised_by == "agent" else None)
        if thread:
            context[oqs._WORKSPACE_THREAD_KEY] = thread
        if thread and in_turn:
            context[oqs._WORKSPACE_TURN_KEY] = True
    elif not people and raised_by == "agent" and to_role == "primary" and turn:
        # trinity-enterprise#1134 (AC4): a chat turn's `primary` ask that fell
        # to the operators (nobody assigned, no owner who can stand in). The
        # person in the chat is not its addressee and reads none of its
        # content — but the chat the turn serves is stamped, with why it fell,
        # so that chat's own read shows one content-free line. An explicit
        # `to: operator` is never stamped: it may be ABOUT the person.
        hit = _turn_chat(agent_name, turn)
        if hit:
            context[oqs._WORKSPACE_THREAD_KEY] = hit
            context[oqs._WORKSPACE_TURN_KEY] = True
            reason = role_addressing.operators_reason(agent_name)
            if reason:
                context[oqs._WORKSPACE_OPERATORS_REASON_KEY] = reason
    if project_id is not None:
        # The platform's own stamp (ent#981's key, reused): written after the
        # strip in `_validated_ask`, so an agent-authored value never survives
        # and the Workspace card shows the project exactly as a gate row does.
        context[oqs._WORKSPACE_PROJECT_ID_KEY] = project_id
        if seat in ("steward", "creator"):
            context[oqs._WORKSPACE_PROJECT_SEAT_KEY] = seat
    if raised_by == "agent" and turn:
        # trinity-enterprise#1225: the turn the answer and ending wakes
        # continue, recorded only while it runs on this agent.
        from services import turn_tier_service
        origin = turn_tier_service.continuation_origin(db, agent_name, turn)
        if origin:
            context[oqs._CONTINUES_TURN_KEY] = origin
    room_id = _raising_room(agent_name, turn, norm["type"]) if raised_by == "agent" else None
    if room_id:
        context[oqs._WORKSPACE_ROOM_KEY] = room_id
    for key, value in (platform_context or {}).items():
        # trinity-enterprise#981: the platform's own attachment, platform keys
        # only — after the strip in `_validated_ask`, so it is the one writer.
        if key in oqs._PLATFORM_CONTEXT_KEYS:
            context[key] = value
    item = {
        "id": norm["request_id"],
        "type": norm["type"],
        "priority": norm["priority"],
        "title": norm["title"],
        "question": norm["question"],
        "options": norm["options"],
        "context": context,
        "created_at": utc_now_iso(),
        "expires_at": to_utc_iso(deadline) if deadline else None,
        "addressed_to_email": addressee,
    }
    out = db.create_native_operator_queue_item(
        agent_name, item,
        max_pending=_max_pending() if raised_by == "agent" else None,
        channel=channel,
        raised_by=raised_by,
        to_role=to_role,
        resolved_to=people or None,
        proposal=norm["proposal"],
        supersedes_expired=predecessor["id"] if predecessor else None,
        # #3130: the cap counts the agent's own asks, never the platform's rows
        # about it (its flood alarm above all).
        exclude_request_id_prefixes=oqs.reserved_id_prefixes(),
        replaces=target["id"] if target else None,
        # T8 (#3247): an AGENT's approval with a non-empty proposal only. A
        # question or an alert is never guarded; a missing or empty proposal is
        # not a proposal. A gate raise is never refused by it and never counted
        # by it (trinity-enterprise#751 raises one approval per occurrence).
        guard_pending_proposal=(raised_by == "agent" and norm["type"] == "approval"
                                and bool(norm["proposal"])),
    )
    if out["outcome"] == "already_pending":
        raise AskRejected(
            409, "already_pending",
            "You already asked this and it is still pending; replace it (set replaces to "
            "that ask's request_id) or wait for the answer.",
            request_id=out["request_id"])
    if out["outcome"] == "queue_full":
        raise AskRejected(429, "queue_full",
                          "You already have the maximum number of open asks; wait for one to end.",
                          max_pending=_max_pending())
    if out["outcome"] == "replaces_not_own":
        # the compare-and-set's belt held where the gate above did not: the
        # same uniform refusal, nothing disclosed
        raise AskRejected(422, "invalid_replaces", _INVALID_REPLACES)
    if out["outcome"] == "replaces_ended":
        if out.get("expired_now"):
            # T5b: the predecessor was still pending past its deadline and the
            # create's compare-and-set expired it — the normal expiry event,
            # exactly as `expire()` announces its own per-id CAS winners. Plus
            # a thin trigger: `expire()` leaves that to the poll cycle behind
            # it, and this path has none, so without one an open page shows the
            # ask as pending until its next poll. The agent-scoped "an ask
            # ended, refetch" trigger — there is no expiry-specific type.
            pred = out["predecessor"]
            _ended(EndingEvent(EXPIRED, (pred,), None), [_expired_audit(pred)],
                   _broadcast_payload({"type": "operator_queue_cancelled",
                                       "data": {"id": pred["id"], "agent_name": pred["agent_name"]}}))
        _refuse_ended_predecessor(out["predecessor"])
    row = out["row"]
    if out["outcome"] == "replayed":   # a concurrent call with the same id won
        return _replay(row, norm, oqs, raised_by)
    if room_id:
        # ent#1135: the room where the agent asked hears that it is waiting —
        # only now, so a refused ask leaves no trace there. Never raises.
        from shared_sessions import service as rooms

        rooms.post_ask_raised_line(room_id, agent_name, row)
    replaced = out.get("predecessor") if target else None

    audit = [{
        "event_action": "raised",
        "source": "api" if raised_by == "agent" else "system",
        # The AGENT raised it, not the owner its key resolves to (the ent#614
        # rule, routers/fan_out.py): the resolver ranks a user first, so there is
        # no `actor_user`; the presented credential and the owner's email — the
        # join back to the human — are carried explicitly.
        "actor_agent_name": agent_name,
        "actor_email": getattr(actor_user, "email", None),
        "mcp_key_id": getattr(actor_user, "mcp_key_id", None),
        "mcp_key_name": getattr(actor_user, "mcp_key_name", None),
        "mcp_scope": getattr(actor_user, "mcp_scope", None),
        "target_type": "operator_queue",
        "target_id": row["id"],
        "details": {
            "agent_name": agent_name,
            "request_id": row["request_id"],
            "channel": channel,
            "raised_by": raised_by,
            "type": norm["type"],
            "to_role": to_role,
            **({"replaces": replaced["id"]} if replaced else {}),
            **_acted_by(actor_user),
        },
    }]
    trigger = _broadcast_payload({"type": "operator_queue_new",
                                  "data": {"id": row["id"], "agent_name": agent_name}})
    try:
        operator_resume_service.spawn_on_loop(lambda: _announce(audit, trigger))
    except Exception:  # noqa: BLE001 — the ask is stored; it must stand
        logger.warning("[AskService] could not schedule the raised announcement", exc_info=True)
    if replaced:
        # #3247: the predecessor's ending — through the one sink every ending
        # takes (audit, thin trigger, observers), with the row AS THE CAS LEFT
        # IT. Agent-keyed like the `raised` row: there is no person `Actor`.
        ending_audit = [{
            "event_action": "replaced",
            "source": "api",
            "actor_agent_name": agent_name,
            "actor_email": getattr(actor_user, "email", None),
            "mcp_key_id": getattr(actor_user, "mcp_key_id", None),
            "mcp_key_name": getattr(actor_user, "mcp_key_name", None),
            "mcp_scope": getattr(actor_user, "mcp_scope", None),
            "target_type": "operator_queue",
            "target_id": replaced["id"],
            "details": {
                "agent_name": agent_name,
                "request_id": replaced["request_id"],
                "replaced_by": row["id"],
                **_acted_by(actor_user),
            },
        }]
        ending_trigger = _broadcast_payload({"type": "operator_queue_cancelled",
                                             "data": {"id": replaced["id"], "agent_name": agent_name}})
        _ended(EndingEvent(CANCELLED, (replaced,), None, reason=REPLACED), ending_audit, ending_trigger)
    return _receipt(row, status="created", resolved=resolved,
                    supersedes_request_id=norm["supersedes_expired"],
                    replaces_request_id=target["request_id"] if target else None)


def _platform_turn(agent_name: str, execution_id: Optional[str]) -> Optional[str]:
    """The platform-supplied turn id iff it is this agent's own execution — never raises."""
    if not execution_id or execution_id == "manual":
        return None
    from services.idempotency_service import resolve_and_validate_execution
    try:
        return execution_id if resolve_and_validate_execution(execution_id, agent_name) is not None else None
    except Exception:  # noqa: BLE001 — provenance never fails the ask
        logger.warning("[AskService] turn lookup failed — ask stored without it", exc_info=True)
        return None


_CAP_MESSAGES = {
    "too_many_options": (
        "Too many options. Split independent decisions into separate asks, or drop "
        "variants — the person can always answer (something else); for an open choice "
        "among many, ask a question instead. Do not retry unchanged."),
    "option_too_long": (
        "An option is too long. Name the choice only; put the reasoning in question and "
        "what the option does in proposal. Do not retry unchanged."),
    "invalid_options": (
        f"An option reads as {SOMETHING_ELSE!r}, which the platform offers on every "
        "approval; do not list it or a lookalike as an option."),
    "title_too_long": (
        "The title is too long. Shorten it to one line a person reads at a glance and "
        "move the detail into question. Do not retry unchanged."),
}


def _refuse_over_caps(norm: Dict[str, Any], oqs, raised_by: str) -> None:
    """Step 2b of `raise_ask`: the #3243 authoring caps, one shared predicate
    with the queue-file ingest. The title limit applies to an agent's raise
    only — a gate's title is platform-authored."""
    hit = options_cap_violation(norm.get("options"),
                                max_options=oqs.OPERATOR_QUEUE_MAX_OPTIONS,
                                max_chars=oqs.OPERATOR_QUEUE_OPTION_MAX_CHARS)
    if hit is None and raised_by == "agent":
        hit = title_cap_violation(norm.get("title"),
                                  max_chars=oqs.OPERATOR_QUEUE_ASK_TITLE_MAX_CHARS)
    if hit is not None:
        code, extras = hit
        raise AskRejected(422, code, _CAP_MESSAGES[code], **extras)


def _too_large(field: str, limit: int, unit: str) -> AskRejected:
    return AskRejected(422, "field_too_large", f"{field} is over {limit} {unit}.",
                       field=field, limit=limit, unit=unit)


def _validated_ask(ask: Any, oqs, *, raised_by: str = "agent",
                   agent_name: str = "") -> Dict[str, Any]:
    """The static half of validation — nothing here depends on the clock or
    the database, so a replay is checked right after it.

    The id namespace depends on who raises: an agent may not use any
    platform-reserved prefix; a gate raise must use its own, `gate-`, so no
    agent can pre-create it and `is_platform_minted` keeps the row's endings
    from waking the agent."""
    from services import role_addressing

    if not isinstance(ask, Mapping):
        raise AskRejected(422, "invalid_ask", "The ask must be an object.")
    rid = ask.get("request_id")
    if (not isinstance(rid, str) or not 0 < len(rid) <= oqs.OPERATOR_QUEUE_ID_MAX
            or not oqs._ID_RE.match(rid)):
        raise AskRejected(
            422, "invalid_request_id",
            f"request_id must be 1-{oqs.OPERATOR_QUEUE_ID_MAX} characters of letters, "
            "digits, '.', '_', ':' or '-'.")
    lowered = rid.strip().lower()
    if raised_by == "gate":
        if not lowered.startswith(oqs.GATE_ASK_ID_PREFIX):
            raise AskRejected(
                422, "invalid_request_id",
                f"A gate raise's request_id must start with {oqs.GATE_ASK_ID_PREFIX!r}.")
    elif lowered.startswith(oqs.reserved_id_prefixes()):
        raise AskRejected(422, "reserved_request_id",
                          "That request_id prefix is reserved for the platform's own alerts and asks.")
    kind = ask.get("type") or "question"
    if kind not in ASK_TYPES:
        raise AskRejected(422, "invalid_type", "Unknown ask type.", allowed=list(ASK_TYPES))
    priority = ask.get("priority") or "medium"
    if priority not in oqs._VALID_PRIORITIES:
        raise AskRejected(422, "invalid_priority", "Unknown priority.",
                          allowed=sorted(oqs._VALID_PRIORITIES))
    title = ask.get("title")
    if not isinstance(title, str) or not title.strip():
        raise AskRejected(422, "invalid_title", "An ask needs a title.")
    if len(title) > oqs.OPERATOR_QUEUE_TITLE_MAX:
        if raised_by == "agent":
            # #3243: name the agent's limit on the FIRST refusal, so a title
            # over the outer 300 belt is not refused twice by two codes.
            raise AskRejected(
                422, "title_too_long", _CAP_MESSAGES["title_too_long"],
                limit=min(oqs.OPERATOR_QUEUE_TITLE_MAX, oqs.OPERATOR_QUEUE_ASK_TITLE_MAX_CHARS),
                length=len(title))
        raise _too_large("title", oqs.OPERATOR_QUEUE_TITLE_MAX, "characters")
    question = ask.get("question")
    if question is not None and not isinstance(question, str):
        raise AskRejected(422, "invalid_question", "question must be text.")
    if question and len(question) > oqs.OPERATOR_QUEUE_QUESTION_MAX:
        raise _too_large("question", oqs.OPERATOR_QUEUE_QUESTION_MAX, "characters")
    options = ask.get("options")
    if options is not None:
        if (not isinstance(options, list)
                or any(not isinstance(o, str) or not o.strip() for o in options)):
            raise AskRejected(422, "invalid_options", "options must be a list of non-empty strings.")
        if SOMETHING_ELSE in options:
            # #3242: the platform's reserved decision, offered on every approval.
            raise AskRejected(422, "invalid_options",
                              f"{SOMETHING_ELSE!r} is reserved by the platform and offered on "
                              "every approval; do not list it as an option.")
        if oqs._json_bytes(options) > oqs.OPERATOR_QUEUE_OPTIONS_MAX_BYTES:
            raise _too_large("options", oqs.OPERATOR_QUEUE_OPTIONS_MAX_BYTES, "bytes")
    if kind == "approval" and not options:
        raise AskRejected(422, "options_required",
                          "An approval needs the options a person can choose from.")
    if kind == "credential" and options:
        # trinity-enterprise#1066: the person types the value into the secret
        # field; there is nothing to choose, and an option list would read as
        # a place to put the value.
        raise AskRejected(422, "invalid_options",
                          "A credential ask offers no options; the person types the value "
                          "into the secret field it opens.")
    context = ask.get("context")
    if context is None:
        context = {}
    if not isinstance(context, Mapping):
        raise AskRejected(422, "invalid_context", "context must be an object.")
    if kind == "credential":
        from services import credential_ask_policy, credential_destination
        problem = credential_ask_policy.credential_name_problem(context.get("credential_name"))
        if problem is not None:
            raise AskRejected(
                422, "invalid_credential_name",
                "A credential ask needs context.credential_name: the environment variable "
                f"the person is filling — a variable name of at most "
                f"{credential_ask_policy.CREDENTIAL_NAME_MAX} characters that the platform "
                "and the agent runtime do not reserve.",
                reason=problem)
        if credential_destination.CONTEXT_KEY in context:
            # trinity-enterprise#1109: where the value lands when that is not
            # this agent — validated here, by name (shape, the outbound A2A
            # switch, the admin's endpoint registry for THIS agent; no
            # resolver call on the loop), and stored normalised.
            try:
                stored = credential_destination.validate(
                    context[credential_destination.CONTEXT_KEY], agent_name=agent_name)
            except credential_destination.DestinationProblem as exc:
                raise AskRejected(422, exc.code, exc.message, reason=exc.reason) from None
            context = {**context, credential_destination.CONTEXT_KEY: stored}
    elif isinstance(context, Mapping) and "credential_destination" in context:
        raise AskRejected(422, "destination_on_non_credential",
                          "Only a credential ask carries a destination.")
    # The workspace thread is platform-written: an agent that could author it
    # would choose which conversation its ask claims to belong to (ent#429), and
    # whether it is drawn in that chat at all (ent#734).
    context = {k: v for k, v in context.items() if k not in oqs._PLATFORM_CONTEXT_KEYS}
    context_bytes = oqs._json_bytes(context)
    if context_bytes is None:
        raise AskRejected(422, "invalid_context", "context must serialize as JSON.")
    if context_bytes > oqs.OPERATOR_QUEUE_CONTEXT_MAX_BYTES:
        raise _too_large("context", oqs.OPERATOR_QUEUE_CONTEXT_MAX_BYTES, "bytes")
    proposal = ask.get("proposal")
    if proposal is not None:
        if not isinstance(proposal, Mapping):
            raise AskRejected(422, "invalid_proposal", "proposal must be an object.")
        proposal_bytes = oqs._json_bytes(dict(proposal))
        if proposal_bytes is None:
            raise AskRejected(422, "invalid_proposal", "proposal must serialize as JSON.")
        if proposal_bytes > oqs.OPERATOR_QUEUE_PROPOSAL_MAX_BYTES:
            raise _too_large("proposal", oqs.OPERATOR_QUEUE_PROPOSAL_MAX_BYTES, "bytes")
    to = ask.get("to") or ("operator" if kind == "alert" else "primary")
    role_id = role_addressing.parse_role_id(to)
    if role_id is not None:
        # trinity-enterprise#1007: a canon role, stored canonical (lower-cased)
        # so a retry spelt `role:Head` replays against `role:head` with no
        # false `differs`. Resolved — and refused by name — in `_address`.
        to = f"{role_addressing.ROLE_ID_PREFIX}{role_id}"
    elif to not in ASK_ROLES and to not in role_addressing.ASK_TARGETS:
        raise AskRejected(422, "invalid_to", "Unknown role.",
                          allowed=[*ASK_ROLES, *role_addressing.ASK_TARGETS, ASK_ROLE_ID_FORM])
    if kind == "credential" and to != "primary":
        # trinity-enterprise#1066: the secret field writes into the agent's
        # environment, so it opens for the agent's primary person only — never
        # a viewer, an approver or a canon role.
        raise AskRejected(422, "invalid_to", "A credential ask is addressed to primary.",
                          allowed=["primary"])
    expires_at = ask.get("expires_at")
    _deadline(expires_at, floor=False)
    supersedes = ask.get("supersedes_expired")
    if supersedes is not None and (not isinstance(supersedes, str) or not supersedes
                                   or len(supersedes) > oqs.OPERATOR_QUEUE_ID_MAX
                                   or not oqs._ID_RE.match(supersedes)):
        raise AskRejected(422, "invalid_supersedes_expired",
                          "supersedes_expired must name one of your own asks that expired.")
    replaces = ask.get("replaces")
    if replaces is not None and (not isinstance(replaces, str) or not replaces
                                 or len(replaces) > oqs.OPERATOR_QUEUE_ID_MAX
                                 or not oqs._ID_RE.match(replaces) or replaces == rid):
        raise AskRejected(422, "invalid_replaces", _INVALID_REPLACES)
    return {
        "request_id": rid,
        "type": kind,
        "priority": priority,
        "title": title,
        "question": question or None,
        "options": list(options) if options else None,
        "context": context,
        "proposal": dict(proposal) if proposal else None,
        "to": to,
        "expires_at": expires_at,
        "supersedes_expired": supersedes,
        "replaces": replaces,
    }


def _refuse_key_state(norm: Mapping[str, Any], state: Optional[str]) -> None:
    """trinity-enterprise#1158 §2: the raise route's read of the asking
    agent's key, applied to a stored `key_env` destination only. The message
    names the field, never the variable or its value."""
    if state not in ("missing", "invalid") or norm.get("type") != "credential":
        return
    from services import credential_destination
    destination = (norm.get("context") or {}).get(credential_destination.CONTEXT_KEY)
    if not isinstance(destination, Mapping) or "key_env" not in destination:
        return
    if state == "missing":
        raise AskRejected(
            422, "destination_key_env_missing",
            "Your environment holds no value under the variable named in destination.key_env, "
            "so a person's value could not be delivered. Name the registered instance with "
            "destination.instance instead (the platform uses the key stored for it), or ask "
            "your owner to set that variable, then raise the ask again.")
    raise AskRejected(
        422, "destination_key_invalid",
        "The value under the variable named in destination.key_env cannot be sent as a key "
        "(it is blank or contains whitespace, control or non-ASCII characters), so a person's "
        "value could not be delivered. Name the registered instance with destination.instance "
        "instead (the platform uses the key stored for it), or ask your owner to fix that "
        "variable, then raise the ask again.")


def _deadline(value: Any, *, floor: bool) -> Optional[datetime]:
    """`expires_at` as an aware UTC datetime, or None. A time without a zone is
    refused, not guessed (Invariant #16: the deadline is compared as text)."""
    if value is None:
        return None
    bad = AskRejected(422, "invalid_expires_at",
                      "expires_at must be an ISO-8601 time with a timezone, e.g. 2026-10-01T09:00:00Z.")
    if not isinstance(value, str) or not value.strip():
        raise bad
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise AskRejected(422, "invalid_expires_at",
                              "expires_at needs a timezone (for example a trailing Z).")
        utc = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise bad
    if floor and utc < datetime.now(timezone.utc) + timedelta(minutes=MIN_DEADLINE_MINUTES):
        raise AskRejected(422, "invalid_expires_at",
                          f"expires_at must be at least {MIN_DEADLINE_MINUTES} minutes from now.",
                          min_minutes=MIN_DEADLINE_MINUTES)
    return utc


def _predecessor(agent_name: str, request_id: Optional[str],
                 raised_by: str = "agent") -> Optional[Dict[str, Any]]:
    """The agent's OWN expired ask a re-ask links, or None when none is named.
    It must have been raised by the same raiser: a gate approval is not a
    re-ask of the agent's own ask, nor the reverse. One refusal for every other
    case — missing, pending, answered, cancelled, another raiser's — so the rule
    reads the same whatever the reason."""
    if request_id is None:
        return None
    row = db.get_operator_queue_item_for_agent_by_request_id(agent_name, request_id)
    if (not row or "expired" not in (row.get("disposition"), row.get("status"))
            or _raiser_of(row) != raised_by):
        raise AskRejected(422, "invalid_supersedes_expired",
                          "supersedes_expired must name one of your own asks that expired.")
    return row


def _raiser_of(row: Dict[str, Any]) -> Optional[str]:
    """Who raised a row: the column when it is set. A row older than the column
    is the agent's own file ask, unless the platform minted it (a
    reserved-prefix alarm, raised by nobody an ask can be linked to)."""
    if row.get("raised_by"):
        return row["raised_by"]
    from services.operator_queue_service import is_platform_minted
    return None if is_platform_minted(row) else "agent"


def _replay(row: Dict[str, Any], norm: Dict[str, Any], oqs, raised_by: str) -> Dict[str, Any]:
    """The first ask's receipt for a retried `request_id`, never across raisers.
    A gate raise is not answered by a row the gate did not raise — a file row
    named `gate-…` written before the prefix was reserved would otherwise come
    back to the gate as `replayed`, holding the agent's proposal. (An agent
    cannot reach a gate row: its `gate-` ids are refused before the replay.)"""
    if raised_by == "gate" and row.get("raised_by") != "gate":
        raise AskRejected(409, "request_id_taken",
                          "That request_id already names an ask the gate did not raise.")
    return _receipt(row, status="replayed", differs=_differs(row, norm, oqs))


def _refuse_unlinked_reask(agent_name: str, proposal: Dict[str, Any], raised_by: str = "agent") -> None:
    """C6: repeating the exact action a timeout already denied needs the link,
    so the person sees it is being asked again ("denied by timeout; do not
    re-ask the same action without new information"). Compared with the SAME
    raiser's expired asks only: the agent's expired ask is not the gate's
    denial, nor the reverse."""
    wanted = _canon(proposal)
    for prior in db.list_expired_operator_queue_proposals(agent_name, _REASK_SCAN, raised_by=raised_by):
        if _canon(prior["proposal"]) == wanted:
            raise AskRejected(
                422, "reask_requires_link",
                "This repeats an action a timeout already denied. Say what is new, and set "
                "supersedes_expired to that ask's request_id.",
                expired_request_id=prior["request_id"])


_INVALID_REPLACES = "replaces must name one of your own pending asks."


def _replace_target(agent_name: str, request_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The agent's OWN pending ask a replace ends, or None when none is named
    (#3247). The security gate of the replace: the lookup is scoped to
    `(agent_name, request_id)` — another agent's row is not found — and the
    row must carry `raised_by == 'agent'` ON THE COLUMN. Never `_raiser_of`:
    it calls an unreserved NULL-raiser row "agent", and a row the platform
    files under the agent's name with a NULL raiser is only kept apart by its
    id prefix being on the reserved list (`skills-reconcile-` and
    `retention-guard-` joined it in #3246) — an agent must never end the
    alarm about its own leaked credential and stop it counting. One uniform
    refusal for missing / another's / a gate's / a platform's / a pre-#611 row.
    An own row that has ENDED is not refused here: it reaches the
    compare-and-set so one refusal names its real state (no read-then-act
    race)."""
    if request_id is None:
        return None
    row = db.get_operator_queue_item_for_agent_by_request_id(agent_name, request_id)
    if not row or row.get("raised_by") != "agent":
        raise AskRejected(422, "invalid_replaces", _INVALID_REPLACES)
    return row


def _refuse_ended_predecessor(pred: Optional[Dict[str, Any]]) -> None:
    """The predecessor ended before the replace could (#3247): one 409 that
    names its real state — ids and enums only, never a person's words."""
    if pred is None:
        raise AskRejected(422, "invalid_replaces", _INVALID_REPLACES)
    _refuse_ended(pred, code="replaces_ended", key="replaces")


def _refuse_ended(row: Dict[str, Any], *, code: str, key: str) -> None:
    """An own ask that ended before the agent's door could end it: the replace's
    `replaces_ended` (#3247, the row named as `replaces`) and the withdraw's
    `ask_ended` (trinity-enterprise#901, named as `request_id`) share one 409
    shape — ids and enums only, never a person's words."""
    disposition = row.get("disposition") or row.get("status")
    successor = request_id_of(row.get("replaced_by"))
    kind = agent_ending_kind(row)
    if disposition == ANSWERED:
        message = "That ask was answered first; the answer stands — read it with get_my_ask."
    elif disposition == EXPIRED:
        message = ("That ask was denied by timeout; re-ask with supersedes_expired only "
                   "with new information.")
    elif kind == REPLACED:
        message = f"That ask was already replaced by {successor}."
    elif kind == WITHDRAWN:
        message = "You already withdrew that ask; raise a new one if it is needed again."
    elif row.get("disposed_by") == "platform":
        message = "The platform ended that ask; do not re-raise it unchanged."
    elif row.get("disposed_by") == "person":
        message = "An operator cancelled that ask; do not re-raise it unchanged."
    else:
        message = "That ask was cancelled; do not re-raise it unchanged."
    # `disposed_by` is an enum (person | timeout | platform | agent) — who ended
    # it, never who they are; the message is branched on it so a platform
    # ending (#3246) is not called an operator's.
    raise AskRejected(409, code, message,
                      **{key: row.get("request_id")},
                      ask_status=row.get("status"),
                      disposition=row.get("disposition"),
                      disposed_by=row.get("disposed_by"),
                      disposed_at=row.get("disposed_at"),
                      replaced_by=successor)


_INVALID_REASON = "reason must be one line of text, 1 to 500 characters after trimming."


def withdraw(agent_name: str, request_id: str, reason: Any, *,
             actor_user: Any = None) -> Dict[str, Any]:
    """The agent ends one of its OWN pending asks with a reason and no successor
    (trinity-enterprise#901): `POST /api/agents/{name}/operator-queue/{request_id}/withdraw`.

    The row ends `cancelled` / `disposed_by='agent'` / `disposition_reason` =
    the reason, `replaced_by` NULL — through the compare-and-set the replace
    owns, so a person's answer always wins and nothing is ever written over an
    ending. The security gate is `_replace_target`'s: the lookup is scoped to
    `(agent_name, request_id)` and the row must carry `raised_by == 'agent'`
    ON THE COLUMN — a gate's approval (ent#751), a platform alarm about the
    agent, a NULL-raiser row, another agent's row and a row about a person are
    one uniform 404 `ask_not_found`. An own row that ended first is a 409
    `ask_ended` naming its real state; one this agent already withdrew is a
    200 `replayed` (an exact repeat is answered with the original; a different
    reason is reported in `differs`, never applied). The reason is
    whitespace-collapsed and bounded at ingest (422 `invalid_reason`), stored
    as text, and never written into the audit row. No wake: the agent holds
    the receipt (`_wake_filer` skips agent-authored endings).
    """
    from services.operator_queue_service import is_about_a_person

    text = " ".join(reason.split()) if isinstance(reason, str) else ""
    if not text or len(text) > MAX_WITHDRAW_REASON:
        raise AskRejected(422, "invalid_reason", _INVALID_REASON, limit=MAX_WITHDRAW_REASON)
    row = db.get_operator_queue_item_for_agent_by_request_id(agent_name, request_id)
    if not row or row.get("raised_by") != "agent" or is_about_a_person(row):
        raise AskRejected(404, "ask_not_found", "Ask not found.")
    out = db.withdraw_operator_queue_item(agent_name, row["id"], reason=text)
    row = out["row"]
    if out["outcome"] == "not_own" or row is None:
        # the compare-and-set's belt held where the gate above did not
        raise AskRejected(404, "ask_not_found", "Ask not found.")
    if out["outcome"] == "ended":
        if agent_ending_kind(row) == WITHDRAWN:
            differs = [] if row.get("disposition_reason") == text else ["reason"]
            return _receipt(row, status="replayed", differs=differs)
        _refuse_ended(row, code="ask_ended", key="request_id")
    if out["outcome"] != "withdrawn":
        # only the compare-and-set's winner reaches the sink; an outcome the db
        # layer grows later fails closed here instead of falling through to `_ended`
        raise RuntimeError(f"unexpected withdraw outcome {out['outcome']!r}")
    # Agent-keyed like the `replaced` row (#3247): there is no person `Actor`.
    # `has_reason` only — the §26.9 rule: ids and enums, never the text.
    audit = [{
        "event_action": "withdrawn",
        "source": "api",
        "actor_agent_name": agent_name,
        "actor_email": getattr(actor_user, "email", None),
        "mcp_key_id": getattr(actor_user, "mcp_key_id", None),
        "mcp_key_name": getattr(actor_user, "mcp_key_name", None),
        "mcp_scope": getattr(actor_user, "mcp_scope", None),
        "target_type": "operator_queue",
        "target_id": row["id"],
        "details": {
            "agent_name": agent_name,
            "request_id": row["request_id"],
            "has_reason": True,
            **_acted_by(actor_user),
        },
    }]
    trigger = _broadcast_payload({"type": "operator_queue_cancelled",
                                  "data": {"id": row["id"], "agent_name": agent_name}})
    _ended(EndingEvent(CANCELLED, (row,), None, reason=text), audit, trigger)
    return _receipt(row, status=WITHDRAWN)


def _named_addressee(addressee: Optional[str], raised_by: str) -> Optional[str]:
    """The lower-cased email a platform raise names, or None when none is named.

    A programming error (`ValueError`), never a refusal: an agent's raise that
    names anyone, or a named addressee that is not an email.
    """
    if addressee is None:
        return None
    if raised_by != "gate":
        raise ValueError("raise_ask: only a gate raise may name its addressee")
    email = str(addressee).strip().lower()
    if "@" not in email:
        raise ValueError("raise_ask: addressee must be an email")
    return email


def _named_addressees(addressees: Optional[List[str]], to_role_override: Optional[str],
                      raised_by: str, named: Optional[str],
                      platform_context: Optional[Mapping[str, Any]] = None) -> Optional[List[str]]:
    """The lower-cased, de-duplicated emails a gate raise pre-lists, or None
    when it lists none (trinity-enterprise#848).

    A programming error (`ValueError`), never a refusal: an agent's raise that
    passes either, an override without the list it stands for, an empty list
    (a gate never asks nobody — `enforce` refused `role_unassigned` first), a
    non-email, or a list beside a single `addressee`. The one empty list
    allowed (trinity-enterprise#1096, R1) is the decider order's third arm: a
    raise stamped with the `admin` decider seat, which an instance admin
    decides (`may_end`).
    """
    if addressees is None and to_role_override is None:
        return None
    if raised_by != "gate":
        raise ValueError("raise_ask: only a gate raise may pre-list its audience")
    if addressees is None:
        raise ValueError("raise_ask: to_role_override needs addressees")
    if named is not None:
        raise ValueError("raise_ask: addressee and addressees are exclusive")
    out: List[str] = []
    for value in addressees:
        email = str(value).strip().lower()
        if "@" not in email:
            raise ValueError("raise_ask: every addressee must be an email")
        if email not in out:
            out.append(email)
    if not out:
        from services import operator_queue_service as oqs
        if (platform_context or {}).get(oqs._WORKSPACE_DECIDER_SEAT_KEY) != ADMIN_SEAT:
            raise ValueError("raise_ask: a gate raise must address someone")
    return out


def _address(agent_name: str, role: str) -> Tuple[List[str], Optional[str], bool]:
    """`(resolved_to, addressed_to_email, resolved)` for a role.

    The one resolution rule of the addressing surfaces,
    `services/role_addressing.resolve_addressee` (ent#606, ent#1134) — shared
    with messages and the enterprise proposals so an ask and a message
    addressed to the same role reach the same people: a provider answering
    "nobody fills primary" stands the agent's OWNER in (the 2026-11-07
    ruling), an owner with no email is still the operators. Several people are recorded in
    `resolved_to` and none becomes the single Workspace addressee: the ONE ask
    is delivered to each of them on read (trinity-enterprise#816, the
    2026-10-07 ruling — the Workspace list and its doors match `resolved_to`
    membership), so `resolved` is true because a person's Inbox shows it. `role` is already validated
    by `_validated_ask` (the only caller passes its output): one of `ASK_ROLES`,
    whose one refusal left to map is an unfilled role — or a canonical canon
    role `role:<id>` (trinity-enterprise#1007), which
    `role_addressing.resolve_role_id_for_ask` resolves on this agent's OWN
    roster with ent#881 reachability applied; each of its arms
    (`role_unavailable`, `role_unassigned`, `role_cannot_open_agent`) maps 1:1
    to a 422 so the agent gets the remedy for the arm it hit, with the role's
    label and never a person in the text.

    The `approver` kind goes through `role_addressing.resolve_approver_for_ask`
    (#4501): its answer is fenced to the people recorded here
    (trinity-enterprise#1053), so only seated approvers who can open the agent
    are recorded, and none is `role_cannot_open_agent` — refused and logged,
    never filed for a reader who cannot see it. A message or a report to the
    approver keeps the plain resolution; `readdress_role` inherits this rule
    through `_fresh_audience`.
    """
    from services import role_addressing

    role_id = role_addressing.parse_role_id(role)
    if role_id is not None:
        try:
            r = role_addressing.resolve_role_id_for_ask(agent_name, role_id)
        except role_addressing.RoleRefused as e:
            raise AskRejected(422, e.code, e.message, role=e.role)
        return r.people, r.single, r.resolved
    try:
        r = (role_addressing.resolve_approver_for_ask(agent_name) if role == "approver"
             else role_addressing.resolve_addressee(agent_name, role, owner_lookup=_owner_email))
    except role_addressing.RoleRefused as e:
        if e.code == "role_cannot_open_agent":
            # A refusal files nothing, so the log is the only trace a stalled
            # agent leaves. The role and the agent, never a person.
            logger.warning("[AskService] an ask to the %s role from %s was refused: nobody "
                           "seated in it can open the agent", role, agent_name)
            raise AskRejected(422, e.code, e.message, role=e.role)
        raise AskRejected(422, "role_unassigned",
                          f"Nobody fills the {role} role for this agent yet; address the ask to "
                          "primary or operator.", role=role)
    return r.people, r.single, r.resolved


def _address_credential(agent_name: str) -> Tuple[List[str], Optional[str], bool]:
    """`(resolved_to, addressed_to_email, resolved)` for a `credential` ask
    (#4579): `role_addressing.resolve_credential_reader` — the agent's primary
    person when they can open the Workspace secret field, else its owner when
    they can, else refused `role_cannot_open_agent`. A credential ask is
    answered through that field alone (`answer` refuses anything else), so a
    reader who cannot open it would leave an ask nobody can fill. Nobody or
    several is returned as it is and refused `no_person` by the caller. A
    refusal files nothing, so it is logged: the agent, never a person."""
    from services import role_addressing

    try:
        r = role_addressing.resolve_credential_reader(agent_name, owner_lookup=_owner_email)
    except role_addressing.RoleRefused as e:
        logger.warning("[AskService] a credential ask from %s was refused: neither its primary "
                       "person nor its owner can open the agent in their Workspace", agent_name)
        raise AskRejected(422, e.code, e.message, role=e.role)
    return r.people, r.single, r.resolved


def _address_target(agent_name: str, target: str,
                    turn: Optional[str]) -> Tuple[List[str], Optional[str], bool, Optional[str],
                                                  Optional[str]]:
    """`(resolved_to, addressed_to_email, resolved, project_id, seat)` for an
    addressing target (trinity-enterprise#1134) — `project`, resolved by the
    PLATFORM from `turn` (the validated raising execution, or None) through
    the resolver the enterprise projects module registers
    (`role_addressing.resolve_target`). Every arm is a 422 by name:
    `project_unavailable` (no resolver, or it failed — an OSS-only install),
    and the resolver's own `not_in_project_turn` / `project_unreachable`, each
    with its remedy. Never a fallback to the owner or the operators: a project
    ask reaches the project's person or is not filed."""
    from services import role_addressing

    try:
        r = role_addressing.resolve_target(target, agent_name, turn)
    except role_addressing.RoleRefused as e:
        raise AskRejected(422, e.code, e.message, role=e.role)
    people = _people_list(r.people)
    single = (r.single or "").strip().lower() or None
    return people, single, bool(people), r.project_id, r.seat


def _in_turn_chat(agent_name: str, email: str, turn: Optional[str]) -> Optional[str]:
    """The Workspace chat `turn` serves iff it is (agent, email)'s own RUNNING
    chat turn (`client_portal.service.chat_for_execution`, ent#734) — else
    None, and no Main is resolved or created. Fail-soft like
    `_workspace_attachment`: a lookup that fails costs the reader a click,
    never the ask."""
    if not turn:
        return None
    try:
        from client_portal.service import chat_for_execution
        return chat_for_execution(agent_name, email, turn) or None
    except Exception:  # noqa: BLE001
        logger.warning("[AskService] could not resolve the chat of the turn that raised a "
                       "project ask from %s; stored without a chat", agent_name, exc_info=True)
        return None


def _turn_chat(agent_name: str, turn: str) -> Optional[str]:
    """The chat a RUNNING Workspace chat turn serves, whoever its person is
    (`client_portal.service.chat_of_turn`, trinity-enterprise#1134) — else
    None. Fail-soft: a lookup that fails costs the person the notice, never
    the ask."""
    try:
        from client_portal.service import chat_of_turn
        hit = chat_of_turn(agent_name, turn)
    except Exception:  # noqa: BLE001
        logger.warning("[AskService] could not resolve the chat of the turn that raised an "
                       "unaddressed ask from %s; stored without a chat", agent_name, exc_info=True)
        return None
    return hit[0] if hit else None


def _owner_email(agent_name: str) -> Optional[str]:
    """The agent owner's email (`role_addressing.owner_email`). Kept as this
    module's own name so the ask tests' patches keep a target that is read."""
    from services import role_addressing

    return role_addressing.owner_email(agent_name)


def _rate_allowed(agent_name: str, oqs) -> bool:
    check = oqs.rate_limiter.check
    return (
        check(f"operator_queue_create:{agent_name}",
              oqs.OPERATOR_QUEUE_CREATE_RATE_LIMIT, oqs.OPERATOR_QUEUE_CREATE_RATE_WINDOW).allowed
        and check("operator_queue_create:_fleet",
                  oqs.OPERATOR_QUEUE_FLEET_CREATE_RATE_LIMIT, oqs.OPERATOR_QUEUE_CREATE_RATE_WINDOW).allowed
    )


def _max_pending() -> int:
    from services import operator_queue_service as oqs
    return oqs.OPERATOR_QUEUE_MAX_PENDING_PER_AGENT


def _canon(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def request_id_of(item_id: Optional[str]) -> Optional[str]:
    """The request_id of the row `item_id` names: how an agent refers to its
    asks. A re-ask's `supersedes_expired` column stores the predecessor's uuid;
    the receipt and the readback both name it by request_id instead."""
    if not item_id:
        return None
    row = db.get_operator_queue_item(item_id)
    return row.get("request_id") if row else None


def _differs(row: Dict[str, Any], norm: Dict[str, Any], oqs) -> List[str]:
    """What a replayed call asked for that the FIRST ask does not carry — so an
    agent that retried with different content learns its change did not land.
    An ask-specific comparison: the file fingerprint's addressee arm would call
    every `primary` ask different (the owner is never on its own roster)."""
    stored_context = row.get("context") if isinstance(row.get("context"), dict) else {}
    stored_context = {k: v for k, v in stored_context.items() if k not in oqs._PLATFORM_CONTEXT_KEYS}
    expires = norm["expires_at"]
    pairs = {
        "title": (norm["title"], row.get("title")),
        "question": (norm["question"] or norm["title"], row.get("question")),
        "options": (_canon(norm["options"]), _canon(row.get("options"))),
        "type": (norm["type"], row.get("type")),
        "priority": (norm["priority"], row.get("priority")),
        "expires_at": (to_utc_iso(parse_iso_timestamp(expires)) if expires else None, row.get("expires_at")),
        "context": (_canon(norm["context"]), _canon(stored_context)),
        "to": (norm["to"], row.get("to_role")),
        "proposal": (_canon(norm["proposal"]), _canon(row.get("proposal"))),
        "supersedes_expired": (norm["supersedes_expired"], request_id_of(row.get("supersedes_expired"))),
        "replaces": (norm.get("replaces"), request_id_of(row.get("replaces"))),
    }
    return sorted(field for field, (asked, stored) in pairs.items() if asked != stored)


def _receipt(
    row: Dict[str, Any],
    *,
    status: str,
    resolved: Optional[bool] = None,
    differs: Optional[List[str]] = None,
    supersedes_request_id: Optional[str] = None,
    replaces_request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The receipt an agent keeps: the ask's state and how it ended if it did
    (a replay of an ended ask must not wait for a wake that already fired), the
    ROLE it went to — never a person's email — and whether an ending will wake
    the agent at all."""
    from services.operator_queue_service import is_platform_minted

    if resolved is None:
        resolved = row.get("to_role") == "operator" or bool(row.get("resolved_to"))
    if supersedes_request_id is None:
        supersedes_request_id = request_id_of(row.get("supersedes_expired"))
    if replaces_request_id is None:
        replaces_request_id = request_id_of(row.get("replaces"))
    receipt = {
        "status": status,
        "id": row["id"],
        "request_id": row["request_id"],
        "raised_by": row.get("raised_by"),
        "channel": row.get("channel"),
        "type": row.get("type"),
        "to_role": row.get("to_role"),
        "resolved": bool(resolved),
        "ask_status": row.get("status"),
        "disposition": row.get("disposition"),
        "disposed_at": row.get("disposed_at"),
        "expires_at": row.get("expires_at"),
        # `_wake_filer` skips a platform-minted row (a gate's), so its receipt
        # never promises a wake, whatever the owner opted in to.
        "wakes_on_ending": _opted_in(row["agent_name"]) and not is_platform_minted(row),
        "supersedes_expired": supersedes_request_id,
        # #3247: the replace link both ways, and who ended it — a replay of a
        # request that was itself replaced says so without a second read.
        "replaces": replaces_request_id,
        "replaced_by": request_id_of(row.get("replaced_by")),
        "disposed_by": row.get("disposed_by"),
        # abilityai/trinity-enterprise#1108 AC #4: `pending` = the answer landed
        # but the agent was not running ("agent not yet woken" — its next start
        # delivers it); `woken` = a turn carried it; None = no wake owed yet.
        "wake_state": row.get("wake_state"),
        # trinity-enterprise#901: which ending the agent authored, if any —
        # derived here, once, never inferred client-side from `replaced_by`.
        "agent_ending": agent_ending_kind(row),
        # trinity-enterprise#1109: where a credential lands when that is not
        # this agent — the agent and the host, never the key's variable name.
        "credential_destination": _destination_view(row),
    }
    if differs is not None:
        receipt["differs"] = differs
    return receipt


def _destination_view(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if row.get("type") != "credential":
        return None
    from services import credential_destination
    return credential_destination.public_view(
        credential_destination.destination_of(row.get("context")))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _broadcast_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The `/ws` payload a call will send, built where the call decides it.

    An identity function with a name on purpose: the ent#467 guard
    (`test_ent467_ws_agent_scope.py`) discovers every `/ws` payload by following
    a `*broadcast*(...)` call back to its dict literal inside ONE function, and
    `_announce` only ever sees a parameter. Built through this, each payload is
    a literal the guard reads — so a key that would leak (an email, the answer)
    or a trigger that names no agent is caught at CI, not assumed absent.
    """
    return payload


def _conflict_code(row: Dict[str, Any], *, address_compared: bool = False) -> str:
    """Why an answer's compare-and-set lost. Still `pending` means the deadline
    refused it (the respond predicate's only other clause) — or, when the
    ending also compared the address (trinity-enterprise#1096, R5) and the
    deadline has not passed, that a re-address moved the row: `readdressed`."""
    if row.get("status") != "pending":
        return "not_pending"
    if address_compared and not _past_deadline(row):
        return "readdressed"
    return "expired"


def _past_deadline(row: Mapping[str, Any]) -> bool:
    deadline = row.get("expires_at")
    if not deadline:
        return False
    try:
        parsed = parse_iso_timestamp(deadline) if isinstance(deadline, str) else deadline
    except Exception:  # noqa: BLE001 — unreadable reads as passed, the safe word
        return True
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= datetime.now(timezone.utc)


def _ending_expectation(row: Mapping[str, Any], actor: "Actor", *,
                        cancelling: bool = False) -> Optional[Dict[str, Any]]:
    """What a seat-fenced ending's compare-and-set must still find
    (trinity-enterprise#1096, R5): the stored address as the caller read it.
    None for a row whose ending does not depend on its address. Raises
    `AskConflict("readdressed")` when a re-address already moved it, and
    `AskNotFound` when it is gone. On the admin seat the stored context (where
    the seat stamp lives) is compared too."""
    if not _seat_fenced(row, cancelling=cancelling):
        return None
    raw = _address_unchanged(row)
    if raw is None:
        fresh = db.get_operator_queue_item(row["id"])
        if not fresh:
            raise AskNotFound(row["id"])
        if fresh.get("status") != "pending":
            raise AskConflict("not_pending", fresh)
        raise AskConflict("readdressed", fresh)
    expect = {"addressed_to_email": raw.get("addressed_to_email"),
              "resolved_to": raw.get("resolved_to")}
    if _admin_seated(row):
        expect["context"] = raw.get("context")
    return expect


def _acted_by(actor_user: Any) -> Dict[str, Any]:
    """`{"acted_by": "user:<id>"}` when an admin's user key acted on behalf of
    the agent (trinity-enterprise#1024), else nothing — so the agent's own rows
    keep exactly their shape, and an admin acting as an agent never reads as the
    agent. An id, never text (the audit-row rule above)."""
    from dependencies import act_as_agent_of

    if actor_user is not None and act_as_agent_of(actor_user):
        return {"acted_by": f"user:{actor_user.id}"}
    return {}


def _actor_fields(actor: Actor) -> Dict[str, Any]:
    return {
        "actor_user": actor.user,
        "actor_email": actor.email,
        "actor_ip": actor.ip,
        "endpoint": actor.endpoint,
    }


def _audit_row(action: str, row: Dict[str, Any], actor: Actor, extra: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event_action": action,
        "source": "api",
        **_actor_fields(actor),
        "target_type": "operator_queue",
        "target_id": row["id"],
        "details": {"agent_name": row["agent_name"], **extra},
    }


async def _announce(audit_rows: List[Dict[str, Any]], trigger: Optional[Dict[str, Any]]) -> None:
    """Best-effort: an ending that committed stands even when neither lands."""
    for row in audit_rows:
        try:
            await platform_audit_service.log(event_type=AuditEventType.OPERATOR_QUEUE, **row)
        except Exception:  # noqa: BLE001
            logger.warning("[AskService] audit %s skipped", row.get("event_action"), exc_info=True)
    if trigger and _websocket_manager:
        try:
            await _websocket_manager.broadcast(json.dumps(trigger))
        except Exception:  # noqa: BLE001
            logger.warning("[AskService] broadcast %s skipped", trigger.get("type"), exc_info=True)


def _ended(
    event: EndingEvent,
    audit_rows: List[Dict[str, Any]],
    trigger: Optional[Dict[str, Any]],
    *,
    batch_id: Optional[str] = None,
) -> Ending:
    """After a compare-and-set WON: announce, then tell the observers."""
    try:
        operator_resume_service.spawn_on_loop(lambda: _announce(audit_rows, trigger))
    except Exception:  # noqa: BLE001 — the ending is committed; it must stand
        logger.warning("[AskService] could not schedule the %s announcement", event.disposition,
                       exc_info=True)
    ok = True
    for observer in list(_observers):
        try:
            observer(event)
        except Exception:  # noqa: BLE001
            ok = False
            logger.exception("[AskService] ending observer %r failed on %s",
                             getattr(observer, "__name__", observer), event.disposition)
    return Ending(rows=list(event.rows), batch_id=batch_id, observers_ok=ok)


def _opted_in(agent_name: str) -> bool:
    """Does this agent wake when its asks end? On unless its owner switched it
    off (abilityai/trinity-enterprise#1108).

    Read ONCE per agent per event, before anything is scheduled, so an agent
    that has not opted in costs one flag read and nothing else (ent#329). The
    spawned work reads the flag again at the moment it would spend — that read
    is the authority; this one only avoids scheduling work that would decline.
    Unreadable ⇒ not opted in: never "spend" on a flag nobody could read.
    """
    try:
        return bool(wake_policy.resolve(agent_name).enabled)
    except Exception:  # noqa: BLE001
        logger.warning("[AskService] resume opt-in unreadable for %s; not waking it",
                       agent_name, exc_info=True)
        return False


def _wake_filer(event: EndingEvent) -> None:
    """The default observer — wake the agent that raised the ask (ent#329).

    An answer keeps its ent#329 resume, one per ask, framed with the answer. A
    cancel or a dismissal wakes the agent through `spawn_ending_dispatch`, one
    dispatch per agent per event; an expiry wakes nobody (ent#1108 ruling).
    Only agents whose owner has not switched the wake off are woken.
    A platform-minted row opened no loop for the agent to resume and carries text
    withheld from it by design (ent#499), so an answer to one never dispatches;
    the ending wake applies the same rule itself. An ending the agent authored
    (`disposed_by == 'agent'`, #3247) is skipped: it holds the receipt already.
    """
    opted: Dict[str, bool] = {}

    def _wakes(row: Dict[str, Any]) -> bool:
        agent = row.get("agent_name") or ""
        if agent and agent not in opted:
            opted[agent] = _opted_in(agent)
        return bool(agent) and opted[agent]

    if event.disposition == ANSWERED:
        from services.operator_queue_service import is_platform_minted

        for row in event.rows:
            if is_platform_minted(row) or not _wakes(row):
                continue
            # ent#1135 (T4): `owed` BEFORE the spawn, so a restart between this
            # ending and the turn still finds the wake owed and re-offers it.
            _mark_wake_owed(lambda row=row: [row])
            operator_resume_service.spawn_resume_dispatch(
                row,
                response=row.get("response"),
                response_text=row.get("response_text"),
                responded_by_email=event.actor_email,
            )
        return
    # abilityai/trinity-enterprise#1108 (merge-train ruling): an expiry wakes
    # nobody. The wake is on by default now, and the sweep spending an owner's
    # turn by default — for a timeout nobody chose — is out; the issue names
    # answers and cancellations. The expired row is still committed, announced
    # and read on the agent's next turn (the readback, the Execution Context
    # line, `get_my_ask`), and the rider reaches it there.
    if event.disposition == EXPIRED:
        return
    # #3247: an ending the agent authored itself (it replaced the ask) wakes
    # nobody — the agent holds the receipt in the same turn (ent#329's spend
    # rule), and `_framed_ending` would otherwise say an operator cancelled it.
    rows = [row for row in event.rows if row.get("disposed_by") != "agent" and _wakes(row)]
    if not rows:
        return
    _mark_wake_owed(lambda: [row for row in rows
                             if operator_resume_service._wakes_on_ending(row)])
    operator_resume_service.spawn_ending_dispatch(
        rows,
        disposition=event.disposition,
        disposed_by_email=event.actor_email,
        disposed_by_id=event.actor_user_id,
        reason=event.reason,
        batch_id=event.batch_id,
    )


def _mark_wake_owed(rows_of) -> None:
    """ent#1135 (T4): stamp `owed` before a spawn. A failed stamp is logged and
    never costs the wake: the spawn still runs and the dispatch writes its own
    `woken` / `pending` — only a restart in between would lose it."""
    try:
        operator_resume_service.mark_wake_owed(rows_of())
    except Exception:  # noqa: BLE001
        logger.warning("ask sink: wake_state=owed not recorded — the wake is still spawned, "
                       "but a restart before its turn would drop it", exc_info=True)


register_ending_observer(_wake_filer)
