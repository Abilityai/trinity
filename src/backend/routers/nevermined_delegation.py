# mcp: none — the organization's secrets are admin REST only (no MCP tool, so they never land in a transcript; ent#966 T7) and widget sessions are minted for the browser panel only; delegation read/revoke tools land in tools/nevermined.ts
"""
Nevermined card delegation router (trinity-enterprise#965/#966).

The buyer side of Nevermined, beside the provider-side config in
``routers/nevermined.py``. Nevermined holds the card and the mandate; these
routes configure the instance's Nevermined organization and bind the
delegation id a person created in Nevermined's widget to one agent.

Errors are ``{"detail": {"code", "message"}}`` so the panel (ent#967) can branch
on ``code``. No route returns a secret.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from dependencies import (
    agent_principal_name,
    get_authorized_agent_by_name,
    get_current_user,
    get_owned_agent_by_name,
    require_admin,
    require_person,
)
from models import (
    NeverminedDelegationBind,
    NeverminedOrganizationState,
    NeverminedOrganizationUpdate,
    User,
)
from services import credential_requirements_service
from services import nevermined_delegation_service as delegation_service
from services.platform_audit_service import AuditEventType, platform_audit_service
from services.rate_limiter import enforce as enforce_rate_limit

router = APIRouter(prefix="/api/nevermined", tags=["nevermined"])
logger = logging.getLogger(__name__)


def _raise(err: "delegation_service.DelegationError"):
    raise HTTPException(status_code=err.status_code, detail=err.detail(), headers=err.headers)


# ---------------------------------------------------------------------------
# Organization (admin)
# ---------------------------------------------------------------------------

@router.get("/organization", response_model=NeverminedOrganizationState)
async def get_nevermined_organization(current_user: User = Depends(require_admin)):
    """The instance's Nevermined organization — never its secrets."""
    return delegation_service.organization_state()


@router.put("/organization", response_model=NeverminedOrganizationState)
async def put_nevermined_organization(
    body: NeverminedOrganizationUpdate,
    current_user: User = Depends(require_admin),
):
    """Create or update the Nevermined organization. Secrets are write-only."""
    try:
        result = delegation_service.save_organization(
            org_id=body.org_id,
            environment=body.environment,
            widget_secret=body.widget_secret,
            agent_api_key=body.agent_api_key,
            updated_by=current_user.email or current_user.username,
        )
    except delegation_service.DelegationError as err:
        _raise(err)

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="nevermined_organization_set",
        source="api",
        actor_user=current_user,
        target_type="nevermined_organization",
        target_id=body.org_id,
        details={
            "org_id": body.org_id,
            "environment": body.environment,
            "created": result["created"],
            # Names of the secrets that changed — never their values.
            "rotated": result["rotated"],
        },
    )
    logger.info(
        "Nevermined organization %s (environment=%s, rotated=%s) by %s",
        "created" if result["created"] else "updated",
        body.environment, ",".join(result["rotated"]) or "none", current_user.username,
    )
    return result["state"]


@router.delete("/organization", response_model=NeverminedOrganizationState)
async def delete_nevermined_organization(current_user: User = Depends(require_admin)):
    """Remove the Nevermined organization; refused while an agent holds a mandate."""
    try:
        removed = delegation_service.delete_organization()
    except delegation_service.DelegationError as err:
        _raise(err)

    if removed:
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="nevermined_organization_delete",
            source="api",
            actor_user=current_user,
            target_type="nevermined_organization",
        )
        logger.info("Nevermined organization removed by %s", current_user.username)
    return delegation_service.organization_state()


# ---------------------------------------------------------------------------
# Widget session (person, owner tier)
# ---------------------------------------------------------------------------

#: A minted session is a bearer credential for Nevermined's widget: no cache,
#: no proxy, no browser history may keep it.
NO_STORE = {"Cache-Control": "no-store"}


@router.post("/agents/{agent_name}/widget-session")
async def create_widget_session(
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(require_person),
):
    """Mint a Nevermined widget session so the signed-in person can enroll a card
    with Nevermined and delegate a mandate to this agent. Nevermined holds the
    card; Trinity only forwards the session."""
    enforce_rate_limit(
        f"nvm-session:{current_user.username}:{agent_name}", 10, 60,
        detail="Too many Nevermined widget sessions requested for this agent.",
    )
    try:
        result = await delegation_service.mint_widget_session(
            current_user.email, agent_name=agent_name, actor=current_user.username,
        )
    except delegation_service.DelegationError as err:
        raise HTTPException(
            status_code=err.status_code, detail=err.detail(),
            headers={**NO_STORE, **(err.headers or {})},
        )
    return JSONResponse(content=result, headers=NO_STORE)


# ---------------------------------------------------------------------------
# Delegation binding
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_name}/delegation")
async def get_agent_delegation(
    agent_name: str = Depends(get_authorized_agent_by_name),
    current_user: User = Depends(get_current_user),
):
    """What this agent can spend through its Nevermined card mandate — live
    from Nevermined, never cached, never computed here. An agent key reads its
    own budget only; any other agent is the same 404 as a missing one."""
    principal_agent = agent_principal_name(current_user)
    if principal_agent is not None and principal_agent != agent_name:
        raise HTTPException(status_code=404, detail="Agent not found")
    enforce_rate_limit(
        f"nvm-read:{current_user.username}:{agent_name}", 30, 60,
        detail="Too many Nevermined spending reads for this agent.",
    )
    return await delegation_service.read_delegation(agent_name)


@router.post("/agents/{agent_name}/delegation", status_code=201)
async def bind_agent_delegation(
    body: NeverminedDelegationBind,
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(require_person),
):
    """Bind the Nevermined delegation the widget created to this agent. Nevermined
    holds the card and the mandate; Trinity records the id and hands the agent
    its `NVM_*` keys, effective at the agent's next execution."""
    try:
        delegation_service.require_organization()
    except delegation_service.DelegationError as err:
        _raise(err)
    enforce_rate_limit(
        f"nvm-bind:{current_user.username}:{agent_name}", 10, 60,
        detail="Too many Nevermined bind attempts for this agent.",
    )
    try:
        state = await delegation_service.bind_delegation(
            agent_name,
            delegation_id=body.delegation_id,
            payment_method_id=body.payment_method_id,
            provider=body.provider,
            actor=current_user.username,
            created_by=current_user.email or current_user.username,
        )
    except delegation_service.DelegationError as err:
        _raise(err)

    credential_requirements_service.invalidate_report_cache(agent_name)
    await platform_audit_service.log(
        event_type=AuditEventType.CREDENTIALS,
        event_action="nevermined_delegation_bind",
        source="api",
        actor_user=current_user,
        target_type="agent",
        target_id=agent_name,
        details={
            "delegation_id": body.delegation_id,
            "payment_method_id": body.payment_method_id,
            "provider": body.provider,
            "environment": state["delegation"]["environment"],
            "api_key_injected": state["api_key_injected"],
        },
    )
    return state


@router.delete("/agents/{agent_name}/delegation")
async def revoke_agent_delegation(
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(require_person),
):
    """Revoke this agent's Nevermined mandate: Nevermined refuses the agent's
    next paid call at once; the keys leave the agent's credential file when
    it is running, otherwise at its next bind."""
    try:
        result = await delegation_service.revoke_delegation(
            agent_name, actor=current_user.username,
            revoked_by=current_user.email or current_user.username,
        )
    except delegation_service.DelegationError as err:
        _raise(err)

    credential_requirements_service.invalidate_report_cache(agent_name)
    if result.get("already_revoked"):  # a concurrent revoke already audited it
        return {
            "revoked": True,
            "delegation_id": result["delegation_id"],
            "env_removed": False,
            "env_note": result["env_note"],
        }
    await platform_audit_service.log(
        event_type=AuditEventType.CREDENTIALS,
        event_action="nevermined_delegation_revoke",
        source="api",
        actor_user=current_user,
        target_type="agent",
        target_id=agent_name,
        details={
            "delegation_id": result["delegation_id"],
            "environment": result["environment"],
            "env_removed": result["env_removed"],
            "api_key_removed": result["api_key_injected"] and result["env_removed"],
        },
    )
    return {
        "revoked": True,
        "delegation_id": result["delegation_id"],
        "env_removed": result["env_removed"],
        "env_note": result["env_note"],
    }
