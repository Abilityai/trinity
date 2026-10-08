# mcp: none for the in-container check (the agent's own key, own gates only; not a tool); skills.ts (list_skill_gates, set_skill_gate, clear_skill_gate) for the gate map
"""
Gated skills — the in-container hook's check (trinity-enterprise#752) and the
per-agent gate map (trinity-enterprise#753).

`POST /api/skill-gate/check` is called by the PreToolUse hook in the agent
image (`docker/base-image/hooks/skill-gate.py`) before Claude Code loads a skill
into a run. The agent comes from the KEY (`get_self_agent`); the request names
no agent. A read of the agent's own gates and of its own run's clearance — a
use, not a grant. Every verdict is a 200; only an unreadable gate map is not
(503 `gate_unavailable`, through the app's SkillGateError handler), and the
hook reads every non-200 as "no answer" and decides by its marker.

`/api/agents/{agent_name}/skill-gates[/{skill_name}]` is the gate map itself:
which skills on the agent need approval, and which kind of person approves.
Read by anyone with access to the agent (an agent key: its own, or — holding
`skills.manage` — an agent its owner owns). Changed by a person who owns the
agent or is an admin, or by an agent holding `skills.manage` on an agent its
owner owns — never on itself (the agent a gate constrains must not lift it).
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from db.capability_grants import CAPABILITY_SKILLS_MANAGE
from dependencies import (
    get_current_user,
    get_capability_owned_agent,
    get_self_agent,
    get_skill_gate_readable_agent_by_name,
    is_person_principal,
    require_person_or_capability,
)
from models import (
    SkillGateCheckRequest,
    SkillGateCheckResponse,
    SkillGateClearResponse,
    SkillGateMapResponse,
    SkillGateSetRequest,
    SkillGateWriteResponse,
    User,
)
from services import skill_gate_map_service, skill_gate_service

router = APIRouter(prefix="/api/skill-gate", tags=["skill-gate"])
agent_router = APIRouter(prefix="/api/agents", tags=["skill-gate"])

# Changing a gate: a person, or a skills.manage holder on an agent its owner
# owns — never on itself (trinity-enterprise#753, the #3236 shape).
_gate_writer = require_person_or_capability(CAPABILITY_SKILLS_MANAGE, self_person_only=True)


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


def _context(request: Request, current_user: User):
    return skill_gate_map_service.gate_context(
        current_user,
        ip=request.client.host if request.client else None,
        endpoint=request.scope.get("path"),
        request_id=getattr(request.state, "request_id", None),
    )


def _for_principal(gate: dict, current_user: User) -> dict:
    """`set_by` is a username — an email for email-login users. People stay
    with people (#715): a machine key (an agent, the system key) never reads who
    set a gate, so a non-owner admin's address never reaches an agent's context."""
    if is_person_principal(current_user) and not getattr(current_user, "vouched_source_agent", None):
        return gate
    return {**gate, "set_by": None}


def _refused(e: skill_gate_map_service.SkillGateMapRefused) -> HTTPException:
    if e.status_code == 404:   # renamed or deleted after the access check: the uniform 404
        return HTTPException(status_code=404, detail="Agent not found")
    from services.chat_execution_service import ERROR_CODE_HEADER
    return HTTPException(status_code=e.status_code, detail=e.as_detail(),
                         headers={ERROR_CODE_HEADER: e.code})


@agent_router.get("/{agent_name}/skill-gates", response_model=SkillGateMapResponse)
async def list_agent_skill_gates(
    agent_name: str = Depends(get_skill_gate_readable_agent_by_name),
    current_user: User = Depends(get_current_user),
):
    """The agent's gated skills, who approves each and whether that approver
    kind reaches anyone right now."""
    view = skill_gate_map_service.list_gates(agent_name)
    view["gates"] = [_for_principal(g, current_user) for g in view["gates"]]
    return view


@agent_router.put("/{agent_name}/skill-gates/{skill_name}", response_model=SkillGateWriteResponse)
async def set_agent_skill_gate(
    skill_name: str,
    body: SkillGateSetRequest,
    request: Request,
    current_user: User = Depends(_gate_writer),
    agent_name: str = Depends(get_capability_owned_agent),
):
    """Gate a skill on the agent, or change its gate. Only the fields sent are
    applied."""
    try:
        out = await skill_gate_map_service.set_gate(
            agent_name, skill_name, changes=body.model_dump(include=body.model_fields_set),
            ctx=_context(request, current_user))
    except skill_gate_map_service.SkillGateMapRefused as e:
        raise _refused(e)
    return {**out, "gate": _for_principal(out["gate"], current_user)}


@agent_router.delete("/{agent_name}/skill-gates/{skill_name}", response_model=SkillGateClearResponse)
async def clear_agent_skill_gate(
    skill_name: str,
    request: Request,
    current_user: User = Depends(_gate_writer),
    agent_name: str = Depends(get_capability_owned_agent),
):
    """Clear the gate on a skill. Idempotent."""
    try:
        return await skill_gate_map_service.clear_gate(
            agent_name, skill_name, ctx=_context(request, current_user))
    except skill_gate_map_service.SkillGateMapRefused as e:
        raise _refused(e)
