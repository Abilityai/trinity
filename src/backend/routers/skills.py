"""
Skills Router - API endpoints for skills management.

Endpoints:
- GET /api/skills/library - List available skills
- GET /api/skills/library/{name} - Get skill content
- POST /api/skills/library/sync - Sync library from GitHub
- GET /api/skills/library/status - Get library status
- GET /api/agents/{name}/skills - List assigned skills
- PUT /api/agents/{name}/skills - Bulk update assignments
- POST /api/agents/{name}/skills/inject - Push skills to running agent
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException

from models import User
from dependencies import (
    get_current_user,
    require_admin,
    reject_agent_principal,
    get_authorized_agent_by_name,
    get_owned_agent_by_name,
)
from database import db
from db_models import AgentSkill, SkillInfo, AgentSkillsUpdate
from services.skill_service import skill_service, SkillInjectionBusy
from services.skill_packaging import validate_skill_name

router = APIRouter(prefix="/api", tags=["skills"])


# ============================================================================
# Skills Library Endpoints
# ============================================================================

@router.get("/skills/library", response_model=List[SkillInfo])
async def list_skills(current_user: User = Depends(get_current_user)):
    """
    List all available skills from the skills library.

    Returns skills with name, description, and path.
    Content is not included for performance.
    """
    skills = skill_service.list_skills()
    return [
        SkillInfo(
            name=s["name"],
            description=s.get("description"),
            path=s["path"],
            automation=s.get("automation"),
            user_invocable=s.get("user_invocable", True),
            allowed_tools=s.get("allowed_tools"),
            requires=s.get("requires") or {"packages": [], "binaries": [], "env": []},
            multi_file=s.get("multi_file", False),
            file_count=s.get("file_count", 0),
            size_bytes=s.get("size_bytes", 0),
            version=s.get("version"),
        )
        for s in skills
    ]


@router.get("/skills/library/status")
async def get_library_status(current_user: User = Depends(get_current_user)):
    """
    Get the current status of the skills library.

    Returns configuration status, sync info, and skill count.
    """
    return skill_service.get_library_status()


@router.get("/skills/library/{skill_name}")
async def get_skill(
    skill_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get details for a specific skill including full content.

    **Human/operator surface only** (ent#139). This returns the skill's full
    content — SKILL.md plus any bundled `scripts/` — so it is executable
    material, not metadata. An agent-scoped key resolves to its owner user, so
    before this gate any agent could pull arbitrary executable content out of
    the library and run it locally: self-acquisition, which is exactly the
    supply-chain risk the skill runner exists to avoid.

    Agents get skills two supported ways instead: assigned/injected by an
    operator, or executed on the dedicated runner under a per-skill allow-list.
    Neither needs raw content over REST. Listing (name + description) stays open
    so an agent can still *discover* what exists.
    """
    reject_agent_principal(current_user)
    skill = skill_service.get_skill(skill_name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")
    return skill


@router.post("/skills/library/sync")
async def sync_library(admin_user: User = Depends(require_admin)):
    """
    Sync the skills library from GitHub.

    Admin-only. Clones or pulls the configured repository.
    """
    result = skill_service.sync_library()
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Sync failed")
        )
    return result


# ============================================================================
# Agent Skills Assignment Endpoints
# ============================================================================

@router.get("/agents/{agent_name}/skills", response_model=List[AgentSkill])
async def get_agent_skills(
    agent_name: str = Depends(get_authorized_agent_by_name),
    current_user: User = Depends(get_current_user)
):
    """
    Get skills assigned to an agent.

    Returns list of AgentSkill objects with assignment metadata.
    """
    return db.get_agent_skills(agent_name)


@router.put("/agents/{agent_name}/skills")
async def update_agent_skills(
    update: AgentSkillsUpdate,
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(get_current_user)
):
    """
    Bulk update skills assigned to an agent.

    Owner-only. Replaces all existing skill assignments with the provided list.
    """
    # The ONE name guard (ent#183): assigned names later reach path math and
    # in-container execs — a traversal-shaped name must never be persisted.
    invalid = [s for s in update.skills if not validate_skill_name(s)]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"invalid_skill_name: {', '.join(repr(s) for s in invalid[:5])}",
        )
    count = db.set_agent_skills(
        agent_name=agent_name,
        skill_names=update.skills,
        assigned_by=current_user.username
    )
    return {
        "success": True,
        "agent_name": agent_name,
        "skills_assigned": count,
        "skills": update.skills
    }


# NOTE: inject endpoint MUST be defined BEFORE {skill_name} routes
# to prevent FastAPI from matching "inject" as a skill_name parameter
@router.post("/agents/{agent_name}/skills/inject")
async def inject_skills(
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(get_current_user)
):
    """
    Inject assigned skills into a running agent as full directory packages.

    Manual sync is a repair action: force=True re-injects unconditionally
    (agent start uses force=False and skips version-unchanged skills).
    Per-skill warnings (missing deps, skipped files) ride the results map.
    """
    try:
        return await skill_service.inject_skills(agent_name, force=True)
    except SkillInjectionBusy as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/agents/{agent_name}/skills/{skill_name}")
async def assign_skill(
    skill_name: str,
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(get_current_user)
):
    """
    Assign a single skill to an agent.
    """
    # Verify skill exists in library
    skill = skill_service.get_skill(skill_name)
    if not skill:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{skill_name}' not found in library"
        )

    result = db.assign_skill(agent_name, skill_name, current_user.username)
    if result is None:
        return {
            "success": True,
            "message": "Skill already assigned",
            "skill_name": skill_name
        }

    return {
        "success": True,
        "message": "Skill assigned",
        "skill": result
    }


@router.delete("/agents/{agent_name}/skills/{skill_name}")
async def unassign_skill(
    skill_name: str,
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(get_current_user)
):
    """
    Remove a skill assignment from an agent.
    """
    removed = db.unassign_skill(agent_name, skill_name)
    return {
        "success": True,
        "removed": removed,
        "skill_name": skill_name
    }
