"""
Agent Service Terminal - WebSocket terminal session management.

Handles interactive PTY-based terminal connections to agent containers.
"""
import json
import asyncio
import dataclasses
import threading
import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import WebSocket, WebSocketDisconnect

from database import db
from services import live_connections
from services.live_connections import CloseReason
from services.docker_service import docker_client, execute_command_in_container, get_agent_container
from services.docker_utils import container_reload, api_exec_create, api_exec_start

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# abilityai/trinity-enterprise#863 — a terminal follows the account it was
# opened for. Shared by both terminal doors (this module's agent terminal and
# `routers/system_agent.py`'s system-agent terminal).
# ---------------------------------------------------------------------------

DOOR_AGENT_TERMINAL = "agent_terminal"
DOOR_SYSTEM_AGENT_TERMINAL = "system_agent_terminal"

# Each terminal's shell is started with this variable set to a fresh random id,
# so the processes of that one session can be found in the container again.
TERMINAL_SESSION_ENV = "TRINITY_TERMINAL_SESSION"
# Bound on the exec that ends a session's shell.
END_SHELL_TIMEOUT_S = 10

# The error frame and close reason per CloseReason. Both close with 4003.
_ACCESS_CLOSE = {
    CloseReason.CHANGED: ("Your access changed. Open the terminal again to continue.", "Access changed"),
    CloseReason.ENDED: ("Your access to this terminal has ended.", "Access ended"),
}

# Ends one terminal session's processes in the container: every process whose
# environment carries the session's marker ($1), and every process in a session
# led by one of them (which reaches what the shell started even after it dropped
# the variable). Those sessions are remembered across the passes, so a process
# that outlives its leader's HUP is still found by its session. HUP first, then
# KILL what is left. It runs as the terminal's
# own user: a container root without CAP_SYS_PTRACE cannot read another user's
# environment, and the user's processes are the ones it must find. Exit status 1
# when something survived the KILL. Plain POSIX sh with sed/cut/tr/grep, read
# from /proc, so it needs nothing beyond the base image.
_END_SHELL_SCRIPT = r"""
m="TRINITY_TERMINAL_SESSION=$1"
self=$$
sid_of() { sed -e 's/^.*) //' "/proc/$1/stat" 2>/dev/null | cut -d' ' -f4; }
marked() { tr '\0' '\n' 2>/dev/null < "/proc/$1/environ" | grep -qxF "$m"; }
init_sid=$(sid_of 1)
sids=" "
targets() {
  pids=""
  for d in /proc/[0-9]*; do
    p=${d#/proc/}
    [ "$p" = "$self" ] && continue
    if marked "$p"; then
      pids="$pids $p"
      s=$(sid_of "$p")
      if [ "$s" = "$p" ] && [ "$s" != "$init_sid" ]; then
        case "$sids" in *" $s "*) ;; *) sids="$sids$s " ;; esac
      fi
    fi
  done
  if [ "$sids" != " " ]; then
    for d in /proc/[0-9]*; do
      p=${d#/proc/}
      [ "$p" = "$self" ] && continue
      case "$sids" in *" $(sid_of "$p") "*) pids="$pids $p" ;; esac
    done
  fi
}
targets
if [ -z "$pids" ]; then echo "nothing to end"; exit 0; fi
kill -HUP $pids 2>/dev/null
sleep 1
targets
if [ -n "$pids" ]; then kill -KILL $pids 2>/dev/null; sleep 0.2; fi
targets
if [ -n "$pids" ]; then echo "still running:$pids"; exit 1; fi
echo "ended"
"""


class TerminalAccess:
    """One terminal session's entry in the worker's live-connection registry.

    A door creates it in the same synchronous step as the account read that
    authorized the session (no await in between), starts the shell with
    :meth:`shell_env`, calls :meth:`go_live` once the start has returned, runs
    its two forwarders through :meth:`forward`, and calls :meth:`finish` in its
    ``finally``.

    The closer (:meth:`close`) sends the error frame and ``4003`` and cancels
    the two forwarder tasks; it never cancels the handler's own task (the
    server's connection task), so the handler returns through its cleanup.
    A re-check that decides to close while the session is still starting only
    marks the entry; the door finds that at :meth:`go_live` and closes itself.
    A session closed this way also has its shell ended (:meth:`finish`).
    """

    def __init__(self, websocket: WebSocket, door: str, user: dict) -> None:
        self._websocket = websocket
        self.session_id = uuid.uuid4().hex
        self._tasks: List[asyncio.Task] = []
        self._container_name: Optional[str] = None
        self.closed_for: Optional[CloseReason] = None
        # The role and email are the ones the door authorized on (from its
        # token read); the rest comes from a read right after it. A change
        # that lands between the two reads therefore differs from the
        # snapshot and closes, instead of being recorded as the opening state.
        snapshot = dataclasses.replace(
            live_connections.read_access(user["sub"]),
            role=user.get("role"),
            email=user.get("email") or "",
        )
        self.handle = live_connections.admit(door, user["sub"], self.close, snapshot=snapshot)

    @property
    def revoked(self) -> Optional[CloseReason]:
        return self.closed_for or self.handle.revoked

    def shell_env(self, container_name: str) -> dict:
        """The marker the shell starts with; also records where it runs."""
        self._container_name = container_name
        return {TERMINAL_SESSION_ENV: self.session_id}

    def go_live(self) -> bool:
        return self.handle.go_live()

    async def close(self, reason: CloseReason) -> None:
        """The closer: error frame, ``4003``, then the forwarders are cancelled."""
        self.closed_for = reason
        message, why = _ACCESS_CLOSE[reason]
        try:
            await self._websocket.send_text(json.dumps({"type": "error", "message": message, "close": True}))
            await self._websocket.close(code=4003, reason=why)
        except Exception as e:  # the browser may already have gone; the close still stands
            logger.info(f"Terminal socket already closed while closing it for an access change: {e}")
        finally:
            for task in self._tasks:
                task.cancel()

    async def forward(self, *forwarders) -> None:
        """Run the forwarders as tasks this session owns, until both end."""
        if self.closed_for is not None:
            for coro in forwarders:
                coro.close()
            return
        self._tasks = [asyncio.ensure_future(coro) for coro in forwarders]
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def finish(self) -> None:
        """Release the entry; end the shell if the session closed for an
        access change. Best effort: a failure is logged, never raised."""
        self.handle.release()
        if self.closed_for is None or self._container_name is None:
            return
        try:
            result = await execute_command_in_container(
                self._container_name,
                ["/bin/sh", "-c", _END_SHELL_SCRIPT, "sh", self.session_id],
                timeout=END_SHELL_TIMEOUT_S,
                user="developer",
            )
        except Exception as e:  # best effort, but loud
            logger.warning(f"Could not end the terminal shell in {self._container_name}: {e}", exc_info=True)
            return
        if result.get("exit_code") != 0:
            logger.warning(
                f"Ending the terminal shell in {self._container_name} did not complete "
                f"(exit {result.get('exit_code')}): {result.get('output', '').strip()}"
            )
        else:
            logger.info(f"Ended the terminal shell in {self._container_name} after an access change")


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
        decode_token_fn,
        model: str = None
    ):
        """
        Handle a WebSocket terminal session.

        Args:
            websocket: The WebSocket connection
            agent_name: Name of the agent to connect to
            mode: Terminal mode ('claude', 'gemini', or 'bash')
            decode_token_fn: Function to decode JWT tokens
            model: Optional model to use (e.g., 'gemini-2.5-flash', 'sonnet')
        """
        await websocket.accept()

        user_email = None
        exec_id = None
        docker_socket = None
        session_start = None
        session_key = None
        access = None

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

                # ent#837 — a shell is an operator surface, and this handler
                # authenticates outside `get_current_user`, so the operator floor
                # is applied here: a demoted owner keeps no shell into its agents.
                from dependencies import WORKSPACE_ONLY_DETAIL, is_workspace_only_role
                if is_workspace_only_role(user_role):
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        **WORKSPACE_ONLY_DETAIL,
                        "close": True
                    }))
                    await websocket.close(code=4003, reason="Access denied")
                    return

                # Check access to this agent.
                # SECURITY (CSO H1): a container shell exposes .env / .credentials.enc
                # and the baked CLAUDE_CODE_OAUTH_TOKEN / TRINITY_MCP_API_KEY, so it
                # must be gated at OWNER/admin tier (can_user_share_agent), not the
                # accessor tier (can_user_access_agent, which admits chat-shared
                # collaborators). Mirrors the deliberately admin-only SSH endpoint.
                if not db.can_user_share_agent(user_email, agent_name) and user_role != "admin":
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "You don't have access to this agent",
                        "close": True
                    }))
                    await websocket.close(code=4003, reason="Access denied")
                    return

                # abilityai/trinity-enterprise#863 — the session follows the
                # account; registered in the step that authorized it.
                access = TerminalAccess(websocket, DOOR_AGENT_TERMINAL, user)

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

            await container_reload(container)
            if container.status != "running":
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"Agent '{agent_name}' is not running. Please start it first.",
                    "close": True
                }))
                self._unregister_session(user_email, agent_name)
                await websocket.close(code=4004, reason="Container not running")
                return

            # Step 4: Track session start
            session_start = datetime.utcnow()

            # Step 5: Create exec with TTY
            # Support multiple terminal modes: claude (Claude Code), gemini
            # (Gemini CLI), codex (OpenAI Codex CLI, #1187), bash
            if mode == "claude":
                cmd = ["claude", "--dangerously-skip-permissions"]
                if model:
                    cmd.extend(["--model", model])
            elif mode == "gemini":
                cmd = ["gemini"]
                if model:
                    cmd.extend(["--model", model])
            elif mode == "codex":
                cmd = ["codex"]
                if model:
                    cmd.extend(["-m", model])
            else:
                cmd = ["/bin/bash"]

            logger.info(f"Starting terminal with command: {cmd}")

            # Use docker API to create exec instance (async to avoid blocking)
            exec_instance = await api_exec_create(
                container.id,
                cmd,
                stdin=True,
                tty=True,
                stdout=True,
                stderr=True,
                user="developer",
                workdir="/home/developer",
                environment={"TERM": "xterm-256color", "COLORTERM": "truecolor",
                             **access.shell_env(container.name)}
            )
            exec_id = exec_instance["Id"]

            # Start exec and get socket (async to avoid blocking)
            exec_output = await api_exec_start(exec_id, socket=True, tty=True)

            # Get the raw socket
            docker_socket = exec_output._sock
            docker_socket.setblocking(False)

            # #863 — closed while starting: refuse (the finally closes the socket).
            if not access.go_live():
                await access.close(access.revoked)
                return

            # Send success message
            await websocket.send_text(json.dumps({"type": "auth_success"}))

            logger.info(f"Terminal session started for {user_email}@{agent_name} (mode: {mode})")

            # Step 6: Bidirectional forwarding using asyncio socket coroutines
            # Uses loop.sock_recv() and loop.sock_sendall() - proper async I/O
            # without thread pool overhead. Socket must be non-blocking.
            loop = asyncio.get_event_loop()

            async def read_from_docker():
                """Read from Docker socket using asyncio sock_recv (no thread pool)."""
                try:
                    while True:
                        # sock_recv is a proper coroutine - awaits until data available
                        data = await loop.sock_recv(docker_socket, 16384)
                        if not data:
                            break
                        await websocket.send_bytes(data)
                except Exception as e:
                    logger.debug(f"Docker read error for {agent_name}: {e}")

            async def read_from_websocket():
                """Read from WebSocket, send to Docker socket."""
                try:
                    while True:
                        message = await websocket.receive()

                        if message["type"] == "websocket.disconnect" or access.revoked is not None:
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

                            # sock_sendall is a proper coroutine - no thread pool
                            await loop.sock_sendall(docker_socket, message["text"].encode())

                        elif "bytes" in message:
                            await loop.sock_sendall(docker_socket, message["bytes"])

                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logger.debug(f"WebSocket read error for {agent_name}: {e}")

            # Run both as tasks the session owns (#863: an access close cancels them)
            await access.forward(read_from_docker(), read_from_websocket())

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

                # Calculate session duration for logging
                session_duration = None
                if session_start:
                    session_duration = (datetime.utcnow() - session_start).total_seconds()

                if session_duration:
                    logger.info(f"Terminal session ended for {user_email}@{agent_name} (duration: {session_duration:.1f}s)")
                else:
                    logger.info(f"Terminal session ended for {user_email}@{agent_name}")

            if access is not None:
                await access.finish()
