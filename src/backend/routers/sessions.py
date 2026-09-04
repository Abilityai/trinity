# mcp: none — per-user resumable-session rows for the UI (retired from Agent Detail, ent#358); agents talk via chat_with_agent
"""
Session endpoints — per-platform-user `--resume` conversations.

Phase 2 of docs/planning/SESSION_TAB_2026-04.md. Six endpoints that mirror
the structure of routers/chat.py (same auth model, same TaskExecutionService)
but persist to the parallel agent_sessions / agent_session_messages tables
and request `persist_session=True` so each turn reattaches via Claude Code's
`--resume` flag.

**The UI surface is retired (ent#358).** Agent Detail no longer renders a
Session panel — the Workspace is the one continuous-conversation surface, and
it runs the same engine (services/session_turn_service.py) against its own
tables. These endpoints and their rows stay: existing sessions remain readable,
and nothing here is a dead path for an API caller.

Surface gated on `services.settings_service.is_session_tab_enabled()`. When
the flag is off, every endpoint returns 404 — the route exists but the
feature is invisible.
"""

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from database import db
from db_models import WebFileUpload, SessionMessageInsert
from dependencies import AuthorizedAgent, get_current_user
from models import CreateSessionRequest, SessionMessageRequest, User
from services.docker_service import get_agent_container
from services.session_cleanup_service import get_session_cleanup_service
from services.session_turn_service import (
    InflightSentinel,
    LOCK_POLL_INTERVAL_SECONDS,
    LOCK_RELEASE_LUA,
    LOCK_TTL_FALLBACK,
    LOCK_WAIT_TOTAL_SECONDS,
    RESUME_NOT_FOUND_MARKERS,
    RUNTIMES_WITHOUT_SESSION_TAB_RESUME,
    ResumeLock,
    clear_session_inflight,
    get_async_redis,
    is_resume_not_found,
    is_turn_in_flight,
    resolve_lock_ttl,
    run_resumable_turn,
    session_inflight_key,
    session_lock_key,
    set_session_inflight,
    supports_session_resume,
)
from services.settings_service import is_session_tab_enabled
from services.upload_service import (
    decode_web_file,
    process_file_uploads,
    WEB_MAX_FILES,
    WEB_MAX_FILE_SIZE,
    WEB_MAX_IMAGE_SIZE,
    WEB_MAX_TOTAL_IMAGE_SIZE,
)
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["sessions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enabled_or_404() -> None:
    """Phase 1.6 flag gate. 404 keeps the surface invisible when off."""
    if not is_session_tab_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _session_or_404(session_id: str, user: User, agent_name: str):
    """Resolve a session row and enforce per-user ownership.

    The Session tab keys sessions by user (E6 in the design doc): even an
    agent owner cannot read or send into another user's session. Returns the
    session row; raises HTTP 404 if missing OR not owned by the caller (404
    rather than 403 to avoid leaking session id existence).
    """
    session = db.get_session(session_id)
    if session is None or session.user_id != user.id or session.agent_name != agent_name:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _serialize_session(session, turn_in_progress: bool = False) -> dict:
    """Convert AgentSession dataclass to JSON-friendly dict.

    The optional ``turn_in_progress`` flag is derived at the endpoint
    layer from the ``session_inflight:{session_id}`` Redis sentinel; it
    drives the UI's onActivated re-sync for #759. Callers that don't pass
    it (list endpoint, write-path responses) get a safe ``False`` — the
    real-time signal only matters on the per-session GET that the polling
    UI loop reads.
    """
    return {
        "id": session.id,
        "agent_name": session.agent_name,
        "user_id": session.user_id,
        "user_email": session.user_email,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "last_message_at": (
            session.last_message_at.isoformat() if session.last_message_at else None
        ),
        "message_count": session.message_count,
        "total_cost": session.total_cost,
        "total_context_used": session.total_context_used,
        "total_context_max": session.total_context_max,
        "status": session.status,
        "subscription_id": session.subscription_id,
        "cached_claude_session_id": session.cached_claude_session_id,
        "last_resume_at": (
            session.last_resume_at.isoformat() if session.last_resume_at else None
        ),
        "consecutive_resume_failures": session.consecutive_resume_failures,
        "compact_count": session.compact_count,
        "turn_in_progress": turn_in_progress,
    }


def _serialize_message(msg) -> dict:
    return {
        "id": msg.id,
        "session_id": msg.session_id,
        "role": msg.role,
        "content": msg.content,
        "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
        "cost": msg.cost,
        "context_used": msg.context_used,
        "context_max": msg.context_max,
        "cache_read_tokens": msg.cache_read_tokens,
        "tool_calls": json.loads(msg.tool_calls) if msg.tool_calls else None,
        "execution_time_ms": msg.execution_time_ms,
        "claude_session_id": msg.claude_session_id,
        "compact_metadata": (
            json.loads(msg.compact_metadata) if msg.compact_metadata else None
        ),
    }


# ---------------------------------------------------------------------------
# Resumable-turn engine — moved to services/session_turn_service.py (ent#358)
# ---------------------------------------------------------------------------

# The Redis resume lock, the in-flight sentinel, the runtime capability gate and
# the resume-not-found detection all used to live here as module privates. They
# now back BOTH continuous-conversation surfaces (this one and Workspace chat),
# so they moved to a service — Invariant #1, and one engine cannot drift from
# itself. The private aliases are kept: this module's behaviour is unchanged,
# and the tests that pin these names keep pinning the same objects.
_LOCK_TTL_FALLBACK = LOCK_TTL_FALLBACK
_LOCK_WAIT_TOTAL_SECONDS = LOCK_WAIT_TOTAL_SECONDS
_LOCK_POLL_INTERVAL_SECONDS = LOCK_POLL_INTERVAL_SECONDS
_LOCK_RELEASE_LUA = LOCK_RELEASE_LUA
_session_lock_key = session_lock_key
_session_inflight_key = session_inflight_key
_resolve_lock_ttl = resolve_lock_ttl
_get_async_redis = get_async_redis
_set_session_inflight = set_session_inflight
_clear_session_inflight = clear_session_inflight
_is_turn_in_flight = is_turn_in_flight
_ResumeLock = ResumeLock
_InflightSentinel = InflightSentinel
_is_resume_not_found = is_resume_not_found
_supports_session_tab_resume = supports_session_resume
_RESUME_NOT_FOUND_MARKERS = RESUME_NOT_FOUND_MARKERS



# ---------------------------------------------------------------------------
# Endpoints — read paths
# ---------------------------------------------------------------------------


@router.post("/{name}/session")
async def create_session(
    name: AuthorizedAgent,
    body: Optional[CreateSessionRequest] = Body(default=None),
    current_user: User = Depends(get_current_user),
):
    """Create a brand-new session row for the current user.

    The first turn against the returned id will be a cold turn (no cached
    Claude UUID), but ``persist_session=True`` ensures the JSONL is written
    so turn 2 can resume.
    """
    _enabled_or_404()

    subscription_id = body.subscription_id if body else None
    if subscription_id is None:
        try:
            subscription_id = db.get_agent_subscription_id(name)
        except Exception:
            subscription_id = None

    session = db.create_session(
        agent_name=name,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        subscription_id=subscription_id,
    )
    return _serialize_session(session)


@router.get("/{name}/sessions")
async def list_sessions(
    name: AuthorizedAgent,
    current_user: User = Depends(get_current_user),
    status: Optional[str] = Query(default=None),
):
    """List the caller's sessions on this agent, newest first.

    Per E6 in the plan: scoped to the current user — even owners cannot see
    other users' sessions. Pass ``status=active`` to filter.
    """
    _enabled_or_404()
    sessions = db.list_sessions(agent_name=name, user_id=current_user.id, status=status)
    return [_serialize_session(s) for s in sessions]


@router.get("/{name}/sessions/{session_id}")
async def get_session_with_messages(
    name: AuthorizedAgent,
    session_id: str,
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Return a single session row plus its most-recent ``limit`` messages.

    The session row carries ``turn_in_progress`` derived from the Redis
    in-flight sentinel so the UI's onActivated re-sync (Issue #759) can
    detect a still-running turn after a navigation away. Pair with
    ``message_count`` to detect the in-progress → done transition: the
    DEL of the sentinel races the INSERT of the assistant message, so
    clients should treat ``(turn_in_progress=true ∧ message_count >
    last_seen)`` as "completed, lock draining" and stop polling.
    """
    _enabled_or_404()
    session = _session_or_404(session_id, current_user, name)
    messages = db.get_session_messages(session_id, limit=limit)
    turn_in_progress = await _is_turn_in_flight(session_id)
    return {
        "session": _serialize_session(session, turn_in_progress=turn_in_progress),
        "messages": [_serialize_message(m) for m in messages],
    }


# ---------------------------------------------------------------------------
# Endpoints — the turn (Phase 2.1 / 2.2 / 2.3)
# ---------------------------------------------------------------------------


@router.post("/{name}/sessions/{session_id}/message")
async def send_session_message(
    name: AuthorizedAgent,
    session_id: str,
    body: SessionMessageRequest,
    current_user: User = Depends(get_current_user),
):
    """The turn endpoint.

    Pipeline:
      1. Resolve session and check ownership.
      2. Persist the user message immediately so it appears even on failure
         (mirrors the chat router pattern; E1 visibility).
      3. Acquire per-(agent, uuid) Redis lock if a cached Claude UUID exists
         (Phase 2.3, Anthropic #20992 mitigation). Cold turns skip the lock.
      4. Call ``execute_task(persist_session=True, resume_session_id=cached)``.
         The persist flag is unconditional — even cold turns must write the
         JSONL so turn 2's resume succeeds (L2 defense).
      5. If the agent reports "no conversation found" on a resume turn,
         clear the cached UUID, ``mark_resume_failure``, and retry once
         with ``resume_session_id=None`` (Phase 2.2, E2/E3).
      6. On success, ``update_cached_claude_session_id`` with the real UUID
         from ``result.session_id`` (now correct since Phase 1.3 parser fix
         — no execution_log scan needed). Reset the failure counter.
      7. Persist the assistant message with cost/context/tool_calls and the
         per-message ``claude_session_id`` audit field.
    """
    _enabled_or_404()
    session = _session_or_404(session_id, current_user, name)

    user_email = current_user.email or current_user.username

    # Phase 5.2 — file uploads. Mirror routers/chat.py's pattern: decode
    # the base64 payloads, write non-images into the agent workspace via
    # process_file_uploads (which uses Docker put_archive), and pass any
    # decoded image bytes to execute_task as `images=` so they become
    # vision blocks on the next API call. The "[File uploaded by X]:
    # name (size) saved to path" line is appended to the prompt so the
    # agent has a textual reference even for non-image uploads.
    image_data: list = []
    effective_message = body.message
    if body.files:
        container = get_agent_container(name)
        if not container:
            raise HTTPException(status_code=503, detail="Agent not found")
        raw_files = []
        for i, f in enumerate(body.files):
            if not isinstance(f, dict):
                continue
            try:
                raw_files.append({
                    "name": f.get("name"),
                    "mimetype": f.get("mimetype"),
                    "size": f.get("size"),
                    "data": decode_web_file(f),
                    "id": f"f{i}",
                })
            except Exception as e:
                logger.warning("[Session] file %s decode failed: %s", f.get("name"), e)
        if raw_files:
            file_descs, _upload_dir, all_writes_failed, image_data = await process_file_uploads(
                raw_files=raw_files,
                agent_name=name,
                container=container,
                session_id=session.id,
                uploader=user_email,
                source="web",
                max_files=WEB_MAX_FILES,
                max_file_size=WEB_MAX_FILE_SIZE,
                max_image_size=WEB_MAX_IMAGE_SIZE,
                max_total_image_size=WEB_MAX_TOTAL_IMAGE_SIZE,
            )
            if all_writes_failed:
                raise HTTPException(
                    status_code=502,
                    detail="File upload failed: could not write to agent workspace.",
                )
            if file_descs:
                effective_message = f"{body.message}\n\n" + "\n".join(file_descs)

    # Step 2: persist the user message up front. If everything below fails
    # the message log still reflects what the user typed (vs. a silent loss).
    # Persist the ORIGINAL user message (without the file_descs append) so
    # the visible chat log reads naturally. The agent sees effective_message
    # which has the file references inline.
    db.add_session_message(SessionMessageInsert(
        session_id=session.id,
        agent_name=name,
        user_id=current_user.id,
        user_email=user_email,
        role="user",
        content=body.message,
    ))

    # In-flight sentinel brackets the turn so GET sessions/{id} can report
    # `turn_in_progress=true` to the UI's onActivated re-sync (Issue #759).
    # TTL = per-agent execution timeout + 30s buffer (capped at 7230s) so
    # very long turns don't drop the sentinel before completing.
    lock_ttl = _resolve_lock_ttl(name)
    async with _InflightSentinel(session.id, lock_ttl):
        cached_uuid = db.get_cached_claude_session_id(session.id)

        # Steps 3–5 (runtime gate, resume lock, cold-retry fallback) are the
        # shared engine — see services/session_turn_service.py. This surface
        # owns only its own bookkeeping: clearing the stale cache and counting
        # the failure, inside the lock, before the retry runs.
        def _on_resume_failure() -> None:
            db.clear_cached_claude_session_id(session.id)
            failure_count = db.mark_resume_failure(session.id)
            logger.warning(
                "[Session] event=session_resume_fallback agent=%s session=%s "
                "stale_uuid=%s consecutive_failures=%d reason=%s",
                name,
                session.id,
                cached_uuid,
                failure_count,
                "resume_jsonl_not_found",
            )

        # `effective_message` + `image_data` are reused on the cold retry so any
        # uploaded files (already written to the workspace before the first
        # attempt) are still referenced in the prompt + sent as vision blocks.
        turn = await run_resumable_turn(
            agent_name=name,
            session_key=session.id,
            message=effective_message,
            cached_uuid=cached_uuid,
            triggered_by="session",
            lock_ttl=lock_ttl,
            on_resume_failure=_on_resume_failure,
            source_user_id=current_user.id,
            source_user_email=user_email,
            model=body.model,
            timeout_seconds=body.timeout_seconds,
            subscription_id=session.subscription_id,
            images=image_data or None,
        )
        result = turn.result
        fallback_fired = turn.fallback_fired
        fallback_reason = turn.fallback_reason

        if result.status != "success":
            # Bubble execute_task's classified error up. The user message is
            # already persisted; we don't insert an empty assistant row.
            raise HTTPException(
                status_code=502,
                detail={
                    "error": result.error or "Agent execution failed",
                    "execution_id": result.execution_id,
                    "fallback_fired": fallback_fired,
                    "fallback_reason": fallback_reason,
                },
            )

        # Step 6: cache the real Claude UUID. Phase 1.3 fixed the parser so
        # result.session_id is now trustworthy — no execution_log scan.
        real_uuid = result.session_id
        if real_uuid and real_uuid != cached_uuid:
            db.update_cached_claude_session_id(session.id, real_uuid)
        if real_uuid:
            db.mark_resume_success(session.id)

        # Step 7: persist the assistant message. cache_read_tokens is captured
        # from the agent metadata when present (Phase 4.1 wires the column to
        # the dashboard; storage starts now so we have backfill).
        metadata = result.raw_response.get("metadata", {}) if result.raw_response else {}
        cache_read_tokens = metadata.get("cache_read_input_tokens") or metadata.get(
            "cache_read_tokens"
        )

        # Auto-compact events captured by the agent server's stream parser
        # (Bundle B). Mirrors the JSON the task_execution_service writes to
        # schedule_executions; lands on agent_session_messages.compact_metadata
        # plus increments agent_sessions.compact_count for the inline reset hint.
        compact_events = metadata.get("compact_events") or []
        compact_metadata_json = json.dumps(compact_events) if compact_events else None

        assistant_msg = db.add_session_message(SessionMessageInsert(
            session_id=session.id,
            agent_name=name,
            user_id=current_user.id,
            user_email=user_email,
            role="assistant",
            content=result.response or "",
            cost=result.cost,
            context_used=result.context_used,
            context_max=result.context_max,
            cache_read_tokens=cache_read_tokens,
            tool_calls=result.execution_log,
            execution_time_ms=None,
            claude_session_id=real_uuid,
            compact_metadata=compact_metadata_json,
            compact_event_count=len(compact_events),
        ))

        # Refresh the session row so the response reflects the post-turn stats.
        refreshed = db.get_session(session.id)

        return {
            "session": _serialize_session(refreshed) if refreshed else _serialize_session(session),
            "message": _serialize_message(assistant_msg),
            "response": result.response or "",
            "claude_session_id": real_uuid,
            "execution_id": result.execution_id,
            "fallback_fired": fallback_fired,
            "fallback_reason": fallback_reason,
            "cost": result.cost,
            "context_used": result.context_used,
            "context_max": result.context_max,
            "cache_read_tokens": cache_read_tokens,
            "compact_events": compact_events,
        }


# ---------------------------------------------------------------------------
# Endpoints — lifecycle (reset / delete)
# ---------------------------------------------------------------------------


@router.post("/{name}/sessions/{session_id}/reset")
async def reset_session_memory(
    name: AuthorizedAgent,
    session_id: str,
    current_user: User = Depends(get_current_user),
):
    """Reset memory: clear cached UUID. Message history stays visible.

    The next turn becomes a cold turn — a new JSONL will be written under
    a fresh UUID. The orphaned old JSONL is reaped by the periodic cleanup
    service (Phase 4.2). We don't reach into the agent container synchronously
    here; doing so would require an agent-server endpoint that lives in the
    same Phase 4 batch.
    """
    _enabled_or_404()
    session = _session_or_404(session_id, current_user, name)
    prior_uuid = session.cached_claude_session_id
    db.clear_cached_claude_session_id(session.id)
    logger.info(
        "[Session] event=session_reset agent=%s session=%s prior_uuid=%s",
        name,
        session.id,
        prior_uuid,
    )
    # Phase 4.2: best-effort synchronous JSONL reap so the user-perceived
    # latency between "Reset memory" and the actual disk reclaim is small.
    # Periodic sweep catches anything we miss here. Never raises.
    if prior_uuid:
        await get_session_cleanup_service().reap_jsonl(name, prior_uuid)
    refreshed = db.get_session(session.id)
    return _serialize_session(refreshed) if refreshed else _serialize_session(session)


@router.delete("/{name}/sessions/{session_id}")
async def delete_session(
    name: AuthorizedAgent,
    session_id: str,
    current_user: User = Depends(get_current_user),
):
    """Delete the session row + its messages. JSONL reaped by Phase 4.2."""
    _enabled_or_404()
    session = _session_or_404(session_id, current_user, name)
    prior_uuid = session.cached_claude_session_id
    deleted = db.delete_session(session.id)
    logger.info(
        "[Session] event=session_delete agent=%s session=%s prior_uuid=%s success=%s",
        name,
        session.id,
        prior_uuid,
        deleted,
    )
    # Phase 4.2: same best-effort reap as reset (the JSONL is now orphaned).
    if prior_uuid:
        await get_session_cleanup_service().reap_jsonl(name, prior_uuid)
    return {"deleted": bool(deleted), "session_id": session.id}
