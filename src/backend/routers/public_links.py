"""
Owner endpoints for managing public agent links (Phase 12.2).

These endpoints require authentication and are used by agent owners
to create, manage, and revoke public shareable links.
"""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import List

from models import User
from database import db, PublicLinkCreate, PublicLinkUpdate, PublicLinkWithUrl
from dependencies import get_current_user
from services.audit_service import log_audit_event
from services.docker_service import get_agent_container
from config import FRONTEND_URL

router = APIRouter(prefix="/api/agents", tags=["public-links"])

# WebSocket manager will be injected from main.py
manager = None


def set_websocket_manager(ws_manager):
    """Set the WebSocket manager for broadcasting events."""
    global manager
    manager = ws_manager


def _build_public_url(token: str) -> str:
    """Build the public URL for a link token."""
    return f"{FRONTEND_URL}/chat/{token}"


def _link_to_response(link: dict, include_usage: bool = True) -> PublicLinkWithUrl:
    """Convert a database link dict to response model with URL."""
    usage_stats = None
    if include_usage:
        usage_stats = db.get_public_link_usage_stats(link["id"])

    return PublicLinkWithUrl(
        id=link["id"],
        agent_name=link["agent_name"],
        token=link["token"],
        created_by=link["created_by"],
        created_at=datetime.fromisoformat(link["created_at"]) if isinstance(link["created_at"], str) else link["created_at"],
        expires_at=datetime.fromisoformat(link["expires_at"]) if link.get("expires_at") else None,
        enabled=link["enabled"],
        name=link.get("name"),
        require_email=link["require_email"],
        url=_build_public_url(link["token"]),
        usage_stats=usage_stats
    )


@router.post("/{agent_name}/public-links", response_model=PublicLinkWithUrl)
async def create_public_link(
    agent_name: str,
    link_request: PublicLinkCreate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Create a new public shareable link for an agent."""
    # Verify agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify user owns the agent (only owners can create public links)
    if not db.can_user_share_agent(current_user.username, agent_name):
        await log_audit_event(
            event_type="public_link",
            action="create",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="unauthorized",
            severity="warning"
        )
        raise HTTPException(
            status_code=403,
            detail="Only the agent owner can create public links"
        )

    # Create the public link
    link = db.create_public_link(
        agent_name=agent_name,
        created_by=current_user.username,
        name=link_request.name,
        require_email=link_request.require_email,
        expires_at=link_request.expires_at
    )

    await log_audit_event(
        event_type="public_link",
        action="create",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={
            "link_id": link["id"],
            "require_email": link["require_email"],
            "expires_at": link.get("expires_at")
        }
    )

    # Broadcast event
    if manager:
        await manager.broadcast(json.dumps({
            "event": "public_link_created",
            "data": {
                "agent_name": agent_name,
                "link_id": link["id"],
                "require_email": link["require_email"]
            }
        }))

    return _link_to_response(link, include_usage=False)


@router.get("/{agent_name}/public-links", response_model=List[PublicLinkWithUrl])
async def list_public_links(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """List all public links for an agent."""
    # Verify agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify user can access the agent
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(
            status_code=403,
            detail="You don't have access to this agent"
        )

    # Only owner can see public links
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(
            status_code=403,
            detail="Only the agent owner can view public links"
        )

    links = db.list_agent_public_links(agent_name)
    return [_link_to_response(link) for link in links]


@router.get("/{agent_name}/public-links/{link_id}", response_model=PublicLinkWithUrl)
async def get_public_link(
    agent_name: str,
    link_id: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get details of a specific public link."""
    # Verify agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify ownership
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(
            status_code=403,
            detail="Only the agent owner can view public links"
        )

    link = db.get_public_link(link_id)
    if not link or link["agent_name"] != agent_name:
        raise HTTPException(status_code=404, detail="Public link not found")

    return _link_to_response(link)


@router.put("/{agent_name}/public-links/{link_id}", response_model=PublicLinkWithUrl)
async def update_public_link(
    agent_name: str,
    link_id: str,
    update_request: PublicLinkUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Update a public link (enable/disable, expiry, etc.)."""
    # Verify agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify ownership
    if not db.can_user_share_agent(current_user.username, agent_name):
        await log_audit_event(
            event_type="public_link",
            action="update",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="unauthorized",
            severity="warning"
        )
        raise HTTPException(
            status_code=403,
            detail="Only the agent owner can update public links"
        )

    # Verify link exists and belongs to this agent
    existing_link = db.get_public_link(link_id)
    if not existing_link or existing_link["agent_name"] != agent_name:
        raise HTTPException(status_code=404, detail="Public link not found")

    # Update the link
    updated_link = db.update_public_link(
        link_id=link_id,
        name=update_request.name,
        enabled=update_request.enabled,
        require_email=update_request.require_email,
        expires_at=update_request.expires_at
    )

    await log_audit_event(
        event_type="public_link",
        action="update",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={
            "link_id": link_id,
            "updates": update_request.model_dump(exclude_none=True)
        }
    )

    # Broadcast event
    if manager:
        await manager.broadcast(json.dumps({
            "event": "public_link_updated",
            "data": {
                "agent_name": agent_name,
                "link_id": link_id,
                "enabled": updated_link["enabled"]
            }
        }))

    return _link_to_response(updated_link)


@router.delete("/{agent_name}/public-links/{link_id}")
async def delete_public_link(
    agent_name: str,
    link_id: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Delete (revoke) a public link."""
    # Verify agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify ownership
    if not db.can_user_share_agent(current_user.username, agent_name):
        await log_audit_event(
            event_type="public_link",
            action="delete",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="unauthorized",
            severity="warning"
        )
        raise HTTPException(
            status_code=403,
            detail="Only the agent owner can delete public links"
        )

    # Verify link exists and belongs to this agent
    existing_link = db.get_public_link(link_id)
    if not existing_link or existing_link["agent_name"] != agent_name:
        raise HTTPException(status_code=404, detail="Public link not found")

    # Delete the link
    db.delete_public_link(link_id)

    await log_audit_event(
        event_type="public_link",
        action="delete",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={"link_id": link_id}
    )

    # Broadcast event
    if manager:
        await manager.broadcast(json.dumps({
            "event": "public_link_deleted",
            "data": {
                "agent_name": agent_name,
                "link_id": link_id
            }
        }))

    return {"message": "Public link deleted", "link_id": link_id}
