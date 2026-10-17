"""AAuth agent identity — the switch and the issuer (abilityai/trinity-enterprise#862).

`GET/PUT /api/settings/aauth`. Turning AAuth on publishes this instance's key
and starts accepting signed calls from agents on other instances that an agent
here has listed; changing the issuer changes the identity of every agent on the
instance. Both are grants, so the write is admin AND interactive: an MCP key of
any scope (agent, user, system) is refused — `assert_admin` alone admits a
user- or system-scoped key carrying an admin owner's role.

The rules live in `services.aauth.settings`; this module holds the gates and
the audit row (Invariant #1).
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from dependencies import assert_admin, get_current_user, require_interactive
from models import AAuthSettingsUpdate, User
from services.aauth import settings as aauth_settings
from services.platform_audit_service import AuditEventType, platform_audit_service

router = APIRouter()


@router.get("/aauth")
async def get_aauth_settings(current_user: User = Depends(get_current_user)):
    """The switches, the issuer, where each value came from, and readiness. Admin-only."""
    assert_admin(current_user)
    return aauth_settings.get_state()


@router.put("/aauth")
async def update_aauth_settings(
    body: AAuthSettingsUpdate,
    request: Request,
    current_user: User = Depends(require_interactive),
):
    """Change the switches and/or the issuer. Admin and interactive only. Audited.

    Only the fields sent are written. Nothing is repaired: an invalid issuer,
    person server or resource allow-list is `422 {reason: invalid_<field>}`.
    """
    assert_admin(current_user)
    try:
        changes = aauth_settings.apply_update(
            enabled=body.enabled, issuer=body.issuer, prototype_enabled=body.prototype_enabled,
            person_server=body.person_server, resource_allowlist_raw=body.resource_allowlist,
        )
    except aauth_settings.AAuthSettingsError as e:
        status = 400 if e.reason == "nothing_to_change" else 422
        raise HTTPException(status_code=status, detail={"reason": e.reason, "message": e.message})

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="aauth_config_change",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=request.scope["path"],
        request_id=getattr(request.state, "request_id", None),
        details={"setting": "aauth", "changes": changes},
    )
    return aauth_settings.get_state()
