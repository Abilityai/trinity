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
import json
from datetime import datetime
from typing import List
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
import httpx

from config import CORS_ORIGINS, GITHUB_PAT, GITHUB_PAT_CREDENTIAL_ID, REDIS_URL
from models import User
from dependencies import get_current_user
from services.docker_service import docker_client, list_all_agents_fast

# Import routers
from routers.auth import router as auth_router
from routers.agents import router as agents_router, set_websocket_manager as set_agents_ws_manager, inject_trinity_meta_prompt
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
from routers.system_agent import router as system_agent_router, set_inject_trinity_meta_prompt
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

# Import scheduler service
from services.scheduler_service import scheduler_service

# Import activity service
from services.activity_service import activity_service

# Import system agent service
from services.system_agent_service import system_agent_service

# Import log archive service
from services.log_archive_service import log_archive_service

# Import credentials manager for GitHub PAT initialization
from credentials import CredentialManager, CredentialCreate

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


manager = ConnectionManager()

# Inject WebSocket manager into routers that need it
set_agents_ws_manager(manager)
set_sharing_ws_manager(manager)
set_chat_ws_manager(manager)
set_public_links_ws_manager(manager)

# Inject trinity meta-prompt function into system agent router
set_inject_trinity_meta_prompt(inject_trinity_meta_prompt)

# Set up scheduler broadcast callback
scheduler_service.set_broadcast_callback(manager.broadcast)

# Set up activity service WebSocket manager
activity_service.set_websocket_manager(manager)

# Set up process engine WebSocket publisher
set_websocket_publisher_broadcast(manager.broadcast)


def initialize_github_pat():
    """
    Upload GitHub PAT from environment to Redis on startup.
    This enables local development without manually adding credentials.
    """
    if not GITHUB_PAT:
        print("GitHub PAT not configured in environment (GITHUB_PAT)")
        return

    try:
        credential_manager = CredentialManager(REDIS_URL)

        # Check if credential already exists
        existing = credential_manager.get_credential(GITHUB_PAT_CREDENTIAL_ID, "admin")
        if existing:
            # Update the secret if PAT changed
            secret_key = f"credentials:{GITHUB_PAT_CREDENTIAL_ID}:secret"
            import json
            credential_manager.redis_client.set(secret_key, json.dumps({"token": GITHUB_PAT}))
            print(f"GitHub PAT updated in Redis (credential_id: {GITHUB_PAT_CREDENTIAL_ID})")
        else:
            # Create new credential with fixed ID
            from datetime import datetime
            import json

            cred_key = f"credentials:{GITHUB_PAT_CREDENTIAL_ID}"
            secret_key = f"{cred_key}:secret"
            metadata_key = f"{cred_key}:metadata"
            user_creds_key = "user:admin:credentials"
            now = datetime.utcnow()

            # Store metadata
            credential_manager.redis_client.hset(metadata_key, mapping={
                "id": GITHUB_PAT_CREDENTIAL_ID,
                "name": "GitHub PAT (Templates)",
                "service": "github",
                "type": "token",
                "description": "Auto-configured from GITHUB_PAT environment variable",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "status": "active",
                "user_id": "admin"
            })

            # Store secret
            credential_manager.redis_client.set(secret_key, json.dumps({"token": GITHUB_PAT}))

            # Add to admin's credentials set
            credential_manager.redis_client.sadd(user_creds_key, GITHUB_PAT_CREDENTIAL_ID)

            print(f"GitHub PAT uploaded to Redis (credential_id: {GITHUB_PAT_CREDENTIAL_ID})")

    except Exception as e:
        print(f"Error initializing GitHub PAT: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Set up structured JSON logging (captured by Vector)
    setup_logging()

    # Initialize GitHub PAT from environment to Redis
    initialize_github_pat()

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

    # NOTE: Embedded scheduler DISABLED (2026-01-13)
    # Schedule execution is now handled by the dedicated scheduler service (trinity-scheduler container)
    # which uses Redis distributed locking to prevent duplicate executions.
    # The scheduler_service module is still imported for:
    # - Manual trigger functionality (trigger_schedule)
    # - CRUD sync with APScheduler jobs (no-op when not initialized)
    # See: src/scheduler/, docs/memory/feature-flows/scheduler-service.md
    print("Embedded scheduler disabled - using dedicated scheduler service (trinity-scheduler)")

    # Initialize log archive service
    try:
        log_archive_service.start()
        print("Log archive service started")
    except Exception as e:
        print(f"Error starting log archive service: {e}")

    # Initialize SMARTS summary scheduler (daily Telegram reports)
    try:
        from services.smarts_summary_service import start_summary_scheduler
        start_summary_scheduler()
        print("SMARTS summary scheduler started")
    except Exception as e:
        print(f"Error starting SMARTS summary scheduler: {e}")

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

    # Shutdown SMARTS summary scheduler
    try:
        from services.smarts_summary_service import stop_summary_scheduler
        stop_summary_scheduler()
        print("SMARTS summary scheduler stopped")
    except Exception as e:
        print(f"Error stopping SMARTS summary scheduler: {e}")


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


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = None):
    """
    WebSocket endpoint for real-time updates.

    Accepts authentication via:
    - Query parameter: /ws?token=<jwt_token>
    - First message after connection (for backward compatibility)

    SECURITY: Authentication is optional for read-only updates,
    but should be enforced in production for sensitive data.
    """
    from jose import JWTError, jwt as jose_jwt
    from config import SECRET_KEY, ALGORITHM

    # Validate token if provided
    authenticated = False
    username = None

    if token:
        try:
            payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            username = payload.get("sub")
            if username:
                authenticated = True
        except JWTError:
            # Invalid token - allow connection but mark as unauthenticated
            # In production, you may want to reject the connection
            pass

    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Could authenticate via first message if not done via query param
            if not authenticated and data.startswith("Bearer "):
                try:
                    msg_token = data[7:]
                    payload = jose_jwt.decode(msg_token, SECRET_KEY, algorithms=[ALGORITHM])
                    username = payload.get("sub")
                    if username:
                        authenticated = True
                        await websocket.send_text(json.dumps({"type": "authenticated", "user": username}))
                except JWTError:
                    pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now()}


# Version endpoint
@app.get("/api/version")
async def get_version():
    """Get Trinity platform version information."""
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
