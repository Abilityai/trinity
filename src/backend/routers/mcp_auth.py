# mcp: auth.ts (request_login, verify_login) + connector.ts over the internal /api/internal/mcp-auth/* surface (#848)
"""Internal surface for MCP inline email auth (#848).

Four endpoints the Trinity MCP server calls on behalf of a **keyless** MCP
session, authenticating itself with ``X-Internal-Secret`` (the same C-003 gate
as ``routers/internal.py``, whose dependency is reused verbatim):

    POST /api/internal/mcp-auth/request    email a 6-digit code
    POST /api/internal/mcp-auth/verify     check the code, resolve reachable agents
    POST /api/internal/mcp-auth/playbooks  exposed playbooks for a verified email
    POST /api/internal/mcp-auth/chat       one turn, attributed to a verified email

Router-only concerns live here (HTTP shape, status codes, rate-limiter
bookkeeping that needs the client IP, audit, idempotency). All logic is in
``services/mcp_auth_service.py`` (Invariant #1); request/response models are in
``models.py`` (Invariant #14).

**The internal secret authenticates the caller, never the action.** Every data
call re-gates on the asserted email's own access (``email_has_agent_access`` +
connector enabled). A compromised MCP server cannot reach an agent the email
cannot, and cannot mint itself an identity — nothing here returns a credential.

Spec: ``docs/memory/requirements/mcp.md`` §7.6.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import (
    APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status,
)
from fastapi.responses import JSONResponse

from models import (
    ConnectorPlaybook,
    McpInlineChatRequest,
    McpInlineChatResponse,
    McpInlineLoginRequest,
    McpInlineLoginVerify,
    McpInlinePlaybooksRequest,
    McpInlineVerifyResponse,
)
from routers.auth import (
    check_login_rate_limit_account_only,
    check_otp_rate_limit,
    record_login_attempt_account_only,
    record_otp_attempt,
)
import config
from routers.internal import verify_internal_secret
from services import idempotency_service, mcp_auth_service
from services.platform_audit_service import platform_audit_service, AuditEventType

logger = logging.getLogger(__name__)


def require_inline_auth_enabled() -> None:
    """404 the whole surface unless MCP_INLINE_AUTH_ENABLED is on.

    The mcp-server has its own gate on the same env key, but the backend must
    not depend on another process for its default-OFF posture: these endpoints
    bypass the email whitelist, create accounts, and dispatch agent chat. An
    install that never opted in should not be answering here at all.

    404 rather than 403 so a disabled deployment does not advertise that the
    surface exists.
    """
    if not config.MCP_INLINE_AUTH_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


router = APIRouter(
    prefix="/api/internal/mcp-auth",
    tags=["mcp_inline_auth"],
    # Order matters only for cost, not correctness — both run for every route.
    dependencies=[Depends(verify_internal_secret), Depends(require_inline_auth_enabled)],
)

# The ONE body ``/request`` ever returns. Constant by contract — see below.
_GENERIC_REQUEST_BODY = {"status": "ok"}


@router.post("/request", status_code=status.HTTP_202_ACCEPTED)
async def request_inline_login(
    body: McpInlineLoginRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """Email a login code — iff the address is already known to Trinity.

    Reachable by an unauthenticated MCP client, so it must not become an open
    email relay: the service sends only to an address with an existing account
    or an ``agent_sharing`` allow-list entry, and silently does nothing
    otherwise.

    **The response is constant.** Same 202, same bytes whether the address is
    known, unknown, or rate-limited, and **no audit row is written**. Latency is
    constant too, but only because every branch-dependent step is deferred to a
    Starlette ``BackgroundTasks`` task, which runs after the response has been
    flushed — do not move any of it back inline. An audit entry is itself an
    enumeration oracle, which is why the web path (``routers/auth.py``, #186)
    emits none either. Do NOT add a 4xx for an unknown address, a distinct
    message, an ``expires_in_seconds`` field, or a blocking send: each one
    re-opens the oracle this endpoint exists to close.

    ``BackgroundTasks`` specifically — **not** a thread. Deferring is what the
    constant-time property needs; leaving the event loop is not, and
    ``asyncio.to_thread`` here silently breaks the feature outright (SQLite
    connections are thread-affine, so the known-check raises in the worker and
    its own fail-closed handler swallows that into "unknown address" — every code
    send stops while this endpoint keeps answering 202). See
    ``mcp_auth_service._process_login_request``, which owns that reasoning.
    """
    mcp_auth_service.schedule_login_code(background_tasks, body.email, body.session_id)
    return _GENERIC_REQUEST_BODY


@router.post("/verify", response_model=McpInlineVerifyResponse)
async def verify_inline_login(
    body: McpInlineLoginVerify,
    request: Request,
) -> McpInlineVerifyResponse:
    """Verify a code and return the identity + the agents it may reach.

    Returns **no credential of any kind** (§7.6: session, not a minted key) —
    the MCP server binds the verified email onto its in-memory session, and the
    binding dies with the connection.

    Unlike ``/request``, verify outcomes ARE audited: by this point the caller
    has demonstrated possession of a code, so ``login_failed`` / ``login_success``
    rows leak nothing and match the web email path.
    """
    email = mcp_auth_service.normalize_email(body.email)
    client_ip = request.client.host if request.client else "unknown"

    # Per-ACCOUNT limiters only. The real client IP is never visible here —
    # every inline-auth call arrives from the MCP server container — so the
    # per-IP bucket would collapse the whole fleet of MCP clients into ONE
    # shared bucket. That is not merely a useless limit, it is a DoS primitive:
    # `check_login_rate_limit` locks out at 30 failures per 5 minutes, so a
    # single anonymous client submitting 30 wrong codes would lock inline login
    # for every user of the instance — and, because `_ip_key` is a shared
    # namespace, would also burn the bucket for real web logins from that
    # egress IP. That is exactly the platform-wide lockout the #591 split-bucket
    # redesign removed; routing this path through the IP bucket would
    # reintroduce it in a worse form (the collapse is structural, not
    # incidental). So: pass the ACCOUNT bucket only, and never record into the
    # IP bucket from here.
    check_login_rate_limit_account_only(email)
    check_otp_rate_limit(email)

    user = mcp_auth_service.verify_login_code(email, body.code)

    if not user:
        record_login_attempt_account_only(email, success=False)
        record_otp_attempt(email, success=False)
        await platform_audit_service.log(
            event_type=AuditEventType.AUTHENTICATION,
            event_action="login_failed",
            source="mcp",
            actor_ip=client_ip,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"method": "mcp_inline", "email": email},
        )
        # Uniform failure — wrong code, expired code and unknown address are one
        # response. The MCP client renders any 401 as "invalid or expired".
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired verification code",
        )

    record_login_attempt_account_only(email, success=True)
    record_otp_attempt(email, success=True)

    await platform_audit_service.log(
        event_type=AuditEventType.AUTHENTICATION,
        event_action="login_success",
        source="mcp",
        actor_ip=client_ip,
        target_type="user",
        target_id=user["username"],
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"method": "mcp_inline", "email": email},
    )

    return mcp_auth_service.build_verify_response(user, email)


@router.post("/playbooks", response_model=List[ConnectorPlaybook])
async def inline_playbooks(body: McpInlinePlaybooksRequest) -> List[ConnectorPlaybook]:
    """Exposed playbooks of one agent, for a verified email.

    Re-gated per call (403 unless the email has access AND the connector is
    enabled) — the MCP server's assertion that a session is verified is not
    authorization.
    """
    return await mcp_auth_service.list_exposed_playbooks(body.email, body.agent)


@router.post("/chat", response_model=McpInlineChatResponse)
async def inline_chat(
    body: McpInlineChatRequest,
    idempotency_key: Optional[str] = Header(None),
):
    """One chat turn against an agent, attributed to a verified email.

    Same 403 gate as ``/playbooks``, applied before anything else.

    Invariant #18: this is a trigger boundary, so it accepts an idempotency key
    — from the ``Idempotency-Key`` header, or from the request body (the MCP
    server's inline-auth calls are plain JSON POSTs and carry it there).

    Scoped per (agent, verified email), NOT per agent: the key is
    caller-supplied and the identity arrives in the body, so an agent-only
    scope would let two different verified users of the same shared agent
    collide and replay each other's response snapshot. See
    ``idempotency_service.make_inline_auth_scope``.
    """
    key = idempotency_key or body.idempotency_key

    # Gate BEFORE the idempotency claim: an unauthorized caller must not be able
    # to occupy a key slot for an agent it cannot reach.
    mcp_auth_service.assert_email_may_reach_agent(body.email, body.agent)

    idem = idempotency_service.begin(
        idempotency_service.make_inline_auth_scope(body.agent, body.email), key
    )
    if idem.replay:
        if idem.in_flight:
            raise HTTPException(
                status_code=409,
                detail="A chat with this Idempotency-Key is still being processed.",
            )
        if idem.snapshot is not None:
            return JSONResponse(
                content=idem.snapshot, headers={"X-Idempotent-Replay": "true"}
            )

    try:
        result = await mcp_auth_service.dispatch_chat(
            body.email, body.agent, body.message
        )
    except Exception:
        idempotency_service.fail(idem)
        raise

    response = McpInlineChatResponse(
        agent=body.agent,
        response=result.response or "",
        execution_id=result.execution_id,
    )
    idempotency_service.complete(idem, result.execution_id, response.model_dump())
    return response
