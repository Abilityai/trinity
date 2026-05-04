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
from dependencies import get_current_user, OwnedAgentByName, CurrentUser
from services.docker_service import get_agent_container
from config import FRONTEND_URL, SITE_PORT
from services.settings_service import get_public_chat_url

router = APIRouter(prefix="/api/agents", tags=["public-links"])

# WebSocket manager will be injected from main.py
manager = None


def set_websocket_manager(ws_manager):
    """Set the WebSocket manager for broadcasting events."""
    global manager
    manager = ws_manager


def _build_public_url(token: str, link_type: str = "chat") -> str:
    """Build the internal public URL for a link token."""
    if link_type == "site":
        return f"{FRONTEND_URL}/site/{token}/"
    return f"{FRONTEND_URL}/chat/{token}"


def _build_external_url(token: str, link_type: str = "chat") -> str | None:
    """Build the external public URL for a link token (if configured)."""
    public_chat_url = get_public_chat_url()
    if not public_chat_url:
        return None
    if link_type == "site":
        return f"{public_chat_url}/site/{token}/"
    return f"{public_chat_url}/chat/{token}"


def _link_to_response(link: dict, include_usage: bool = True) -> PublicLinkWithUrl:
    """Convert a database link dict to response model with URL."""
    usage_stats = None
    if include_usage:
        usage_stats = db.get_public_link_usage_stats(link["id"])

    link_type = link.get("type", "chat")
    return PublicLinkWithUrl(
        id=link["id"],
        agent_name=link["agent_name"],
        token=link["token"],
        created_by=link["created_by"],
        created_at=datetime.fromisoformat(link["created_at"]) if isinstance(link["created_at"], str) else link["created_at"],
        expires_at=datetime.fromisoformat(link["expires_at"]) if link.get("expires_at") else None,
        enabled=link["enabled"],
        name=link.get("name"),
        link_type=link_type,
        url=_build_public_url(link["token"], link_type),
        external_url=_build_external_url(link["token"], link_type),
        usage_stats=usage_stats
    )


@router.post("/{agent_name}/public-links", response_model=PublicLinkWithUrl)
async def create_public_link(
    agent_name: OwnedAgentByName,
    link_request: PublicLinkCreate,
    request: Request,
    current_user: CurrentUser
):
    """Create a new public shareable link for an agent."""
    # Verify agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Validate link_type
    if link_request.link_type not in ("chat", "site"):
        raise HTTPException(status_code=400, detail="link_type must be 'chat' or 'site'")

    # Create the public link
    link = db.create_public_link(
        agent_name=agent_name,
        created_by=current_user.username,
        name=link_request.name,
        expires_at=link_request.expires_at,
        link_type=link_request.link_type,
    )

    # Broadcast event
    if manager:
        await manager.broadcast(json.dumps({
            "event": "public_link_created",
            "data": {
                "agent_name": agent_name,
                "link_id": link["id"],
            }
        }))

    return _link_to_response(link, include_usage=False)


@router.get("/{agent_name}/public-links", response_model=List[PublicLinkWithUrl])
async def list_public_links(
    agent_name: OwnedAgentByName,
    request: Request
):
    """List all public links for an agent."""
    # Verify agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    links = db.list_agent_public_links(agent_name)
    return [_link_to_response(link) for link in links]


@router.get("/{agent_name}/public-links/{link_id}", response_model=PublicLinkWithUrl)
async def get_public_link(
    agent_name: OwnedAgentByName,
    link_id: str,
    request: Request
):
    """Get details of a specific public link."""
    # Verify agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    link = db.get_public_link(link_id)
    if not link or link["agent_name"] != agent_name:
        raise HTTPException(status_code=404, detail="Public link not found")

    return _link_to_response(link)


@router.put("/{agent_name}/public-links/{link_id}", response_model=PublicLinkWithUrl)
async def update_public_link(
    agent_name: OwnedAgentByName,
    link_id: str,
    update_request: PublicLinkUpdate,
    request: Request
):
    """Update a public link (enable/disable, expiry, etc.)."""
    # Verify agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify link exists and belongs to this agent
    existing_link = db.get_public_link(link_id)
    if not existing_link or existing_link["agent_name"] != agent_name:
        raise HTTPException(status_code=404, detail="Public link not found")

    # Update the link
    updated_link = db.update_public_link(
        link_id=link_id,
        name=update_request.name,
        enabled=update_request.enabled,
        expires_at=update_request.expires_at
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
    agent_name: OwnedAgentByName,
    link_id: str,
    request: Request
):
    """Delete (revoke) a public link."""
    # Verify agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify link exists and belongs to this agent
    existing_link = db.get_public_link(link_id)
    if not existing_link or existing_link["agent_name"] != agent_name:
        raise HTTPException(status_code=404, detail="Public link not found")

    # Delete the link
    db.delete_public_link(link_id)

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
