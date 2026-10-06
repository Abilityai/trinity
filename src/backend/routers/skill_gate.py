# mcp: none — the in-container skill-gate hook's own check; the agent's own key, own gates only; not a tool
"""
Gated skills — the in-container hook's check (trinity-enterprise#752).

`POST /api/skill-gate/check` is called by the PreToolUse hook in the agent
image (`docker/base-image/hooks/skill-gate.py`) before Claude Code loads a skill
into a run. The agent comes from the KEY (`get_self_agent`); the request names
no agent. A read of the agent's own gates and of its own run's clearance — a
use, not a grant. Every verdict is a 200; only an unreadable gate map is not
(503 `gate_unavailable`, through the app's SkillGateError handler), and the
hook reads every non-200 as "no answer" and decides by its marker.
"""
from fastapi import APIRouter, Depends

from dependencies import get_current_user, get_self_agent
from models import SkillGateCheckRequest, SkillGateCheckResponse, User
from services import skill_gate_service

router = APIRouter(prefix="/api/skill-gate", tags=["skill-gate"])


@router.post("/check", response_model=SkillGateCheckResponse)
async def check_skill_invocation(
    body: SkillGateCheckRequest,
    agent_name: str = Depends(get_self_agent),
    current_user: User = Depends(get_current_user),
):
    """May this run load these skills? Asked by the agent about itself."""
    return await skill_gate_service.check_invocation(
        agent_name,
        via=body.via,
        invoked=body.invoked,
        names=body.names,
        resolved=body.resolved,
        subagent=body.subagent,
        execution_id=body.execution_id,
        marker=body.marker,
        current_user=current_user,
    )
