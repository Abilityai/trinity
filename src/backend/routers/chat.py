"""
Agent chat and activity routes for the Trinity backend.

Includes execution queue integration to prevent parallel execution on the same agent.
"""
from fastapi import APIRouter, Depends, HTTPException, Header
import httpx
import json
import logging
from datetime import datetime
from typing import Optional

from models import User, ChatMessageRequest, ModelChangeRequest, ParallelTaskRequest, ActivityType, ExecutionSource
from dependencies import get_current_user
from services.audit_service import log_audit_event
from services.docker_service import get_agent_container
from services.activity_service import activity_service
from services.execution_queue import get_execution_queue, QueueFullError, AgentBusyError
from database import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["chat"])

# WebSocket manager (injected from main.py)
_websocket_manager = None


def set_websocket_manager(manager):
    """Set WebSocket manager for broadcasting collaboration events."""
    global _websocket_manager
    _websocket_manager = manager


async def broadcast_collaboration_event(source_agent: str, target_agent: str, action: str = "chat"):
    """Broadcast agent collaboration event to all WebSocket clients."""
    if _websocket_manager:
        event = {
            "type": "agent_collaboration",
            "source_agent": source_agent,
            "target_agent": target_agent,
            "action": action,
            "timestamp": datetime.utcnow().isoformat()
        }
        await _websocket_manager.broadcast(json.dumps(event))
    else:
        print(f"[Warning] WebSocket manager not set, skipping collaboration broadcast")


@router.post("/{name}/chat")
async def chat_with_agent(
    name: str,
    request: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None)
):
    """
    Proxy chat messages to agent's internal web server and persist to database.

    This endpoint enforces single-execution-at-a-time via the execution queue.
    If the agent is busy, the request is queued (up to 3 waiting).
    If the queue is full, returns 429 Too Many Requests.
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    # Determine execution source
    if x_source_agent:
        source = ExecutionSource.AGENT
    else:
        source = ExecutionSource.USER

    # Create execution request and submit to queue
    queue = get_execution_queue()
    execution = queue.create_execution(
        agent_name=name,
        message=request.message,
        source=source,
        source_agent=x_source_agent,
        source_user_id=str(current_user.id),
        source_user_email=current_user.email or current_user.username
    )

    try:
        queue_result, execution = await queue.submit(execution, wait_if_busy=True)
        logger.info(f"[Chat] Agent '{name}' execution {execution.id}: {queue_result}")
    except QueueFullError as e:
        logger.warning(f"[Chat] Agent '{name}' queue full, rejecting request")
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Agent queue is full",
                "agent": name,
                "queue_length": e.queue_length,
                "retry_after": 30,
                "message": f"Agent '{name}' is busy with {e.queue_length} queued requests. Please try again later."
            }
        )

    # Track queue position for observability
    is_queued = queue_result.startswith("queued:")

    # Broadcast collaboration event if this is agent-to-agent communication
    collaboration_activity_id = None
    if x_source_agent:
        await broadcast_collaboration_event(
            source_agent=x_source_agent,
            target_agent=name,
            action="chat"
        )

        # Track agent collaboration activity
        collaboration_activity_id = await activity_service.track_activity(
            agent_name=x_source_agent,  # Activity belongs to source agent
            activity_type=ActivityType.AGENT_COLLABORATION,
            user_id=current_user.id,
            triggered_by="agent",
            details={
                "source_agent": x_source_agent,
                "target_agent": name,
                "action": "chat",
                "message_preview": request.message[:100],
                "execution_id": execution.id,
                "queue_status": queue_result
            }
        )

    # Get or create chat session for this user+agent
    session = db.get_or_create_chat_session(
        agent_name=name,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username
    )

    # Track chat start activity
    chat_activity_id = await activity_service.track_activity(
        agent_name=name,
        activity_type=ActivityType.CHAT_START,
        user_id=current_user.id,
        triggered_by="agent" if x_source_agent else "user",
        parent_activity_id=collaboration_activity_id,  # Link to collaboration if agent-initiated
        details={
            "message_preview": request.message[:100],
            "source_agent": x_source_agent,
            "execution_id": execution.id,
            "queue_status": queue_result
        }
    )

    # Log user message to database
    user_message = db.add_chat_message(
        session_id=session.id,
        agent_name=name,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        role="user",
        content=request.message
    )

    execution_success = False
    try:
        payload = {"message": request.message, "stream": False}
        if request.model:
            payload["model"] = request.model

        start_time = datetime.utcnow()

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://agent-{name}:8000/api/chat",
                json=payload,
                timeout=300.0  # Increased timeout for queued execution
            )
            response.raise_for_status()

            response_data = response.json()

            # Extract metadata for persistence
            execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            metadata = response_data.get("metadata", {})
            session_data = response_data.get("session", {})

            # Serialize tool calls if present
            execution_log = response_data.get("execution_log", [])
            tool_calls_json = json.dumps(execution_log) if execution_log else None

            # Log assistant response to database with observability data
            assistant_message = db.add_chat_message(
                session_id=session.id,
                agent_name=name,
                user_id=current_user.id,
                user_email=current_user.email or current_user.username,
                role="assistant",
                content=response_data.get("response", ""),
                cost=metadata.get("cost_usd"),
                context_used=session_data.get("context_tokens"),
                context_max=session_data.get("context_window"),
                tool_calls=tool_calls_json,
                execution_time_ms=execution_time_ms
            )

            # Track tool calls (granular tracking in agent_activities)
            for tool_call in execution_log:
                await activity_service.track_activity(
                    agent_name=name,
                    activity_type=ActivityType.TOOL_CALL,
                    user_id=current_user.id,
                    triggered_by="agent" if x_source_agent else "user",
                    parent_activity_id=chat_activity_id,
                    related_chat_message_id=assistant_message.id,
                    details={
                        "tool_name": tool_call.get("tool", "unknown"),
                        "duration_ms": tool_call.get("duration_ms"),
                        "success": tool_call.get("success", True)
                    }
                )

            # Track chat completion
            await activity_service.complete_activity(
                activity_id=chat_activity_id,
                status="completed",
                details={
                    "related_chat_message_id": assistant_message.id,
                    "context_used": session_data.get("context_tokens"),
                    "context_max": session_data.get("context_window"),
                    "cost_usd": metadata.get("cost_usd"),
                    "execution_time_ms": execution_time_ms,
                    "tool_count": len(execution_log),
                    "execution_id": execution.id
                }
            )

            # Complete collaboration activity if this was agent-to-agent
            if collaboration_activity_id:
                await activity_service.complete_activity(
                    activity_id=collaboration_activity_id,
                    status="completed",
                    details={
                        "related_chat_message_id": assistant_message.id,
                        "response_length": len(response_data.get("response", "")),
                        "execution_time_ms": execution_time_ms,
                        "execution_id": execution.id
                    }
                )

            await log_audit_event(
                event_type="agent_interaction",
                action="chat",
                user_id=current_user.username,
                agent_name=name,
                resource=f"agent-{name}",
                result="success"
            )

            execution_success = True

            # Add execution metadata to response
            response_data["execution"] = {
                "id": execution.id,
                "queue_status": queue_result,
                "was_queued": is_queued
            }

            return response_data
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to communicate with agent {name}: {e}")

        # Track chat failure
        await activity_service.complete_activity(
            activity_id=chat_activity_id,
            status="failed",
            error=type(e).__name__  # Don't expose full error message
        )

        await log_audit_event(
            event_type="agent_interaction",
            action="chat",
            user_id=current_user.username,
            agent_name=name,
            resource=f"agent-{name}",
            result="failed",
            severity="error",
            details={"error_type": type(e).__name__, "execution_id": execution.id}
        )
        raise HTTPException(
            status_code=503,
            detail="Failed to communicate with agent. The agent may be starting up or busy."
        )
    finally:
        # Always release the queue slot when done
        await queue.complete(name, success=execution_success)


@router.post("/{name}/task")
async def execute_parallel_task(
    name: str,
    request: ParallelTaskRequest,
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None)
):
    """
    Execute a stateless task in parallel mode (no conversation context).

    Unlike /chat, this endpoint:
    - Does NOT use execution queue (parallel allowed)
    - Does NOT use --continue flag (stateless)
    - Each call is independent and can run concurrently

    Use this for:
    - Agent delegation from orchestrators
    - Batch processing without context pollution
    - Parallel task execution

    Note: Does NOT update conversation history or session state.
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    # Determine execution source for logging
    if x_source_agent:
        source = ExecutionSource.AGENT
    else:
        source = ExecutionSource.USER

    # Track parallel task activity
    task_activity_id = await activity_service.track_activity(
        agent_name=name,
        activity_type=ActivityType.CHAT_START,  # Reusing CHAT_START for now
        user_id=current_user.id,
        triggered_by="agent" if x_source_agent else "user",
        details={
            "message_preview": request.message[:100],
            "source_agent": x_source_agent,
            "parallel_mode": True,
            "model": request.model,
            "timeout_seconds": request.timeout_seconds
        }
    )

    # Broadcast collaboration event if this is agent-to-agent communication
    if x_source_agent:
        await broadcast_collaboration_event(
            source_agent=x_source_agent,
            target_agent=name,
            action="parallel_task"
        )

    try:
        # Build payload for agent's /api/task endpoint
        payload = {
            "message": request.message,
            "model": request.model,
            "allowed_tools": request.allowed_tools,
            "system_prompt": request.system_prompt,
            "timeout_seconds": request.timeout_seconds
        }

        start_time = datetime.utcnow()

        # Call agent's /api/task endpoint directly (no execution queue)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://agent-{name}:8000/api/task",
                json=payload,
                timeout=float(request.timeout_seconds or 300) + 10  # Add buffer to agent timeout
            )
            response.raise_for_status()

            response_data = response.json()

            execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            metadata = response_data.get("metadata", {})

            # Track task completion
            await activity_service.complete_activity(
                activity_id=task_activity_id,
                status="completed",
                details={
                    "session_id": response_data.get("session_id"),
                    "cost_usd": metadata.get("cost_usd"),
                    "execution_time_ms": execution_time_ms,
                    "tool_count": len(response_data.get("execution_log", [])),
                    "parallel_mode": True
                }
            )

            await log_audit_event(
                event_type="agent_interaction",
                action="parallel_task",
                user_id=current_user.username,
                agent_name=name,
                resource=f"agent-{name}",
                result="success",
                details={
                    "source": source.value,
                    "source_agent": x_source_agent,
                    "session_id": response_data.get("session_id")
                }
            )

            return response_data

    except httpx.TimeoutException:
        await activity_service.complete_activity(
            activity_id=task_activity_id,
            status="failed",
            error="Task execution timed out"
        )

        await log_audit_event(
            event_type="agent_interaction",
            action="parallel_task",
            user_id=current_user.username,
            agent_name=name,
            resource=f"agent-{name}",
            result="failed",
            severity="error",
            details={"error": "timeout", "source_agent": x_source_agent}
        )
        raise HTTPException(
            status_code=504,
            detail=f"Task execution timed out after {request.timeout_seconds} seconds"
        )

    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to execute parallel task on {name}: {e}")

        await activity_service.complete_activity(
            activity_id=task_activity_id,
            status="failed",
            error=type(e).__name__
        )

        await log_audit_event(
            event_type="agent_interaction",
            action="parallel_task",
            user_id=current_user.username,
            agent_name=name,
            resource=f"agent-{name}",
            result="failed",
            severity="error",
            details={"error_type": type(e).__name__, "source_agent": x_source_agent}
        )
        raise HTTPException(
            status_code=503,
            detail="Failed to execute task. The agent may be unavailable."
        )


@router.get("/{name}/chat/history")
async def get_agent_chat_history(
    name: str,
    current_user: User = Depends(get_current_user)
):
    """Get agent's conversation history."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=503,
            detail="Agent UI not enabled for this agent"
        )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/chat/history",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to get chat history for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to get chat history"
        )


@router.delete("/{name}/chat/history")
async def reset_agent_chat_history(
    name: str,
    current_user: User = Depends(get_current_user)
):
    """Reset/clear agent's conversation history (start a new session)."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=503,
            detail="Agent is not running"
        )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"http://agent-{name}:8000/api/chat/history",
                timeout=10.0
            )
            # Agent may not implement this endpoint yet
            if response.status_code == 405:
                # Clear activity instead as a fallback
                await client.delete(
                    f"http://agent-{name}:8000/api/activity",
                    timeout=10.0
                )
                return {"status": "reset", "message": "Session activity cleared"}
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to reset chat history for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to reset chat history"
        )


@router.get("/{name}/chat/session")
async def get_agent_chat_session(
    name: str,
    current_user: User = Depends(get_current_user)
):
    """Get agent's current session info including context usage."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=400,
            detail="Agent is not running"
        )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/chat/session",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to get session info for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to get session info"
        )


# Activity Monitoring Routes

@router.get("/{name}/activity")
async def get_agent_activity(
    name: str,
    current_user: User = Depends(get_current_user)
):
    """Get session activity for real-time monitoring."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        return {
            "status": "idle",
            "active_tool": None,
            "tool_counts": {},
            "timeline": [],
            "totals": {
                "calls": 0,
                "duration_ms": 0,
                "started_at": None
            }
        }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/activity",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        return {
            "status": "idle",
            "active_tool": None,
            "tool_counts": {},
            "timeline": [],
            "totals": {
                "calls": 0,
                "duration_ms": 0,
                "started_at": None
            }
        }


@router.get("/{name}/activity/{tool_id}")
async def get_agent_activity_detail(
    name: str,
    tool_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get full details for a specific tool call."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=400,
            detail="Agent is not running"
        )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/activity/{tool_id}",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to get activity detail for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to get activity detail"
        )


@router.delete("/{name}/activity")
async def clear_agent_activity(
    name: str,
    current_user: User = Depends(get_current_user)
):
    """Clear session activity (called when starting a new session)."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        return {
            "status": "cleared",
            "message": "Agent is not running - nothing to clear"
        }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"http://agent-{name}:8000/api/activity",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to clear activity for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to clear activity"
        )


# Model Routes

@router.get("/{name}/model")
async def get_agent_model(
    name: str,
    current_user: User = Depends(get_current_user)
):
    """Get agent's current model configuration."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=400,
            detail="Agent is not running"
        )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/model",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to get model info for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to get model info"
        )


@router.put("/{name}/model")
async def set_agent_model(
    name: str,
    request: ModelChangeRequest,
    current_user: User = Depends(get_current_user)
):
    """Set agent's model for subsequent messages."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=400,
            detail="Agent is not running"
        )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"http://agent-{name}:8000/api/model",
                json={"model": request.model},
                timeout=10.0
            )
            response.raise_for_status()

            await log_audit_event(
                event_type="agent_interaction",
                action="model_change",
                user_id=current_user.username,
                agent_name=name,
                resource=f"agent-{name}",
                result="success",
                details={"model": request.model}
            )

            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to set model for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to set model"
        )


# Persistent Chat History Routes

@router.get("/{name}/chat/history/persistent")
async def get_persistent_chat_history(
    name: str,
    limit: int = 100,
    user_filter: bool = False,
    current_user: User = Depends(get_current_user)
):
    """
    Get persistent chat history from database.

    This returns messages across all sessions, persisted in the database.
    Unlike /chat/history which returns only the current container session.

    Parameters:
    - limit: Maximum number of messages to return (default 100)
    - user_filter: If true, only show current user's messages (default false, shows all users for agent owners)
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Determine if user should see all messages or just their own
    # Agent owners can see all messages, others only see their own
    user_id_filter = None
    if user_filter or current_user.role != "admin":
        # For non-admins, always filter to their own messages unless they're the owner
        # (Owner check would require checking agent_ownership table)
        user_id_filter = current_user.id

    messages = db.get_agent_chat_history(
        agent_name=name,
        user_id=user_id_filter,
        limit=limit
    )

    return {
        "agent_name": name,
        "message_count": len(messages),
        "messages": [msg.model_dump() for msg in messages]
    }


@router.get("/{name}/chat/sessions")
async def get_agent_chat_sessions(
    name: str,
    status: str = None,
    current_user: User = Depends(get_current_user)
):
    """
    Get all chat sessions for an agent.

    Returns session metadata including message counts, costs, and timestamps.
    Non-admin users only see their own sessions.

    Parameters:
    - status: Filter by session status ('active' or 'closed')
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Non-admins only see their own sessions
    user_id_filter = None if current_user.role == "admin" else current_user.id

    sessions = db.get_agent_chat_sessions(
        agent_name=name,
        user_id=user_id_filter,
        status=status
    )

    return {
        "agent_name": name,
        "session_count": len(sessions),
        "sessions": [session.model_dump() for session in sessions]
    }


@router.get("/{name}/chat/sessions/{session_id}")
async def get_chat_session_detail(
    name: str,
    session_id: str,
    limit: int = 100,
    current_user: User = Depends(get_current_user)
):
    """
    Get detailed information about a specific chat session, including all messages.

    Parameters:
    - limit: Maximum number of messages to return (default 100)
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    session = db.get_chat_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")

    # Verify session belongs to this agent
    if session.agent_name != name:
        raise HTTPException(status_code=403, detail="Session does not belong to this agent")

    # Non-admins can only see their own sessions
    if current_user.role != "admin" and session.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You don't have access to this session")

    messages = db.get_chat_messages(session_id, limit=limit)

    return {
        "session": session.model_dump(),
        "message_count": len(messages),
        "messages": [msg.model_dump() for msg in messages]
    }


@router.post("/{name}/chat/sessions/{session_id}/close")
async def close_chat_session(
    name: str,
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    """Close a chat session (marks it as closed but keeps the history)."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    session = db.get_chat_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")

    # Verify session belongs to this agent and user
    if session.agent_name != name:
        raise HTTPException(status_code=403, detail="Session does not belong to this agent")

    if current_user.role != "admin" and session.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You don't have access to this session")

    success = db.close_chat_session(session_id)

    if success:
        await log_audit_event(
            event_type="agent_interaction",
            action="close_chat_session",
            user_id=current_user.username,
            agent_name=name,
            resource=f"session-{session_id}",
            result="success"
        )
        return {"status": "closed", "session_id": session_id}
    else:
        raise HTTPException(status_code=500, detail="Failed to close session")
