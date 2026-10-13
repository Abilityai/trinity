# mcp: tools/aauth.ts
"""AAuth resource call — prototype (trinity-enterprise#879).

``POST /api/agents/{agent_name}/aauth/resource-call`` calls an allow-listed
AAuth resource (``{resource}{path}``) AS the agent, climbing the draft's
requirement ladder (agent token → person token → auth token, ent#878's
binding leg underneath), or resumes a pending call by its handle. The router
does four things and nothing else (Invariant #1): the gates, the rate
budgets, the audit + activity rows, and the HTTP mapping of a service outcome.

Gates, in order — ent#878's, reused not re-implemented (plan T7):

1. **AAuth live** (``aauth_person.require_live`` as a ``Depends`` declared
   after ``get_current_user``): flag off → uniform 404 BEFORE body validation,
   so a malformed body cannot reveal the route while inert.
2. **The agent's own key only** (``aauth_person.assert_own_key``): a human
   owner/admin, a sibling agent's key or a ``system`` key → 403
   ``aauth_agent_only``. This is a USE of the agent's identity, never a grant.
   The allow-list is consulted only after this gate, so a refused principal
   learns nothing about which resources are listed (no oracle).

Request: a discriminated body — a new call (``resource, method, path, body,
justification, dedup_label``) XOR a resume (``pending_handle``); no field can
carry a token (pinned). Protocol outcomes (``result``, ``interaction_required``,
``pending``, ``denied``, ``expired``) are **HTTP 200 with ``outcome``** — they
are things the agent must relay to a person, not transport failures (the
#3185 lesson: name the actor). Refusals are HTTP errors with
``{reason, message}`` so an operator's curl sees them as such (plan T3).
"""
from __future__ import annotations

import json
import logging
from typing import Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from dependencies import AuthorizedAgentByName, get_current_user
from models import (AAuthBudget, AAuthResourceCallNew, AAuthResourceCallRequest, AAuthResourceCallResponse,
                    AAuthResourceCallResume, AAuthResourceInteraction, User)
from routers import aauth_person
from services import rate_limiter
from services.aauth import resource_call
from services.aauth import resource_client as rc
from services.aauth import signer as aauth_signer
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["aauth"])

#: A new call may cost the resource budget and may mint a consent code; a
#: resume is a poll (`Retry-After: 5` is 12/min). Separate budgets (ent#878 shape).
CALL_RATE_LIMIT, CALL_RATE_WINDOW = 20, 60
RESUME_RATE_LIMIT, RESUME_RATE_WINDOW = 30, 60
FLEET_RATE_LIMIT, FLEET_RATE_WINDOW = 120, 60

#: `Refusal.code` → HTTP. Default 502: "the peer did something we refused or
#: could not use" (the #736 rule — never 200 with the peer's error as an answer).
_REFUSAL_STATUS = {
    "invalid_resource": 422,
    "invalid_method": 422,
    "invalid_path": 422,
    "unsupported_signature_components": 422,
    "resource_not_allowed": 403,
    "person_server_unconfigured": 409,
    "state_unavailable": 503,
    "timeout": 504,
    "rate_limited": 429,
    "payment_required": 402,
    "endpoint_not_https": 400,
    "endpoint_private_address": 400,
    "endpoint_dns_failure": 400,
    "endpoint_invalid": 400,
}

_NEXT_STEP = {
    "result": ("The resource answered: `status` is its HTTP status and `body` its response (sanitised, capped). "
               "A later call with a new `dedup_label` reuses the held auth token — no new consent, no new "
               "person-server trip."),
    "interaction": ("Relay `interaction.link` (or url + code) PRIVATELY to your OWNER in this conversation and "
                    "nowhere else — whoever approves first becomes the person this agent acts for. Then call again "
                    "with `pending_handle` after `interaction.retry_after` seconds; keep doing so until the outcome "
                    "changes. The call you asked for is re-sent automatically once consent arrives."),
    "approval": ("The person server is waiting for the person to approve; there is nothing to relay. Call again "
                 "with `pending_handle` after `interaction.retry_after` seconds."),
    "deferred": ("The resource accepted the call and is still working on it. Call again with `pending_handle` "
                 "after `interaction.retry_after` seconds."),
    "denied": ("The person declined (or the person server could not reach them). Do not retry on your own and do "
               "not route around it; ask again only if your owner wants to."),
    "expired": "The pending call expired before it finished. Call again WITHOUT a handle to start over.",
}


def _next_step(outcome: rc.Outcome) -> str:
    if isinstance(outcome, rc.ResourceResult):
        return _NEXT_STEP["result"]
    if isinstance(outcome, rc.InteractionRequired):
        return _NEXT_STEP["interaction"]
    if isinstance(outcome, rc.ResourcePending):
        return _NEXT_STEP["approval" if outcome.requirement == "approval" else "deferred"]
    if isinstance(outcome, rc.Denied):
        return _NEXT_STEP["denied"]
    return _NEXT_STEP["expired"]


def _budget(outcome: rc.Outcome) -> Optional[AAuthBudget]:
    budget = getattr(outcome, "budget", None)
    return AAuthBudget(cost=budget.cost, balance=budget.balance, unit=budget.unit) if budget else None


def _response(outcome: rc.Outcome, *, replayed: bool) -> AAuthResourceCallResponse:
    common = dict(resource=outcome.resource or None, challenges=list(outcome.challenges),
                  replayed=replayed, next_step=_next_step(outcome))
    if isinstance(outcome, rc.ResourceResult):
        return AAuthResourceCallResponse(outcome="result", method=outcome.method, path=outcome.path,
                                         status=outcome.status, body=outcome.body, content_type=outcome.content_type,
                                         truncated=outcome.truncated, budget=_budget(outcome),
                                         token_kind=outcome.token_kind, **common)
    if isinstance(outcome, rc.InteractionRequired):
        return AAuthResourceCallResponse(
            outcome="interaction_required",
            interaction=AAuthResourceInteraction(pending_handle=outcome.handle, retry_after=outcome.retry_after,
                                                 requirement="interaction", at="person_server", url=outcome.url,
                                                 code=outcome.code, link=outcome.link, expires_at=outcome.expires_at),
            **common)
    if isinstance(outcome, rc.ResourcePending):
        requirement = "approval" if outcome.requirement == "approval" else "deferred"
        return AAuthResourceCallResponse(
            outcome="pending",
            interaction=AAuthResourceInteraction(pending_handle=outcome.handle, retry_after=outcome.retry_after,
                                                 requirement=requirement,
                                                 at="person_server" if outcome.at == "person_server" else "resource",
                                                 expires_at=outcome.expires_at),
            **common)
    if isinstance(outcome, rc.Denied):
        return AAuthResourceCallResponse(success=False, outcome="denied", reason=outcome.reason, detail=outcome.detail,
                                         do_not_retry=True, budget=_budget(outcome), **common)
    return AAuthResourceCallResponse(success=False, outcome="expired", reason=outcome.reason, do_not_retry=True,
                                     **common)


def _refusal(outcome: rc.Refusal) -> HTTPException:
    detail = {"reason": outcome.code, "message": outcome.detail}
    if outcome.resource:
        detail["resource"] = outcome.resource
    if outcome.challenges:
        detail["challenges"] = list(outcome.challenges)
    if outcome.status is not None:
        detail["resource_status"] = outcome.status
    if outcome.retry_after is not None:
        detail["retry_after"] = outcome.retry_after
    if outcome.possibly_executed:
        detail["possibly_executed"] = True      # #914: a POST may have run — do not blindly re-send
    if outcome.budget is not None:
        detail["budget"] = outcome.budget.as_dict()
    return HTTPException(status_code=_REFUSAL_STATUS.get(outcome.code, 502), detail=detail)


def _request_body(body: AAuthResourceCallNew) -> Optional[bytes]:
    """The exact bytes that are signed and sent (plan §3.3: serialised ONCE)."""
    if body.method == "GET":
        return None
    if body.body is None:
        return b""
    return json.dumps(body.body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@router.post("/{agent_name}/aauth/resource-call", response_model=AAuthResourceCallResponse)
async def call_aauth_resource(
    agent_name: AuthorizedAgentByName,
    request: Request,
    body: AAuthResourceCallRequest = Body(...),
    current_user: User = Depends(get_current_user),
    _live: None = Depends(aauth_person.require_live),
):
    """Call an allow-listed AAuth resource as `agent_name`, or resume a pending call."""
    aauth_person.assert_own_key(current_user, agent_name, what="A resource is called")

    resuming = isinstance(body, AAuthResourceCallResume)
    if resuming:
        rate_limiter.enforce(f"aauth_rc_resume:{agent_name}", RESUME_RATE_LIMIT, RESUME_RATE_WINDOW,
                             detail="Too many resource-call resumes for this agent.")
    else:
        rate_limiter.enforce(f"aauth_rc_call:{agent_name}", CALL_RATE_LIMIT, CALL_RATE_WINDOW,
                             detail="Too many resource calls for this agent.")
    rate_limiter.enforce("aauth_rc:fleet", FLEET_RATE_LIMIT, FLEET_RATE_WINDOW,
                         detail="Too many resource calls across the fleet.")

    replayed = False
    method = path = None
    try:
        if resuming:
            outcome = await rc.resume(agent_name, body.pending_handle,
                                      wait=rc.RESUME_WAIT if body.wait is None else body.wait)
        else:
            method, path = body.method, body.path
            outcome, replayed = await resource_call.guarded_call(
                agent_name, resource=body.resource, method=body.method, path=body.path,
                body=_request_body(body), justification=body.justification,
                dedup_label=body.dedup_label, execution_id=body.execution_id,
            )
    except aauth_signer.SigningUnavailable:
        raise HTTPException(status_code=404, detail="Not found")
    except resource_call.EffectUnguardedError as exc:
        # #2392: pull-mode agent, no usable execution id — refused, not retryable.
        raise HTTPException(status_code=422, detail={"reason": "effect_unguarded", "message": str(exc)})
    except resource_call.EffectInProgressError as exc:
        raise HTTPException(status_code=409, detail={"reason": "effect_in_progress", "message": str(exc)})

    event_type, action = resource_call.audit_action(outcome)
    await platform_audit_service.log(
        event_type=AuditEventType[event_type],
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
        details=resource_call.audit_details(agent_name, outcome, method=method, path=path, replayed=replayed,
                                            resumed=resuming, execution_id=body.execution_id,
                                            trigger_source=body.trigger_source or "api"),
    )
    await resource_call.record_activity(agent_name, outcome)

    if isinstance(outcome, rc.Refusal):
        raise _refusal(outcome)
    return _response(outcome, replayed=replayed)
