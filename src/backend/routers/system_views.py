# mcp: none — saved dashboard tag filters — UI state only (ORG-001)
"""
System Views API Router (ORG-001 Phase 2: Agent Systems & Tags).

System Views are saved filters that group agents by tags.
"""

from fastapi import APIRouter, Depends, HTTPException
from typing import List

from database import db
from dependencies import get_current_user
from db_models import (
    User,
    SystemView,
    SystemViewCreate,
    SystemViewUpdate,
    SystemViewList
)


router = APIRouter(prefix="/api/system-views", tags=["system-views"])


@router.get("", response_model=SystemViewList)
async def list_system_views(current_user: User = Depends(get_current_user)):
    """
    List all system views accessible to the current user.

    Returns views owned by the user plus all shared views.
    Views are sorted alphabetically by name.
    """
    views = db.list_user_system_views(str(current_user.id))
    return SystemViewList(views=views)


@router.post("", response_model=SystemView, status_code=201)
async def create_system_view(
    data: SystemViewCreate,
    current_user: User = Depends(get_current_user)
):
    """
    Create a new system view.

    System views are saved filters that group agents by tags.
    Views can be shared with all users.
    """
    # Validate name
    if not data.name or not data.name.strip():
        raise HTTPException(status_code=400, detail="View name is required")

    if len(data.name) > 100:
        raise HTTPException(status_code=400, detail="View name too long (max 100 characters)")

    # Validate filter_tags
    if not data.filter_tags or len(data.filter_tags) == 0:
        raise HTTPException(status_code=400, detail="At least one filter tag is required")

    # Validate color format if provided
    if data.color and not data.color.startswith("#"):
        raise HTTPException(status_code=400, detail="Color must be a hex code (e.g., #8B5CF6)")

    view = db.create_system_view(str(current_user.id), data)
    return view


@router.get("/{view_id}", response_model=SystemView)
async def get_system_view(
    view_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get a system view by ID.

    User must own the view or it must be shared.
    """
    # Check access
    is_admin = current_user.role == "admin"
    if not is_admin and not db.can_user_view_system_view(str(current_user.id), view_id):
        raise HTTPException(status_code=403, detail="Access denied to this view")

    view = db.get_system_view(view_id)
    if not view:
        raise HTTPException(status_code=404, detail="View not found")

    return view


@router.put("/{view_id}", response_model=SystemView)
async def update_system_view(
    view_id: str,
    data: SystemViewUpdate,
    current_user: User = Depends(get_current_user)
):
    """
    Update a system view.

    Only the owner or admin can update a view.
    """
    is_admin = current_user.role == "admin"

    # Check edit permission
    if not db.can_user_edit_system_view(str(current_user.id), view_id, is_admin):
        raise HTTPException(status_code=403, detail="Only the owner or admin can edit this view")

    # Validate name if provided
    if data.name is not None:
        if not data.name or not data.name.strip():
            raise HTTPException(status_code=400, detail="View name cannot be empty")
        if len(data.name) > 100:
            raise HTTPException(status_code=400, detail="View name too long (max 100 characters)")

    # Validate filter_tags if provided
    if data.filter_tags is not None and len(data.filter_tags) == 0:
        raise HTTPException(status_code=400, detail="At least one filter tag is required")

    # Validate color format if provided
    if data.color and not data.color.startswith("#"):
        raise HTTPException(status_code=400, detail="Color must be a hex code (e.g., #8B5CF6)")

    view = db.update_system_view(view_id, data)
    if not view:
        raise HTTPException(status_code=404, detail="View not found")

    return view


@router.delete("/{view_id}", status_code=204)
async def delete_system_view(
    view_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Delete a system view.

    Only the owner or admin can delete a view.
    """
    is_admin = current_user.role == "admin"

    # Check edit permission
    if not db.can_user_edit_system_view(str(current_user.id), view_id, is_admin):
        raise HTTPException(status_code=403, detail="Only the owner or admin can delete this view")

    deleted = db.delete_system_view(view_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="View not found")

    return None
