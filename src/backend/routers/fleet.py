"""
Fleet-level endpoints (#390 / S6).

Currently a single endpoint: `/api/fleet/sync-audit` — aggregates per-agent
sync state across the fleet for bulk health checks and operator tooling
(`/trinity:sync` skill, ops playbooks).

Access control follows the monitoring-router pattern: admins see every
git-enabled agent; non-admins see only the agents they own or have shared
with them.
"""
from fastapi import APIRouter, Depends

from dependencies import get_current_user
from models import User
from services.agent_service.helpers import get_accessible_agents
from services.fleet_audit_service import build_fleet_sync_audit

router = APIRouter(prefix="/api/fleet", tags=["fleet"])


@router.get("/sync-audit")
async def fleet_sync_audit(current_user: User = Depends(get_current_user)):
    """Return a fleet-wide sync audit (per #390 AC)."""
    if current_user.role == "admin":
        agent_names = None  # No filter — see all.
    else:
        accessible = get_accessible_agents(current_user)
        agent_names = [a["name"] for a in accessible]
    return await build_fleet_sync_audit(agent_names=agent_names)
