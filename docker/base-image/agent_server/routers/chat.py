"""
Chat endpoints for the agent server.

Now supports multiple runtimes (Claude Code, Gemini CLI) via runtime adapter.
"""
import json
import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import StreamingResponse

from ..models import ChatRequest, ModelRequest, ParallelTaskRequest
from ..state import agent_state
from ..services.claude_code import get_execution_lock
from ..services.runtime_adapter import get_runtime
from ..services.process_registry import get_process_registry

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/chat")
async def chat(request: ChatRequest):
    """
    Send a message to Claude Code and get response with execution log.

    This endpoint uses an asyncio lock to ensure only one execution happens
    at a time. This is a safety net - the platform-level execution queue
    should prevent parallel requests, but this provides defense-in-depth.
    """
    # Acquire execution lock - only one execution at a time
    # The platform-level queue should prevent this, but this is a safety net
    async with get_execution_lock():
        logger.info(f"[Chat] Execution lock acquired for message: {request.message[:50]}...")

        # Add user message to history
        agent_state.add_message("user", request.message)

        # Execute via runtime adapter (supports Claude Code or Gemini CLI)
        runtime = get_runtime()
        # Use request.model if provided, otherwise use the model set via /api/model endpoint
        effective_model = request.model or agent_state.current_model
        response_text, execution_log, metadata, raw_messages = await runtime.execute(
            prompt=request.message,
            model=effective_model,
            continue_session=True,
            stream=request.stream,
            system_prompt=request.system_prompt,
            execution_id=request.execution_id
        )

        # Add assistant response to history
        agent_state.add_message("assistant", response_text)

        # Update session-level stats
        if metadata.cost_usd:
            agent_state.session_total_cost += metadata.cost_usd
        agent_state.session_total_output_tokens += metadata.output_tokens
        # Context window usage: metadata.input_tokens should contain the complete total
        # (from modelUsage.inputTokens which includes all turns and cached tokens)
        # However, with --continue flag, Claude Code may sometimes report only new tokens
        # Fix: Context should monotonically increase during a session, so keep the max
        if metadata.input_tokens > agent_state.session_context_tokens:
            agent_state.session_context_tokens = metadata.input_tokens
            logger.debug(f"Context updated to {metadata.input_tokens} tokens")
        elif metadata.input_tokens > 0 and metadata.input_tokens < agent_state.session_context_tokens:
            # Claude reported fewer tokens than before - likely only new input, not cumulative
            # Keep the previous (higher) value as context should only grow
            logger.warning(
                f"Context tokens decreased from {agent_state.session_context_tokens} to {metadata.input_tokens}. "
                f"Keeping previous value (likely --continue reporting issue)"
            )
        agent_state.session_context_window = metadata.context_window

        logger.info(f"[Chat] Execution lock releasing after completion")

        # Return enhanced response with execution log and session stats
        # Use raw_messages (full Claude Code JSON transcript) for execution log viewer compatibility
        # Also include simplified execution_log for backward compatibility with activity tracking
        return {
            "response": response_text,
            "execution_log": raw_messages,  # Full Claude Code stream-json format for UI
            "execution_log_simplified": [entry.model_dump() for entry in execution_log],  # For activity tracking
            "metadata": metadata.model_dump(),
            "session": {
                "total_cost_usd": agent_state.session_total_cost,
                "context_tokens": agent_state.session_context_tokens,
                "context_window": agent_state.session_context_window,
                "message_count": len(agent_state.conversation_history),
                "model": agent_state.current_model
            },
            "timestamp": datetime.now().isoformat()
        }


@router.post("/api/task")
async def execute_task(request: ParallelTaskRequest):
    """
    Execute a stateless task in parallel mode (no conversation context).

    Unlike /api/chat, this endpoint:
    - Does NOT acquire execution lock (parallel allowed)
    - Does NOT use --continue flag (stateless) by default
    - Each call is independent and can run concurrently

    Use this for:
    - Agent delegation from orchestrators
    - Batch processing without context pollution
    - Parallel task execution
    - Resuming previous sessions with resume_session_id (EXEC-023)

    Note: Does NOT update conversation history or session state.
    """
    if request.resume_session_id:
        logger.info(f"[Task] Resuming session {request.resume_session_id}: {request.message[:50]}...")
    else:
        logger.info(f"[Task] Executing parallel task: {request.message[:50]}...")

    # Execute via runtime adapter in headless mode (no lock, no --continue)
    runtime = get_runtime()
    response_text, raw_messages, metadata, session_id = await runtime.execute_headless(
        prompt=request.message,
        model=request.model,
        allowed_tools=request.allowed_tools,
        system_prompt=request.system_prompt,
        timeout_seconds=request.timeout_seconds or 900,  # Default 15 minutes for research tasks
        max_turns=request.max_turns,
        execution_id=request.execution_id,  # Use provided ID for process registry (enables termination)
        resume_session_id=request.resume_session_id,  # Resume previous session (EXEC-023)
        persist_session=bool(request.persist_session),  # Session tab: write JSONL for future --resume
        images=request.images,  # Vision images from channel adapters (#562)
    )

    logger.info(f"[Task] Task {session_id} completed successfully")

    # raw_messages contains the full Claude Code JSON stream (init, assistant, user, result)
    # This is the complete execution transcript showing thinking, tool calls, and results
    return {
        "response": response_text,
        "execution_log": raw_messages,  # Full JSON transcript from Claude Code
        "metadata": metadata.model_dump(),
        "session_id": session_id,
        "timestamp": datetime.now().isoformat()
    }


@router.get("/api/chat/history")
async def get_chat_history():
    """Get conversation history"""
    return agent_state.conversation_history


@router.get("/api/chat/session")
async def get_session_info():
    """Get current session information including token usage"""
    return {
        "session_started": agent_state.session_started,
        "message_count": len(agent_state.conversation_history),
        "total_cost_usd": agent_state.session_total_cost,
        "context_tokens": agent_state.session_context_tokens,
        "context_window": agent_state.session_context_window,
        "context_percent": round(
            (agent_state.session_context_tokens / agent_state.session_context_window) * 100, 1
        ) if agent_state.session_context_window > 0 else 0,
        "model": agent_state.current_model
    }


@router.get("/api/model")
async def get_model():
    """Get the current model being used"""
    runtime = agent_state.agent_runtime

    if runtime == "gemini-cli" or runtime == "gemini":
        return {
            "model": agent_state.current_model,
            "runtime": runtime,
            "available_models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
            "note": "Gemini models. 2.5-pro has 1M context window."
        }
    else:
        return {
            "model": agent_state.current_model,
            "runtime": runtime,
            "available_models": ["sonnet", "opus", "haiku"],
            "note": "Claude model aliases: sonnet (Sonnet 4.5), opus (Opus 4.5), haiku. Add [1m] suffix for 1M context."
        }


@router.put("/api/model")
async def set_model(request: ModelRequest):
    """Set the model to use for subsequent messages"""
    from fastapi import HTTPException

    runtime = agent_state.agent_runtime

    # Validate based on runtime
    if runtime == "gemini-cli" or runtime == "gemini":
        valid_models = ["gemini-3-pro", "gemini-3-flash", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"]
        if request.model in valid_models or request.model.startswith("gemini-"):
            agent_state.current_model = request.model
            logger.info(f"Model changed to: {request.model}")
            return {
                "status": "success",
                "model": agent_state.current_model,
                "note": "Model will be used for subsequent messages"
            }
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Gemini model: {request.model}. Use: gemini-2.5-pro, gemini-2.5-flash, etc."
            )
    else:
        # Claude Code validation
        valid_aliases = ["sonnet", "opus", "haiku", "sonnet[1m]", "opus[1m]", "haiku[1m]"]
        if request.model in valid_aliases or request.model.startswith("claude-"):
            agent_state.current_model = request.model
            logger.info(f"Model changed to: {request.model}")
            return {
                "status": "success",
                "model": agent_state.current_model,
                "note": "Model will be used for subsequent messages"
            }
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Claude model: {request.model}. Use aliases (sonnet, opus, haiku) or full model names."
            )


@router.delete("/api/chat/history")
async def clear_chat_history():
    """Clear conversation history and reset session"""
    agent_state.reset_session()
    return {
        "status": "cleared",
        "session_reset": True,
        "session": {
            "total_cost_usd": 0.0,
            "context_tokens": 0,
            "context_window": agent_state.session_context_window,
            "message_count": 0
        }
    }


# ============================================================================
# Execution Termination Endpoints
# ============================================================================

@router.post("/api/executions/{execution_id}/terminate")
async def terminate_execution(execution_id: str):
    """
    Terminate a running execution by ID.

    Sends SIGINT for graceful termination, then SIGKILL if needed.
    This allows Claude Code to finish its current operation gracefully.
    """
    registry = get_process_registry()
    # registry.terminate() does up to 7s of synchronous process.wait() (SIGINT grace + SIGKILL grace);
    # run in the default executor so the event loop stays responsive to concurrent /health probes.
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, registry.terminate, execution_id)

    if result["success"]:
        logger.info(f"[Terminate] Execution {execution_id} terminated successfully")
        return {"status": "terminated", "execution_id": execution_id}
    elif result["reason"] == "not_found":
        raise HTTPException(status_code=404, detail="Execution not found")
    elif result["reason"] == "already_finished":
        return {"status": "already_finished", "execution_id": execution_id, "returncode": result.get("returncode")}
    else:
        raise HTTPException(status_code=500, detail=result.get("error", "Termination failed"))


@router.get("/api/executions/running")
async def list_running_executions():
    """
    List all currently running executions.

    Returns execution_id, start time, and metadata for each running process.
    """
    registry = get_process_registry()
    return {"executions": registry.list_running()}


@router.get("/api/executions/{execution_id}/status")
async def get_execution_status(execution_id: str):
    """
    Get status of a specific execution.

    Returns running state, return code (if finished), and metadata.
    """
    registry = get_process_registry()
    status = registry.get_status(execution_id)
    if not status:
        raise HTTPException(status_code=404, detail="Execution not found")
    return status


@router.get("/api/executions/{execution_id}/last-error")
async def get_execution_last_error(execution_id: str):
    """
    Get the last error from an execution's log buffer.

    Used by the cleanup service to preserve error context when marking
    stale executions as failed. Returns the most recent error found
    in the execution's log buffer (if any).

    Returns:
        - 200: {"error_type": str, "error_message": str} if error found
        - 200: {"error_type": null, "error_message": null} if no error
        - 404: Execution not found in buffer
    """
    registry = get_process_registry()

    # Check if execution exists (running or recently completed with buffer)
    if not registry.is_execution_running(execution_id):
        buffered = registry.get_buffered_logs(execution_id)
        if buffered is None:
            raise HTTPException(status_code=404, detail="Execution not found")

    # Extract last error from buffer
    error_info = registry.get_last_error(execution_id)
    if error_info:
        return error_info

    # No error found in buffer
    return {"error_type": None, "error_message": None}


# ============================================================================
# Live Execution Streaming Endpoints
# ============================================================================

@router.get("/api/executions/{execution_id}/stream")
async def stream_execution_log(execution_id: str):
    """
    Stream execution log entries via Server-Sent Events (SSE).

    Sends log entries in real-time as they are produced by Claude Code.
    If the execution is already running, first sends all buffered entries,
    then streams new ones.

    SSE Event format:
    - data: JSON-encoded log entry from Claude Code
    - Final message: {"type": "stream_end"}

    Note: This endpoint has no authentication - security is handled by the
    backend proxy which validates user access before proxying to agent.
    """
    registry = get_process_registry()

    # Check if execution exists
    if not registry.is_execution_running(execution_id):
        # Try to get buffered logs for recently completed execution
        buffered = registry.get_buffered_logs(execution_id)
        if buffered is None:
            raise HTTPException(status_code=404, detail="Execution not found")

        # Return buffered logs as static stream (for completed executions)
        async def static_generator():
            for entry in buffered:
                yield f"data: {json.dumps(entry)}\n\n"
            yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"

        return StreamingResponse(
            static_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    # Subscribe to live stream
    queue = registry.subscribe_logs(execution_id)
    if queue is None:
        raise HTTPException(status_code=404, detail="Execution not found")

    async def event_generator():
        """Generate SSE events from the log queue."""
        try:
            while True:
                try:
                    # Wait for next entry with timeout (allows checking if client disconnected)
                    entry = await asyncio.wait_for(queue.get(), timeout=30.0)

                    yield f"data: {json.dumps(entry)}\n\n"

                    # Check if stream ended
                    if entry.get("type") == "stream_end":
                        break
                except asyncio.TimeoutError:
                    # Send keepalive comment
                    yield ": keepalive\n\n"
        finally:
            # Unsubscribe when client disconnects
            registry.unsubscribe_logs(execution_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ============================================================================
# WebSocket Endpoints
# ============================================================================

@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket endpoint for streaming chat (internal use)"""
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            # Add user message
            agent_state.add_message("user", message["content"])

            # Send response via runtime adapter
            runtime = get_runtime()
            # Use model from message if provided, otherwise use current_model from state
            effective_model = message.get("model") or agent_state.current_model
            response_text, execution_log, metadata, raw_messages = await runtime.execute(
                prompt=message["content"],
                model=effective_model,
                continue_session=True,
                stream=True
            )
            agent_state.add_message("assistant", response_text)

            await websocket.send_json({
                "type": "message",
                "role": "assistant",
                "content": response_text,
                "execution_log": raw_messages,  # Full Claude Code stream-json format
                "metadata": metadata.model_dump()
            })
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
