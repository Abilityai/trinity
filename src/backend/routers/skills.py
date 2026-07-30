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
from urllib.parse import urlparse

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
from models import SkillSourceCreate, SkillSourceUpdate
from db.skill_sources import DuplicateSkillSource, DefaultSourceExists
from services.platform_audit_service import platform_audit_service, AuditEventType
from utils.url_validation import validate_skills_library_url
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
            # ent#237: provenance. Explicitly constructed models drop anything
            # not named here, so a new service field is invisible over REST
            # until it is listed — which is why these three are spelled out.
            source_id=s.get("source_id"),
            source_name=s.get("source_name"),
            shadowed_by=s.get("shadowed_by") or [],
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


# ============================================================================
# Skill Source Management (ent#237 — multi-source library)
# ============================================================================
#
# Every MUTATION here carries `reject_agent_principal` in ADDITION to
# `require_admin`, and that is load-bearing rather than defensive padding.
# `require_admin` answers "what role", not "is this a human": an agent-scoped
# MCP key resolves to its owner CARRYING the owner's role, so on a default
# admin-owned install every agent's injected TRINITY_MCP_API_KEY satisfies
# `require_admin` (ent#293, the third occurrence of that class after
# trinity-ops-agent#232 → #1644 → #1816).
#
# Adding a source is the GRANT action from the learnings.md grant-vs-use
# distinction — it decides which repo the fleet executes code from. A
# prompt-injected agent that could register its own repo would get unattended,
# fleet-wide, persistent prompt injection, since skills are instructions Claude
# follows and ent#236 automates the sync + re-inject. Reading and syncing an
# already-configured source is USE and stays role-gated only.

def _reject_embedded_credentials(url: str) -> None:
    """Refuse a source URL carrying userinfo (`https://<token>@github.com/...`).

    `validate_skills_library_url` checks `parsed.hostname`, which IGNORES
    userinfo, and returns the URL unchanged — so a tokenized clone URL passes
    SSRF validation and is then persisted verbatim in `skill_sources.url`,
    returned by GET /skills/sources, and rendered in the Settings panel. Pasting
    one is an easy mistake: it is the form GitHub hands you for scripted clones.

    Fail closed with a named reason pointing at the supported mechanism, rather
    than stripping the credential silently — a silently-stripped token would
    leave the admin believing private-repo auth was configured when it was not.
    Deliberately enforced HERE and not in the shared validator: that helper also
    serves the pre-ent#237 `skills_library_url` setting, and an install relying
    on an embedded token for private-repo access would break on upgrade.
    """
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400,
            detail=(
                "Repository URL must not embed a token or password. It is stored "
                "and displayed in plain text. Configure a GitHub PAT in Settings "
                "for private repositories instead."
            ),
        )


async def _audit_source(request, actor, action: str, source_id: str, details: dict):
    """Audit a source mutation. Best-effort — an audit failure must not undo a
    write that already succeeded."""
    try:
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action=action,
            source="api",
            actor_user=actor,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            target_type="skill_source",
            target_id=source_id,
            details=details,
        )
    except Exception:  # noqa: BLE001
        pass


@router.get("/skills/sources")
async def list_skill_sources(admin_user: User = Depends(require_admin)):
    """List configured skill sources in RESOLUTION order (first wins a clash).

    Admin-only because the rows carry repo URLs, which for a private source are
    themselves sensitive. The per-agent Skills tab does not use this — it gets
    `source_name` from `GET /skills/library`, which exposes no URLs.

    `reject_agent_principal` even though this is a READ: `require_admin` alone
    would not deliver the sensitivity argument above. An agent-scoped MCP key
    resolves to its owner carrying the owner's role (ent#293), so on a default
    admin-owned install every agent could read the private repo URLs this gate
    exists to protect — and a prompt-injected agent reading them is precisely
    the disclosure the admin-gating is for.
    """
    reject_agent_principal(admin_user)
    return skill_service.get_library_status()


@router.post("/skills/sources", status_code=201)
async def create_skill_source(
    request: Request,
    body: SkillSourceCreate,
    admin_user: User = Depends(require_admin),
):
    """Register a skills repo as a source."""
    reject_agent_principal(admin_user)

    # SSRF allowlist (#179) at the boundary, so a bad URL is rejected on write
    # rather than surfacing later as a recurring sync failure.
    _reject_embedded_credentials(body.url)
    try:
        url = validate_skills_library_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid repository URL: {e}")

    try:
        source = db.create_skill_source(
            name=body.name,
            url=url,
            ref=body.ref,
            ref_type=body.ref_type,
            enabled=body.enabled,
            is_default=False,
            created_by=str(admin_user.id),
        )
    except DuplicateSkillSource as e:
        raise HTTPException(status_code=409, detail=str(e))
    except DefaultSourceExists as e:  # pragma: no cover — is_default is forced False
        raise HTTPException(status_code=409, detail=str(e))

    await _audit_source(
        request, admin_user, "skill_source_create", source.id,
        {"url": url, "ref": body.ref, "ref_type": body.ref_type},
    )
    return source


@router.put("/skills/sources/{source_id}")
async def update_skill_source(
    request: Request,
    source_id: str,
    body: SkillSourceUpdate,
    admin_user: User = Depends(require_admin),
):
    """Patch a source (name, url, ref, ref_type, enabled, priority)."""
    reject_agent_principal(admin_user)

    fields = body.model_dump(exclude_unset=True, exclude_none=True)
    if "url" in fields:
        _reject_embedded_credentials(fields["url"])
        try:
            fields["url"] = validate_skills_library_url(fields["url"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid repository URL: {e}")

    try:
        source = db.update_skill_source(source_id, **fields)
    except DuplicateSkillSource as e:
        raise HTTPException(status_code=409, detail=str(e))
    if source is None:
        raise HTTPException(status_code=404, detail="Skill source not found")

    await _audit_source(
        request, admin_user, "skill_source_update", source_id,
        {"changed": sorted(fields.keys())},
    )
    return source


@router.delete("/skills/sources/{source_id}")
async def delete_skill_source(
    request: Request,
    source_id: str,
    admin_user: User = Depends(require_admin),
):
    """Remove a source.

    Assignments referencing it are intentionally left alone: the skill keeps
    resolving by bare name through whatever source still provides it, and
    cascading would silently strip capabilities that are still available.
    """
    reject_agent_principal(admin_user)

    if not db.delete_skill_source(source_id):
        raise HTTPException(status_code=404, detail="Skill source not found")

    await _audit_source(request, admin_user, "skill_source_delete", source_id, {})
    return {"deleted": True, "source_id": source_id}


@router.post("/skills/sources/{source_id}/sync")
async def sync_skill_source(
    source_id: str,
    admin_user: User = Depends(require_admin),
):
    """Sync ONE source, leaving the others untouched."""
    if db.get_skill_source(source_id) is None:
        raise HTTPException(status_code=404, detail="Skill source not found")

    result = skill_service.sync_library(source_id=source_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Sync failed"))
    return result
