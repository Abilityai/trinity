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

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

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
from services.platform_audit_service import platform_audit_service, AuditEventType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["skills"])

# Strong references to in-flight background fleet sweeps — asyncio holds only a
# weak reference to a bare `create_task`, so without this the sweep can be
# garbage-collected mid-run (the #1083 `_inflight` lesson).
_BACKGROUND_SWEEPS: set = set()


async def _remove_unassigned_skills(
    agent_name: str,
    removed_names: List[str],
    current_user: User,
    request: Optional[Request] = None,
) -> Optional[Dict[str, Any]]:
    """Remove just-unassigned skill packages from the agent (ent#236).

    Best-effort by design, and the ordering is the point: the DB unassign has
    already committed and is authoritative. A stopped agent, a busy injection
    lock, or a transport failure must NOT fail the caller's unassign — it
    degrades to a named `deferred` outcome, and the start-path reconcile
    (`reconcile_agent_skills`) finishes the job the next time the agent runs.

    Returns the removal report for the response body, or None when nothing was
    unassigned.
    """
    names = [n for n in removed_names if validate_skill_name(n)]
    if not names:
        return None

    try:
        result = await skill_service.remove_skills(agent_name, names)
    except SkillInjectionBusy:
        return {
            "status": "deferred",
            "reason": "injection_in_progress",
            "skills": sorted(names),
        }
    except Exception as e:  # noqa: BLE001 — never fail a committed unassign
        logger.warning(f"skill removal failed for {agent_name}: {e}")
        return {
            "status": "deferred",
            "reason": "removal_error",
            "skills": sorted(names),
        }

    try:
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="skill_removed",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request and request.client else None,
            endpoint=str(request.url.path) if request else None,
            request_id=getattr(request.state, "request_id", None) if request else None,
            target_type="agent",
            target_id=agent_name,
            details={
                "trigger": "unassign",
                "skills": sorted(names)[:50],
                "removed": result.get("skills_removed", 0),
                "failed": result.get("skills_failed", 0),
            },
        )
    except Exception:  # noqa: BLE001
        pass

    return {
        "status": "completed" if result.get("success") else "partial",
        "skills_removed": result.get("skills_removed", 0),
        "skills_failed": result.get("skills_failed", 0),
        "results": result.get("results", {}),
    }


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

    ent#236: when the fleet re-inject flag is on AND the pull actually moved the
    library commit, the fleet sweep is spawned in the BACKGROUND — the operator
    clicked "Sync Library", not "block my browser until every agent is
    updated". The sweep's own report is the honest surface (Settings panel +
    operator alarm on failure), so nothing is lost by not awaiting it here.
    """
    result = await asyncio.to_thread(skill_service.sync_library)
    if not result.get("success"):
        # 409, not 400: a concurrent sync (the scheduled loop, or another admin)
        # is a retryable contention, not a bad request — and it means the
        # library IS being updated, just not by this call.
        raise HTTPException(
            status_code=409 if result.get("busy") else 400,
            detail=result.get("error", "Sync failed")
        )

    fleet_spawned = False
    if result.get("commit_changed"):
        from services.settings_service import is_skills_auto_reinject_enabled
        from services.skills_sync_service import skills_sync_service

        if is_skills_auto_reinject_enabled():
            # Strong ref so the task isn't garbage-collected mid-flight
            # (the #1083 `_inflight` footgun).
            task = asyncio.create_task(
                skills_sync_service.run_fleet_reinject(
                    commit_sha=result.get("commit_sha"), trigger="manual_sync"
                )
            )
            _BACKGROUND_SWEEPS.add(task)
            task.add_done_callback(_BACKGROUND_SWEEPS.discard)
            fleet_spawned = True

    result["fleet_reinject_started"] = fleet_spawned
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
    request: Request,
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(get_current_user)
):
    """
    Bulk update skills assigned to an agent.

    Owner-only. Replaces all existing skill assignments with the provided list.

    ent#236: this is the primary assignment surface (UI + MCP `set_agent_skills`),
    so it drops skills far more often than the single DELETE does. Names removed
    by the replace are removed from the agent too — gating removal on the DELETE
    endpoint alone would leave the common path silently accumulating packages
    forever, which is the gap this issue exists to close.
    """
    # The ONE name guard (ent#183): assigned names later reach path math and
    # in-container execs — a traversal-shaped name must never be persisted.
    invalid = [s for s in update.skills if not validate_skill_name(s)]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"invalid_skill_name: {', '.join(repr(s) for s in invalid[:5])}",
        )

    # Snapshot BEFORE the replace — afterwards the dropped names are unknowable.
    try:
        previous = set(db.get_agent_skill_names(agent_name))
    except Exception:  # noqa: BLE001 — never block the assignment itself
        previous = set()

    count = db.set_agent_skills(
        agent_name=agent_name,
        skill_names=update.skills,
        assigned_by=current_user.username
    )

    removal = await _remove_unassigned_skills(
        agent_name, sorted(previous - set(update.skills)), current_user, request
    )

    return {
        "success": True,
        "agent_name": agent_name,
        "skills_assigned": count,
        "skills": update.skills,
        "removal": removal,
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
    request: Request,
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(get_current_user)
):
    """
    Remove a skill assignment from an agent, and the injected package with it.

    ent#236: before this, unassigning only deleted a DB row — the skill stayed
    on the agent, still listed in CLAUDE.md, still invocable, forever. The
    package removal is best-effort and never fails the unassign; see
    `_remove_unassigned_skills`.
    """
    removed = db.unassign_skill(agent_name, skill_name)

    removal = None
    if removed:
        removal = await _remove_unassigned_skills(
            agent_name, [skill_name], current_user, request
        )

    return {
        "success": True,
        "removed": removed,
        "skill_name": skill_name,
        "removal": removal,
    }
