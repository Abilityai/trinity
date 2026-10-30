# mcp: memory.ts (search_memory) — the agent door; the owner-tier configuration and views are UI-only (trinity-enterprise#1022, #1028)
"""Retrieval memory per agent (trinity-enterprise#1022, MEM-002).

Six routes under ``/api/agents/{name}/retrieval-memory`` — named so they sit
clearly apart from ``/user-memory``, the MEM-001 write door the agent itself
calls:

* ``GET  …``            — status for this agent (a use; agent-reachable).
* ``PUT  …``            — the per-agent opt-out (a grant; owner + human-only, #2996).
* ``GET  …/facts``      — the owner view of one scope's facts (ent#962's readers rule).
* ``DELETE …/facts/{id}`` — forget one fact (soft delete, recorded with who).
* ``GET  …/events``     — the journal: what happened to a fact, or in one execution.
* ``GET  …/search``     — the owner's search over one scope (trinity-enterprise#1028).
* ``POST …/search``     — the agent door: MCP ``search_memory``, the scope read off the
  execution row by the whose-memory rule, never from the caller.

The platform switch itself is ``trinity_memory_enabled`` through the generic
settings door; ``GET /api/settings/memory`` (admin) reports the platform status.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from database import db
from db_models import User
from dependencies import (
    AuthorizedAgentByName,
    CurrentUser,
    OwnedAgentByName,
    assert_agent_access,
    assert_own_agent,
    get_current_user,
    reject_agent_principal,
    require_person,
)
from db.memory_facts import EVENT_OPS
from models import (
    MemoryFactEventOut,
    MemoryFactOut,
    MemoryOptOutUpdate,
    MemorySearchHit,
    MemorySearchRequest,
    MemorySearchResponse,
)
from services import memory_service, memory_subject
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["retrieval-memory"])


def _agent_status(agent_name: str) -> dict:
    platform = memory_service.status()
    opt_out = db.get_memory_opt_out(agent_name)
    stats = db.memory_fact_stats(agent_name)
    return {
        "agent_name": agent_name,
        "enabled_platform": platform["enabled"],
        "opt_out": opt_out,
        "effective": bool(platform["enabled"] and not opt_out),
        "available": platform["available"],
        "reason": platform["reason"],
        "embedding_model": platform["embedding_model"],
        "fact_count": stats["count"],
        "last_retained_at": stats["last_retained_at"],
    }


@router.get("/{agent_name}/retrieval-memory")
async def get_retrieval_memory_endpoint(
    agent_name: AuthorizedAgentByName,
    current_user: CurrentUser,
):
    """Whether retrieval memory is on for this agent, and whether it can run.

    A use, not a grant: an agent may read its OWN status (and only its own —
    the self-fence is the first statement, trinity-enterprise#840 / #2996).
    """
    assert_own_agent(current_user, agent_name,
                     detail="Agent-scoped key may only read its own agent's memory status")
    return await asyncio.to_thread(_agent_status, agent_name)


@router.put("/{agent_name}/retrieval-memory")
async def set_retrieval_memory_opt_out_endpoint(
    agent_name: OwnedAgentByName,
    body: MemoryOptOutUpdate,
    current_user: User = Depends(require_person),
):
    """Keep this agent out of retrieval memory, or let it back in. Owner-only,
    human-only: an agent must not be able to switch its own memory off."""
    reject_agent_principal(current_user)
    if not db.set_memory_opt_out(agent_name, body.opt_out):
        raise HTTPException(status_code=404, detail="Agent not found")
    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="retrieval_memory_config",
        source="api",
        actor_user=current_user,
        target_type="agent",
        target_id=agent_name,
        details={"opt_out": body.opt_out},
    )
    return await asyncio.to_thread(_agent_status, agent_name)


@router.get("/{agent_name}/retrieval-memory/facts", response_model=list[MemoryFactOut])
async def list_retrieval_memory_facts_endpoint(
    agent_name: OwnedAgentByName,
    current_user: User = Depends(require_person),
    user_email: str | None = Query(default=None, description="A person's scope; omitted = the agent scope"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """The owner view of one scope's live facts, newest first."""
    reject_agent_principal(current_user)
    scope = memory_service.AGENT_SCOPE
    if user_email:
        email = user_email.strip().lower()
        if not memory_subject.looks_like_email(email):
            raise HTTPException(status_code=422, detail={"code": "invalid_email", "message": "user_email is not an email address"})
        scope = memory_service.scope_for(agent_name, email)
    rows = db.list_memory_facts(agent_name, scope, limit=limit, offset=offset, with_embedding=False)
    return [MemoryFactOut(**{k: row.get(k) for k in MemoryFactOut.model_fields}) for row in rows]


@router.delete("/{agent_name}/retrieval-memory/facts/{fact_id}")
async def forget_retrieval_memory_fact_endpoint(
    agent_name: OwnedAgentByName,
    fact_id: str,
    current_user: User = Depends(require_person),
):
    """Forget one fact. A soft delete that records who asked; 404 unless the
    fact is a live fact of this agent."""
    reject_agent_principal(current_user)
    deleted_by = getattr(current_user, "email", None) or getattr(current_user, "username", None) or "owner"
    if not db.soft_delete_memory_fact(fact_id, agent_name, deleted_by=str(deleted_by)):
        raise HTTPException(status_code=404, detail="Fact not found")
    try:
        db.insert_memory_fact_events([{"agent_name": agent_name, "fact_id": fact_id, "op": "forget", "actor": str(deleted_by)}])
    except Exception:  # noqa: BLE001 — the journal is bookkeeping, never a failure
        logger.debug("[memory] forget not journaled for %s", fact_id)
    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="retrieval_memory_forget",
        source="api",
        actor_user=current_user,
        target_type="agent",
        target_id=agent_name,
        details={"fact_id": fact_id},
    )
    return {"agent_name": agent_name, "fact_id": fact_id, "forgotten": True}


@router.get("/{agent_name}/retrieval-memory/events", response_model=list[MemoryFactEventOut])
async def list_retrieval_memory_events_endpoint(
    agent_name: OwnedAgentByName,
    current_user: User = Depends(require_person),
    fact_id: str | None = Query(default=None, description="One fact's lineage"),
    execution_id: str | None = Query(default=None, description="Everything one execution did to the memory"),
    op: str | None = Query(default=None, description="One op: " + ", ".join(EVENT_OPS)),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """The owner view of the journal, newest first: how a fact came to read the
    way it does (``fact_id``), or what one run proposed, stored, merged, retired
    or recalled (``execution_id``). Narrow by ``op`` to, say, every candidate
    the screen dropped."""
    reject_agent_principal(current_user)
    if op is not None and op.lower() not in EVENT_OPS:
        raise HTTPException(status_code=422, detail={"code": "invalid_op", "message": "op must be one of " + ", ".join(EVENT_OPS)})
    rows = await asyncio.to_thread(
        db.list_memory_fact_events, agent_name, fact_id=fact_id or None, execution_id=execution_id or None,
        op=op, limit=limit, offset=offset,
    )
    return [MemoryFactEventOut(**{k: row.get(k) for k in MemoryFactEventOut.model_fields}) for row in rows]


# --------------------------------------------------------------------------- #
# Search (trinity-enterprise#1028)
# --------------------------------------------------------------------------- #

def _query_or_422(q: str | None) -> str:
    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=422, detail={"code": "query_empty", "message": "Give a few words to look for: a name, a deadline, a decision, a past outcome."})
    return query


def _memory_on_or_4xx(agent_name: str) -> None:
    """422 when retrieval memory is off for this agent (the platform switch or
    the per-agent opt-out), 503 when it is on but the embedder is not there —
    an honest refusal rather than an empty answer that reads as 'nothing matched'."""
    if not memory_service.is_enabled(agent_name):
        raise HTTPException(status_code=422, detail={
            "code": "memory_off",
            "message": "Retrieval memory is off for this agent (Settings → General → Retrieval memory, or the agent's own Memory setting).",
        })
    platform = memory_service.status()
    if not platform["available"]:
        raise HTTPException(status_code=503, detail={"code": "memory_unavailable", "message": platform["reason"] or "embedding model not loaded"})


def _hit(record: dict) -> MemorySearchHit:
    return MemorySearchHit(
        id=record["id"], text=record["text"], line=record.get("line") or memory_service.render_fact_line(record),
        source=record.get("source") or "", execution_id=record.get("execution_id"),
        created_at=record.get("created_at"), updated_at=record.get("updated_at"),
        relevance=float(record.get("relevance") or 0.0), score=float(record.get("score") or 0.0),
    )


@router.get("/{agent_name}/retrieval-memory/search", response_model=list[MemorySearchHit])
async def search_retrieval_memory_endpoint(
    agent_name: OwnedAgentByName,
    current_user: User = Depends(require_person),
    q: str = Query(..., description="What to look for"),
    k: int = Query(default=10, ge=1, le=memory_service.SEARCH_K_MAX),
    user_email: str | None = Query(default=None, description="A person's scope; omitted = the agent scope"),
):
    """The owner's search over one scope: the same store, ranking and recency
    weight the prompt gets, past the injected tail. Every hit is journaled as a
    ``search`` row with the query."""
    reject_agent_principal(current_user)
    query = _query_or_422(q)
    email = None
    if user_email:
        email = user_email.strip().lower()
        if not memory_subject.looks_like_email(email):
            raise HTTPException(status_code=422, detail={"code": "invalid_email", "message": "user_email is not an email address"})
    _memory_on_or_4xx(agent_name)
    try:
        records = await memory_service.search_records(agent_name, email, query, k=k)
    except memory_service.MemorySearchError:
        raise HTTPException(status_code=503, detail={
            "code": "memory_unavailable",
            "message": "Memory search could not be answered — the embedder or the store did not respond. Try again; if it persists, check Settings → General → Retrieval memory.",
        })
    return [_hit(r) for r in records]


@router.post("/{agent_name}/retrieval-memory/search", response_model=MemorySearchResponse)
async def search_retrieval_memory_agent_endpoint(
    agent_name: str,
    body: MemorySearchRequest,
    current_user: User = Depends(get_current_user),
):
    """The agent door (MCP ``search_memory``). The scope is the one the
    execution serves — the person on a person turn, the agent's own work on a
    headless run — read off the row by ``memory_subject``; a run that serves no
    one is refused with the reason. An agent-scoped key may only search its own
    agent (trinity-enterprise#840), checked first so a key outside its reach
    gets the same refusal."""
    assert_own_agent(current_user, agent_name, detail="Agent-scoped key may only search its own agent's memory")
    assert_agent_access(current_user, agent_name, detail="Not authorized")
    query = _query_or_422(body.query)
    execution = db.get_execution(body.execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution.agent_name != agent_name:
        raise HTTPException(status_code=403, detail="Execution does not belong to this agent")
    subject = memory_subject.resolve(execution)
    if subject.kind == memory_subject.SKIP:
        raise HTTPException(status_code=422, detail={
            "code": "memory_no_subject", "reason": subject.reason,
            "message": ("This run serves no one whose memory can be searched: an anonymous public turn, a group "
                        f"conversation, or a '{subject.triggered_by}' trigger. Memory is searched on a person's "
                        "verified turn or on the agent's own headless runs."),
        })
    _memory_on_or_4xx(agent_name)
    try:
        records = await memory_service.search_records(agent_name, subject.user_email, query, k=body.k,
                                                      execution_id=body.execution_id)
    except memory_service.MemorySearchError:
        raise HTTPException(status_code=503, detail={
            "code": "memory_unavailable",
            "message": "Memory search could not be answered — the embedder or the store did not respond. Try again; if it persists, check Settings → General → Retrieval memory.",
        })
    return MemorySearchResponse(agent_name=agent_name, scope="person" if subject.is_person else "agent",
                                hits=[_hit(r) for r in records])
