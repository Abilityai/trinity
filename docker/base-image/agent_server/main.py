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

from .middleware.auth import AgentAuthMiddleware
from .routers import (
    chat_router,
    activity_router,
    credentials_router,
    git_router,
    files_router,
    trinity_router,
    info_router,
    dashboard_router,
    skills_router,
    snapshot_router,
    brain_orb_router,
)
from .state import agent_state
from .services.execution_env import arm_subscription_auth_guard
from .services.trinity_mcp import inject_trinity_mcp_if_configured
from .auto_sync import schedule_auto_sync_if_enabled
from .heartbeat import schedule_heartbeat
from .services.result_callback import schedule_pending_result_resend
from .services.orphan_sweeper import schedule_orphan_sweeper
from .utils.thread_diagnostics import (
    enable as _enable_thread_diagnostics,
    schedule_loop_watchdog as _schedule_loop_watchdog,
)
from .services.pull_worker import (
    schedule_pull_workers,
    schedule_pending_pull_result_resend,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create FastAPI application
# #2455: arm the thread-stack dump BEFORE anything can wedge. Free until
# used — a signal handler plus a function; no polling, no timer. Gives ops
# `kill -USR1 <pid>` on a wedged container, and lets the stuck-reader
# branches below dump automatically at the instant they notice.
_enable_thread_diagnostics()

app = FastAPI(
    title="Claude Agent API",
    description="Internal API for Claude Code agent (not exposed externally)",
    version="2.0.0"
)

# #1159: per-agent inbound auth. Every backend→agent call carries a derived
# X-Trinity-Agent-Token; this middleware enforces it on all routes except the
# Docker /health probe and OPTIONS preflight, with a grace path (empty token
# env → allow) for the non-breaking rollout. CORSMiddleware was removed: the
# agent server is internal-only (Docker network), never hit by a browser, so
# allow_origins=["*"] + allow_credentials=True was pure attack surface.
app.add_middleware(AgentAuthMiddleware)

# Include all routers
app.include_router(info_router)  # Root and health endpoints
app.include_router(chat_router)  # Chat endpoints
app.include_router(activity_router)  # Session activity endpoints
app.include_router(credentials_router)  # Credential management
app.include_router(git_router)  # Git sync endpoints
app.include_router(files_router)  # File browser endpoints
app.include_router(trinity_router)  # Trinity injection API
app.include_router(dashboard_router)  # Dashboard endpoint
app.include_router(skills_router)  # Skills/playbooks listing endpoint
app.include_router(snapshot_router)  # Snapshot/restore primitives (#384, S3)
app.include_router(brain_orb_router)  # Brain Orb visualization data (#58)

# #2114: when the boot baseline says subscription auth is active (truthy
# CLAUDE_CODE_OAUTH_TOKEN on a Claude runtime), force-unset API-key-style auth
# from every spawn env — a stale .env ANTHROPIC_API_KEY otherwise shadows the
# subscription token at every spawn (Claude Code prefers the key). Boot-time,
# not module-import-time, so tests control INITIAL_ENV before arming.
arm_subscription_auth_guard()

# #389 S1a: auto-sync heartbeat loop (gated by GIT_SYNC_AUTO env var).
schedule_auto_sync_if_enabled(app)

# RELIABILITY-004 / #307: liveness heartbeat loop. Gated on TRINITY_BACKEND_URL
# + TRINITY_MCP_API_KEY both present, so old-image agents simply never beat.
schedule_heartbeat(app)

# #1083 fire-and-forget: on startup re-send any result-callback envelope left on
# disk by a crash/restart mid-callback, so completed work isn't lost to a phantom
# LEASE_EXPIRED. Gated on the same callback creds as the heartbeat.
schedule_pending_result_resend(app)

# #817 follow-up: periodic cgroup orphan sweep. Catches orphans that
# escape the per-task cleanup path — specifically Eugene's production
# scenario where Trinity-side CB termination skips drain_reader_threads
# and subsequent tasks fast-fail before reaching the agent.
schedule_orphan_sweeper(app)

# #2455 (09-02 occurrence): the wedge is not a teardown artifact — the event
# loop stopped completing requests ~10 min BEFORE claude finished and ~3h
# before any drain branch could notice, while heartbeats kept the agent
# looking healthy. A beat task + watchdog THREAD is the only vantage point
# that can notice a wedged loop; past a 60s stall it dumps every thread's
# stack and the loop's task await-chains, re-dumping every 5 min while the
# stall persists, and logs the recovery that bounds the wedge window.
_schedule_loop_watchdog(app)

# #946 / #1081 Phase 2: agent-side pull worker pool. DEFAULT OFF — gated on the
# per-agent TRINITY_PULL_MODE flag (allowlist-injected by the backend). When off
# (every existing agent) this registers no startup handler and the push path is
# unchanged; when on, a bounded pool pulls work via /api/internal/next-task.
schedule_pull_workers(app)

# B6 fix (#1081): on startup re-send any pull terminal left on disk by a
# shutdown/deploy mid-delivery, so a completed-but-unreported turn isn't lost
# (the row would otherwise stay `running` → the lease reaper re-runs the whole
# turn from scratch). Mirrors schedule_pending_result_resend (#1083). Gated on
# the same worker creds; a no-op for any agent that never ran the pull pool.
schedule_pending_pull_result_resend(app)


def run_server():
    """Run the agent server with uvicorn"""
    import uvicorn

    port = int(os.getenv("AGENT_SERVER_PORT", "8000"))

    logger.info(f"Starting Agent API Server on port {port}")
    logger.info(f"Agent Name: {agent_state.agent_name}")
    logger.info(f"Runtime: {agent_state.agent_runtime} (available: {agent_state.runtime_available})")
    logger.info(f"Context Window: {agent_state.session_context_window:,} tokens")
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
