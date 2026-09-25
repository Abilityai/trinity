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

What does NOT live here: who may end an ask (the person gate is
`dependencies.reject_non_person_principal`; the portal keeps its addressee
check), and the refusals each route words its own way (status already
terminal, divergence not acknowledged, an empty answer). The one check that
DOES live here is the #2376 rule that an answer must be one of the options the
agent offered: this is the only writer of an answer, so no entry point can
reach the approval channel without it.

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
from services import operator_resume_service
from services.operator_queue_choices import validate_response_choice
from services.platform_audit_service import AuditEventType, platform_audit_service
from utils.helpers import parse_iso_timestamp, to_utc_iso, utc_now_iso

logger = logging.getLogger(__name__)

ANSWERED = "answered"
CANCELLED = "cancelled"
EXPIRED = "expired"

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

    disposition: str                 # answered | cancelled | expired
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
    """
    if fn not in _observers:
        _observers.append(fn)
    return fn


# ---------------------------------------------------------------------------
# The four ways an ask ends
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

    Raises `ResponseNotOfferedError` (#2376) before anything is written, then
    `AskNotFound` / `AskConflict` when the compare-and-set did not land. The
    options are frozen at ingest, so validating against the caller's read is
    sound; the status is not, which is what the compare-and-set is for.
    """
    validate_response_choice(item, response)
    updated = db.respond_to_operator_queue_item(
        item_id=item["id"],
        response=response,
        response_text=response_text,
        responded_by_id=responded_by_id,
        responded_by_email=actor.email,
        divergence_acknowledged=divergence_acknowledged,
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


def cancel(item_id: str, *, actor: Actor, reason: Optional[str] = None) -> Ending:
    """A person cancelled one ask. Raises `AskNotFound` / `AskConflict`."""
    updated = db.cancel_operator_queue_item(item_id, disposed_by_email=actor.email, reason=reason)
    if not updated:
        raise AskNotFound(item_id)
    if updated.pop("_status_conflict", False):
        raise AskConflict("not_pending", updated)
    audit = [_audit_row("cancelled", updated, actor, {"has_reason": bool(reason)})]
    trigger = _broadcast_payload({"type": "operator_queue_cancelled",
                                  "data": {"id": updated["id"], "agent_name": updated["agent_name"]}})
    return _ended(EndingEvent(CANCELLED, (updated,), actor.email, reason=reason), audit, trigger)


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
    out = db.bulk_cancel_operator_queue_items(
        ids, accessible_agent_names, disposed_by_email=actor.email, reason=reason,
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
        "details": {"agent_name": r["agent_name"]},
    } for r in rows]
    return _ended(EndingEvent(EXPIRED, tuple(rows), None), audit, None)


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
) -> Dict[str, Any]:
    """Raise an ask through the platform and return its receipt
    (trinity-enterprise#611).

    The seam the agent's MCP tool calls today, and the one a gate calls later
    with `raised_by="gate"` and a `request_id` derived from the paused call. In
    order, so each refusal is honest about why:

    1. validate the shape — `AskRejected(422)` with a named code, never a
       silent truncation (that is the file path's compatibility behaviour);
    2. REPLAY: `(agent, request_id)` exists → the first ask's receipt,
       `status: "replayed"`, plus `differs` (what this call changed). Before
       every time- or state-dependent check, so a retry minutes later gets its
       receipt back rather than a refusal it did not earn the first time;
    3. the deadline floor (C5), the re-ask link (`supersedes_expired` must name
       the agent's own expired ask) and its guard (C6), then the role (`to:`)
       resolved to a person;
    4. the #1632 rate caps — the SAME buckets as the file poller, so the two
       channels share one budget;
    5. the create: replay, depth cap and insert in one per-agent serialized
       step (`queue_full` → 429);
    6. one audit row (`raised`, ids and enums only) and one thin broadcast.

    The receipt names the ROLE an ask went to, never the resolved email.
    """
    from services import operator_queue_service as oqs

    norm = _validated_ask(ask, oqs)
    existing = db.get_operator_queue_item_for_agent_by_request_id(agent_name, norm["request_id"])
    if existing:
        return _receipt(existing, status="replayed", differs=_differs(existing, norm, oqs))

    deadline = _deadline(norm["expires_at"], floor=True)
    predecessor = _predecessor(agent_name, norm["supersedes_expired"])
    if norm["proposal"] is not None and predecessor is None:
        _refuse_unlinked_reask(agent_name, norm["proposal"])
    people, addressee, resolved = _address(agent_name, norm["to"])

    if not _rate_allowed(agent_name, oqs):
        raise AskRejected(429, "rate_limited",
                          "Too many asks in a short time; try again in a minute.")

    context = dict(norm["context"])
    if addressee:
        # The addressee's Main chat (ent#429/#523), resolved at raise time. Only
        # after the caps passed: attaching may create the chat.
        thread = oqs._workspace_thread_for(agent_name, addressee)
        if thread:
            context[oqs._WORKSPACE_THREAD_KEY] = thread
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
        max_pending=_max_pending(),
        channel=channel,
        raised_by=raised_by,
        to_role=norm["to"],
        resolved_to=people or None,
        proposal=norm["proposal"],
        supersedes_expired=predecessor["id"] if predecessor else None,
    )
    if out["outcome"] == "queue_full":
        raise AskRejected(429, "queue_full",
                          "You already have the maximum number of open asks; wait for one to end.",
                          max_pending=_max_pending())
    row = out["row"]
    if out["outcome"] == "replayed":   # a concurrent call with the same id won
        return _receipt(row, status="replayed", differs=_differs(row, norm, oqs))

    audit = [{
        "event_action": "raised",
        "source": "api" if raised_by == "agent" else "system",
        "actor_agent_name": agent_name,
        "actor_user": actor_user,
        "target_type": "operator_queue",
        "target_id": row["id"],
        "details": {
            "agent_name": agent_name,
            "request_id": row["request_id"],
            "channel": channel,
            "raised_by": raised_by,
            "type": norm["type"],
            "to_role": norm["to"],
        },
    }]
    trigger = _broadcast_payload({"type": "operator_queue_new",
                                  "data": {"id": row["id"], "agent_name": agent_name}})
    try:
        operator_resume_service.spawn_on_loop(lambda: _announce(audit, trigger))
    except Exception:  # noqa: BLE001 — the ask is stored; it must stand
        logger.warning("[AskService] could not schedule the raised announcement", exc_info=True)
    return _receipt(row, status="created", resolved=resolved,
                    supersedes_request_id=norm["supersedes_expired"])


def _too_large(field: str, limit: int, unit: str) -> AskRejected:
    return AskRejected(422, "field_too_large", f"{field} is over {limit} {unit}.",
                       field=field, limit=limit, unit=unit)


def _validated_ask(ask: Any, oqs) -> Dict[str, Any]:
    """The static half of validation — nothing here depends on the clock or
    the database, so a replay is checked right after it."""
    if not isinstance(ask, Mapping):
        raise AskRejected(422, "invalid_ask", "The ask must be an object.")
    rid = ask.get("request_id")
    if (not isinstance(rid, str) or not 0 < len(rid) <= oqs.OPERATOR_QUEUE_ID_MAX
            or not oqs._ID_RE.match(rid)):
        raise AskRejected(
            422, "invalid_request_id",
            f"request_id must be 1-{oqs.OPERATOR_QUEUE_ID_MAX} characters of letters, "
            "digits, '.', '_', ':' or '-'.")
    if rid.strip().lower().startswith(oqs._RESERVED_ID_PREFIXES):
        raise AskRejected(422, "reserved_request_id",
                          "That request_id prefix is reserved for the platform's own alerts.")
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
    # would choose which conversation its ask claims to belong to (ent#429).
    context = {k: v for k, v in context.items() if k != oqs._WORKSPACE_THREAD_KEY}
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


def _predecessor(agent_name: str, request_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The agent's OWN expired ask a re-ask links, or None when none is named.
    One refusal for every other case — missing, pending, answered, cancelled —
    so the rule reads the same whatever the reason."""
    if request_id is None:
        return None
    row = db.get_operator_queue_item_for_agent_by_request_id(agent_name, request_id)
    if not row or "expired" not in (row.get("disposition"), row.get("status")):
        raise AskRejected(422, "invalid_supersedes_expired",
                          "supersedes_expired must name one of your own asks that expired.")
    return row


def _refuse_unlinked_reask(agent_name: str, proposal: Dict[str, Any]) -> None:
    """C6: repeating the exact action a timeout already denied needs the link,
    so the person sees it is being asked again ("denied by timeout; do not
    re-ask the same action without new information")."""
    wanted = _canon(proposal)
    for prior in db.list_expired_operator_queue_proposals(agent_name, _REASK_SCAN):
        if _canon(prior["proposal"]) == wanted:
            raise AskRejected(
                422, "reask_requires_link",
                "This repeats an action a timeout already denied. Say what is new, and set "
                "supersedes_expired to that ask's request_id.",
                expired_request_id=prior["request_id"])


def _address(agent_name: str, role: str) -> Tuple[List[str], Optional[str], bool]:
    """`(resolved_to, addressed_to_email, resolved)` for a role.

    A registered provider answers first (`assignment_provider.people_for`); with
    no answer the core defaults hold: `primary` → the agent's owner (their
    Workspace Main chat); `operator` → the operators, no person recorded;
    `approver` / `viewer` → refused until someone fills them. An owner with no
    email makes a `primary` ask an operator ask, and the receipt says so
    (`resolved: false`). Several people are recorded, but none becomes the
    single Workspace addressee.
    """
    from services import assignment_provider

    people = assignment_provider.resolve_role_people(agent_name, role)
    if people:
        return people, (people[0] if len(people) == 1 else None), True
    if role == "primary":
        owner = _owner_email(agent_name)
        return ([owner], owner, True) if owner else ([], None, False)
    if role == "operator":
        return [], None, True
    raise AskRejected(422, "role_unassigned",
                      f"Nobody fills the {role} role for this agent yet; address the ask to "
                      "primary or operator.", role=role)


def _owner_email(agent_name: str) -> Optional[str]:
    """The agent owner's email, or None when there is none (the default admin
    often has none). Unreadable ⇒ None: an operator ask, never a guess."""
    try:
        owner = db.get_agent_owner(agent_name)
        username = (owner or {}).get("owner_username")
        user = db.get_user_by_username(username) if username else None
        email = ((user or {}).get("email") or "").strip().lower()
    except Exception:  # noqa: BLE001
        logger.warning("[AskService] owner lookup failed for %s", agent_name, exc_info=True)
        return None
    return email if "@" in email else None


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


def _request_id_of(item_id: Optional[str]) -> Optional[str]:
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
    stored_context = {k: v for k, v in stored_context.items() if k != oqs._WORKSPACE_THREAD_KEY}
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
        "supersedes_expired": (norm["supersedes_expired"], _request_id_of(row.get("supersedes_expired"))),
    }
    return sorted(field for field, (asked, stored) in pairs.items() if asked != stored)


def _receipt(
    row: Dict[str, Any],
    *,
    status: str,
    resolved: Optional[bool] = None,
    differs: Optional[List[str]] = None,
    supersedes_request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The receipt an agent keeps: the ask's state and how it ended if it did
    (a replay of an ended ask must not wait for a wake that already fired), the
    ROLE it went to — never a person's email — and whether an ending will wake
    the agent at all."""
    if resolved is None:
        resolved = row.get("to_role") == "operator" or bool(row.get("resolved_to"))
    if supersedes_request_id is None:
        supersedes_request_id = _request_id_of(row.get("supersedes_expired"))
    receipt = {
        "status": status,
        "id": row["id"],
        "request_id": row["request_id"],
        "channel": row.get("channel"),
        "type": row.get("type"),
        "to_role": row.get("to_role"),
        "resolved": bool(resolved),
        "ask_status": row.get("status"),
        "disposition": row.get("disposition"),
        "disposed_at": row.get("disposed_at"),
        "expires_at": row.get("expires_at"),
        "wakes_on_ending": _opted_in(row["agent_name"]),
        "supersedes_expired": supersedes_request_id,
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
    the ending wake applies the same rule itself.
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
    rows = [row for row in event.rows if _wakes(row)]
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
