"""
Agent Server - Internal API for Claude Code agents
Runs inside each agent container on port 8000 (internal Docker network only)

SECURITY: This server is NOT exposed externally. All access goes through
the authenticated Trinity backend at /api/agents/{name}/chat

The HTML UI has been removed for security - use the Trinity web interface instead.
"""
import os
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import (
    chat_router,
    activity_router,
    credentials_router,
    git_router,
    files_router,
    trinity_router,
    info_router,
)
from .state import agent_state
from .services.trinity_mcp import inject_trinity_mcp_if_configured

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create FastAPI application
app = FastAPI(
    title="Claude Agent API",
    description="Internal API for Claude Code agent (not exposed externally)",
    version="2.0.0"
)

# CORS - only needed for internal Docker network communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Backend communicates via internal network
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include all routers
app.include_router(info_router)  # Root and health endpoints
app.include_router(chat_router)  # Chat endpoints
app.include_router(activity_router)  # Session activity endpoints
app.include_router(credentials_router)  # Credential management
app.include_router(git_router)  # Git sync endpoints
app.include_router(files_router)  # File browser endpoints
app.include_router(trinity_router)  # Trinity injection API


def run_server():
    """Run the agent server with uvicorn"""
    import uvicorn

    port = int(os.getenv("AGENT_SERVER_PORT", "8000"))

    logger.info(f"Starting Agent API Server on port {port}")
    logger.info(f"Agent Name: {agent_state.agent_name}")
    logger.info(f"Claude Code Available: {agent_state.claude_code_available}")
    logger.info("SECURITY: This server is internal-only, accessed via Trinity backend proxy")

    # Phase: Agent-to-Agent Collaboration - Inject Trinity MCP if configured
    if inject_trinity_mcp_if_configured():
        logger.info("Trinity MCP server configured - agent-to-agent communication enabled")

    # Bind to 0.0.0.0 for Docker internal network communication
    # Port is NOT exposed externally - backend proxies requests via Docker network
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )


if __name__ == "__main__":
    run_server()
