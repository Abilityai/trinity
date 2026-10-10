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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from database import db
from db.operator_queue import OUTCOME_UNKNOWN
from services import operator_resume_service
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
AGENT_ENDING_REASONS = (REPLACED,)

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


class AskNotAddressee(Exception):
    """trinity-enterprise#751: a gated-skill approval may be decided only by a
    person it was addressed to (an admin may cancel it, never approve it)."""

    def __init__(self, item_id: str):
        super().__init__("not_addressee")
        self.item_id = item_id


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


@dataclass(frozen=True)
class EndingEvent:
    """What an ending observer receives: the rows THIS call ended, never a row
    another writer ended first."""

    disposition: str                 # answered | cancelled | dismissed | expired
    rows: tuple                      # the CAS-won rows, as they stand after the transition
    actor_email: Optional[str]       # the person; None for timeout
    reason: Optional[str] = None     # the operator's cancel reason — DATA, never instructions
    batch_id: Optional[str] = None   # bulk cancel only


@dataclass
class Ending:
    """What the caller gets back."""

    rows: List[Dict[str, Any]]
    batch_id: Optional[str] = None
    # Every observer accepted the event without raising. The portal reports a
    # resume only when this holds (ent#430 AC #5: never claim work that was
    # not set in motion).
    observers_ok: bool = True


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


def may_end(row: Mapping[str, Any], actor: "Actor", *, cancelling: bool = False) -> bool:
    """Who may end an ask — the one rule every ending door shares
    (trinity-enterprise#751; the operator routes, the Workspace answer and a
    bulk sweep all reach it through this sink).

    Only a GATED-SKILL APPROVAL is narrowed: its decision runs a business action,
    so it belongs to the people the ask was addressed to (`resolved_to`). An
    admin may CANCEL one — the escalation — but never approve it. Every other
    ask keeps the rule it had: any person with access to the agent, checked by
    the caller. Person-only endings stay the routes' check
    (`dependencies.reject_non_person_principal`), since an agent key carries its
    owner's email."""
    if not (row.get("raised_by") == "gate" and row.get("type") == "approval"):
        return True
    email = (getattr(actor, "email", None) or "").strip().casefold()
    resolved = row.get("resolved_to") or []
    if isinstance(resolved, str):
        try:
            resolved = json.loads(resolved)
        except ValueError:
            resolved = []
    if email and email in {str(p).strip().casefold() for p in resolved}:
        return True
    return cancelling and getattr(getattr(actor, "user", None), "role", None) == "admin"


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
) -> Ending:
    """A person answered `item` (the row the caller read and checked).

    Raises `ResponseNotOfferedError` (#2376), or a `ReservedAnswerError` for the
    reserved `SOMETHING_ELSE` decision (#3242), before anything is written, then
    `AskNotFound` / `AskConflict` when the compare-and-set did not land. The
    options are frozen at ingest, so validating against the caller's read is
    sound; the status is not, which is what the compare-and-set is for.
    """
    # #3242: a platform-minted approval (a skill gate) counts only its own
    # options and no agent reads the instruction — refused, named.
    if response == SOMETHING_ELSE and decided_by_options(item):
        raise NotOffMenuError()
    validate_response_choice(item, response, response_text=response_text)
    if not may_end(item, actor):
        raise AskNotAddressee(item["id"])
    from services.operator_queue_service import is_platform_minted
    updated = db.respond_to_operator_queue_item(
        item_id=item["id"],
        response=response,
        response_text=response_text,
        responded_by_id=responded_by_id,
        responded_by_email=actor.email,
        divergence_acknowledged=divergence_acknowledged,
        # #2372: a platform alert has no agent audience — nobody will ever
        # acknowledge it from a file, so the person's answer is its last event.
        terminal=is_platform_minted(item),
    )
    if not updated:
        raise AskNotFound(item["id"])
    if updated.pop("_status_conflict", False):
        raise AskConflict(_conflict_code(updated), updated)
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
    `AskNotAddressee` (a gate approval the actor may not end, #751)."""
    current = db.get_operator_queue_item(item_id)
    if current and not may_end(current, actor, cancelling=True):
        raise AskNotAddressee(item_id)
    updated = db.cancel_operator_queue_item(item_id, disposed_by_email=actor.email, reason=reason)
    if not updated:
        raise AskNotFound(item_id)
    if updated.pop("_status_conflict", False):
        raise AskConflict("not_pending", updated)
    audit = [_audit_row("cancelled", updated, actor, {"has_reason": bool(reason)})]
    trigger = _broadcast_payload({"type": "operator_queue_cancelled",
                                  "data": {"id": updated["id"], "agent_name": updated["agent_name"]}})
    return _ended(EndingEvent(CANCELLED, (updated,), actor.email, reason=reason), audit, trigger)


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
    return _ended(EndingEvent(DISMISSED, (updated,), actor.email), audit, trigger)


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
    # like any other row this sweep may not touch.
    permitted = [i for i in ids
                 if may_end(db.get_operator_queue_item(i) or {}, actor, cancelling=True)]
    out = db.bulk_cancel_operator_queue_items(
        permitted, accessible_agent_names, disposed_by_email=actor.email, reason=reason,
    )
    rows, batch_id = out["rows"], out["batch_id"]
    if not rows:
        return Ending(rows=[], batch_id=None)
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
        },
    }]
    # Fleet-level: a sweep can span agents, so the trigger names none and
    # carries only a count; listeners refetch the access-controlled list.
    trigger = _broadcast_payload({"type": "operator_queue_cleared",
                                  "data": {"scope": "pending", "count": len(rows)}})
    return _ended(
        EndingEvent(CANCELLED, tuple(rows), actor.email, reason=reason, batch_id=batch_id),
        audit, trigger, batch_id=batch_id,
    )


def expire() -> Ending:
    """The clock ended every pending ask past its deadline.

    Sends NO trigger of its own: its one caller is the poll cycle, which sends
    ONE thin trigger per cycle (#2915) and folds the expiry into it.
    """
    rows = db.mark_operator_queue_expired()
    if not rows:
        return Ending(rows=[])
    audit = [{
        "event_action": "expired",
        "source": "system",
        "target_type": "operator_queue",
        "target_id": r["id"],
        # trinity-enterprise#844: an approval whose action the platform did not
        # hold expired without telling anyone whether that action went ahead.
        # A boolean, never the reason text (no person's words in an audit row).
        "details": {"agent_name": r["agent_name"],
                    **({"outcome_unknown": True}
                       if r.get("disposition_reason") == OUTCOME_UNKNOWN else {})},
    } for r in rows]
    return _ended(EndingEvent(EXPIRED, tuple(rows), None), audit, None)


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


ASK_TYPES = ("approval", "question", "alert")
ASK_ROLES = ("primary", "approver", "viewer", "operator")
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


def raise_ask(
    agent_name: str,
    ask: Mapping[str, Any],
    *,
    raised_by: str,
    channel: str,
    actor_user: Any = None,
    addressee: Optional[str] = None,
    platform_execution_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Raise an ask through the platform and return its receipt
    (trinity-enterprise#611).

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

    if raised_by not in ASK_RAISERS:
        raise ValueError(f"raise_ask: unknown raised_by {raised_by!r}")
    if channel not in ASK_CHANNELS:
        raise ValueError(f"raise_ask: unknown channel {channel!r}")
    if (raised_by == "gate") != (channel == "gate"):
        raise ValueError(f"raise_ask: channel {channel!r} does not go with raised_by {raised_by!r}")
    named = _named_addressee(addressee, raised_by)
    norm = _validated_ask(ask, oqs, raised_by=raised_by)
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
    if named:
        people, addressee, resolved = [named], named, True
    else:
        people, addressee, resolved = _address(agent_name, norm["to"])

    context = dict(norm["context"])
    turn = _platform_turn(agent_name, platform_execution_id)
    if turn:
        context["execution_id"] = turn
    if addressee:
        # The chat the raising turn serves (ent#734), else the addressee's Main
        # (ent#429/#523), resolved at raise time. Only after the caps passed:
        # attaching may create Main. Only an AGENT's raise reads the turn: a
        # gate's ask is a background ask, and belongs to the Inbox only
        # (the ent#610 amendment of 2026-09-30).
        thread, in_turn = oqs._workspace_attachment(
            agent_name, addressee, execution_id=turn if raised_by == "agent" else None)
        if thread:
            context[oqs._WORKSPACE_THREAD_KEY] = thread
        if thread and in_turn:
            context[oqs._WORKSPACE_TURN_KEY] = True
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
        to_role=norm["to"],
        resolved_to=people or None,
        proposal=norm["proposal"],
        supersedes_expired=predecessor["id"] if predecessor else None,
        # #3130: the cap counts the agent's own asks, never the platform's rows
        # about it (its flood alarm above all).
        exclude_request_id_prefixes=oqs._RESERVED_ID_PREFIXES,
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
            _ended(EndingEvent(EXPIRED, (pred,), None), [{
                "event_action": "expired",
                "source": "system",
                "target_type": "operator_queue",
                "target_id": pred["id"],
                "details": {"agent_name": pred["agent_name"]},
            }], _broadcast_payload({"type": "operator_queue_cancelled",
                                    "data": {"id": pred["id"], "agent_name": pred["agent_name"]}}))
        _refuse_ended_predecessor(out["predecessor"])
    row = out["row"]
    if out["outcome"] == "replayed":   # a concurrent call with the same id won
        return _replay(row, norm, oqs, raised_by)
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
            "to_role": norm["to"],
            **({"replaces": replaced["id"]} if replaced else {}),
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


def _validated_ask(ask: Any, oqs, *, raised_by: str = "agent") -> Dict[str, Any]:
    """The static half of validation — nothing here depends on the clock or
    the database, so a replay is checked right after it.

    The id namespace depends on who raises: an agent may not use any
    platform-reserved prefix; a gate raise must use its own, `gate-`, so no
    agent can pre-create it and `is_platform_minted` keeps the row's endings
    from waking the agent."""
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
    elif lowered.startswith(oqs._RESERVED_ID_PREFIXES):
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
    context = ask.get("context")
    if context is None:
        context = {}
    if not isinstance(context, Mapping):
        raise AskRejected(422, "invalid_context", "context must be an object.")
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
    if to not in ASK_ROLES:
        raise AskRejected(422, "invalid_to", "Unknown role.", allowed=list(ASK_ROLES))
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
    disposition = pred.get("disposition") or pred.get("status")
    successor = request_id_of(pred.get("replaced_by"))
    if disposition == ANSWERED:
        message = "That ask was answered first; the answer stands — read it with get_my_ask."
    elif disposition == EXPIRED:
        message = ("That ask was denied by timeout; re-ask with supersedes_expired only "
                   "with new information.")
    elif pred.get("disposed_by") == "agent" and successor:
        message = f"That ask was already replaced by {successor}."
    elif pred.get("disposed_by") == "platform":
        message = "The platform ended that ask; do not re-raise it unchanged."
    elif pred.get("disposed_by") == "person":
        message = "An operator cancelled that ask; do not re-raise it unchanged."
    else:
        message = "That ask was cancelled; do not re-raise it unchanged."
    # `disposed_by` is an enum (person | timeout | platform | agent) — who ended
    # it, never who they are; the message is branched on it so a platform
    # ending (#3246) is not called an operator's.
    raise AskRejected(409, "replaces_ended", message,
                      replaces=pred.get("request_id"),
                      ask_status=pred.get("status"),
                      disposition=pred.get("disposition"),
                      disposed_by=pred.get("disposed_by"),
                      disposed_at=pred.get("disposed_at"),
                      replaced_by=successor)


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


def _address(agent_name: str, role: str) -> Tuple[List[str], Optional[str], bool]:
    """`(resolved_to, addressed_to_email, resolved)` for a role.

    The one resolution rule, `services/role_addressing.resolve` (ent#606) —
    shared with reports and messages so an ask and a report addressed to the
    same role reach the same people. Several people are recorded, but none
    becomes the single Workspace addressee. `role` is already validated
    against `ASK_ROLES` by `_validated_ask` (the only caller passes its output),
    so the one refusal left to map is an unfilled role.
    """
    from services import role_addressing

    try:
        r = role_addressing.resolve(agent_name, role, owner_lookup=_owner_email)
    except role_addressing.RoleRefused:
        raise AskRejected(422, "role_unassigned",
                          f"Nobody fills the {role} role for this agent yet; address the ask to "
                          "primary or operator.", role=role)
    return r.people, r.single, r.resolved


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
    }
    if differs is not None:
        receipt["differs"] = differs
    return receipt


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


def _conflict_code(row: Dict[str, Any]) -> str:
    """Why an answer's compare-and-set lost. Still `pending` means the deadline
    refused it (the respond predicate's only other clause)."""
    return "expired" if row.get("status") == "pending" else "not_pending"


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
    """Has this agent's owner opted in to being woken by its asks' endings?

    Read ONCE per agent per event, before anything is scheduled, so an agent
    that has not opted in costs one flag read and nothing else (ent#329). The
    spawned work reads the flag again at the moment it would spend — that read
    is the authority; this one only avoids scheduling work that would decline.
    Unreadable ⇒ not opted in: never "spend" on a flag nobody could read.
    """
    try:
        return bool(db.get_operator_resume_enabled(agent_name))
    except Exception:  # noqa: BLE001
        logger.warning("[AskService] resume opt-in unreadable for %s; not waking it",
                       agent_name, exc_info=True)
        return False


def _wake_filer(event: EndingEvent) -> None:
    """The default observer — wake the agent that raised the ask (ent#329).

    An answer keeps its ent#329 resume, one per ask, framed with the answer. A
    cancel or an expiry wakes the agent through `spawn_ending_dispatch`, one
    dispatch per agent per event. Only agents whose owner opted in are woken.
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
            operator_resume_service.spawn_resume_dispatch(
                row,
                response=row.get("response"),
                response_text=row.get("response_text"),
                responded_by_email=event.actor_email,
            )
        return
    # #3247: an ending the agent authored itself (it replaced the ask) wakes
    # nobody — the agent holds the receipt in the same turn (ent#329's spend
    # rule), and `_framed_ending` would otherwise say an operator cancelled it.
    rows = [row for row in event.rows if row.get("disposed_by") != "agent" and _wakes(row)]
    if not rows:
        return
    operator_resume_service.spawn_ending_dispatch(
        rows,
        disposition=event.disposition,
        disposed_by_email=event.actor_email,
        reason=event.reason,
        batch_id=event.batch_id,
    )


register_ending_observer(_wake_filer)
