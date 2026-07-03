"""
Operator Queue API Router (OPS-001).

REST API for the Operating Room — lists queue items, submits responses,
and provides statistics. Items are synced from agent JSON files by the
operator_queue_service background poller.
"""

import json
from typing import Optional, Set
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from models import BulkCancelRequest, ClearResolvedRequest, OperatorResponse

from database import db
from dependencies import get_current_user
from db_models import User
from services.platform_audit_service import platform_audit_service, AuditEventType


router = APIRouter(prefix="/api/operator-queue", tags=["operator-queue"])

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
    return {"items": items, "count": len(items)}


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
    """
    accessible = _accessible_set(current_user)
    ids = list(dict.fromkeys(body.ids))  # dedupe, keep order — honest skipped count
    cancelled = db.bulk_cancel_operator_queue_items(ids, accessible)
    skipped = len(ids) - cancelled

    if cancelled > 0:
        await platform_audit_service.log(
            event_type=AuditEventType.OPERATOR_QUEUE,
            event_action="bulk_cancel",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="operator_queue",
            endpoint=str(request.url.path),
            details={"cancelled": cancelled, "skipped": skipped, "ids": body.ids},
        )
        if _websocket_manager:
            await _websocket_manager.broadcast(json.dumps({
                "type": "operator_queue_cleared",
                "data": {
                    "scope": "pending",
                    "count": cancelled,
                    "cleared_by": current_user.email or current_user.username,
                }
            }))

    return {"cancelled": cancelled, "skipped": skipped}


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
    return item


@router.post("/{item_id}/respond")
async def respond_to_queue_item(
    item_id: str,
    body: OperatorResponse,
    current_user: User = Depends(get_current_user),
):
    """Submit an operator response to a pending queue item."""
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

    item = db.respond_to_operator_queue_item(
        item_id=item_id,
        response=body.response,
        response_text=body.response_text,
        responded_by_id=str(current_user.id),
        responded_by_email=current_user.email or current_user.username,
    )

    # Lost the race: the item left 'pending' between the check above and the
    # UPDATE (e.g. a bulk-cancel landed). The response was NOT recorded —
    # surface that instead of a silent 200 (#1017).
    if item and item.pop("_status_conflict", False):
        raise HTTPException(
            status_code=409,
            detail=f"Item is no longer pending (now '{item['status']}') — response was not recorded"
        )

    # Broadcast WebSocket event
    if _websocket_manager and item:
        await _websocket_manager.broadcast(json.dumps({
            "type": "operator_queue_responded",
            "data": {
                "id": item_id,
                "agent_name": item["agent_name"],
                "responded_by_email": current_user.email or current_user.username,
                "response": body.response,
            }
        }))

    return item


@router.post("/{item_id}/cancel")
async def cancel_queue_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
):
    """Cancel a pending queue item."""
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

    item = db.cancel_operator_queue_item(item_id)
    return item


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
    return {"agent_name": agent_name, "items": items, "count": len(items)}
