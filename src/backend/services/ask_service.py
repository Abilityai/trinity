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
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from database import db
from services import operator_resume_service
from services.operator_queue_choices import validate_response_choice
from services.platform_audit_service import AuditEventType, platform_audit_service

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
