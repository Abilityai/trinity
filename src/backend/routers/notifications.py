"""
Notifications API Router (NOTIF-001).

Enables agents to send structured notifications to the Trinity platform.
Notifications are persisted, broadcast via WebSocket, and queryable via API.
"""

import json
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query

from database import db
from dependencies import get_current_user, AuthorizedAgent
from services.agent_service import get_accessible_agents
from db_models import (
    User,
    NotificationCreate,
    Notification,
    NotificationList,
    NotificationAcknowledge
)


router = APIRouter(prefix="/api", tags=["notifications"])

# WebSocket manager for broadcasting notifications
_websocket_manager = None
_filtered_websocket_manager = None


def set_websocket_manager(manager):
    """Set the WebSocket manager for broadcasting notifications."""
    global _websocket_manager
    _websocket_manager = manager


def set_filtered_websocket_manager(manager):
    """Set the filtered WebSocket manager for Trinity Connect."""
    global _filtered_websocket_manager
    _filtered_websocket_manager = manager


async def _broadcast_notification(notification: Notification):
    """Broadcast a notification event via WebSocket."""
    event = {
        "type": "agent_notification",
        "notification_id": notification.id,
        "agent_name": notification.agent_name,
        "notification_type": notification.notification_type,
        "title": notification.title,
        "priority": notification.priority,
        "category": notification.category,
        "timestamp": notification.created_at
    }
    event_json = json.dumps(event)

    # Broadcast to main WebSocket (all UI clients)
    if _websocket_manager:
        await _websocket_manager.broadcast(event_json)

    # Broadcast to filtered WebSocket (Trinity Connect clients)
    if _filtered_websocket_manager:
        await _filtered_websocket_manager.broadcast_filtered(event)


# ============================================================================
# Notification Endpoints
# ============================================================================

@router.post("/notifications", response_model=Notification, status_code=201)
async def create_notification(
    data: NotificationCreate,
    current_user: User = Depends(get_current_user)
):
    """
    Create a new notification.

    This endpoint is called by agents via MCP to send notifications.
    The agent_name is extracted from the MCP auth context (agent-scoped key).

    For user-scoped keys, the notification is created on behalf of the user
    (useful for testing or manual notification creation).
    """
    # Validate notification_type
    valid_types = {"alert", "info", "status", "completion", "question"}
    if data.notification_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid notification_type. Must be one of: {', '.join(valid_types)}"
        )

    # Validate priority
    valid_priorities = {"low", "normal", "high", "urgent"}
    if data.priority not in valid_priorities:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid priority. Must be one of: {', '.join(valid_priorities)}"
        )

    # Validate title length
    if len(data.title) > 200:
        raise HTTPException(
            status_code=400,
            detail="Title too long (max 200 characters)"
        )

    # Get agent name from user context
    # For agent-scoped keys, current_user.agent_name is the agent sending the notification
    # For user-scoped keys, we fall back to username (for manual testing/admin use)
    agent_name = current_user.agent_name if current_user.agent_name else current_user.username

    notification = db.create_notification(agent_name, data)

    # Broadcast the notification
    await _broadcast_notification(notification)

    return notification


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    agent_name: Optional[str] = Query(None, description="Filter by agent name"),
    status: Optional[str] = Query(None, description="Filter by status: pending, acknowledged, dismissed"),
    priority: Optional[str] = Query(None, description="Filter by priority (comma-separated): low,normal,high,urgent"),
    limit: int = Query(50, ge=1, le=500, description="Maximum number of results"),
    current_user: User = Depends(get_current_user)
):
    """
    List notifications with optional filters.

    Returns notifications sorted by creation time (newest first).
    """
    # Validate status
    if status and status not in {"pending", "acknowledged", "dismissed"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid status. Must be: pending, acknowledged, or dismissed"
        )

    # Parse priority filter
    priority_list = None
    if priority:
        priority_list = [p.strip() for p in priority.split(",")]
        valid_priorities = {"low", "normal", "high", "urgent"}
        invalid = set(priority_list) - valid_priorities
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid priorities: {', '.join(invalid)}"
            )

    # Filter to notifications from agents the user can access
    accessible_agent_names = {a['name'] for a in get_accessible_agents(current_user)}

    # If filtering by specific agent, verify access
    if agent_name:
        if agent_name not in accessible_agent_names:
            raise HTTPException(status_code=403, detail="Access denied to agent")

    notifications = db.list_notifications(
        agent_name=agent_name,
        status=status,
        priority=priority_list,
        limit=limit * 2 if not agent_name else limit  # Fetch extra for filtering
    )

    # Filter to only accessible agents' notifications
    if not agent_name:
        notifications = [n for n in notifications if n.agent_name in accessible_agent_names][:limit]

    return NotificationList(
        count=len(notifications),
        notifications=notifications
    )


@router.get("/notifications/{notification_id}", response_model=Notification)
async def get_notification(
    notification_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get a specific notification by ID.
    """
    notification = db.get_notification(notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not db.can_user_access_agent(current_user.username, notification.agent_name):
        raise HTTPException(status_code=403, detail="Access denied")
    return notification


@router.post("/notifications/{notification_id}/acknowledge", response_model=NotificationAcknowledge)
async def acknowledge_notification(
    notification_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Acknowledge a pending notification.

    Changes the notification status from 'pending' to 'acknowledged'.
    """
    # Check the notification exists and user has access to the agent
    existing = db.get_notification(notification_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not db.can_user_access_agent(current_user.username, existing.agent_name):
        raise HTTPException(status_code=403, detail="Access denied")

    notification = db.acknowledge_notification(
        notification_id,
        acknowledged_by=str(current_user.id)
    )
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    return NotificationAcknowledge(
        id=notification.id,
        status=notification.status,
        acknowledged_at=notification.acknowledged_at or "",
        acknowledged_by=notification.acknowledged_by or ""
    )


@router.post("/notifications/{notification_id}/dismiss", response_model=NotificationAcknowledge)
async def dismiss_notification(
    notification_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Dismiss a notification.

    Changes the notification status to 'dismissed'.
    """
    # Check the notification exists and user has access to the agent
    existing = db.get_notification(notification_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not db.can_user_access_agent(current_user.username, existing.agent_name):
        raise HTTPException(status_code=403, detail="Access denied")

    notification = db.dismiss_notification(
        notification_id,
        dismissed_by=str(current_user.id)
    )
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    return NotificationAcknowledge(
        id=notification.id,
        status=notification.status,
        acknowledged_at=notification.acknowledged_at or "",
        acknowledged_by=notification.acknowledged_by or ""
    )


# ============================================================================
# Agent-Specific Notification Endpoints
# ============================================================================

@router.get("/agents/{name}/notifications", response_model=NotificationList)
async def get_agent_notifications(
    name: AuthorizedAgent,
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=500, description="Maximum number of results")
):
    """
    Get notifications for a specific agent.

    Requires authorization to access the agent.
    """
    # Validate status
    if status and status not in {"pending", "acknowledged", "dismissed"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid status. Must be: pending, acknowledged, or dismissed"
        )

    notifications = db.list_agent_notifications(
        agent_name=name,
        status=status,
        limit=limit
    )

    return NotificationList(
        count=len(notifications),
        notifications=notifications
    )


@router.get("/agents/{name}/notifications/count")
async def count_agent_notifications(
    name: AuthorizedAgent
):
    """
    Count pending notifications for an agent.
    """
    count = db.count_pending_notifications(agent_name=name)
    return {"agent_name": name, "pending_count": count}
