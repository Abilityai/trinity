"""
Session tab endpoints — `--resume`-default chat surface.

Phase 2 of docs/planning/SESSION_TAB_2026-04.md. Six endpoints that mirror
the structure of routers/chat.py (same auth model, same TaskExecutionService)
but persist to the parallel agent_sessions / agent_session_messages tables
and request `persist_session=True` so each turn reattaches via Claude Code's
`--resume` flag.

Surface gated on `services.settings_service.is_session_tab_enabled()`. When
the flag is off, every endpoint returns 404 — the route exists but the
feature is invisible. Default off until Phase 5 rollout.
"""

import asyncio
import json
import logging
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from database import db
from db_models import WebFileUpload, SessionMessageInsert
from dependencies import AuthorizedAgent, get_current_user
from models import CreateSessionRequest, SessionMessageRequest, User
from services.docker_service import get_agent_container, get_agent_status_from_container
from services.session_cleanup_service import get_session_cleanup_service
from services.settings_service import is_session_tab_enabled
from services.task_execution_service import get_task_execution_service
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


# #1187 Phase H: runtimes whose Session-tab turns must NOT use the cached-UUID
# `--resume` machinery. They support plain chat continuity (in the Chat tab) but
# not the Session tab's Claude-JSONL resume/fallback/reaping model, so we run a
# stateless turn for them instead. ONE backend constant (decision 1) — keep in
# sync with the agent-side `RuntimeCapabilities.session_tab_resume`.
RUNTIMES_WITHOUT_SESSION_TAB_RESUME = {"codex"}


def _supports_session_tab_resume(agent_name: str) -> bool:
    """False for runtimes (e.g. Codex) that lack the cached-UUID `--resume`
    machinery. Defaults to True on any lookup failure — assume Claude-like
    so a transient Docker hiccup never silently downgrades a real session."""
    try:
        container = get_agent_container(agent_name)
        if container is None:
            return True
        status = get_agent_status_from_container(container)
        runtime = (status.runtime or "claude-code").lower()
        return runtime not in RUNTIMES_WITHOUT_SESSION_TAB_RESUME
    except Exception:
        logger.warning(
            "[Session] runtime lookup failed for %s; assuming resume-capable",
            agent_name, exc_info=True,
        )
        return True


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
# Redis primitives — resume lock (#20992) + in-flight sentinel (#759)
# ---------------------------------------------------------------------------

# Two distinct Redis primitives gate session turns:
#
# 1. `session_lock:{agent}:{uuid}` — per-(agent, claude_uuid) lock that
#    serialises concurrent `claude --resume <same-uuid>` calls (Anthropic
#    #20992: concurrent resume calls corrupt the JSONL). Cold turns (no
#    cached UUID yet) skip this lock — there's no JSONL to corrupt.
#
# 2. `session_inflight:{session_id}` — per-session sentinel SET for the
#    duration of any turn (cold or warm). Drives the `turn_in_progress`
#    field on GET sessions/{id} so the UI can reattach on activation
#    (Issue #759). Distinct from the resume lock because the lock skips
#    cold turns, whereas the in-flight signal must cover them.
#
# TTL for both is dynamic: at acquire time we look up the per-agent
# `execution_timeout_seconds` (default 900) and add a 30s buffer, capped
# at 7230s. Stale-lock cleanup after a backend crash is bounded by this
# TTL (worst case ≈ 2h); admins can manually `DEL` if needed.

_LOCK_TTL_FALLBACK = 7230          # cap + default on lookup failure (≈ 2h)
_LOCK_WAIT_TOTAL_SECONDS = 30.0    # hard ceiling for chat UX
_LOCK_POLL_INTERVAL_SECONDS = 0.25

_LOCK_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


def _session_lock_key(agent_name: str, claude_session_id: str) -> str:
    """Canonical key for the per-(agent, uuid) resume lock.

    Extracted as a helper so the producer (``_ResumeLock``) and any
    future consumer that probes lock state share one source of truth. A
    typo here would be a silent split-brain — locks claimed against one
    key, probed against another.
    """
    return f"session_lock:{agent_name}:{claude_session_id}"


def _session_inflight_key(session_id: str) -> str:
    """Sentinel key set for the duration of any session turn.

    Distinct from the resume lock — that one serialises JSONL writes;
    this one signals "a turn is in flight on this session id" to the
    UI's onActivated re-sync (Issue #759). Covers cold turns, which the
    lock skips by design.
    """
    return f"session_inflight:{session_id}"


def _resolve_lock_ttl(agent_name: str) -> int:
    """Resolve a turn's TTL from the agent's per-agent execution timeout.

    Uses ``db.get_execution_timeout(agent_name)`` (default 900s) plus a
    30s buffer, capped at ``_LOCK_TTL_FALLBACK``. Falls back to the cap
    on any lookup failure — safer to over-TTL than under-TTL since both
    keys are auto-expiring strings, not state we care to keep precise.
    """
    try:
        timeout = db.get_execution_timeout(agent_name)
        return min(timeout + 30, _LOCK_TTL_FALLBACK)
    except Exception as e:
        logger.warning(
            "[Session] get_execution_timeout failed for %s (%s) — using fallback %ds",
            agent_name,
            e,
            _LOCK_TTL_FALLBACK,
        )
        return _LOCK_TTL_FALLBACK


def _get_async_redis():
    """Lazy async-Redis client. Returns None if unavailable."""
    try:
        import redis.asyncio as aioredis  # noqa: WPS433
        from config import REDIS_URL  # noqa: WPS433
    except Exception as e:
        logger.warning("[Session] Cannot import async Redis client: %s", e)
        return None
    try:
        return aioredis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        logger.warning("[Session] Cannot construct async Redis client: %s", e)
        return None


async def _set_session_inflight(session_id: str, ttl: int) -> None:
    """SET the in-flight sentinel for a session. Degrades silently."""
    redis = _get_async_redis()
    if redis is None:
        return
    try:
        await redis.set(_session_inflight_key(session_id), "1", ex=ttl)
    except Exception as e:
        logger.warning(
            "[Session] inflight SET failed for %s (%s) — degraded mode",
            session_id,
            e,
        )


async def _clear_session_inflight(session_id: str) -> None:
    """DEL the in-flight sentinel. Best-effort; TTL is the backstop."""
    redis = _get_async_redis()
    if redis is None:
        return
    try:
        await redis.delete(_session_inflight_key(session_id))
    except Exception as e:
        logger.warning(
            "[Session] inflight DEL failed for %s (%s) — TTL will expire",
            session_id,
            e,
        )


async def _is_turn_in_flight(session_id: str) -> bool:
    """Whether a turn is currently in flight on this session.

    Reads the sentinel set at the start of ``send_session_message``.
    Covers cold + warm turns. Returns False if Redis is unavailable
    (degraded mode — frontend should fall back to message_count delta to
    detect completion).
    """
    redis = _get_async_redis()
    if redis is None:
        return False
    try:
        return bool(await redis.exists(_session_inflight_key(session_id)))
    except Exception as e:
        logger.warning(
            "[Session] inflight EXISTS failed for %s (%s) — degraded",
            session_id,
            e,
        )
        return False


class _ResumeLock:
    """Async context manager for the per-session turn lock.

    Two key shapes:
      * **warm turn** — ``session_lock:{agent}:{claude_session_id}`` keyed by
        the cached Claude UUID (via ``_session_lock_key``).
      * **cold turn** — ``session_lock:cold:{session_id}`` keyed by the
        persisted session row id (#779). Cold turns previously short-circuited
        to ``key=None`` (no lock), allowing two concurrent first-turn POSTs to
        race on ``update_cached_claude_session_id`` and orphan a JSONL.

    Acts as a no-op when Redis is unavailable (degraded mode — log and
    proceed; lock contention is an optimisation, not a correctness gate at
    the platform layer).
    """

    def __init__(
        self,
        agent_name: str,
        claude_session_id: Optional[str],
        session_id: str,
        ttl_seconds: int = _LOCK_TTL_FALLBACK,
    ):
        self._key = (
            _session_lock_key(agent_name, claude_session_id)
            if claude_session_id
            else f"session_lock:cold:{session_id}"
        )
        self._ttl = ttl_seconds
        self._token = secrets.token_urlsafe(16)
        self._redis = None
        self._held = False

    async def __aenter__(self) -> "_ResumeLock":
        self._redis = _get_async_redis()
        if self._redis is None:
            logger.warning(
                "[Session] Redis unavailable for resume lock %s — proceeding unlocked",
                self._key,
            )
            return self

        deadline = asyncio.get_event_loop().time() + _LOCK_WAIT_TOTAL_SECONDS
        while True:
            try:
                acquired = await self._redis.set(
                    self._key,
                    self._token,
                    nx=True,
                    ex=self._ttl,
                )
            except Exception as e:
                logger.warning(
                    "[Session] Redis SET failed for %s (%s) — proceeding unlocked",
                    self._key,
                    e,
                )
                self._redis = None
                return self

            if acquired:
                self._held = True
                return self
            if asyncio.get_event_loop().time() >= deadline:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "Another turn on this session is in progress",
                        "retry_after": 5,
                        "session_lock_key": self._key,
                    },
                )
            await asyncio.sleep(_LOCK_POLL_INTERVAL_SECONDS)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self._held or self._redis is None:
            return
        try:
            await self._redis.eval(_LOCK_RELEASE_LUA, 1, self._key, self._token)
        except Exception as e:
            logger.warning(
                "[Session] Lock release failed for %s (%s) — TTL will expire it",
                self._key,
                e,
            )


class _InflightSentinel:
    """Async context manager that brackets a session turn with the sentinel.

    SET on enter, DEL on exit (success or exception). The DEL is
    guaranteed-best-effort: if Redis is down or the call fails, the TTL
    is the backstop. Used by the turn handler so the UI's onActivated
    re-sync (#759) sees an accurate ``turn_in_progress`` flag — including
    on cold turns that the resume lock skips by design.
    """

    def __init__(self, session_id: str, ttl_seconds: int):
        self._session_id = session_id
        self._ttl = ttl_seconds

    async def __aenter__(self) -> "_InflightSentinel":
        await _set_session_inflight(self._session_id, self._ttl)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await _clear_session_inflight(self._session_id)


# ---------------------------------------------------------------------------
# Resume-fallback detection (Phase 2.2)
# ---------------------------------------------------------------------------

# When `claude --resume <uuid>` cannot find the JSONL the CLI prints
# "No conversation found with session ID: ..." to stderr. The agent server
# bubbles that up as the 5xx body that lands in TaskExecutionResult.error.
# We match on the substring (case-insensitive) so a wording bump in a future
# Claude release still triggers the fallback path. E2/E3 in the design doc.
_RESUME_NOT_FOUND_MARKERS = (
    "no conversation found",
    "session not found",
)


def _is_resume_not_found(error_text: Optional[str]) -> bool:
    if not error_text:
        return False
    lowered = error_text.lower()
    return any(marker in lowered for marker in _RESUME_NOT_FOUND_MARKERS)


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

        # #1187 Phase H: for runtimes lacking the cached-UUID --resume model
        # (Codex), run a plain stateless turn — the Claude-specific resume +
        # JSONL-not-found fallback below would mis-handle their failure markers.
        # Only pay the runtime lookup when there's actually a cached UUID to drop.
        if cached_uuid and not _supports_session_tab_resume(name):
            logger.info(
                "[Session] agent=%s runtime lacks session-tab resume — "
                "running a stateless turn (no --resume)", name,
            )
            cached_uuid = None

        service = get_task_execution_service()
        fallback_fired = False
        fallback_reason: Optional[str] = None

        async with _ResumeLock(name, cached_uuid, session.id, ttl_seconds=lock_ttl):
            # Step 4: cold or resume turn.
            result = await service.execute_task(
                agent_name=name,
                message=effective_message,
                triggered_by="session",
                source_user_id=current_user.id,
                source_user_email=user_email,
                model=body.model,
                timeout_seconds=body.timeout_seconds,
                resume_session_id=cached_uuid,
                persist_session=True,
                subscription_id=session.subscription_id,
                images=image_data or None,
            )

            # Step 5: resume-failure fallback. Only triggers when we *had* a
            # cached UUID and execute_task came back failed with the marker.
            if (
                cached_uuid
                and result.status != "success"
                and _is_resume_not_found(result.error)
            ):
                fallback_fired = True
                fallback_reason = "resume_jsonl_not_found"
                db.clear_cached_claude_session_id(session.id)
                failure_count = db.mark_resume_failure(session.id)
                logger.warning(
                    "[Session] event=session_resume_fallback agent=%s session=%s "
                    "stale_uuid=%s consecutive_failures=%d reason=%s",
                    name,
                    session.id,
                    cached_uuid,
                    failure_count,
                    fallback_reason,
                )
                # Retry once cold. Lock is no longer relevant — the stale UUID
                # is gone, and the new cold turn will write a fresh JSONL with
                # a new UUID (no contention with anyone else by definition).
                # Use effective_message + image_data so any uploaded files
                # (already written to the workspace before the first attempt)
                # are still referenced in the prompt + sent as vision blocks.
                result = await service.execute_task(
                    agent_name=name,
                    message=effective_message,
                    triggered_by="session",
                    source_user_id=current_user.id,
                    source_user_email=user_email,
                    model=body.model,
                    timeout_seconds=body.timeout_seconds,
                    resume_session_id=None,
                    persist_session=True,
                    subscription_id=session.subscription_id,
                    images=image_data or None,
                )

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
