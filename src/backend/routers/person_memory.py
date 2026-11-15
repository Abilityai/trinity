# mcp: none — owner-tier, human-only view of other people's memory; UI-only (Agent Settings → Person memory, trinity-enterprise#962)
"""The owner's view of a person's memory on an agent (trinity-enterprise#962, MEM-001).

Four routes under ``/api/agents/{name}/person-memory``:

* ``GET  …``                         — everyone with memory on the agent, newest change first.
* ``GET  …/person?user_email=``      — one person: notes, summary, identity, first history page.
* ``GET  …/writes?user_email=&before=`` — a further page of that person's history.
* ``POST …/writes/{write_id}/undo``  — undo one write, latest-first within its section.

**Readers** (the ent#638 seat-decision rule): the agent's owner and an admin
(`OwnedAgentByName` → `can_user_share_agent`, the uniform 404 otherwise);
nobody else — a person the agent is shared with and any assignment kind gain
nothing (ent#500). **Human-only**: `require_person` is a decorator dependency,
so an agent, system or connector key is refused 403 on the principal alone,
before any lookup of the agent (#186: nothing about the target is disclosed).

The person's retrieval-memory facts are served by the MEM-002 routes in
``routers/agent_memory.py`` (facts / forget / events / search), which already
follow this readers rule.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from dependencies import CurrentUser, OwnedAgentByName, require_person
from models import (
    PersonMemoryDetail,
    PersonMemoryHistory,
    PersonMemoryPeople,
    PersonMemoryUndoRequest,
    PersonMemoryUndoResult,
)
from services import person_memory_service as pms
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["person-memory"])

_PERSON_NOT_FOUND = {"code": "person_not_found",
                     "message": "This person has no memory with this agent."}
_INVALID_CURSOR = {"code": "invalid_cursor",
                   "message": "That page cursor is not valid. Reopen the person to start from the newest change."}


def _operator(current_user) -> str:
    """Who acted, as `undone_by` records it: the operator's email, else the
    username (the bootstrap admin may have no email)."""
    return str(getattr(current_user, "email", None) or getattr(current_user, "username", None) or "operator")


@router.get("/{agent_name}/person-memory", response_model=PersonMemoryPeople,
            dependencies=[Depends(require_person)])
async def list_person_memory_people(
    agent_name: OwnedAgentByName,
    offset: int = Query(default=0, ge=0, le=100_000),
    limit: int = Query(default=pms.PEOPLE_PAGE, ge=1, le=pms.PEOPLE_PAGE_MAX),
):
    """Everyone with memory on this agent — notes or a summary, any history row,
    or live retrieval-memory facts in their own scope — newest change first."""
    return await asyncio.to_thread(pms.list_people, agent_name, offset=offset, limit=limit)


@router.get("/{agent_name}/person-memory/person", response_model=PersonMemoryDetail,
            dependencies=[Depends(require_person)])
async def get_person_memory(
    agent_name: OwnedAgentByName,
    user_email: str = Query(..., min_length=1, max_length=320),
):
    """One person's memory: notes and summary text, the channel identity a
    request captured, and the first page of the write history. Never creates a
    memory row; a person with no memory here is the 404."""
    try:
        return await asyncio.to_thread(pms.person_detail, agent_name, user_email)
    except pms.PersonNotFound:
        raise HTTPException(status_code=404, detail=_PERSON_NOT_FOUND)


@router.get("/{agent_name}/person-memory/writes", response_model=PersonMemoryHistory,
            dependencies=[Depends(require_person)])
async def list_person_memory_writes(
    agent_name: OwnedAgentByName,
    user_email: str = Query(..., min_length=1, max_length=320),
    before: Optional[str] = Query(default=None, max_length=200, description="next_before of the previous page"),
    limit: int = Query(default=pms.HISTORY_PAGE, ge=1, le=pms.HISTORY_PAGE_MAX),
):
    """A page of one person's write history, both sections, newest first."""
    try:
        email = pms.normalise_email(user_email)
        return await asyncio.to_thread(pms.history_page, agent_name, email, before=before, limit=limit)
    except pms.PersonNotFound:
        raise HTTPException(status_code=404, detail=_PERSON_NOT_FOUND)
    except pms.InvalidCursor:
        raise HTTPException(status_code=422, detail=_INVALID_CURSOR)


@router.post("/{agent_name}/person-memory/writes/{write_id}/undo", response_model=PersonMemoryUndoResult,
             dependencies=[Depends(require_person)])
async def undo_person_memory_write(
    agent_name: OwnedAgentByName,
    write_id: str,
    body: PersonMemoryUndoRequest,
    current_user: CurrentUser,
):
    """Undo one write to a person's memory — the same primitive as the
    person's own Undo, recorded with `undone_by` = the operator. Latest-first
    within the write's section: a NAMED 409 (`not_latest` / `already_undone`);
    an id that is not this person's write on this agent is the uniform 404.
    The person's Workspace history then reads "Undone by an operator of this
    agent", without saying who."""
    from services import rate_limiter
    operator = _operator(current_user)
    rate_limiter.enforce(f"person_memory_undo:{operator}", 30, 60)
    try:
        email = pms.normalise_email(body.user_email)
        result = await asyncio.to_thread(pms.undo_write, agent_name, email, write_id, undone_by=operator)
    except pms.PersonNotFound:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "That memory change was not found."})
    except pms.MemoryUndoRefused as e:
        logger.info("[ent#962] undo refused on %s (%s): %s", agent_name, write_id, e.code)
        raise HTTPException(status_code=e.status_code, detail={"code": e.code, "message": e.detail})
    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="person_memory_undo",
        source="api",
        actor_user=current_user,
        target_type="agent",
        target_id=agent_name,
        details={"user_email": email, "write_id": write_id},
    )
    return result
