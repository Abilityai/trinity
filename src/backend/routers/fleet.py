# mcp: monitoring.ts (get_fleet_sync_audit)
"""
Fleet-level endpoints (#390 / S6).

Currently a single endpoint: `/api/fleet/sync-audit` — aggregates per-agent
sync state across the fleet for bulk health checks and operator tooling
(`/trinity:sync` skill, ops playbooks).

Access control follows the monitoring-router pattern: admins see every
git-enabled agent; non-admins see only the agents they own or have shared
with them. An agent-scoped MCP key resolves to its owner and sees the
owner's set (trinity-enterprise#707, decision D12): an orchestrator's fleet
sweep runs as an agent. The payload carries no raw git error text.

Exposed over MCP as `get_fleet_sync_audit` (trinity-enterprise#707).
"""
from fastapi import APIRouter, Depends

from dependencies import get_current_user
from models import User
from services.agent_service.helpers import accessible_agent_names
from services.fleet_audit_service import build_fleet_sync_audit

router = APIRouter(prefix="/api/fleet", tags=["fleet"])


@router.get("/sync-audit")
async def fleet_sync_audit(current_user: User = Depends(get_current_user)):
    """Return a fleet-wide sync audit (per #390 AC)."""
    return await build_fleet_sync_audit(agent_names=accessible_agent_names(current_user))
