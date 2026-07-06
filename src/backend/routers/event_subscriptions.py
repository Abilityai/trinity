"""
Agent Event Subscriptions Router (EVT-001).

Lightweight pub/sub for inter-agent pipelines.
Agents emit named events, other agents subscribe and receive
templated messages as async tasks when matching events fire.
"""

import asyncio
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from models import EmitEventRequest

from database import db
from dependencies import get_current_user, AuthorizedAgent, OwnedAgent
from db_models import (
    User,
    EventSubscriptionCreate,
    EventSubscriptionUpdate,
    EventSubscription,
    EventSubscriptionList,
    AgentEvent,
    AgentEventList,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["event-subscriptions"])

# WebSocket managers for broadcasting events
_websocket_manager = None
_filtered_websocket_manager = None


def set_websocket_manager(manager):
    """Set the WebSocket manager for broadcasting events."""
    global _websocket_manager
    _websocket_manager = manager


def set_filtered_websocket_manager(manager):
    """Set the filtered WebSocket manager for Trinity Connect."""
    global _filtered_websocket_manager
    _filtered_websocket_manager = manager


# ============================================================================
# Helpers
# ============================================================================

def _interpolate_template(template: str, payload: dict) -> str:
    """
    Replace {{payload.field}} placeholders with actual values.

    Supports nested access: {{payload.nested.field}}
    Missing fields are left as-is.
    """
    def replacer(match):
        path = match.group(1)  # e.g., "payload.pred_id"
        parts = path.split(".")
        # Skip the leading "payload" prefix
        if parts and parts[0] == "payload":
            parts = parts[1:]
        value = payload
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return match.group(0)  # Leave placeholder as-is
        return str(value)

    return re.sub(r"\{\{(payload(?:\.[a-zA-Z0-9_]+)+)\}\}", replacer, template)


async def _broadcast_event(event: AgentEvent, subscriptions_triggered: int):
    """Broadcast an agent event via WebSocket."""
    ws_event = {
        "type": "agent_event",
        "event_id": event.id,
        "source_agent": event.source_agent,
        "event_type": event.event_type,
        "subscriptions_triggered": subscriptions_triggered,
        "timestamp": event.created_at,
    }
    event_json = json.dumps(ws_event)

    if _websocket_manager:
        await _websocket_manager.broadcast(event_json)
    if _filtered_websocket_manager:
        await _filtered_websocket_manager.broadcast_filtered(ws_event)


async def _trigger_subscription(
    subscription: EventSubscription,
    event: AgentEvent,
):
    """
    Send an async task to the subscribing agent with the interpolated message.

    Uses the backend's internal task endpoint to avoid circular MCP calls.
    """
    import httpx

    # Interpolate payload into target message
    message = subscription.target_message
    if event.payload:
        message = _interpolate_template(message, event.payload)

    # Add event context to the message
    message = (
        f"[Event from {event.source_agent}: {event.event_type}]\n\n"
        f"{message}"
    )

    try:
        # Call the agent's task endpoint directly (async, fire-and-forget)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"http://localhost:8000/api/agents/{subscription.subscriber_agent}/task",
                json={
                    "message": message,
                    "async_mode": True,  # Fire-and-forget
                    "system_prompt": (
                        f"This task was triggered by an event subscription. "
                        f"Source agent: {event.source_agent}, "
                        f"Event type: {event.event_type}, "
                        f"Event ID: {event.id}"
                    ),
                },
                headers={
                    "Authorization": f"Bearer {_get_internal_token()}",
                    "X-Source-Agent": event.source_agent,
                    "X-Via-MCP": "true",
                },
            )
            if response.status_code >= 400:
                logger.warning(
                    f"[EVT-001] Failed to trigger subscription {subscription.id} "
                    f"on {subscription.subscriber_agent}: {response.status_code} {response.text[:200]}"
                )
            else:
                logger.info(
                    f"[EVT-001] Triggered subscription {subscription.id}: "
                    f"{event.source_agent}.{event.event_type} -> {subscription.subscriber_agent}"
                )
    except Exception as e:
        logger.error(
            f"[EVT-001] Error triggering subscription {subscription.id} "
            f"on {subscription.subscriber_agent}: {e}"
        )


def _get_internal_token() -> str:
    """Get a JWT token for internal API calls."""
    from jose import jwt
    from config import SECRET_KEY, ALGORITHM
    from datetime import datetime, timedelta

    payload = {
        "sub": "admin",
        "exp": datetime.utcnow() + timedelta(minutes=5),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# ============================================================================
# Subscription CRUD Endpoints
# ============================================================================

@router.post(
    "/agents/{name}/event-subscriptions",
    response_model=EventSubscription,
    status_code=201,
)
async def create_event_subscription(
    name: OwnedAgent,
    data: EventSubscriptionCreate,
    current_user: User = Depends(get_current_user),
):
    """
    Create an event subscription for an agent.

    The agent identified by {name} will receive tasks when the source_agent
    emits events matching event_type. The target_message template supports
    {{payload.field}} placeholders.

    Requires owner access to the subscribing agent.
    Permission check: the subscribing agent must have permission to call
    the source agent (via agent_permissions).
    """
    # Validate the source agent and the subscriber's permission to call it.
    # For a cross-agent subscription, collapse "source not found" (was a 400)
    # and "not permitted" (403) into a single uniform 403 so the body's
    # source_agent can't be used as an existence oracle (#186). Self-subscription
    # is always allowed — the subscribing agent {name} is owner-validated by the
    # OwnedAgent dependency, so it necessarily exists.
    if name != data.source_agent:
        source_exists = db.get_agent_owner(data.source_agent) is not None
        is_permitted = db.is_agent_permitted(name, data.source_agent)
        if not (source_exists and is_permitted):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Agent '{name}' does not have permission to communicate with "
                    f"'{data.source_agent}'. Grant permission in the Permissions tab first."
                ),
            )

    # Validate event_type format (namespaced: word.word or just word)
    if not re.match(r"^[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)*$", data.event_type):
        raise HTTPException(
            status_code=400,
            detail="event_type must be a dot-separated identifier (e.g., 'prediction.resolved')",
        )

    try:
        subscription = db.create_event_subscription(
            subscriber_agent=name,
            data=data,
            created_by=current_user.username,
        )
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"Subscription already exists for {name} -> {data.source_agent}:{data.event_type}",
            )
        raise

    logger.info(
        f"[EVT-001] Created subscription {subscription.id}: "
        f"{name} subscribes to {data.source_agent}.{data.event_type}"
    )
    return subscription


@router.get(
    "/agents/{name}/event-subscriptions",
    response_model=EventSubscriptionList,
)
async def list_event_subscriptions(
    name: AuthorizedAgent,
    direction: Optional[str] = Query(
        "subscriber",
        description="Filter direction: 'subscriber' (subscriptions this agent has), "
        "'source' (subscriptions others have to this agent's events), or 'both'",
    ),
    limit: int = Query(100, ge=1, le=500),
):
    """
    List event subscriptions for an agent.

    Use direction='subscriber' to see what this agent subscribes to.
    Use direction='source' to see who subscribes to this agent's events.
    """
    if direction == "source":
        subs = db.list_event_subscriptions(source_agent=name, limit=limit)
    elif direction == "both":
        subs_as_subscriber = db.list_event_subscriptions(subscriber_agent=name, limit=limit)
        subs_as_source = db.list_event_subscriptions(source_agent=name, limit=limit)
        # Deduplicate by ID
        seen = set()
        subs = []
        for s in subs_as_subscriber + subs_as_source:
            if s.id not in seen:
                seen.add(s.id)
                subs.append(s)
        subs = subs[:limit]
    else:
        subs = db.list_event_subscriptions(subscriber_agent=name, limit=limit)

    return EventSubscriptionList(count=len(subs), subscriptions=subs)


@router.get(
    "/event-subscriptions/{subscription_id}",
    response_model=EventSubscription,
)
async def get_event_subscription(
    subscription_id: str,
    current_user: User = Depends(get_current_user),
):
    """Get a specific event subscription by ID."""
    sub = db.get_event_subscription(subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Event subscription not found")
    return sub


@router.put(
    "/event-subscriptions/{subscription_id}",
    response_model=EventSubscription,
)
async def update_event_subscription(
    subscription_id: str,
    data: EventSubscriptionUpdate,
    current_user: User = Depends(get_current_user),
):
    """
    Update an event subscription.

    Can update event_type, target_message, and/or enabled status.
    """
    existing = db.get_event_subscription(subscription_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Event subscription not found")

    # Only owner or admin can update
    if not db.can_user_share_agent(current_user.username, existing.subscriber_agent):
        raise HTTPException(
            status_code=403,
            detail="Only the owner can modify event subscriptions",
        )

    if data.event_type and not re.match(r"^[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)*$", data.event_type):
        raise HTTPException(
            status_code=400,
            detail="event_type must be a dot-separated identifier",
        )

    updated = db.update_event_subscription(
        subscription_id,
        event_type=data.event_type,
        target_message=data.target_message,
        enabled=data.enabled,
    )
    return updated


@router.delete("/event-subscriptions/{subscription_id}")
async def delete_event_subscription(
    subscription_id: str,
    current_user: User = Depends(get_current_user),
):
    """Delete an event subscription."""
    existing = db.get_event_subscription(subscription_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Event subscription not found")

    # Only owner or admin can delete
    if not db.can_user_share_agent(current_user.username, existing.subscriber_agent):
        raise HTTPException(
            status_code=403,
            detail="Only the owner can delete event subscriptions",
        )

    db.delete_event_subscription(subscription_id)
    return {"status": "deleted", "subscription_id": subscription_id}


# ============================================================================
# Event Emission Endpoint
# ============================================================================


@router.post("/events", response_model=AgentEvent, status_code=201)
async def emit_event(
    data: EmitEventRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Emit an event from an agent.

    This endpoint is primarily called by agents via MCP (emit_event tool).
    The source agent is determined from the MCP auth context.

    When an event is emitted:
    1. The event is persisted to the database
    2. Matching subscriptions are found
    3. For each match, an async task is sent to the subscribing agent
    4. The event is broadcast via WebSocket
    """
    # Determine source agent from auth context
    source_agent = current_user.agent_name if current_user.agent_name else current_user.username

    # Validate event_type format
    if not re.match(r"^[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)*$", data.event_type):
        raise HTTPException(
            status_code=400,
            detail="event_type must be a dot-separated identifier (e.g., 'prediction.resolved')",
        )

    # Find matching subscriptions
    matching_subs = db.find_matching_event_subscriptions(source_agent, data.event_type)

    # Persist the event
    event = db.create_agent_event(
        source_agent=source_agent,
        event_type=data.event_type,
        payload=data.payload,
        subscriptions_triggered=len(matching_subs),
    )

    logger.info(
        f"[EVT-001] Event emitted: {source_agent}.{data.event_type} "
        f"({len(matching_subs)} subscriptions matched)"
    )

    # Trigger matching subscriptions (fire-and-forget)
    for sub in matching_subs:
        asyncio.create_task(_trigger_subscription(sub, event))

    # Broadcast event via WebSocket
    await _broadcast_event(event, len(matching_subs))

    return event


@router.post("/agents/{name}/emit-event", response_model=AgentEvent, status_code=201)
async def emit_event_for_agent(
    name: AuthorizedAgent,
    data: EmitEventRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Emit an event on behalf of a specific agent.

    Alternative to POST /api/events for cases where the source agent
    needs to be explicitly specified (e.g., admin triggering events).
    """
    # Validate event_type format
    if not re.match(r"^[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)*$", data.event_type):
        raise HTTPException(
            status_code=400,
            detail="event_type must be a dot-separated identifier",
        )

    matching_subs = db.find_matching_event_subscriptions(name, data.event_type)

    event = db.create_agent_event(
        source_agent=name,
        event_type=data.event_type,
        payload=data.payload,
        subscriptions_triggered=len(matching_subs),
    )

    logger.info(
        f"[EVT-001] Event emitted for {name}: {data.event_type} "
        f"({len(matching_subs)} subscriptions matched)"
    )

    for sub in matching_subs:
        asyncio.create_task(_trigger_subscription(sub, event))

    await _broadcast_event(event, len(matching_subs))

    return event


# ============================================================================
# Event History Endpoints
# ============================================================================

@router.get("/agents/{name}/events", response_model=AgentEventList)
async def list_agent_events(
    name: AuthorizedAgent,
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    limit: int = Query(50, ge=1, le=500),
):
    """List events emitted by a specific agent."""
    events = db.list_agent_events(source_agent=name, event_type=event_type, limit=limit)
    return AgentEventList(count=len(events), events=events)


@router.get("/events", response_model=AgentEventList)
async def list_all_events(
    source_agent: Optional[str] = Query(None, description="Filter by source agent"),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
):
    """List all events with optional filters."""
    events = db.list_agent_events(
        source_agent=source_agent,
        event_type=event_type,
        limit=limit,
    )
    return AgentEventList(count=len(events), events=events)
