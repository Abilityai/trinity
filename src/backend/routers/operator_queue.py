# mcp: operator_queue.ts (list_operator_queue, get_operator_queue_item, respond_to_operator_queue — a person-scoped key only; ask_operator → agent_router's raise; get_my_ask → agent_router's self-readback)
"""
Operator Queue API Router (OPS-001).

REST API for the Operating Room — lists queue items, submits responses,
and provides statistics. Items are synced from agent JSON files by the
operator_queue_service background poller.

Every way an ask ENDS — answer, cancel, bulk cancel — goes through
`services/ask_service.py` (trinity-enterprise#611); the routes here validate,
gate and map errors. Only a person ends an ask (`reject_non_person_principal`).
"""

import json
from typing import Any, Dict, List, Optional, Set
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from models import (
    BulkCancelRequest,
    ClearResolvedRequest,
    OperatorAskCreate,
    OperatorCancel,
    OperatorResponse,
)

from database import db
from dependencies import (
    get_current_user,
    get_self_acting_agent,
    is_person_principal,
    reject_non_person_principal,
)
from db_models import User
from services.platform_audit_service import platform_audit_service, AuditEventType
from services.operator_queue_choices import ResponseNotOfferedError
from services import ask_service, operator_queue_service


router = APIRouter(prefix="/api/operator-queue", tags=["operator-queue"])

# trinity-enterprise#611: the agent's own readback lives under the agent it
# belongs to (Invariant #15; the `routers/loops.py` two-router precedent).
agent_router = APIRouter(prefix="/api/agents", tags=["operator-queue"])

# WebSocket manager injected from main.py
_websocket_manager = None


def set_websocket_manager(manager):
    """Set the WebSocket manager for broadcasting events."""
    global _websocket_manager
    _websocket_manager = manager


# ============================================================================
# Access control helper
# ============================================================================

def _accessible_set(current_user: User) -> Optional[Set[str]]:
    """Return the set of agent names the user may access in the operator queue.

    Returns None for admins (no filter — sees everything).
    Returns a set (possibly empty) for regular users.
    """
    if current_user.role == "admin":
        return None
    user_email = current_user.email or ""
    names = db.get_accessible_agent_names(user_email, is_admin=False)
    return set(names)


def _assert_agent_accessible(agent_name: str, accessible: Optional[Set[str]]) -> None:
    """Raise 403 if the user cannot access the given agent."""
    if accessible is not None and agent_name not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")


# trinity-enterprise#611: the person fields that arrived with the ask object. A
# machine principal (an agent-, system- or ops-scoped key) reads the queue for its
# own work, never to learn which person ended an ask or whom it was resolved to.
# The PRE-existing person fields on the row (`responded_by_email`,
# `addressed_to_email`) still pass here — a registered residual, not a decision.
_PERSON_FIELDS_WITHHELD_FROM_MACHINES = ("disposed_by_email", "resolved_to")


def _for_principal(items: List[Dict[str, Any]], current_user: User) -> List[Dict[str, Any]]:
    if is_person_principal(current_user):
        return items
    for item in items:
        for key in _PERSON_FIELDS_WITHHELD_FROM_MACHINES:
            item.pop(key, None)
    return items


# The agent's own readback (trinity-enterprise#611). An ALLOWLIST, so a column
# added later stays out until someone decides an agent should read it. No person's
# email, no person refs, no platform sync internals; `to_role` is the role the ask
# was addressed to, never the person it resolved to.
_READBACK_FIELDS = (
    "id", "request_id", "agent_name", "type", "priority", "status",
    "title", "question", "options", "created_at", "expires_at",
    "response", "response_text", "responded_at",
    "disposition", "disposed_at", "disposed_by", "disposition_reason",
    "raised_by", "channel", "to_role", "proposal", "supersedes_expired",
)


def _actor(current_user: User, request: Request) -> ask_service.Actor:
    return ask_service.Actor(
        email=current_user.email or current_user.username,
        user=current_user,
        ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
    )


# ============================================================================
# Endpoints
# ============================================================================

@router.get("")
async def list_queue_items(
    status: Optional[str] = Query(None, description="Filter by status"),
    type: Optional[str] = Query(None, description="Filter by type"),
    priority: Optional[str] = Query(None, description="Filter by priority"),
    agent_name: Optional[str] = Query(None, description="Filter by agent"),
    since: Optional[str] = Query(None, description="Items created after this ISO timestamp"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """List operator queue items with optional filters."""
    accessible = _accessible_set(current_user)
    items = db.list_operator_queue_items(
        status=status,
        type=type,
        priority=priority,
        agent_name=agent_name,
        since=since,
        limit=limit,
        offset=offset,
        accessible_agent_names=accessible,
    )
    # #2915: aging is computed once, here, from the operator's bound — the
    # frontend renders `aging`/`aged_since`, it never recomputes them. The two
    # counts are the visible escalation (undelivered answers, items the agent
    # closed on its side) the Operations header shows instead of minting queue
    # items about queue items.
    items = _for_principal(operator_queue_service.annotate_aging(items), current_user)
    flags = db.count_operator_queue_flags(accessible_agent_names=accessible)
    return {
        "items": items,
        "count": len(items),
        "undelivered_count": flags["undelivered"],
        "closed_by_filer_count": flags["closed_by_filer"],
    }


@router.get("/stats")
async def get_queue_stats(
    current_user: User = Depends(get_current_user),
):
    """Get queue statistics (counts by status, type, priority, agent)."""
    accessible = _accessible_set(current_user)
    return db.get_operator_queue_stats(accessible_agent_names=accessible)


@router.post("/bulk-cancel")
async def bulk_cancel_queue_items(
    body: BulkCancelRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Cancel a list of pending queue items in one call (#1017).

    Only the listed ids are touched; non-pending or inaccessible ids are
    skipped (reported in `skipped`). Affects all operators of the agents.

    trinity-enterprise#611: a person only; the sweep's rows share one
    `batch_id` (returned), carry the optional `reason`, and are audited and
    woken as ONE event — the ids it actually cancelled, never the ids asked for.
    """
    reject_non_person_principal(current_user)
    accessible = _accessible_set(current_user)
    ids = list(dict.fromkeys(body.ids))  # dedupe, keep order — honest skipped count
    ending = ask_service.bulk_cancel(
        ids, accessible, actor=_actor(current_user, request), reason=body.reason,
    )
    cancelled = len(ending.rows)
    return {"cancelled": cancelled, "skipped": len(ids) - cancelled, "batch_id": ending.batch_id}


@router.post("/clear-resolved")
async def clear_resolved_queue_items(
    body: ClearResolvedRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Hide terminal (acknowledged/cancelled/expired) queue items (#1017).

    Sets cleared_at so the rows drop out of listings; actual deletion is
    deferred to the retention sweep (#1142) because a DELETE could be
    resurrected by the sync loop (see db layer docstring). 'responded'
    items awaiting agent acknowledgement are kept visible — their response
    still has to be delivered to the agent. Affects all operators of the
    agents. Idempotent: an empty match returns {"cleared": 0}.
    """
    accessible = _accessible_set(current_user)
    if body.agent_name:
        _assert_agent_accessible(body.agent_name, accessible)

    cleared = db.clear_resolved_operator_queue_items(
        agent_name=body.agent_name,
        accessible_agent_names=accessible,
    )

    if cleared > 0:
        await platform_audit_service.log(
            event_type=AuditEventType.OPERATOR_QUEUE,
            event_action="clear_resolved",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="operator_queue",
            target_id=body.agent_name,
            endpoint=str(request.url.path),
            details={"cleared": cleared, "agent_name": body.agent_name},
        )
        if _websocket_manager:
            await _websocket_manager.broadcast(json.dumps({
                "type": "operator_queue_cleared",
                "data": {
                    "scope": "resolved",
                    "count": cleared,
                    "cleared_by": current_user.email or current_user.username,
                }
            }))

    return {"cleared": cleared}


@router.get("/{item_id}")
async def get_queue_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
):
    """Get a single queue item by ID."""
    item = db.get_operator_queue_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")
    accessible = _accessible_set(current_user)
    _assert_agent_accessible(item["agent_name"], accessible)
    return _for_principal(operator_queue_service.annotate_aging([item]), current_user)[0]


@router.post("/{item_id}/respond")
async def respond_to_queue_item(
    item_id: str,
    body: OperatorResponse,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Submit an operator response to a pending queue item.

    trinity-enterprise#611: a person only — refused before the row is read, so an
    agent key learns nothing about which ids exist. The answer, its ledger, its
    audit row, its broadcast and the ent#329 resume all happen in the ask sink.
    """
    reject_non_person_principal(current_user)
    existing = db.get_operator_queue_item(item_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Queue item not found")

    accessible = _accessible_set(current_user)
    _assert_agent_accessible(existing["agent_name"], accessible)

    if existing["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot respond to item with status '{existing['status']}'"
        )

    # #2915: the agent rewrote or closed its own copy of this item after the
    # platform ingested it. The card the human read is the platform's frozen
    # snapshot; delivering an answer to it would hand the agent a decision
    # about a different question. Refused, named, until the human has SEEN the
    # divergence and answers anyway (`acknowledge_divergence`, set by the UI on
    # the second click). The portal answer path carries the same rule.
    if (
        existing.get("sync_state") in operator_queue_service.REFUSE_RESPONSE_STATES
        and not body.acknowledge_divergence
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "item_diverged",
                "message": "The agent changed this item after you opened it. Review it and send again.",
                "sync_state": existing.get("sync_state"),
                "sync_detail": existing.get("sync_detail"),
            },
        )

    try:
        # #2376: the decision has to be one the AGENT offered — checked by the
        # ask sink, the one writer of an answer (trinity-enterprise#611), before
        # anything is written.
        ending = ask_service.answer(
            existing,
            response=body.response,
            response_text=body.response_text,
            actor=_actor(current_user, request),
            responded_by_id=str(current_user.id),
            # #2989 review: the acknowledgement is recorded on the row so the
            # write-back delivers into the entry as it is now ("send again to
            # answer anyway").
            divergence_acknowledged=bool(
                body.acknowledge_divergence
                and existing.get("sync_state") in operator_queue_service.REFUSE_RESPONSE_STATES
            ),
        )
    except ResponseNotOfferedError as e:
        # Nothing checked this at any layer before #2376, so #2370 recorded
        # `"approved"` against `["Approve", "Deny"]` for five months with no 4xx.
        # Named 422 rather than a bare one: the options are agent-authored, so a
        # refusal that does not list them leaves the operator guessing.
        raise HTTPException(
            status_code=422,
            detail={
                "code": e.code,
                "message": str(e),
                "offered_options": e.options,
            },
        )
    except ask_service.AskNotFound:
        raise HTTPException(status_code=404, detail="Queue item not found")
    except ask_service.AskConflict as conflict:
        # The response was NOT recorded — surfaced instead of a silent 200
        # (#1017). `expired`: still pending but past its deadline (#611) — the
        # poller has not swept it yet, and an approval must not land after it.
        if conflict.code == "expired":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "expired",
                    "message": "This ask expired before it was answered — the response was not recorded.",
                },
            )
        raise HTTPException(
            status_code=409,
            detail=f"Item is no longer pending (now '{conflict.item['status']}') — response was not recorded"
        )
    return ending.rows[0]


@router.post("/{item_id}/cancel")
async def cancel_queue_item(
    item_id: str,
    request: Request,
    body: Optional[OperatorCancel] = None,
    current_user: User = Depends(get_current_user),
):
    """Cancel a pending queue item.

    trinity-enterprise#611: a person only; an optional `reason` (≤ 500 chars) is
    recorded on the row and framed as data in the agent's wake. Audited,
    broadcast and woken through the ask sink; a cancel that lost the race to an
    answer or an expiry is a 409, never a 200 over an ending that did not happen.
    """
    reject_non_person_principal(current_user)
    existing = db.get_operator_queue_item(item_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Queue item not found")

    accessible = _accessible_set(current_user)
    _assert_agent_accessible(existing["agent_name"], accessible)

    if existing["status"] != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel item with status '{existing['status']}'"
        )

    try:
        ending = ask_service.cancel(
            item_id, actor=_actor(current_user, request), reason=body.reason if body else None,
        )
    except ask_service.AskNotFound:
        raise HTTPException(status_code=404, detail="Queue item not found")
    except ask_service.AskConflict as conflict:
        raise HTTPException(
            status_code=409,
            detail=f"Item is no longer pending (now '{conflict.item['status']}') — it was not cancelled"
        )
    return ending.rows[0]


@router.get("/agents/{agent_name}")
async def get_agent_queue_items(
    agent_name: str,
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
):
    """Get queue items for a specific agent."""
    accessible = _accessible_set(current_user)
    _assert_agent_accessible(agent_name, accessible)
    items = db.list_operator_queue_items(
        agent_name=agent_name,
        status=status,
        limit=limit,
    )
    items = _for_principal(operator_queue_service.annotate_aging(items), current_user)
    return {"agent_name": agent_name, "items": items, "count": len(items)}


# ============================================================================
# The agent raises an ask, and reads it back (trinity-enterprise#611)
# ============================================================================

@agent_router.post("/{name}/operator-queue")
async def raise_my_ask(
    body: OperatorAskCreate,
    response: Response,
    name: str = Depends(get_self_acting_agent),
    current_user: User = Depends(get_current_user),
):
    """An agent asks a person for a decision — one call, validated here, stored,
    broadcast, and answered with a receipt. No file is written.

    The calling agent only, as itself (`get_self_acting_agent`): the record
    says an agent raised it. 201 with the receipt on a create; 200 with the
    FIRST ask's receipt (`status: "replayed"`) when this `request_id` was
    already raised, so a retry is a no-op. Refusals carry named codes — 422
    for a malformed ask, 429 for the caps.
    """
    try:
        receipt = ask_service.raise_ask(
            name,
            body.model_dump(exclude_none=True),
            raised_by="agent",
            channel="mcp",
            actor_user=current_user,
        )
    except ask_service.AskRejected as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"code": e.code, "message": e.message, **e.extra},
        )
    response.status_code = 201 if receipt["status"] == "created" else 200
    return receipt


@agent_router.get("/{name}/operator-queue/{request_id}")
async def get_my_ask(
    request_id: str,
    name: str = Depends(get_self_acting_agent),
):
    """How one of this agent's asks stands, by the `request_id` the agent chose.

    The agent's delivery channel for an ending: still readable after Clear All
    (which only hides a row from the operator's list), and the only way an agent
    that was stopped when its ask ended learns how it ended. The calling agent
    only, as itself (`get_self_acting_agent`); a redacted projection
    (`_READBACK_FIELDS`) that never carries a person's email.
    """
    item = db.get_operator_queue_item_for_agent_by_request_id(name, request_id)
    if not item:
        raise HTTPException(status_code=404, detail="Ask not found")
    return {key: item.get(key) for key in _READBACK_FIELDS}
