"""
Trinity Agent Platform - Backend API

A universal infrastructure platform for deploying Claude Code agent configurations.
Each agent runs as an isolated Docker container with standardized interfaces.

Refactored for better concern separation:
- config.py: Configuration constants
- models.py: Pydantic models
- dependencies.py: FastAPI dependencies (auth)
- services/: Business logic (docker, template)
- routers/: API endpoints organized by domain
- utils/: Helper functions
"""
import asyncio
import json
from datetime import datetime
from typing import List
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Request, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx

from config import CORS_ORIGINS
from models import User
from dependencies import get_current_user
from services.docker_service import docker_client, list_all_agents_fast

# Import routers
from routers.auth import router as auth_router
from routers.agents import router as agents_router, set_websocket_manager as set_agents_ws_manager, set_filtered_websocket_manager as set_agents_filtered_ws_manager
from routers.agent_config import router as agent_config_router
from routers.agent_files import router as agent_files_router
from routers.agent_rename import router as agent_rename_router, set_websocket_manager as set_agent_rename_ws_manager, set_filtered_websocket_manager as set_agent_rename_filtered_ws_manager
from routers.agent_ssh import router as agent_ssh_router
from routers.credentials import router as credentials_router
from routers.templates import router as templates_router
from routers.sharing import router as sharing_router, set_websocket_manager as set_sharing_ws_manager
from routers.mcp_keys import router as mcp_keys_router
from routers.chat import router as chat_router, set_websocket_manager as set_chat_ws_manager
from routers.schedules import router as schedules_router
from routers.git import router as git_router
from routers.activities import router as activities_router
from routers.settings import router as settings_router
from routers.systems import router as systems_router
from routers.observability import router as observability_router
from routers.system_agent import router as system_agent_router
from routers.ops import router as ops_router
from routers.public_links import router as public_links_router, set_websocket_manager as set_public_links_ws_manager
from routers.public import router as public_router
from routers.setup import router as setup_router
from routers.telemetry import router as telemetry_router
from routers.logs import router as logs_router
from routers.agent_dashboard import router as agent_dashboard_router
from routers.processes import router as processes_router
from routers.executions import router as executions_router
from routers.approvals import router as approvals_router
from routers.triggers import router as triggers_router
from routers.alerts import router as alerts_router
from routers.process_templates import router as process_templates_router
from routers.audit import router as audit_router
from routers.docs import router as docs_router
from routers.skills import router as skills_router
from routers.internal import router as internal_router
from routers.tags import router as tags_router
from routers.system_views import router as system_views_router
from routers.notifications import router as notifications_router, set_websocket_manager as set_notifications_ws_manager, set_filtered_websocket_manager as set_notifications_filtered_ws_manager
from routers.subscriptions import router as subscriptions_router
from routers.monitoring import router as monitoring_router, set_websocket_manager as set_monitoring_ws_manager, set_filtered_websocket_manager as set_monitoring_filtered_ws_manager
from routers.slack import public_router as slack_public_router, auth_router as slack_auth_router
from routers.paid import router as paid_router
from routers.nevermined import router as nevermined_router
from routers.image_generation import router as image_generation_router
from routers.avatar import router as avatar_router
from routers.operator_queue import router as operator_queue_router, set_websocket_manager as set_operator_queue_ws_manager
from routers.users import router as users_router

# Import activity service
from services.activity_service import activity_service

# Import system agent service
from services.system_agent_service import system_agent_service

# Import log archive service
from services.log_archive_service import log_archive_service

# Import operator queue sync service
from services.operator_queue_service import operator_queue_service, set_websocket_manager as set_opqueue_sync_ws_manager

# Import cleanup service
from services.cleanup_service import cleanup_service


# Import process engine WebSocket publisher
from services.process_engine.events import set_websocket_publisher_broadcast

# Import execution recovery function
from routers.executions import run_execution_recovery

# Import logging configuration
from logging_config import setup_logging


class ConnectionManager:
    """WebSocket connection manager for broadcasting events."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                pass


class FilteredWebSocketManager:
    """
    WebSocket manager that filters events based on user's accessible agents.

    Used by /ws/events endpoint for external listeners (Trinity Connect).
    Events are filtered server-side based on user's owned and shared agents.
    """

    def __init__(self):
        from typing import Dict, Set
        self.connections: Dict[WebSocket, Dict] = {}  # ws -> {email, is_admin, accessible_agents}

    async def connect(self, websocket: WebSocket, email: str, is_admin: bool, accessible_agents: List[str]):
        """Register a new connection with its accessible agents."""
        self.connections[websocket] = {
            "email": email,
            "is_admin": is_admin,
            "accessible_agents": set(accessible_agents)
        }

    def disconnect(self, websocket: WebSocket):
        """Remove a connection."""
        self.connections.pop(websocket, None)

    def update_accessible_agents(self, websocket: WebSocket, accessible_agents: List[str]):
        """Update the accessible agents list for a connection."""
        if websocket in self.connections:
            self.connections[websocket]["accessible_agents"] = set(accessible_agents)

    async def broadcast_filtered(self, event: dict):
        """
        Broadcast event only to users who can access the event's agent.

        Extracts agent name from various event fields and checks if
        each connected user can access that agent.
        """
        # Extract agent name from event (different fields for different event types)
        agent_name = (
            event.get("agent_name") or
            event.get("agent") or
            event.get("name") or  # agent_started/agent_stopped events
            event.get("source_agent") or
            (event.get("details") or {}).get("source_agent") or
            (event.get("details") or {}).get("target_agent")
        )

        if not agent_name:
            return  # Can't filter without agent name

        disconnected = []
        for websocket, info in self.connections.items():
            # Admin sees all, otherwise check accessible agents
            if info["is_admin"] or agent_name in info["accessible_agents"]:
                try:
                    await websocket.send_json(event)
                except Exception:
                    disconnected.append(websocket)

        # Clean up disconnected clients
        for ws in disconnected:
            self.disconnect(ws)


manager = ConnectionManager()
filtered_manager = FilteredWebSocketManager()

# Inject WebSocket manager into routers that need it
set_agents_ws_manager(manager)
set_agents_filtered_ws_manager(filtered_manager)
set_agent_rename_ws_manager(manager)
set_agent_rename_filtered_ws_manager(filtered_manager)
set_sharing_ws_manager(manager)
set_chat_ws_manager(manager)
set_public_links_ws_manager(manager)
set_notifications_ws_manager(manager)
set_notifications_filtered_ws_manager(filtered_manager)
set_monitoring_ws_manager(manager)
set_monitoring_filtered_ws_manager(filtered_manager)
set_operator_queue_ws_manager(manager)
set_opqueue_sync_ws_manager(manager)

# NOTE: Trinity platform instructions are now injected at runtime via
# --append-system-prompt on every chat/task request (Issue #136).
# No startup injection callback needed.

# NOTE: Scheduler broadcast callbacks removed - dedicated scheduler (trinity-scheduler)
# publishes events to Redis which backend subscribes to, or via internal API calls

# Set up activity service WebSocket manager
activity_service.set_websocket_manager(manager)
activity_service.set_filtered_websocket_manager(filtered_manager)

# Set up process engine WebSocket publisher
set_websocket_publisher_broadcast(manager.broadcast)




@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Set up structured JSON logging (captured by Vector)
    setup_logging()

    if docker_client:
        try:
            agents = list_all_agents_fast()  # Fast startup - no slow Docker API calls
            print(f"Found {len(agents)} existing Trinity agent containers")
            for agent in agents:
                print(f"  - Agent: {agent.name} (status: {agent.status}, ssh_port: {agent.port})")
        except Exception as e:
            print(f"Error checking agents: {e}")

        # Auto-deploy system agent (Phase 11.1)
        try:
            result = await system_agent_service.ensure_deployed()
            print(f"System agent: {result['action']} - {result['message']}")
            if result.get('status') == 'error':
                print(f"  Warning: System agent deployment issue - {result.get('message')}")
        except Exception as e:
            print(f"Error deploying system agent: {e}")
            # Don't fail startup - system agent is important but not critical for platform operation
    else:
        print("Docker not available - running in demo mode")

    # NOTE: Embedded scheduler REMOVED (2026-02-11)
    # All schedule execution is handled by the dedicated scheduler service (trinity-scheduler container)
    # which uses Redis distributed locking and syncs schedules from database periodically.
    # Manual triggers are also delegated to the dedicated scheduler.
    # See: src/scheduler/, docs/memory/feature-flows/scheduler-service.md
    print("Using dedicated scheduler service (trinity-scheduler)")

    # Initialize log archive service
    try:
        log_archive_service.start()
        print("Log archive service started")
    except Exception as e:
        print(f"Error starting log archive service: {e}")

    # Start operator queue sync service (OPS-001)
    try:
        operator_queue_service.start()
        print("Operator queue sync service started")
    except Exception as e:
        print(f"Error starting operator queue sync service: {e}")

    # Start cleanup service for stale executions/activities/slots
    try:
        cleanup_service.start()
        print("Cleanup service started")
    except Exception as e:
        print(f"Error starting cleanup service: {e}")

    # Run process execution recovery (IT5 P0 reliability feature)
    try:
        recovery_report = await run_execution_recovery()
        if recovery_report.total_processed > 0:
            print(
                f"Execution recovery: "
                f"resumed={len(recovery_report.resumed)}, "
                f"retried={len(recovery_report.retried)}, "
                f"failed={len(recovery_report.failed)}, "
                f"errors={len(recovery_report.errors)}"
            )
        else:
            print("Execution recovery: no interrupted executions found")
    except Exception as e:
        print(f"Error running execution recovery: {e}")
        # Don't fail startup - recovery is important but not critical

    yield

    # NOTE: Embedded scheduler shutdown removed - scheduler runs in dedicated container
    # See: src/scheduler/, docs/memory/feature-flows/scheduler-service.md

    # Shutdown log archive service
    try:
        log_archive_service.stop()
        print("Log archive service stopped")
    except Exception as e:
        print(f"Error stopping log archive service: {e}")

    # Shutdown cleanup service
    try:
        cleanup_service.stop()
        print("Cleanup service stopped")
    except Exception as e:
        print(f"Error stopping cleanup service: {e}")

    # Shutdown operator queue sync service
    try:
        operator_queue_service.stop()
        print("Operator queue sync service stopped")
    except Exception as e:
        print(f"Error stopping operator queue sync service: {e}")


# Create FastAPI app
app = FastAPI(
    title="Trinity Agent Platform",
    description="Universal infrastructure for deploying Claude Code agent configurations",
    version="2.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Source-Agent", "Accept"],
)

# Include all routers
app.include_router(auth_router)
app.include_router(agents_router)
app.include_router(agent_config_router)
app.include_router(agent_files_router)
app.include_router(agent_rename_router)
app.include_router(agent_ssh_router)
app.include_router(activities_router)
app.include_router(credentials_router)
app.include_router(templates_router)
app.include_router(sharing_router)
app.include_router(mcp_keys_router)
app.include_router(chat_router)
app.include_router(schedules_router)
app.include_router(git_router)
app.include_router(settings_router)
app.include_router(systems_router)
app.include_router(observability_router)
app.include_router(system_agent_router)
app.include_router(ops_router)
app.include_router(public_links_router)
app.include_router(public_router)
app.include_router(setup_router)
app.include_router(telemetry_router)
app.include_router(logs_router)
app.include_router(agent_dashboard_router)
app.include_router(processes_router)
app.include_router(executions_router)
app.include_router(approvals_router)
app.include_router(triggers_router)
app.include_router(alerts_router)
app.include_router(process_templates_router)
app.include_router(audit_router)  # IT5 P1: Audit logging
app.include_router(docs_router)   # Process documentation serving
app.include_router(skills_router) # Skills Management System
app.include_router(internal_router)  # Internal agent-to-backend endpoints (no auth)
app.include_router(tags_router)  # Agent Tags (ORG-001)
app.include_router(system_views_router)  # System Views (ORG-001 Phase 2)
app.include_router(notifications_router)  # Agent Notifications (NOTIF-001)
app.include_router(subscriptions_router)  # Subscription Management (SUB-001)
app.include_router(monitoring_router)  # Agent Monitoring (MON-001)
app.include_router(slack_public_router)  # Slack Integration Public (SLACK-001)
app.include_router(slack_auth_router)  # Slack Integration Auth (SLACK-001)
app.include_router(paid_router)  # Nevermined Paid Chat (NVM-001)
app.include_router(nevermined_router)  # Nevermined Admin Config (NVM-001)
app.include_router(image_generation_router)  # Image Generation (IMG-001)
app.include_router(avatar_router)  # Agent Avatars (AVATAR-001)
app.include_router(operator_queue_router)  # Operator Queue (OPS-001)
app.include_router(users_router)  # User Management (ROLE-001)


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(default=None)):
    """
    WebSocket endpoint for real-time updates.

    Security (#178, C-002): Authentication is REQUIRED before accepting the
    connection. The JWT token MUST be provided via query parameter:
      /ws?token=<jwt_token>

    Connections without a valid token are rejected before websocket.accept()
    to prevent any unauthenticated data leakage.
    """
    from jose import JWTError, jwt as jose_jwt
    from config import SECRET_KEY, ALGORITHM

    # Reject immediately if no token provided — before accept()
    if not token:
        await websocket.close(code=4001, reason="Authentication required: provide ?token=<jwt>")
        return

    # Validate token before accepting the connection
    try:
        payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            await websocket.close(code=4001, reason="Invalid token: missing subject")
            return
    except JWTError:
        await websocket.close(code=4001, reason="Invalid authentication token")
        return

    # Token validated — now accept the connection
    await manager.connect(websocket)

    try:
        while True:
            data = await websocket.receive_text()

            # Handle ping/pong for keepalive (prevents idle timeout)
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# WebSocket endpoint for external listeners (Trinity Connect)
@app.websocket("/ws/events")
async def websocket_events_endpoint(
    websocket: WebSocket,
    token: str = Query(None, description="MCP API key for authentication")
):
    """
    WebSocket endpoint for external event listeners (Trinity Connect).

    Authentication: MCP API key via ?token= query parameter
    Events: Filtered to only agents the authenticated user can access

    Usage:
        websocat "ws://localhost:8000/ws/events?token=trinity_mcp_xxx"
        wscat -c "ws://localhost:8000/ws/events?token=trinity_mcp_xxx"

    Events received:
        - agent_activity (chat_start, schedule_start, tool_call completions)
        - schedule_execution_completed
        - agent_started / agent_stopped
        - agent_collaboration

    Commands (send as text):
        - "ping" -> receives "pong"
        - "refresh" -> refreshes accessible agents list
    """
    from database import db

    # Validate MCP API key
    if not token or not token.startswith("trinity_mcp_"):
        await websocket.close(code=4001, reason="MCP API key required (use ?token=trinity_mcp_xxx)")
        return

    key_info = db.validate_mcp_api_key(token)
    if not key_info:
        await websocket.close(code=4001, reason="Invalid or inactive MCP API key")
        return

    user_email = key_info.get("user_email")
    # Determine if admin by checking user role
    user_data = db.get_user_by_username(key_info.get("user_id"))  # user_id is actually username
    is_admin = user_data and user_data.get("role") == "admin"

    # Get list of accessible agents for this user
    accessible_agents = db.get_accessible_agent_names(user_email, is_admin)

    await websocket.accept()
    await websocket.send_json({
        "type": "connected",
        "user": user_email,
        "accessible_agents": accessible_agents,
        "message": "Listening for events. Events filtered to your accessible agents."
    })

    # Add to filtered connections manager
    await filtered_manager.connect(websocket, user_email, is_admin, accessible_agents)

    try:
        while True:
            # Keep connection alive, handle commands
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
            elif data == "refresh":
                # Refresh accessible agents list (e.g., after sharing changes)
                accessible_agents = db.get_accessible_agent_names(user_email, is_admin)
                filtered_manager.update_accessible_agents(websocket, accessible_agents)
                await websocket.send_json({
                    "type": "refreshed",
                    "accessible_agents": accessible_agents
                })
    except WebSocketDisconnect:
        filtered_manager.disconnect(websocket)


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now()}


# Version endpoint
@app.get("/api/version")
async def get_version(current_user: User = Depends(get_current_user)):
    """Get Trinity platform version information. Requires authentication (SEC-180)."""
    import os
    from pathlib import Path

    # Read version from VERSION file (check multiple locations)
    version = "unknown"
    version_paths = [
        Path("/app/VERSION"),  # In container (mounted)
        Path(__file__).parent.parent.parent / "VERSION",  # Development
    ]
    for version_file in version_paths:
        if version_file.exists():
            version = version_file.read_text().strip()
            break

    return {
        "version": version,
        "platform": "trinity",
        "components": {
            "backend": version,
            "agent_server": version,
            "base_image": f"trinity-agent-base:{version}"
        },
        "runtimes": ["claude-code", "gemini-cli"],
        "build_date": os.getenv("BUILD_DATE", "unknown")
    }


# User info endpoint
@app.get("/api/users/me")
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user information."""
    from database import db
    user_data = db.get_user_by_username(current_user.username)
    if user_data:
        return {
            "username": user_data["username"],
            "email": user_data.get("email"),
            "name": user_data.get("name"),
            "picture": user_data.get("picture"),
            "role": user_data["role"]
        }
    return {"username": current_user.username, "role": current_user.role}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
