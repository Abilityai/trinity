"""
Trinity Agent Platform - Backend API

A universal infrastructure platform for deploying Claude Code agent configurations.
Each agent runs as an isolated Docker container with standardized interfaces.

Refactored for better concern separation:
- config.py: Configuration constants
- models.py: Pydantic models
- dependencies.py: FastAPI dependencies (auth)
- services/: Business logic (audit, docker, template)
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

from config import CORS_ORIGINS, AUDIT_URL, GITHUB_PAT, GITHUB_PAT_CREDENTIAL_ID, REDIS_URL
from models import User
from dependencies import get_current_user
from services.docker_service import docker_client, list_all_agents

# Import routers
from routers.auth import router as auth_router
from routers.agents import router as agents_router, set_websocket_manager as set_agents_ws_manager
from routers.credentials import router as credentials_router
from routers.templates import router as templates_router
from routers.sharing import router as sharing_router, set_websocket_manager as set_sharing_ws_manager
from routers.mcp_keys import router as mcp_keys_router
from routers.chat import router as chat_router, set_websocket_manager as set_chat_ws_manager
from routers.schedules import router as schedules_router
from routers.git import router as git_router
from routers.activities import router as activities_router
from routers.settings import router as settings_router

# Import scheduler service
from services.scheduler_service import scheduler_service

# Import activity service
from services.activity_service import activity_service

# Import credentials manager for GitHub PAT initialization
from credentials import CredentialManager, CredentialCreate


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

# Set up scheduler broadcast callback
scheduler_service.set_broadcast_callback(manager.broadcast)

# Set up activity service WebSocket manager
activity_service.set_websocket_manager(manager)


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
    # Initialize GitHub PAT from environment to Redis
    initialize_github_pat()

    if docker_client:
        try:
            agents = list_all_agents()
            print(f"Found {len(agents)} existing Trinity agent containers")
            for agent in agents:
                print(f"  - Agent: {agent.name} (status: {agent.status}, ssh_port: {agent.port})")
        except Exception as e:
            print(f"Error checking agents: {e}")
    else:
        print("Docker not available - running in demo mode")

    # Initialize the scheduler
    try:
        scheduler_service.initialize()
        print("Scheduler service initialized")
    except Exception as e:
        print(f"Error initializing scheduler: {e}")

    yield

    # Shutdown the scheduler
    try:
        scheduler_service.shutdown()
        print("Scheduler service shutdown")
    except Exception as e:
        print(f"Error shutting down scheduler: {e}")


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
    allow_methods=["*"],
    allow_headers=["*"],
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


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now()}


# Audit logs endpoint (admin only)
@app.get("/api/audit/logs")
async def get_audit_logs(
    request: Request,
    event_type: str = None,
    agent_name: str = None,
    limit: int = 100,
    current_user: User = Depends(get_current_user)
):
    """Get audit logs (admin only)."""
    if current_user.role != "admin":
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    try:
        async with httpx.AsyncClient() as client:
            params = {"limit": limit}
            if event_type:
                params["event_type"] = event_type
            if agent_name:
                params["agent_name"] = agent_name

            response = await client.get(
                f"{AUDIT_URL}/api/audit/logs",
                params=params,
                timeout=5.0
            )
            return response.json()
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"Failed to get audit logs: {str(e)}")


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
