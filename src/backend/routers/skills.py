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
from models import (
    SkillAssignmentAgent,
    SkillAssignmentsResponse,
    SkillsLibraryStatus,
    SkillSourceCreate,
    SkillSourceUpdate,
)
from db.skill_sources import DuplicateSkillSource, DefaultSourceExists
from services.platform_audit_service import platform_audit_service, AuditEventType
from utils.url_validation import (
    EmbeddedCredentialError,
    reject_embedded_credentials,
    validate_skills_library_url,
)
from services.skill_service import skill_service, SkillInjectionBusy
from services.skill_packaging import validate_skill_name

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
            # ent#237: provenance. Explicitly constructed models drop anything
            # not named here, so a new service field is invisible over REST
            # until it is listed — which is why these three are spelled out.
            source_id=s.get("source_id"),
            source_name=s.get("source_name"),
            shadowed_by=s.get("shadowed_by") or [],
        )
        for s in skills
    ]


@router.get("/skills/library/status", response_model=SkillsLibraryStatus)
async def get_library_status(current_user: User = Depends(get_current_user)):
    """
    Get the current status of the skills library.

    Returns configuration status, sync info, and skill count.

    **`response_model` is a security boundary here, not documentation**
    (ent#334). This route is open to every authenticated caller — including
    agent-scoped keys, deliberately, since the per-agent Skills tab and the
    MCP `get_skills_library_status` tool both read it — while the same
    service dict is also served by `GET /skills/sources`, which is
    `require_admin` + `reject_agent_principal` precisely because repo URLs
    are sensitive. Returning the dict raw handed the admin-gated value to the
    callers that gate excludes. `SkillsLibraryStatus` names what may leave;
    everything else is dropped, so the next sensitive field the service grows
    is fail-closed. Keep it, and see the model's docstring before widening it.
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

    Admin-only AND human-only. Clones or pulls every configured source.

    `reject_agent_principal` on top of `require_admin`, for the same reason the
    source mutations carry it. This route is not a read: it clones executable
    material, and when the commit moves with `skills_auto_reinject_enabled` on
    it spawns `run_fleet_reinject`, pushing skill `scripts/` to every running
    agent. An agent-scoped MCP key resolves to its owner CARRYING the owner's
    role (ent#293), so role-gating alone lets an agent trigger fleet-wide
    executable delivery on a default admin-owned install. "Use, not grant" was
    the original rationale and it does not survive that effect — the same
    grant-vs-use misread that had to be corrected once already on this branch,
    for the LIST route.

    ent#236: when the fleet re-inject flag is on AND the pull actually moved the
    library commit, the fleet sweep is spawned in the BACKGROUND — the operator
    clicked "Sync Library", not "block my browser until every agent is
    updated". The sweep's own report is the honest surface (Settings panel +
    operator alarm on failure), so nothing is lost by not awaiting it here.
    """
    reject_agent_principal(admin_user)
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

# #471: the pure-DB visible-set rule moved to ONE home —
# `services.agent_service.helpers.visible_agent_names` (its docstring carries
# the ent#384 Docker-fault rationale) — now that the subscription-pressure
# batch endpoint is a second consumer. Aliased to keep this module's call
# sites unchanged.
from services.agent_service.helpers import visible_agent_names as _visible_agent_names


@router.get("/skills/assignments", response_model=SkillAssignmentsResponse)
async def get_skill_assignments(current_user: User = Depends(get_current_user)):
    """Which agents hold each skill, for the whole library, in one read (ent#384).

    Backs the Library's Skills tab, where every skill block names its holders.
    ONE endpoint rather than one call per block: the per-block shape is the N+1
    mount loop the ent#260 List view deleted rather than migrated.

    **Access-scoped, and that is a disclosure boundary rather than a display
    detail.** Unscoped, this is a fleet-wide agent-name enumeration oracle for
    any authenticated `role=user` — the Invariant #8 class already called out
    for `GET /api/subscriptions`. Admins read it unfiltered; everyone else sees
    only agents they own or that are shared with them. What is new here even
    within that grant is the *shape*: a capability map of the fleet. Widen this
    gate deliberately, or not at all.

    `reject_agent_principal` on top of the auth dependency: an agent-scoped MCP
    key resolves to its owner CARRYING the owner's role (ent#293), so on a
    default admin-owned install every agent's injected `TRINITY_MCP_API_KEY`
    would read the unfiltered map. There is deliberately no MCP tool for this
    surface and no agent consumer, which makes the gate free — and it matches
    this router's own `/skills/library/{name}` (ent#139) and `/skills/sources`
    (ent#293) gates. Ghost and connector keys are already fenced by their
    allow-lists in `dependencies.get_current_user`. If an agent consumer is
    ever added, this gate is the thing that has to be reconsidered first.

    **Scope of that gate, stated exactly:** `reject_agent_principal` fires on
    `User.agent_name`, which `get_current_user` populates only for
    `scope == "agent"`. A `scope == "system"` key — the `trinity-system`
    orchestrator's — sets neither `agent_name` nor `connector_agent` and so
    reads this unfiltered. That is a deliberate exemption, not an oversight:
    the system agent is documented as bypassing permission checks platform-wide
    (see Authentication & Authorization in architecture.md), and fleet
    management is its job. If this route ever needs to be human-only in the
    strict sense, the allow-list form is `reject_non_interactive_principal`
    (#1854), which passes only a JWT caller.

    The `response_model` is an allow-list, not documentation — the ent#334
    lesson from this same file. The db layer selects from `agent_ownership`;
    the model names the two fields that may leave.

    Size: one entry per (agent, skill) pair the caller may see, so O(agents ×
    skills) — roughly 25k entries for a 500-agent fleet carrying 50 skills.
    Deliberately uncapped: a cap would understate holder counts, which is the
    exact failure this endpoint exists to remove. The UI bounds *rendering*
    instead.
    """
    reject_agent_principal(current_user)

    visible = _visible_agent_names(current_user)

    assignments: Dict[str, List[SkillAssignmentAgent]] = {}
    for row in db.get_all_skill_assignments():
        agent_name = row["agent_name"]
        # `visible is None` is admin (no filter) and MUST NOT collapse with an
        # empty set, which is a real non-admin who can reach no agent at all.
        # A falsy check here would hand that user the whole fleet.
        if visible is not None and agent_name not in visible:
            continue
        assignments.setdefault(row["skill_name"], []).append(
            SkillAssignmentAgent(
                name=agent_name,
                display_label=row.get("display_label"),
            )
        )

    return SkillAssignmentsResponse(
        assignments=assignments,
        scope="all" if visible is None else "accessible",
    )


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
# follows and ent#236 automates the sync + re-inject.
#
# The SYNC routes carry the gate too (both this one and /skills/library/sync).
# They were originally role-gated only, as "use, not grant" — but grant-vs-use
# is a claim about EFFECT, and on this branch the effect of a sync is cloning
# executable material and, when the commit moves, spawning a fleet-wide
# re-inject of it. That is the second time the same axis was misread here; the
# first was the LIST route, gated because "read" said nothing about the private
# repo URLs it returns. The rule that survives both: gate on what the route
# does, not on which verb it is.

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
    try:
        reject_embedded_credentials(body.url)
    except EmbeddedCredentialError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
        try:
            reject_embedded_credentials(fields["url"])
        except EmbeddedCredentialError as e:
            raise HTTPException(status_code=400, detail=str(e))
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

    # Reclaim the clone. Row first, disk second: the row is authoritative, so a
    # filesystem failure here must not fail a delete that already committed —
    # it degrades to an orphan directory that the sync sweep
    # (`_reclaim_orphan_checkouts`) picks up. The reverse order would delete a
    # live source's content while the row still points at it.
    reclaimed = await asyncio.to_thread(
        skill_service.discard_source_checkout, source_id
    )

    await _audit_source(
        request, admin_user, "skill_source_delete", source_id,
        {"checkout_reclaimed": reclaimed},
    )
    return {"deleted": True, "source_id": source_id, "checkout_reclaimed": reclaimed}


@router.post("/skills/sources/{source_id}/sync")
async def sync_skill_source(
    source_id: str,
    admin_user: User = Depends(require_admin),
):
    """Sync ONE source, leaving the others untouched.

    Human-only for the same reason as the full sweep above — this reaches the
    same clone-and-re-inject machinery, just scoped to one source.
    """
    reject_agent_principal(admin_user)

    if db.get_skill_source(source_id) is None:
        raise HTTPException(status_code=404, detail="Skill source not found")

    # Off the event loop, same as the full sweep: this is synchronous git
    # subprocess work bounded only by the clone timeout, so running it inline in
    # an async handler stalls every other request on this worker for as long as
    # a clone takes.
    result = await asyncio.to_thread(skill_service.sync_library, source_id)
    if not result.get("success"):
        # 409 on contention, mirroring the full-sweep route. ent#237 moved the
        # ent#236 sync lock into the shared `sync_library`, so this route can
        # now come back `busy` too — reporting that as a 400 would tell the
        # caller their request was bad when the library is simply being updated
        # by someone else, and is retryable.
        raise HTTPException(
            status_code=409 if result.get("busy") else 400,
            detail=result.get("error", "Sync failed"),
        )
    return result
