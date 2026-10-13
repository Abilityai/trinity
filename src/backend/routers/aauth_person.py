# mcp: tools/aauth.ts
"""AAuth person token — prototype (trinity-enterprise#878).

``POST /api/agents/{agent_name}/aauth/person-token`` obtains a person token
for a resource from the configured person server AS the agent, or resumes a
pending binding by its handle. The router does four things and nothing else
(Invariant #1): the gates, the two local rate budgets, the audit row, and the
HTTP mapping of a service outcome.

Gates, in order:

1. **AAuth live** (flag ON + issuer) else a uniform 404 — a disabled deployment
   does not advertise the surface (the §32.5 FR-11 shape).
2. **The agent's own key only.** ``AuthorizedAgentByName`` admits the owner,
   shared users and admins; here that would let a human make the backend sign
   as the agent, and the #623 invariant is that only the agent's own key path
   may cause a signature as that agent. A human principal gets 403
   ``aauth_agent_only`` — stricter than ``a2a.py::_assert_outbound_self``,
   which admits humans for attribution-only reasons. This is a *use* of the
   capability, not a grant, so ``reject_agent_principal`` is the wrong tool.
   An ephemeral ghost key is refused by construction (not on the
   ``_EPHEMERAL_ALLOWED_ROUTES`` fence); a connector key by
   ``_enforce_connector_scope`` inside the dependency.

The response never carries the token (``PersonTokenResponse`` has no field
for it); the audit row carries ``token_jti`` and never the handle, url or
code. Binding hijack is a stated prototype limit (plan T7): the link is
relayed by the agent itself, so the tool description says "relay only to
your owner, privately — whoever approves first becomes the person this agent
acts for", and the audit row records the execution and trigger source.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from dependencies import AuthorizedAgentByName, get_current_user
from models import PersonTokenInteraction, PersonTokenRequest, PersonTokenResponse, User
from services import rate_limiter
from services.aauth import config as aauth_config
from services.aauth import person_token as pt
from services.aauth import signer as aauth_signer
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["aauth"])

#: Separate budgets (plan §2.5 E18 / S8): a new request mints a Hellō code,
#: a poll does not — and a script polling at `Retry-After: 5` is 12/min.
POST_RATE_LIMIT, POST_RATE_WINDOW = 10, 60
POLL_RATE_LIMIT, POLL_RATE_WINDOW = 30, 60
FLEET_RATE_LIMIT, FLEET_RATE_WINDOW = 120, 60

#: Refusals that are configuration or infrastructure, not protocol outcomes,
#: and therefore HTTP errors with `{reason, message}`.
_REFUSAL_STATUS = {
    "invalid_resource": 422,
    "person_server_unconfigured": 409,
    "state_unavailable": 503,
    "person_server_unreachable": 502,
    "person_server_metadata_invalid": 502,
}

_NEXT_STEP = {
    "issued": ("The platform now holds a person token for this resource, bound to this agent's key; "
               "present it by calling the resource — there is nothing to relay and no secret to handle."),
    "pending": ("Relay `interaction.link` (or url + code) PRIVATELY to your OWNER in this conversation and nowhere "
                "else — whoever approves first becomes the person this agent acts for. Then call again with "
                "`pending_handle` after `interaction.retry_after` seconds; keep doing so until the outcome changes."),
    "denied": "The person declined. Do not retry on your own; ask again only if your owner wants to.",
    "abandoned": "The person abandoned the approval. Ask your owner whether to start over (call again without a handle).",
    "revoked": "The binding was revoked at the person server. Ask your owner before starting over.",
    "expired": "The interaction expired before it was approved. Call again without a handle to start over.",
    "gone": "This pending interaction is gone and must not be polled again. Start a fresh request if your owner wants to.",
    "slow_down": "The person server asked for a slower poll; wait `interaction.retry_after` seconds, then resume.",
    "pending_unknown": "No pending interaction under that handle (it may have expired). Call again without a handle.",
    "too_many_pending": "This agent already has the maximum number of open interactions; resume or let them expire first.",
    "signature_rejected": "The person server rejected this instance's signature; tell your operator (nothing you do will change it).",
    "unsupported_requirement": "The person server requires something this platform cannot satisfy yet; tell your operator.",
    "token_invalid": "The person server issued a token that failed the platform's checks; tell your operator.",
}


def _next_step(outcome: pt.Outcome) -> str:
    if isinstance(outcome, pt.PersonTokenIssued):
        return _NEXT_STEP["issued"]
    if isinstance(outcome, pt.PendingInteraction):
        return _NEXT_STEP["slow_down"] if outcome.status == "slow_down" else _NEXT_STEP["pending"]
    return _NEXT_STEP.get(outcome.code, "The person server refused; tell your operator the reason and do not retry blindly.")


def _response(outcome: pt.Outcome) -> PersonTokenResponse:
    if isinstance(outcome, pt.PersonTokenIssued):
        return PersonTokenResponse(outcome="issued", jti=outcome.jti, aud=outcome.aud, sub=outcome.sub,
                                   issuer=outcome.iss, exp=outcome.exp, cached=outcome.cached,
                                   reissued_for_key=outcome.reissued_for_key, next_step=_next_step(outcome))
    if isinstance(outcome, pt.PendingInteraction):
        return PersonTokenResponse(
            outcome="pending", aud=outcome.resource, issuer=outcome.iss,
            interaction=PersonTokenInteraction(url=outcome.url, code=outcome.code, link=outcome.link,
                                               pending_handle=outcome.handle, retry_after=outcome.retry_after,
                                               status=outcome.status, expires_at=outcome.expires_at),
            next_step=_next_step(outcome),
        )
    return PersonTokenResponse(success=False, outcome="refused", aud=outcome.resource, issuer=outcome.iss,
                               reason=outcome.code, message=outcome.detail, ps_status=outcome.ps_status,
                               next_step=_next_step(outcome))


def _require_live() -> None:
    if not aauth_config.active_issuer():
        raise HTTPException(status_code=404, detail="Not found")


def _assert_own_key(current_user: User, agent_name: str) -> None:
    if not current_user.agent_name or current_user.agent_name != agent_name:
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "aauth_agent_only",
                "message": (
                    "A person token is requested AS the agent's AAuth identity, so only the agent "
                    "itself may call this (use the agent's own key)."
                ),
            },
        )


@router.post("/{agent_name}/aauth/person-token", response_model=PersonTokenResponse)
async def request_person_token(
    agent_name: AuthorizedAgentByName,
    body: PersonTokenRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Obtain a person token for `body.resource` as `agent_name`, or resume a pending binding."""
    _require_live()
    _assert_own_key(current_user, agent_name)

    resuming = body.pending_handle is not None
    if resuming:
        rate_limiter.enforce(f"aauth_pt_poll:{agent_name}", POLL_RATE_LIMIT, POLL_RATE_WINDOW,
                             detail="Too many person-token polls for this agent.")
    else:
        rate_limiter.enforce(f"aauth_pt_post:{agent_name}", POST_RATE_LIMIT, POST_RATE_WINDOW,
                             detail="Too many person-token requests for this agent.")
    rate_limiter.enforce("aauth_pt:fleet", FLEET_RATE_LIMIT, FLEET_RATE_WINDOW,
                         detail="Too many person-token requests across the fleet.")

    try:
        if resuming:
            outcome = await pt.resume(agent_name, body.pending_handle,
                                      wait=pt.RESUME_WAIT if body.wait is None else body.wait)
        else:
            outcome = await pt.person_token(agent_name, body.resource,
                                            wait=pt.POST_WAIT if body.wait is None else body.wait)
    except aauth_signer.SigningUnavailable:
        raise HTTPException(status_code=404, detail="Not found")

    action = pt.audit_action(outcome)
    if action:
        await platform_audit_service.log(
            event_type=AuditEventType.CREDENTIALS,
            event_action=action,
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            mcp_key_id=getattr(current_user, "mcp_key_id", None),
            mcp_scope=getattr(current_user, "mcp_scope", None),
            target_type="agent",
            target_id=agent_name,
            endpoint=request.scope["path"],
            request_id=getattr(request.state, "request_id", None),
            details=pt.audit_details(agent_name, outcome, execution_id=body.execution_id,
                                     trigger_source=body.trigger_source or "api"),
        )

    if isinstance(outcome, pt.PersonTokenRefused):
        status = _REFUSAL_STATUS.get(outcome.code)
        if status is not None:
            raise HTTPException(status_code=status, detail={"reason": outcome.code, "message": outcome.detail})
    return _response(outcome)
