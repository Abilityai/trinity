"""
Agent Service Terminal - WebSocket terminal session management.

Handles interactive PTY-based terminal connections to agent containers.
"""
import json
import select
import asyncio
import threading
import logging
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

from database import db
from services.docker_service import docker_client, get_agent_container
from services.audit_service import log_audit_event

logger = logging.getLogger(__name__)


class TerminalSessionManager:
    """
    Manages terminal sessions for agents.

    Limits sessions to 1 per user per agent and handles
    stale session cleanup.
    """

    def __init__(self):
        self._active_sessions: dict = {}  # (user_id, agent_name) -> session_info
        self._lock = threading.Lock()

    def _check_and_register_session(self, user_email: str, agent_name: str, timeout_seconds: int = 300) -> bool:
        """
        Check if a session can be created and register it.

        Returns True if session was registered, False if limit reached.
        """
        session_key = (user_email, agent_name)
        with self._lock:
            if session_key in self._active_sessions:
                session_info = self._active_sessions[session_key]
                session_age = (datetime.utcnow() - session_info["started_at"]).total_seconds()
                if session_age < timeout_seconds:
                    return False  # Session limit reached
                else:
                    # Stale session, clean it up
                    logger.warning(f"Cleaning up stale terminal session for {user_email}@{agent_name} (age: {session_age:.0f}s)")
            self._active_sessions[session_key] = {"started_at": datetime.utcnow()}
            return True

    def _unregister_session(self, user_email: str, agent_name: str):
        """Remove a session from tracking."""
        session_key = (user_email, agent_name)
        with self._lock:
            self._active_sessions.pop(session_key, None)

    async def handle_terminal_session(
        self,
        websocket: WebSocket,
        agent_name: str,
        mode: str,
        decode_token_fn
    ):
        """
        Handle a WebSocket terminal session.

        Args:
            websocket: The WebSocket connection
            agent_name: Name of the agent to connect to
            mode: Terminal mode ('claude' or 'bash')
            decode_token_fn: Function to decode JWT tokens
        """
        await websocket.accept()

        user_email = None
        exec_id = None
        docker_socket = None
        session_start = None
        session_key = None

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
                user = decode_token_fn(token)
                if not user:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Invalid token",
                        "close": True
                    }))
                    await websocket.close(code=4001, reason="Invalid token")
                    return

                user_email = user.get("email") or user.get("sub") or "unknown"
                user_role = user.get("role", "user")

                # Check access to this agent
                if not db.can_user_access_agent(user_email, agent_name) and user_role != "admin":
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "You don't have access to this agent",
                        "close": True
                    }))
                    await websocket.close(code=4003, reason="Access denied")
                    return

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

            # Step 2: Check for existing session (limit 1 per user per agent)
            session_key = (user_email, agent_name)
            if not self._check_and_register_session(user_email, agent_name):
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "You already have an active terminal session for this agent. Close it first.",
                    "close": True
                }))
                await websocket.close(code=4002, reason="Session limit reached")
                return

            # Step 3: Get container
            container = get_agent_container(agent_name)
            if not container:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"Agent '{agent_name}' container not found",
                    "close": True
                }))
                self._unregister_session(user_email, agent_name)
                await websocket.close(code=4004, reason="Container not found")
                return

            container.reload()
            if container.status != "running":
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"Agent '{agent_name}' is not running. Please start it first.",
                    "close": True
                }))
                self._unregister_session(user_email, agent_name)
                await websocket.close(code=4004, reason="Container not running")
                return

            # Step 4: Audit log - session start
            session_start = datetime.utcnow()
            await log_audit_event(
                event_type="terminal_session",
                action="start",
                user_id=user_email,
                agent_name=agent_name,
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
            docker_socket = exec_output._sock
            docker_socket.setblocking(False)

            # Send success message
            await websocket.send_text(json.dumps({"type": "auth_success"}))

            logger.info(f"Terminal session started for {user_email}@{agent_name} (mode: {mode})")

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
                        logger.debug(f"Docker read error for {agent_name}: {e}")
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
                        logger.debug(f"WebSocket read error for {agent_name}: {e}")
                        break

            # Run both tasks concurrently
            await asyncio.gather(
                read_from_docker(),
                read_from_websocket(),
                return_exceptions=True
            )

        except WebSocketDisconnect:
            logger.info(f"Terminal WebSocket disconnected for {user_email}@{agent_name}")
        except Exception as e:
            logger.error(f"Terminal session error for {agent_name}: {e}")
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
            if user_email and agent_name:
                self._unregister_session(user_email, agent_name)

                # Audit log - session end
                session_duration = None
                if session_start:
                    session_duration = (datetime.utcnow() - session_start).total_seconds()

                try:
                    await log_audit_event(
                        event_type="terminal_session",
                        action="end",
                        user_id=user_email,
                        agent_name=agent_name,
                        details={
                            "mode": mode,
                            "duration_seconds": session_duration
                        }
                    )
                except:
                    pass

                if session_duration:
                    logger.info(f"Terminal session ended for {user_email}@{agent_name} (duration: {session_duration:.1f}s)")
                else:
                    logger.info(f"Terminal session ended for {user_email}@{agent_name}")
