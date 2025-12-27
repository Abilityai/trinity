"""
System Agent routes for the Trinity backend.

Provides endpoints for managing the Trinity system agent:
- Status check
- Re-initialization (reset to clean state)
- Interactive terminal via WebSocket (PTY forwarding)

The system agent is auto-deployed on platform startup and cannot be deleted.
"""
import os
import json
import asyncio
import logging
import select
import threading
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, Query
import httpx

from models import User
from database import db
from dependencies import get_current_user, decode_token
from services.audit_service import log_audit_event
from services.docker_service import get_agent_container, docker_client
from db.agents import SYSTEM_AGENT_NAME

router = APIRouter(prefix="/api/system-agent", tags=["system-agent"])
logger = logging.getLogger(__name__)

# Reference to the agents router's functions - will be imported at runtime to avoid circular imports
_inject_trinity_meta_prompt = None


def set_inject_trinity_meta_prompt(func):
    """Set the inject_trinity_meta_prompt function from agents router."""
    global _inject_trinity_meta_prompt
    _inject_trinity_meta_prompt = func


def require_admin(current_user: User):
    """Verify user is an admin."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/status")
async def get_system_agent_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the status of the system agent.

    Returns health information including:
    - Container status (running/stopped/not found)
    - Agent details if running
    - Last activity timestamp
    """
    container = get_agent_container(SYSTEM_AGENT_NAME)

    if not container:
        return {
            "exists": False,
            "status": "not_found",
            "message": "System agent container not found",
            "name": SYSTEM_AGENT_NAME
        }

    container.reload()
    status = container.status

    result = {
        "exists": True,
        "status": status,
        "name": SYSTEM_AGENT_NAME,
        "container_id": container.short_id
    }

    # Get additional info from database
    owner = db.get_agent_owner(SYSTEM_AGENT_NAME)
    if owner:
        result["owner"] = owner.get("owner_username")
        result["created_at"] = owner.get("created_at")
        result["is_system"] = owner.get("is_system", True)

    # If running, try to get health info from agent
    if status == "running":
        try:
            agent_url = f"http://agent-{SYSTEM_AGENT_NAME}:8000/api/health"
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(agent_url)
                if response.status_code == 200:
                    result["health"] = response.json()
        except Exception as e:
            result["health_error"] = str(e)

    await log_audit_event(
        event_type="system_agent",
        action="status",
        user_id=current_user.username,
        agent_name=SYSTEM_AGENT_NAME,
        ip_address=request.client.host if request.client else None,
        result="success"
    )

    return result


@router.post("/reinitialize")
async def reinitialize_system_agent(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Re-initialize the system agent.

    Admin-only endpoint that:
    1. Stops the system agent if running
    2. Clears workspace content
    3. Re-starts the agent
    4. Re-injects Trinity meta-prompt

    Does NOT delete database records or MCP API key.
    This is a "reset to clean state" operation.
    """
    require_admin(current_user)

    container = get_agent_container(SYSTEM_AGENT_NAME)
    if not container:
        raise HTTPException(
            status_code=404,
            detail="System agent container not found. It may not have been deployed yet."
        )

    steps_completed = []
    errors = []

    try:
        # Step 1: Stop the container
        container.reload()
        if container.status == "running":
            container.stop(timeout=30)
            steps_completed.append("stopped")
            logger.info(f"System agent {SYSTEM_AGENT_NAME} stopped for re-initialization")

        # Step 2: Clear workspace content (not the volume, just files)
        try:
            container.start()
            container.reload()

            # Execute cleanup command inside container
            cleanup_result = container.exec_run(
                "bash -c 'rm -rf /home/developer/workspace/* /home/developer/.claude /home/developer/.trinity'",
                user="developer"
            )
            if cleanup_result.exit_code == 0:
                steps_completed.append("workspace_cleared")
            else:
                errors.append(f"Workspace cleanup warning: {cleanup_result.output.decode()}")
        except Exception as e:
            errors.append(f"Workspace cleanup error: {str(e)}")

        # Step 3: Container is already running from step 2
        steps_completed.append("started")
        logger.info(f"System agent {SYSTEM_AGENT_NAME} started after re-initialization")

        # Step 4: Re-copy template files (.claude and CLAUDE.md)
        try:
            copy_result = container.exec_run(
                "bash -c 'cp -r /template/.claude /home/developer/ 2>/dev/null; cp /template/CLAUDE.md /home/developer/ 2>/dev/null; true'",
                user="developer"
            )
            if copy_result.exit_code == 0:
                steps_completed.append("template_copied")
            else:
                errors.append(f"Template copy warning: {copy_result.output.decode()}")
        except Exception as e:
            errors.append(f"Template copy error: {str(e)}")

        # Step 5: Re-inject Trinity meta-prompt
        if _inject_trinity_meta_prompt:
            try:
                injection_result = await _inject_trinity_meta_prompt(SYSTEM_AGENT_NAME)
                if injection_result.get("status") == "success":
                    steps_completed.append("trinity_injected")
                else:
                    errors.append(f"Trinity injection issue: {injection_result}")
            except Exception as e:
                errors.append(f"Trinity injection error: {str(e)}")
        else:
            errors.append("Trinity injection function not available")

        await log_audit_event(
            event_type="system_agent",
            action="reinitialize",
            user_id=current_user.username,
            agent_name=SYSTEM_AGENT_NAME,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={
                "steps_completed": steps_completed,
                "errors": errors if errors else None
            }
        )

        return {
            "success": True,
            "message": "System agent re-initialized successfully",
            "name": SYSTEM_AGENT_NAME,
            "steps_completed": steps_completed,
            "warnings": errors if errors else None
        }

    except Exception as e:
        logger.error(f"Failed to re-initialize system agent: {e}")
        await log_audit_event(
            event_type="system_agent",
            action="reinitialize",
            user_id=current_user.username,
            agent_name=SYSTEM_AGENT_NAME,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e), "steps_completed": steps_completed}
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to re-initialize system agent: {str(e)}"
        )


@router.post("/restart")
async def restart_system_agent(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Restart the system agent.

    Admin-only endpoint that stops and starts the system agent.
    Does NOT clear workspace or re-initialize - just a simple restart.
    """
    require_admin(current_user)

    container = get_agent_container(SYSTEM_AGENT_NAME)
    if not container:
        raise HTTPException(
            status_code=404,
            detail="System agent container not found. It may not have been deployed yet."
        )

    try:
        container.reload()
        was_running = container.status == "running"

        if was_running:
            container.stop(timeout=30)

        container.start()
        container.reload()

        # Re-inject Trinity meta-prompt
        trinity_result = None
        if _inject_trinity_meta_prompt:
            try:
                trinity_result = await _inject_trinity_meta_prompt(SYSTEM_AGENT_NAME)
            except Exception as e:
                logger.warning(f"Trinity injection after restart failed: {e}")

        await log_audit_event(
            event_type="system_agent",
            action="restart",
            user_id=current_user.username,
            agent_name=SYSTEM_AGENT_NAME,
            ip_address=request.client.host if request.client else None,
            result="success"
        )

        return {
            "success": True,
            "message": "System agent restarted successfully",
            "name": SYSTEM_AGENT_NAME,
            "status": container.status,
            "trinity_injection": trinity_result.get("status") if trinity_result else None
        }

    except Exception as e:
        logger.error(f"Failed to restart system agent: {e}")
        await log_audit_event(
            event_type="system_agent",
            action="restart",
            user_id=current_user.username,
            agent_name=SYSTEM_AGENT_NAME,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to restart system agent: {str(e)}"
        )


# ============================================================================
# TERMINAL WEBSOCKET ENDPOINT
# ============================================================================

# Track active terminal sessions (limit 1 per user)
_active_terminal_sessions: dict = {}  # user_id -> session_info
_terminal_lock = threading.Lock()


@router.websocket("/terminal")
async def system_agent_terminal(
    websocket: WebSocket,
    mode: str = Query(default="claude")  # 'claude' or 'bash'
):
    """
    Interactive terminal WebSocket for System Agent.

    Provides full PTY-based terminal access to the System Agent container.
    Supports both Claude Code and bash shell modes.

    Authentication:
    - First message must be JSON: {"type": "auth", "token": "<jwt_token>"}
    - Only admin users can access this endpoint

    Control Messages (JSON):
    - {"type": "resize", "cols": 80, "rows": 24} - Resize terminal

    All other messages are forwarded as terminal input.
    """
    await websocket.accept()

    user_email = None
    exec_id = None
    docker_socket = None
    session_start = None

    try:
        # Step 1: Authenticate via first message
        try:
            auth_msg = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            auth_data = json.loads(auth_msg)

            if auth_data.get("type") != "auth":
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Expected auth message first",
                    "close": True
                }))
                await websocket.close(code=4001, reason="Expected auth message")
                return

            token = auth_data.get("token")
            if not token:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Token required",
                    "close": True
                }))
                await websocket.close(code=4001, reason="Token required")
                return

            # Decode and validate token
            user = decode_token(token)
            if not user:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid token",
                    "close": True
                }))
                await websocket.close(code=4001, reason="Invalid token")
                return

            # Check admin access
            if user.get("role") != "admin":
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Admin access required",
                    "close": True
                }))
                await websocket.close(code=4003, reason="Admin access required")
                return

            user_email = user.get("email") or user.get("sub") or "unknown"

        except asyncio.TimeoutError:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Authentication timeout",
                "close": True
            }))
            await websocket.close(code=4001, reason="Auth timeout")
            return
        except json.JSONDecodeError:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Invalid JSON in auth message",
                "close": True
            }))
            await websocket.close(code=4001, reason="Invalid auth format")
            return

        # Step 2: Check for existing session (limit 1 per user)
        # Sessions older than 300 seconds (5 min) are considered stale (cleanup failed)
        SESSION_TIMEOUT_SECONDS = 300
        with _terminal_lock:
            if user_email in _active_terminal_sessions:
                session_info = _active_terminal_sessions[user_email]
                session_age = (datetime.utcnow() - session_info["started_at"]).total_seconds()
                if session_age < SESSION_TIMEOUT_SECONDS:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "You already have an active terminal session. Close it first.",
                        "close": True
                    }))
                    await websocket.close(code=4002, reason="Session limit reached")
                    return
                else:
                    # Stale session, clean it up
                    logger.warning(f"Cleaning up stale terminal session for {user_email} (age: {session_age:.0f}s)")
            _active_terminal_sessions[user_email] = {"started_at": datetime.utcnow()}

        # Step 3: Get container
        container = get_agent_container(SYSTEM_AGENT_NAME)
        if not container:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "System agent container not found",
                "close": True
            }))
            with _terminal_lock:
                _active_terminal_sessions.pop(user_email, None)
            await websocket.close(code=4004, reason="Container not found")
            return

        container.reload()
        if container.status != "running":
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "System agent is not running. Please start it first.",
                "close": True
            }))
            with _terminal_lock:
                _active_terminal_sessions.pop(user_email, None)
            await websocket.close(code=4004, reason="Container not running")
            return

        # Step 4: Audit log - session start
        session_start = datetime.utcnow()
        await log_audit_event(
            event_type="terminal_session",
            action="start",
            user_id=user_email,
            agent_name=SYSTEM_AGENT_NAME,
            details={"mode": mode}
        )

        # Step 5: Create exec with TTY
        cmd = ["claude"] if mode == "claude" else ["/bin/bash"]

        # Use docker API to create exec instance
        exec_instance = docker_client.api.exec_create(
            container.id,
            cmd,
            stdin=True,
            tty=True,
            stdout=True,
            stderr=True,
            user="developer",
            workdir="/home/developer",
            environment={"TERM": "xterm-256color", "COLORTERM": "truecolor"}
        )
        exec_id = exec_instance["Id"]

        # Start exec and get socket
        exec_output = docker_client.api.exec_start(exec_id, socket=True, tty=True)

        # Get the raw socket
        # docker-py returns a SocketIO wrapper, we need the underlying socket
        docker_socket = exec_output._sock
        docker_socket.setblocking(False)

        # Send success message
        await websocket.send_text(json.dumps({"type": "auth_success"}))

        logger.info(f"Terminal session started for {user_email} (mode: {mode})")

        # Step 6: Bidirectional forwarding
        loop = asyncio.get_event_loop()

        async def read_from_docker():
            """Read from Docker socket, send to WebSocket."""
            while True:
                try:
                    # Use select for non-blocking read with short timeout for responsiveness
                    # 5ms timeout provides good balance between CPU usage and latency
                    ready = await loop.run_in_executor(
                        None,
                        lambda: select.select([docker_socket], [], [], 0.005)
                    )
                    if ready[0]:
                        # Larger buffer (16KB) to reduce read syscalls for large outputs
                        data = await loop.run_in_executor(
                            None,
                            lambda: docker_socket.recv(16384)
                        )
                        if not data:
                            break
                        await websocket.send_bytes(data)
                    # No extra sleep needed - select timeout handles pacing
                except Exception as e:
                    logger.debug(f"Docker read error: {e}")
                    break

        async def read_from_websocket():
            """Read from WebSocket, send to Docker socket."""
            while True:
                try:
                    message = await websocket.receive()

                    if message["type"] == "websocket.disconnect":
                        break

                    if "text" in message:
                        # Check if it's a control message
                        try:
                            ctrl = json.loads(message["text"])
                            if ctrl.get("type") == "resize":
                                # Resize the PTY
                                cols = ctrl.get("cols", 80)
                                rows = ctrl.get("rows", 24)
                                docker_client.api.exec_resize(
                                    exec_id,
                                    height=rows,
                                    width=cols
                                )
                                continue
                        except json.JSONDecodeError:
                            pass

                        # Plain text input - send to container
                        await loop.run_in_executor(
                            None,
                            lambda: docker_socket.sendall(message["text"].encode())
                        )

                    elif "bytes" in message:
                        await loop.run_in_executor(
                            None,
                            lambda: docker_socket.sendall(message["bytes"])
                        )

                except WebSocketDisconnect:
                    break
                except Exception as e:
                    logger.debug(f"WebSocket read error: {e}")
                    break

        # Run both tasks concurrently
        await asyncio.gather(
            read_from_docker(),
            read_from_websocket(),
            return_exceptions=True
        )

    except WebSocketDisconnect:
        logger.info(f"Terminal WebSocket disconnected for {user_email}")
    except Exception as e:
        logger.error(f"Terminal session error: {e}")
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": str(e)
            }))
        except:
            pass
    finally:
        # Cleanup
        if docker_socket:
            try:
                docker_socket.close()
            except:
                pass

        # Remove from active sessions
        if user_email:
            with _terminal_lock:
                _active_terminal_sessions.pop(user_email, None)

            # Audit log - session end
            session_duration = None
            if session_start:
                session_duration = (datetime.utcnow() - session_start).total_seconds()

            try:
                await log_audit_event(
                    event_type="terminal_session",
                    action="end",
                    user_id=user_email,
                    agent_name=SYSTEM_AGENT_NAME,
                    details={
                        "mode": mode,
                        "duration_seconds": session_duration
                    }
                )
            except:
                pass

            logger.info(f"Terminal session ended for {user_email} (duration: {session_duration:.1f}s)" if session_duration else f"Terminal session ended for {user_email}")
