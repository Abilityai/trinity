# mcp: none — the organization's secrets are admin REST only (no MCP tool, so they never land in a transcript; ent#966 T7); delegation read/revoke tools land in tools/nevermined.ts
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

from dependencies import require_admin
from models import NeverminedOrganizationState, NeverminedOrganizationUpdate, User
from services import nevermined_delegation_service as delegation_service
from services.platform_audit_service import AuditEventType, platform_audit_service

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
