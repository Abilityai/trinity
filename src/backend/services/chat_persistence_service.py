"""
Authenticated chat-session persistence for the ``/task`` endpoint (#1444).

Extracted from ``routers/chat.py`` (#1483, Invariant #1 enforcement) — routers
hold no business logic. This service owns the DB writes to
``chat_sessions``/``chat_messages`` for a completed ``/task`` execution and the
``chat_response_ready`` WebSocket broadcast; it holds no HTTP concern.

Behavior is preserved byte-for-byte from the former router helpers:
  * ``persist_chat_session`` (was ``_persist_chat_session``): SUCCESS-guard,
    the #1444 IDOR owner-check that falls through to the caller's own session,
    and the fail-loud-but-non-fatal ``logger.error(exc_info=True)`` that carries
    ONLY agent name + execution_id + exception type (never user content) and
    does NOT re-raise (the turn is already billed).
  * ``persist_and_broadcast_chat_session`` (was ``_persist_and_broadcast_chat_session``).

The ``hide_parameters=True`` engine flag in ``db/engine.py`` (which keeps a
DB-error traceback from leaking bound values) is unchanged.
"""
import json
import logging
from typing import Optional

from models import ParallelTaskRequest, TaskExecutionStatus
from database import db
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# WebSocket manager (injected from main.py via set_websocket_manager). Kept as a
# per-service global (the established activity_service/report_service pattern) so
# a missing setter is a silent no-op, not an import error.
_websocket_manager = None


def set_websocket_manager(manager):
    """Set the WebSocket manager for the chat_response_ready broadcast."""
    global _websocket_manager
    _websocket_manager = manager


async def persist_chat_session(
    agent_name: str,
    request: ParallelTaskRequest,
    result,  # TaskExecutionResult
    user_id: int,
    user_email: str,
    subscription_id: Optional[str] = None,
    execution_time_ms: Optional[int] = None,
):
    """
    Persist a /task execution to the authenticated chat session (THINK-001).

    Shared by the sync and async branches of execute_parallel_task. Only persists
    on SUCCESS — avoids writing empty assistant messages for FAILED/CANCELLED
    executions. Returns the session id (or None on failure).
    """
    if result.status != TaskExecutionStatus.SUCCESS:
        return None

    try:
        if request.create_new_session:
            session = db.create_new_chat_session(
                agent_name=agent_name,
                user_id=user_id,
                user_email=user_email,
                subscription_id=subscription_id,
            )
        elif request.chat_session_id:
            session = db.get_chat_session(request.chat_session_id)
            # Security F2 (#1444): a caller-supplied chat_session_id must belong
            # to this user. Without this gate a forged/leaked id would append
            # into another user's session (IDOR). On absence OR ownership
            # mismatch, fall through to the caller's own active session.
            if not session or session.user_id != user_id:
                session = db.get_or_create_chat_session(
                    agent_name=agent_name,
                    user_id=user_id,
                    user_email=user_email,
                )
        else:
            session = db.get_or_create_chat_session(
                agent_name=agent_name,
                user_id=user_id,
                user_email=user_email,
            )

        original_user_message = request.user_message or request.message
        db.add_chat_message(
            session_id=session.id,
            agent_name=agent_name,
            user_id=user_id,
            user_email=user_email,
            role="user",
            content=original_user_message,
        )
        db.add_chat_message(
            session_id=session.id,
            agent_name=agent_name,
            user_id=user_id,
            user_email=user_email,
            role="assistant",
            content=result.response or "",
            cost=result.cost,
            context_used=result.context_used,
            context_max=result.context_max,
            execution_time_ms=execution_time_ms,
        )
        logger.debug(f"[Task] Saved to chat session {session.id} for agent '{agent_name}'")
        return session.id
    except Exception as e:
        # Fail-loud (#1444): a swallowed error here silently loses the user's
        # Chat-tab history. Log at ERROR with a stack trace so the exact raise
        # site is named on the next run. Security F3: the message string carries
        # ONLY non-sensitive identifiers — agent name, execution_id, exception
        # type — never user_message, user_email, or result.response. We do NOT
        # re-raise: the execution is already complete and billed. The sync branch
        # surfaces a `chat_persist_failed` marker to the caller, and the async
        # wrapper's done-callback surfaces unhandled errors; persistence is also
        # asserted by tests, so silent drift is caught by tests, not only logs.
        logger.error(
            "[Task] Failed to persist chat session for agent '%s' "
            "(execution_id=%s, exc=%s)",
            agent_name,
            getattr(result, "execution_id", None),
            type(e).__name__,
            exc_info=True,
        )
        return None


async def persist_and_broadcast_chat_session(
    *, agent_name, request, result, execution_id, user_id, user_email,
    subscription_id, execution_time_ms,
):
    """Post-task block 1 (THINK-001): persist the authenticated chat session and
    broadcast chat_response_ready. Returns the chat_session_id (or None when
    persistence isn't applicable) — the caller threads it to signal_sync_waiter.

    The persist body is fail-loud but non-fatal (#1444): `persist_chat_session`
    logs a DB error at ERROR (stack trace, no user content) and returns None
    rather than propagating — so a dropped write never breaks the caller's
    finally / waiter signal. The broadcast is separately best-effort.
    """
    if not (request.save_to_session and user_id and user_email):
        return None
    chat_session_id = await persist_chat_session(
        agent_name=agent_name,
        request=request,
        result=result,
        user_id=user_id,
        user_email=user_email,
        subscription_id=subscription_id,
        execution_time_ms=execution_time_ms,
    )
    if chat_session_id and _websocket_manager:
        try:
            await _websocket_manager.broadcast(json.dumps({
                "type": "chat_response_ready",
                "execution_id": execution_id,
                "agent_name": agent_name,
                "chat_session_id": chat_session_id,
                "timestamp": utc_now_iso(),
            }))
        except Exception as e:
            logger.warning(f"[Task Async] chat_response_ready broadcast failed: {e}")
    return chat_session_id
