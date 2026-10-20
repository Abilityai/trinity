"""
Tags API Router (ORG-001: Agent Systems & Tags).

Lightweight organizational layer for grouping agents into logical systems using tags.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional

from database import db
from db.tags import ORG_TAG_PREFIXES
from dependencies import get_current_user, AuthorizedAgent, OwnedAgent, is_fleet_admin
from services.agent_service.helpers import visible_agent_names
from db_models import User, AgentTagList, AgentTagsUpdate, AllTagsResponse, TagWithCount


router = APIRouter(prefix="/api", tags=["tags"])

logger = logging.getLogger(__name__)

# WebSocket manager, injected from main.py (agent_rename pattern).
manager = None


def set_websocket_manager(ws_manager):
    """Set the WebSocket manager for broadcasting tag-change events."""
    global manager
    manager = ws_manager


async def _broadcast_tags_changed(agent_name: str) -> None:
    """Fleet-wide `agent_tags_changed` so every open Grid converges on a tag
    edit (zones/lines/ribbons) without waiting for a roster poll
    (trinity-enterprise#305). Best-effort — a delivery failure never fails
    the write.

    THIN TRIGGER ONLY — never the tag values. `/ws` is SCOPE_ALL and
    unfiltered (the #918 report-broadcast rule), so a payload carrying
    `dept-*`/`reports-to-*` values would hand any authenticated `/ws` client
    every tenant's department membership and reporting edges, including
    agents it cannot see in `GET /api/agents`. Listeners refetch through the
    access-controlled `GET /api/agents/{name}/tags` instead
    (`stores/network.js` `_refetchAgentTags`). Pinned by
    `tests/unit/test_305_tags_broadcast.py`."""
    if manager is None:
        return
    try:
        await manager.broadcast(
            json.dumps({"type": "agent_tags_changed", "agent_name": agent_name})
        )
    except Exception:
        logger.debug("agent_tags_changed broadcast failed", exc_info=True)


def _guard_org_namespace(current_user: User, touched: List[str]) -> None:
    """Org-overlay namespaces (`dept-*`, `reports-to-*`) are HUMAN-only
    (trinity-enterprise#305; mirrors the #1578 reserved event namespace).
    Agent-scoped principals resolve to the owner on REST, which would let a
    prompt-injected agent silently redraw the org chart operators act on —
    reject any write that touches a reserved prefix."""
    if not current_user.agent_name:
        return
    reserved = [t for t in touched if t.startswith(ORG_TAG_PREFIXES)]
    if reserved:
        raise HTTPException(
            status_code=403,
            detail=(
                "Tags 'dept-*' and 'reports-to-*' carry the org chart and can "
                "only be changed by a human operator, not an agent key. "
                f"Blocked: {', '.join(sorted(reserved)[:5])}"
            ),
        )


# ============================================================================
# Global Tags Endpoints
# ============================================================================

@router.get("/tags", response_model=AllTagsResponse)
async def list_all_tags(current_user: User = Depends(get_current_user)):
    """
    List all unique tags with agent counts, over the agents the caller may see
    (an admin person: every agent).

    Returns tags sorted by count (descending), then alphabetically.
    """
    visible = visible_agent_names(current_user)
    if visible is not None:
        # trinity-enterprise#872: count only the agents the caller may see — an
        # agent key its reach, a non-admin person their owned and shared agents.
        counts: dict = {}
        for agent_tags in db.get_tags_for_agents(sorted(visible)).values():
            for tag in agent_tags:
                counts[tag] = counts.get(tag, 0) + 1
        tags = [TagWithCount(tag=t, count=n)
                for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    else:
        tags = db.list_all_tags()
    if not is_fleet_admin(current_user):
        # trinity-enterprise#305: org namespaces carry department names +
        # headcounts and manager agent names (`reports-to-<agent>`). Org tags
        # are an operator surface — the
        # overlay reads them per-agent through access-controlled routes, and
        # generic tag surfaces hide them via `isOrgTag` anyway, so non-admins
        # lose nothing they could render.
        tags = [t for t in tags if not t.tag.startswith(ORG_TAG_PREFIXES)]
    return AllTagsResponse(tags=tags)


# ============================================================================
# Agent Tag Endpoints
# ============================================================================

@router.get("/agents/{name}/tags", response_model=AgentTagList)
async def get_agent_tags(name: AuthorizedAgent):
    """
    Get all tags for an agent.

    Returns tags sorted alphabetically.
    """
    tags = db.get_agent_tags(name)
    return AgentTagList(agent_name=name, tags=tags)


@router.put("/agents/{name}/tags", response_model=AgentTagList)
async def set_agent_tags(
    name: OwnedAgent,
    request: AgentTagsUpdate,
    current_user: User = Depends(get_current_user),
):
    """
    Replace all tags for an agent.

    Only the agent owner or admin can modify tags. Tags are normalized
    (lowercase, trimmed) and deduplicated. Agent-scoped keys cannot change
    reserved org-overlay tags (`dept-*`, `reports-to-*`) — the guard checks
    the DELTA, so an agent may still rewrite its own plain tags.
    """
    # Same per-tag validation as the POST route — the overlay writes
    # exclusively via this PUT, and gridOrg.js mirrors a 50-char cap that
    # must actually exist on the path it uses (trinity-enterprise#305 review).
    if len(request.tags) > 100:
        raise HTTPException(status_code=400, detail="Too many tags (max 100)")
    for t in request.tags:
        normalized_t = t.lower().strip()
        if not normalized_t:
            continue  # blank entries are dropped by normalization below
        if len(normalized_t) > 50:
            raise HTTPException(
                status_code=400, detail=f"Tag too long (max 50 characters): {normalized_t[:60]}"
            )
        if not all(c.isalnum() or c == '-' for c in normalized_t):
            raise HTTPException(
                status_code=400,
                detail="Tags can only contain letters, numbers, and hyphens",
            )

    incoming = {t.lower().strip() for t in request.tags if t.strip()}
    current = set(db.get_agent_tags(name))
    _guard_org_namespace(current_user, sorted(incoming.symmetric_difference(current)))
    tags = db.set_agent_tags(name, request.tags)
    await _broadcast_tags_changed(name)
    return AgentTagList(agent_name=name, tags=tags)


@router.post("/agents/{name}/tags/{tag}", response_model=AgentTagList)
async def add_agent_tag(
    name: OwnedAgent,
    tag: str,
    current_user: User = Depends(get_current_user),
):
    """
    Add a single tag to an agent.

    Only the agent owner or admin can modify tags.
    Tag is normalized (lowercase, trimmed).
    """
    # Validate tag format
    normalized = tag.lower().strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Tag cannot be empty")
    if len(normalized) > 50:
        raise HTTPException(status_code=400, detail="Tag too long (max 50 characters)")
    if not all(c.isalnum() or c == '-' for c in normalized):
        raise HTTPException(status_code=400, detail="Tags can only contain letters, numbers, and hyphens")

    _guard_org_namespace(current_user, [normalized])
    tags = db.add_agent_tag(name, tag)
    await _broadcast_tags_changed(name)
    return AgentTagList(agent_name=name, tags=tags)


@router.delete("/agents/{name}/tags/{tag}", response_model=AgentTagList)
async def remove_agent_tag(
    name: OwnedAgent,
    tag: str,
    current_user: User = Depends(get_current_user),
):
    """
    Remove a single tag from an agent.

    Only the agent owner or admin can modify tags. Agent-scoped keys cannot
    remove reserved org-overlay tags (an agent hiding itself from the org
    chart is the same threat as it redrawing the chart).
    """
    _guard_org_namespace(current_user, [tag.lower().strip()])
    tags = db.remove_agent_tag(name, tag)
    await _broadcast_tags_changed(name)
    return AgentTagList(agent_name=name, tags=tags)
