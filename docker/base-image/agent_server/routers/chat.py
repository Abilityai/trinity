"""
Chat endpoints for the agent server.
"""
import json
import logging
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..models import ChatRequest, ModelRequest, ParallelTaskRequest
from ..state import agent_state
from ..services.claude_code import execute_claude_code, execute_headless_task, get_execution_lock

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

        # Execute Claude Code - now returns (response, execution_log, metadata)
        response_text, execution_log, metadata = await execute_claude_code(
            request.message,
            stream=request.stream,
            model=request.model
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
        return {
            "response": response_text,
            "execution_log": [entry.model_dump() for entry in execution_log],
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
    - Does NOT use --continue flag (stateless)
    - Each call is independent and can run concurrently

    Use this for:
    - Agent delegation from orchestrators
    - Batch processing without context pollution
    - Parallel task execution

    Note: Does NOT update conversation history or session state.
    """
    logger.info(f"[Task] Executing parallel task: {request.message[:50]}...")

    # Execute in headless mode (no lock, no --continue)
    response_text, execution_log, metadata, session_id = await execute_headless_task(
        prompt=request.message,
        model=request.model,
        allowed_tools=request.allowed_tools,
        system_prompt=request.system_prompt,
        timeout_seconds=request.timeout_seconds or 300
    )

    logger.info(f"[Task] Task {session_id} completed successfully")

    return {
        "response": response_text,
        "execution_log": [entry.model_dump() for entry in execution_log],
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
    return {
        "model": agent_state.current_model,
        "available_models": ["sonnet", "opus", "haiku"],
        "note": "Model aliases: sonnet (Sonnet 4.5), opus (Opus 4.5), haiku. Add [1m] suffix for 1M context (e.g., sonnet[1m])"
    }


@router.put("/api/model")
async def set_model(request: ModelRequest):
    """Set the model to use for subsequent messages"""
    from fastapi import HTTPException

    valid_aliases = ["sonnet", "opus", "haiku", "sonnet[1m]", "opus[1m]", "haiku[1m]"]

    # Accept aliases or full model names (e.g., claude-sonnet-4-5-20250929)
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
            detail=f"Invalid model: {request.model}. Use aliases (sonnet, opus, haiku) or full model names."
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

            # Send response with execution log
            response_text, execution_log, metadata = await execute_claude_code(message["content"], stream=True)
            agent_state.add_message("assistant", response_text)

            await websocket.send_json({
                "type": "message",
                "role": "assistant",
                "content": response_text,
                "execution_log": [entry.model_dump() for entry in execution_log],
                "metadata": metadata.model_dump()
            })
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
