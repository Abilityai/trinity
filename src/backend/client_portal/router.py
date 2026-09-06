"""FastAPI router for the Workspace / client portal (epic #78, exposure #79).

OSS core since ent#356 — mounted in every build, entitlement-free. Individual
routes keep their own gates: the admin-only exposure config is `require_admin`,
and every client-facing route resolves identity through `get_portal_identity`
(a verified-email portal session, or a signed-in platform user).

Exposes the portal base-URL / exposure-mode seam so an operator can switch the
portal between public (tunnel) and private (VPN/LAN) reachability with no code
change. Reads report the resolved base URL actually in use.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import httpx
from fastapi import (
    BackgroundTasks,
    APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse

from dependencies import (
    CurrentUser,
    OwnedAgentByName,
    assert_admin,
    get_current_user,
    reject_agent_principal,
    require_admin,
)
from models import REPORT_ROWS_PAGE_MAX, User
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container
from services.platform_audit_service import AuditEventType, platform_audit_service

from . import agent_page, service
from .models import (
    PortalSessionRename,
    PortalRatingRequest,
    PortalRatingResult,
    PortalAuthRequest,
    PortalAuthVerify,
    PortalBlockRequest,
    PortalBlockResult,
    PortalClientRoster,
    PortalClientState,
    PortalLogoutResult,
    PortalUnblockResult,
    PortalChatRequest,
    PortalExchangeRequest,
    PortalExchangeResponse,
    PortalChatResponse,
    PortalDocuments,
    PortalHistory,
    PortalExposureConfig,
    PortalExposureUpdate,
    PortalBriefings,
    PortalRoster,
    PortalSearchResults,
    PortalSession,
    PortalAllSessions,
    PortalSessions,
    PortalAgentPage,
    PortalAgentReports,
    PortalChatState,
    PortalSessionSummary,
    PortalTtsRequest,
    PortalTurnStarted,
    PortalUpload,
    PortalUploads,
)
from .portal_auth import PortalPrincipal, get_portal_principal
from .service import ClientPortalError, InvalidChatTitle

logger = logging.getLogger(__name__)
_signin_email_tasks: set = set()

# --- Per-client abuse controls on the two surfaces that spend money and disk ---
#
# ent#287. `/tts` and `/stt` already carry a per-(client, agent) limiter;
# `/chat` and `/documents` carried none. Chat was bounded only *indirectly*, by
# the agent's own capacity/backlog inside `execute_task` — which protects the
# agent from overload but does nothing to stop one client email spending that
# agent's entire model budget. Upload was bounded per file (25 MiB) and per
# inbox (100 MiB / client / agent), but the inbox write is an OVERWRITE: the
# same filename re-uploaded never grows `used`, so the quota never trips and the
# per-request work (two docker execs + a 25 MiB tar) was unbounded.
#
# Two tiers per surface, because one is not enough:
#   * burst (60s)  — well clear of what a human drives from the shipped portal
#                    (chat is one turn per send; upload is one file per pick),
#                    with headroom for a headless client batching. Set AT the
#                    20/min the voice surfaces use: chat must never be TIGHTER
#                    than /tts, or voice mode — one chat + one tts per turn —
#                    429s on the chat leg first.
#   * sustained (1h) — the tier that actually meets this issue's goal. A 20/min
#                    limit alone still permits 1200 turns/hour, which is not a
#                    budget bound at all.
#
# Env-tunable: the right ceiling depends on the licensee's clients, and an
# operator must be able to loosen this without a release.
PORTAL_CHAT_BURST_LIMIT = int(os.getenv("PORTAL_CHAT_BURST_LIMIT", "20"))
PORTAL_CHAT_HOURLY_LIMIT = int(os.getenv("PORTAL_CHAT_HOURLY_LIMIT", "300"))
PORTAL_UPLOAD_BURST_LIMIT = int(os.getenv("PORTAL_UPLOAD_BURST_LIMIT", "20"))
PORTAL_UPLOAD_HOURLY_LIMIT = int(os.getenv("PORTAL_UPLOAD_HOURLY_LIMIT", "100"))

# Ratings take the same TWO tiers, for the same reason and with the same shape
# (ent#366 review). A rating is one click, so the burst tier stays generous —
# but this route dispatches a full `execute_task` on a down-rating with a
# comment, i.e. the same expensive resource `portal_chat` is metered on, and a
# single 60/min tier permits ~3600 turns an hour against that client's stated
# 300. The hourly ceiling is the tier that actually bounds it; the per-target
# dispatch claim in the service is what stops one target being re-rated into a
# turn generator. Env-tunable, because the right ceiling depends on the
# licensee's clients.
PORTAL_RATING_BURST_LIMIT = int(os.getenv("PORTAL_RATING_BURST_LIMIT", "60"))
PORTAL_RATING_HOURLY_LIMIT = int(os.getenv("PORTAL_RATING_HOURLY_LIMIT", "300"))

_CHAT_LIMIT_DETAIL = "Too many messages to this agent."
_UPLOAD_LIMIT_DETAIL = "Too many uploads."
_RATING_LIMIT_DETAIL = "Too many ratings for this agent."


def _require_roster(agent_name: str, email: str, include_owned: bool = False) -> None:
    """Uniform 404 for an agent outside the caller's roster.

    The services re-check this — it is duplicated here deliberately, as an
    access-first gate (OSS invariant #8), so the rate limiters below never key
    on an unvalidated path param. Without it a caller could mint an unbounded
    number of distinct limiter keys (`portal_chat:{email}:{anything}`), each
    holding Redis memory for its window, without ever tripping a limit —
    turning the control into an amplifier. On the upload path it also means the
    body is never `read()` into memory, and no docker work is done, for an agent
    the caller cannot reach.
    """
    if not service.agent_on_roster(agent_name, email, include_owned):
        raise HTTPException(status_code=404, detail="Agent not found")


# ent#356: OSS core — no `requires_entitlement`. The workspace is the main
# surface a non-operator uses to work with agents, so gating it behind the paid
# tier capped adoption at exactly the population we most want using it.
#
# The `/api/enterprise/client-portal` PREFIX stays, deliberately. It is now a
# misnomer, but ent#83 shipped this as the documented integration surface for
# fully custom, API-only clients; renaming it would break those integrations for
# no functional gain, and "existing installs are unaffected" is an acceptance
# criterion of the move. A cosmetic alias can be added later if wanted.
router = APIRouter(
    prefix="/api/enterprise/client-portal",
    tags=["client-portal"],
)


# --- Client sign-in (verified email → portal session; no platform account) ---

@router.post("/auth/request")
async def portal_auth_request(body: PortalAuthRequest, request: Request):
    """Step 1 — request a 6-digit code. ALWAYS returns the same generic body so
    a caller can't tell which emails have portal access (#186).

    ent#309 / ent#311: this reimplements OSS email sign-in and used to inherit
    NONE of its brute-force/enumeration protections. Two things are wired here:

    * A per-(email, IP) request-rate limit (`services/rate_limiter`, the shared
      sliding-window primitive) BEFORE any access-dependent work — it bounds the
      code-minting flood (30 requests used to mint 30 simultaneously-valid codes,
      shrinking the OTP guess space) and caps how fast an attacker can sample the
      timing side-channel (ent#309). The limit applies to every email regardless
      of access, so it is not itself an oracle; 429 reveals nothing about the
      address.

    * The access-check + code-mint move INTO the fire-and-forget task (ent#309).
      The synchronous path is now identical whether or not the email has a share
      — the measurable DB-write difference that distinguished a client from a
      stranger no longer happens before the response. Not constant-time in a
      strict sense (framework jitter dominates), but the dominant term is gone.
    """
    from services import rate_limiter

    client_ip = request.client.host if request.client else "unknown"
    email = (body.email or "").strip().lower()
    # Per-IP stops one host sweeping many addresses (to time or to mint codes);
    # per-email stops a targeted flood at one address. Both fail open (Redis
    # down → allowed), matching the platform posture.
    rate_limiter.enforce(f"portal_signin_req_ip:{client_ip}", 30, 60,
                         detail="Too many sign-in requests.")
    rate_limiter.enforce(f"portal_signin_req_email:{email}", 5, 300,
                         detail="Too many sign-in requests for this address.")

    async def _issue_and_send(addr: str):
        # Access check + code mint happen HERE, off the measured path (ent#309).
        try:
            code = service.portal_signin_request(addr)
            if not code:
                return
            from services.email_service import EmailService
            await EmailService().send_verification_code(addr, code, context_label="Client Portal")
        except Exception:
            logger.exception("Failed to issue/send portal login code")

    task = asyncio.create_task(_issue_and_send(email))
    _signin_email_tasks.add(task)
    task.add_done_callback(_signin_email_tasks.discard)
    return {"success": True, "message": "If your email has access, you'll receive a code shortly"}


@router.post("/auth/verify", response_model=PortalSession)
def portal_auth_verify(body: PortalAuthVerify, request: Request):
    """Step 2 — verify the code, mint a portal session token (a verified email,
    not a platform account).

    ent#311: gate the 6-digit code against brute force. Reuses the OSS OTP
    failure-counter LOGIC (`check_otp_rate_limit` / `record_otp_attempt`, the
    pentest-3.1.5 cap that invalidates a code after OTP_MAX_ATTEMPTS=5 wrong
    tries) — one source of truth for "how many wrong OTPs before lockout" — but
    under a **portal-scoped key** (`otp_attempts:portal:{email}`).

    The key namespace is load-bearing, not cosmetic. Those functions build their
    key from the identifier passed in, and the platform email path passes the
    bare email → `otp_attempts:{email}`. Reusing the bare email here would share
    the bucket with platform login: an unauthenticated attacker POSTing 5 wrong
    codes at a victim's address would trip the platform's counter and lock that
    user out of platform sign-in (a cross-surface lockout DoS this endpoint must
    not create). Prefixing with `portal:` keeps the shared logic and isolates the
    state.

    Deliberately NOT reusing `check_login_rate_limit`: its per-account bucket is
    the platform-login bucket (same collision), and it is redundant here — the
    OTP cap already bounds guesses per email. The per-IP dimension is covered
    instead by the enterprise `rate_limiter` under a portal-only key, so nothing
    the portal writes can throttle a platform surface.
    """
    from routers.auth import check_otp_rate_limit, record_otp_attempt
    from services import rate_limiter

    client_ip = request.client.host if request.client else "unknown"
    email = (body.email or "").strip().lower()
    otp_scope = f"portal:{email}"  # isolate from the platform `otp_attempts:{email}` bucket

    # Per-IP coarse abuse cap (isolated portal key, sliding window) + per-email
    # OTP failure cap (isolated portal key, failure-counter). Both fail open.
    rate_limiter.enforce(f"portal_signin_verify_ip:{client_ip}", 60, 60,
                         detail="Too many sign-in attempts.")
    check_otp_rate_limit(otp_scope)  # raises 429 past the cap, before any comparison

    token = service.portal_signin_verify(email, body.code)
    if not token:
        record_otp_attempt(otp_scope, success=False)
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    record_otp_attempt(otp_scope, success=True)
    return PortalSession(token=token, email=email)


@router.post("/auth/exchange", response_model=PortalExchangeResponse)
async def portal_auth_exchange(
    body: PortalExchangeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """ent#163 — trusted-issuer token exchange: assert an end user, get a portal
    session for them.

    This is how a licensee runs their OWN customer portal against Trinity while
    keeping their own IdP. Their backend holds one admin-issued
    `portal_delegate` key, says "this request is for bob@example.com", and gets
    a normal portal session token back. Everything downstream — `/my-agents`,
    chat, history, documents — is the surface that already shipped with #78, and
    is roster-scoped to that email, so nothing here widens it.

    Authorization is two independent checks:
      * the key must be `portal_delegate`-scoped. OSS fences that scope to THIS
        route alone at the single auth entry point, so a leaked issuer key
        cannot read the platform — it can only mint portal sessions. We re-check
        the flag here rather than trusting the fence: defence in depth, and it
        makes the requirement legible at the endpoint that depends on it.
      * the asserted email must actually have portal access. That stays
        Trinity's call, never the issuer's — `email_has_access` requires a real
        share, so an issuer cannot conjure a session for an arbitrary address.

    **Enumeration posture — deliberately different from `/auth/request`.** That
    endpoint is anonymous, so it always returns the same generic body and never
    reveals which emails are clients (#186). This caller is authenticated,
    trusted, and admin-issued: they already know their own user list, so a 403
    tells them nothing they did not supply, and a generic 200 would instead hand
    them an unusable token and a support ticket. The divergence is intentional —
    please do not "fix" it back into a generic response.
    """
    if not getattr(current_user, "portal_delegate", False):
        raise HTTPException(
            status_code=403,
            detail="This endpoint requires a portal-delegate API key",
        )

    email = (body.email or "").strip().lower()
    token = service.portal_exchange(email)

    # Audited either way: a denied exchange is the more interesting event.
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHENTICATION,
        event_action="portal_delegate_exchange",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="portal_client",
        target_id=email or None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"granted": bool(token)},
    )

    if not token:
        raise HTTPException(
            status_code=403,
            detail="This email has no access to any agent on this instance",
        )

    # ent#375: report the token's ACTUAL lifetime (the idle window), not a
    # constant. The session now slides, so this is when it expires *if the
    # client goes quiet* — a client that keeps using it keeps it alive.
    idle_s, _ = settings_service.get_portal_session_policy()
    return PortalExchangeResponse(
        token=token,
        email=email,
        expires_in=idle_s,
    )


@router.get("/exposure", response_model=PortalExposureConfig)
def get_exposure(_: User = Depends(require_admin)):
    return service.get_status()


@router.put("/exposure", response_model=PortalExposureConfig)
def set_exposure(body: PortalExposureUpdate, current_user: User = Depends(require_admin)):
    try:
        return service.configure(body, actor_email=getattr(current_user, "email", None))
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# ---------------------------------------------------------------------------
# Operator controls over a signed-in client (ent#281)
# ---------------------------------------------------------------------------
#
# Permissions differ on purpose, and the difference is the design:
#
#   log out  — any owner of an agent shared with this client. Ends live sessions
#              and nothing else; the client can sign straight back in, so the
#              worst an owner can do to a peer's client is interrupt them once.
#   block    — ADMIN ONLY. It bars the email platform-wide, so letting any agent
#              owner do it would let them lock a client out of a different
#              owner's agents. Owners already have a per-agent kill switch that
#              needs no new endpoint: unshare.
#
# Both are agent-scoped in the URL (that is where the operator is standing) but
# global in effect, and the response models say so.

async def _audit_client_control(request: Request, current_user: User, action: str,
                                email: str, outcome: dict) -> None:
    """One audit row per operator control action (ent#281 AC)."""
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHENTICATION,
        event_action=action,
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="portal_client",
        target_id=email,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details=outcome,
    )


@router.get("/agents/{agent_name}/clients", response_model=PortalClientRoster)
def list_agent_clients(agent_name: OwnedAgentByName, current_user: CurrentUser):
    """Clients of this agent with their control state (ent#281).

    Owner/admin. The emails an agent is shared with ARE its portal accounts
    (#78), so `agent_sharing` is the roster; each row carries last-seen, the
    durable block state, and the session-revocation cutoff.
    """
    return PortalClientRoster(
        agent_name=agent_name,
        clients=[PortalClientState(**c) for c in service.get_agent_client_roster(agent_name)],
    )


@router.post("/agents/{agent_name}/clients/{email}/logout", response_model=PortalLogoutResult)
async def logout_agent_client(agent_name: OwnedAgentByName, email: str,
                              request: Request, current_user: CurrentUser):
    """End every live portal session for this client (ent#281).

    Owner of the agent (or admin). Global in effect — one portal token covers a
    client's whole roster, so there is no per-agent session to end. Non-
    destructive: the client may sign in again immediately unless also blocked.

    Human-only: ending a person's access is an operator decision, and an
    agent-scoped key resolves to its owner, so without this a prompt-injected
    agent could sign clients out of the agent it runs as.
    """
    reject_agent_principal(current_user)
    try:
        result = service.logout_client(email)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    await _audit_client_control(request, current_user, "portal_client_logout",
                                result["email"], result)
    return PortalLogoutResult(**result)


@router.post("/agents/{agent_name}/clients/{email}/block", response_model=PortalBlockResult)
async def block_agent_client(agent_name: OwnedAgentByName, email: str, body: PortalBlockRequest,
                             request: Request, current_user: CurrentUser):
    """Bar this client from signing in again, anywhere, and end their sessions.

    **Admin only** — `OwnedAgentByName` establishes that the operator is standing
    at an agent they own, but the effect is platform-wide, so ownership alone is
    not enough authority.

    `assert_admin` is NOT sufficient on its own: it rejects *connector*
    principals, but an agent-scoped key resolves to its owner **carrying the
    owner's role**, so on a default admin-owned install it would pass. That is
    the trinity-ops-agent#232 shape the retention-acknowledge endpoint hit.
    `reject_agent_principal` is what makes this human-only.
    """
    reject_agent_principal(current_user)
    assert_admin(current_user, detail="Blocking a client requires admin access")
    try:
        result = service.block_client(
            email,
            actor_id=str(getattr(current_user, "id", "") or "") or None,
            actor_email=getattr(current_user, "email", None),
            reason=body.reason,
        )
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    await _audit_client_control(request, current_user, "portal_client_block",
                                result["email"], result)
    return PortalBlockResult(**result)


@router.delete("/agents/{agent_name}/clients/{email}/block", response_model=PortalUnblockResult)
async def unblock_agent_client(agent_name: OwnedAgentByName, email: str,
                               request: Request, current_user: CurrentUser):
    """Lift the block. Admin + human-only, mirroring block exactly.

    The symmetry is load-bearing: a weaker guard here would let the principals
    the block constrains undo it, making the stricter guard on block decorative.

    Restores nothing but access: revoked tokens stay revoked (the client signs in
    again and gets a fresh one), and no data was ever removed — block is not
    delete.
    """
    reject_agent_principal(current_user)
    assert_admin(current_user, detail="Unblocking a client requires admin access")
    try:
        result = service.unblock_client(email)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    await _audit_client_control(request, current_user, "portal_client_unblock",
                                result["email"], result)
    return PortalUnblockResult(**result)


@router.get("/my-agents", response_model=PortalRoster)
async def my_agents(principal: PortalPrincipal = Depends(get_portal_principal)):
    """The signed-in client's "My Agents" roster (agents shared with their email).

    Not admin-only — this is the client-facing surface. Identity comes from
    `get_portal_identity`: a portal session token (a verified email, no platform
    account) OR a platform user's email (operator preview). Agents are resolved
    from `agent_sharing` for that email.

    #2163: this no longer waits for any agent HTTP. Each card ships
    `briefing_state="pending"` and the description + hint cards #138 used to
    resolve here are fetched by `GET /briefings` off the critical path.
    """
    # ent#357: a platform session also sees the agents it OWNS. Trinity refuses
    # a self-share, so without this an owner's roster is always empty — one
    # click to a dead page. An external client's roster is unchanged.
    return await service.get_roster(principal.email, include_owned=principal.is_platform)


@router.get("/search", response_model=PortalSearchResults)
def portal_search(q: str = "", limit: int = 30, principal: PortalPrincipal = Depends(get_portal_principal)):
    """Cross-chat search over the signed-in client's conversations (all rostered
    agents), by thread title or message content — the portal's main-page search.
    Roster-scoped; a short/empty query returns no results (never an error)."""
    # No agent gate here: search is scoped to the caller's own portal rows by
    # email, so there is no roster decision to mirror.
    return service.search_chats(principal.email, q, limit=min(max(limit, 1), 50),
                                include_owned=principal.is_platform)


@router.get("/sessions", response_model=PortalAllSessions)
def portal_all_sessions(principal: PortalPrincipal = Depends(get_portal_principal)):
    """Every conversation thread the signed-in viewer has, across every agent on
    their roster — the Workspace sidebar's list, in one call (#2198).

    Declared HERE, beside `/my-agents`, `/search` and `/chat-state`, because it is
    the same kind of thing: viewer-scoped, no agent parameter. `Portal.vue` already
    articulated the shape when `/chat-state` was added — "kept out of
    `fetchAllSessions` on purpose: that call fans out over the roster and degrades
    per agent, while this is one call for the whole viewer". Sessions now join it.

    Invariant #4: this router has no top-level `/{param}` catch-all today (every
    segment-1 is a literal), so `sessions` cannot be shadowed — but keeping it in
    this block means a future catch-all cannot capture it either.

    Invariant #8: no agent parameter, so there is no existence oracle to probe —
    strictly LESS enumerable than the per-agent route it replaces.
    """
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can SEE.
    include_owned = principal.is_platform

    from services import rate_limiter

    # This becomes the hottest authenticated read in the Workspace — six
    # `refreshThreads()` call sites including every thread open and every
    # completed turn — and unlike the fan-out it replaces it is no longer even
    # incidentally throttled by the browser's per-host connection cap (and in
    # production, behind cloudflared/HTTP-2, there is no such cap at all). There
    # is no global limiter middleware, so every bounded portal surface enforces
    # explicitly; this follows `portal_report_detail`.
    rate_limiter.enforce(f"portal_sessions_all:{email}", 120, 60)
    try:
        return service.list_all_sessions(email, include_owned=include_owned)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# #2163 — how many agent names one `?agents=` filter may carry. The filtered
# form exists for ONE agent (the active chat); this is a shape belt on a query
# string, not a product limit, and the unfiltered form covers the whole roster
# with no cap at all.
_MAX_BRIEFING_NAMES = 200


@router.get("/briefings", response_model=PortalBriefings)
async def portal_briefings(
    agents: Optional[str] = Query(None),
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """The briefings `GET /my-agents` no longer waits for (#2163).

    The roster used to carry each agent's description + hint cards, resolved by
    fanning agent HTTP across the whole fleet and awaiting `gather` — so the
    Workspace's first paint was bounded by the slowest agent, for every user, on
    every sign-in. That work moved here, off the critical path.

    Two shapes, one route: no `agents=` briefs the caller's whole roster (the
    client's background batch, which fills the picker and the composer's `/`
    typeahead), while `agents=a,b` briefs a subset — used for the ACTIVE agent,
    so its hints arrive at its own speed instead of the batch's slowest member.
    A per-agent route would have re-created the N+1 #2198 removed; a batch with
    no filter would have moved the floor from the roster onto the hint zone.

    Invariant #4: declared in the viewer-scoped block beside `/my-agents`,
    `/sessions`, `/search` and `/chat-state`. This router has no top-level
    `/{param}` catch-all today, so `briefings` cannot be shadowed — keeping it
    here means a future one could not capture it either.

    Invariant #8: no agent path parameter, and an unknown or off-roster name in
    the filter is dropped rather than answered, so there is no existence oracle
    — strictly less enumerable than the per-agent page route.
    """
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can SEE.
    include_owned = principal.is_platform

    names: Optional[list[str]] = None
    if agents is not None:
        names, seen = [], set()
        for raw in agents.split(","):
            n = raw.strip()
            if not n or n in seen:
                continue
            seen.add(n)
            names.append(n)
        # Raised BEFORE the limiter so an over-cap request is work-free and
        # cannot be used to burn the caller's own bucket. Named, not silent
        # truncation: a caller that asked for 300 agents and got 200 back has
        # no way to tell which 100 are missing.
        if len(names) > _MAX_BRIEFING_NAMES:
            raise HTTPException(
                status_code=422,
                detail=f"agents: at most {_MAX_BRIEFING_NAMES} names per request",
            )

    from services import rate_limiter

    # Per viewer, like every other bounded portal surface (there is no global
    # limiter middleware). A READ still needs one here because it fans out to
    # agent containers: the two forms get two keys, and the unfiltered one is
    # much tighter because a single call to it costs one bounded agent request
    # per rostered agent — a 100-agent viewer could otherwise drive ~12k agent
    # calls a minute through a GET.
    if names is None:
        rate_limiter.enforce(f"portal_briefings_all:{email}", 10, 60)
    else:
        rate_limiter.enforce(f"portal_briefings:{email}", 60, 60)

    try:
        return await service.get_briefings(email, names, include_owned=include_owned)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# --- Per-user chat state: stars + unread (ent#359) ----------------------------
#
# No roster gate on any of the three. Every row is keyed by the caller's own
# email, so these read and write exactly one tenant's state and there is no agent
# to authorize against. Neither writer checks that the chat exists, deliberately:
# a 404 for an unknown id would turn `star` into an existence oracle over every
# chat id in the install (OSS invariant #8). The service's row cap is what bounds
# writing junk ids instead.

@router.get("/chat-state", response_model=PortalChatState)
def portal_chat_state(principal: PortalPrincipal = Depends(get_portal_principal)):
    """Star + unread state for the signed-in viewer's chats, both kinds."""
    return service.get_chat_state(principal.email)


@router.put("/chat-state/{chat_kind}/{chat_id}/star", status_code=204)
def portal_star_chat(chat_kind: str, chat_id: str,
                     principal: PortalPrincipal = Depends(get_portal_principal)):
    """Pin a chat above the sidebar's date groups, for this viewer only."""
    try:
        service.set_chat_star(principal.email, chat_kind, chat_id, True)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return Response(status_code=204)


@router.delete("/chat-state/{chat_kind}/{chat_id}/star", status_code=204)
def portal_unstar_chat(chat_kind: str, chat_id: str,
                       principal: PortalPrincipal = Depends(get_portal_principal)):
    """Unpin a chat. Keeps the row — it still carries the read cursor."""
    try:
        service.set_chat_star(principal.email, chat_kind, chat_id, False)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return Response(status_code=204)


@router.post("/chat-state/{chat_kind}/{chat_id}/read", status_code=204)
def portal_mark_chat_read(chat_kind: str, chat_id: str,
                          principal: PortalPrincipal = Depends(get_portal_principal)):
    """Advance this viewer's read cursor — clears the chat's unread count and
    its share of the agent row's badge."""
    try:
        service.mark_chat_read(principal.email, chat_kind, chat_id)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return Response(status_code=204)


# --- The agent page (ent#360) -------------------------------------------------
#
# Roster-gated like every other per-agent route. Read-only by construction:
# nothing here writes, and the payload carries no schedules, skills, logs, costs
# or model — AC #7 keeps configuration operator-side, and the viewer may be an
# external client rather than an operator.

@router.get("/agents/{agent_name}/page", response_model=PortalAgentPage)
async def portal_agent_page(
    agent_name: str,
    window: str = "7d",
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Everything the agent page needs, in one call.

    One request rather than five because the page is one screen: fetching the
    header, stats, asks and recent work separately renders it in pieces, and a
    stats call that outruns the header shows numbers above a nameless card.
    """
    email = principal.email
    _require_roster(agent_name, email, principal.is_platform)
    if window not in agent_page.WINDOWS:
        raise HTTPException(status_code=422, detail="Unknown window")
    # #2160: ONE card, not the whole roster. This projected the roster's card so
    # the page and the sidebar could not disagree about an agent's capabilities —
    # correct, but it built every card and fanned `_agent_briefing` across the
    # whole fleet to use one, making the page's load time depend on the slowest
    # agent in it. `get_agent_card` keeps the shared builder and does one.
    card = await service.get_agent_card(email, agent_name,
                                        include_owned=principal.is_platform)
    return agent_page.build_page(
        email, agent_name, card.model_dump() if card else None, window=window,
        # #2423: the page reports different things to a client and an operator,
        # and this is the same flag `get_agent_card` above already keys on.
        is_platform=principal.is_platform,
    )


@router.get("/agents/{agent_name}/canvas")
def portal_agent_canvases(
    agent_name: str,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """The canvases this agent has published to the people it works with (ent#438).

    Metadata only — blocks are fetched per canvas on open, the same split the
    reports pair above uses and for the same reason: a canvas is capped at
    512 KiB and a list of them is not a list view.

    Roster-gated like every route on this prefix, and additionally narrowed to
    `audience='roster'` inside the accessor. Both are needed and neither is
    redundant: the roster gate answers "may this person reach this agent", the
    audience narrowing answers "did the agent mean this for them" — an
    operator-only canvas stays invisible to a rostered client.
    """
    _require_roster(agent_name, principal.email, principal.is_platform)
    return {"agent_name": agent_name, "canvases": agent_page.canvases(agent_name)}


@router.get("/agents/{agent_name}/canvas/{canvas_id}")
def portal_agent_canvas_detail(
    agent_name: str,
    canvas_id: str,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """One published canvas with its blocks.

    A canvas the agent did not publish to its roster returns the same 404 as
    one that does not exist, so this is not an existence oracle for the
    operator-only surfaces (the uniform-404 contract the report detail route
    above states).
    """
    _require_roster(agent_name, principal.email, principal.is_platform)
    from services import rate_limiter

    # A canvas re-reads and re-parses its whole block list per request. Bounded
    # for the same reason the report detail route is, and keyed after the
    # roster gate so an unreachable agent cannot mint limiter keys.
    rate_limiter.enforce(f"portal_canvas_detail:{principal.email}:{agent_name}", 60, 60)
    canvas = agent_page.canvas_detail(agent_name, canvas_id)
    if canvas is None:
        raise HTTPException(status_code=404, detail="Canvas not found")
    return canvas


@router.get("/agents/{agent_name}/reports", response_model=PortalAgentReports)
def portal_agent_reports(
    agent_name: str,
    limit: int = 20,
    offset: int = 0,
    session_id: Optional[str] = Query(
        None,
        description="Narrow to deliverables produced in one Workspace chat (ent#365).",
    ),
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Deliverables this agent addressed to the caller (ent#365).

    Metadata only — payloads are fetched per report on expansion, since one is
    capped at 5 MiB (`REPORT_PAYLOAD_MAX_BYTES`) and a list of them is not a
    list view.

    `session_id` is what the inline chat cards read. It narrows within the
    caller's own rows and is never a widening: the audience condition is applied
    regardless, so passing another client's session id returns nothing rather
    than their deliverables.
    """
    _require_roster(agent_name, principal.email, principal.is_platform)
    return {
        "agent_name": agent_name,
        "reports": agent_page.reports(
            agent_name, principal.email,
            limit=min(max(limit, 1), 50), offset=max(offset, 0),
            portal_session_id=session_id,
        ),
    }


@router.get("/agents/{agent_name}/reports/{report_id}")
def portal_agent_report_detail(
    agent_name: str,
    report_id: str,
    rows_offset: int = Query(0, ge=0),
    rows_limit: Optional[int] = Query(None, ge=1, le=REPORT_ROWS_PAGE_MAX),
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """One report's payload. The report must belong to the path agent — a report
    id from another agent returns the same 404 as one that does not exist, so
    this is not a cross-agent read oracle.

    `rows_offset`/`rows_limit` (#2162) window a **tabular** payload's rows, so a
    large table is not shipped whole to a client — #1537's pattern, without
    #1537's route: the operator row reader is JWT-gated and a portal principal
    cannot reach it, and a second route here would need a second copy of the
    uniform-404 contract above. Both params are optional and default to today's
    behaviour; a non-tabular payload with `rows_limit` set comes back whole with
    no `row_meta` rather than a 400, because the server holds the payload and the
    client should not have to guess its shape from an agent-authored hint.

    Bounds come from `models.REPORT_ROWS_PAGE_MAX`, the same constant the
    operator reader uses — imported, never re-typed, so the two page sizes cannot
    drift apart with each side's tests pinning its own version.
    """
    email = principal.email
    _require_roster(agent_name, email, principal.is_platform)
    from services import rate_limiter

    # Paging re-reads and re-parses the whole (≤5 MiB) blob per request, so the
    # route that exists to cut TRANSFER raises READS — fine behind an operator
    # JWT, an amplification primitive on a prefix a client can loop. Keyed after
    # the roster gate so an unreachable agent can never mint limiter keys.
    rate_limiter.enforce(f"portal_report_detail:{email}:{agent_name}", 60, 60)
    report = agent_page.report_detail(
        agent_name, report_id, client_email=email,
        rows_offset=rows_offset, rows_limit=rows_limit,
    )
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.post("/agents/{agent_name}/chat", response_model=PortalChatResponse)
async def portal_chat(
    agent_name: str,
    body: PortalChatRequest,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """One client chat turn to a rostered agent. Identity from
    `get_portal_identity` (portal session token or operator preview); scoped to
    the caller's roster (a scope miss is a uniform 404). Runs the standard
    platform execution — observable under Executions, cost-tracked — instead of
    exposing the fenced OSS `/api/agents/{name}/chat` to the portal token.

    Rate-limited per (client, agent) in two tiers (ent#287) — a paid surface, so
    the agent's capacity limiter (which bounds concurrency, not spend) is not a
    sufficient control on its own.
    """
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can
    # SEE — `get_roster` unions owned agents for a platform session, so the
    # gate below has to as well, or an owner 404s on their own agent.
    include_owned = principal.is_platform
    from services import rate_limiter

    _require_roster(agent_name, email, include_owned)
    # Burst first: a rejected burst returns before the hourly window records a
    # hit, so a client held at the per-minute limit does not also burn their
    # hourly budget. (The converse costs one burst slot on an hourly rejection —
    # it decays in 60s and is not worth a two-phase commit.)
    rate_limiter.enforce(
        f"portal_chat:{email}:{agent_name}", PORTAL_CHAT_BURST_LIMIT, 60, detail=_CHAT_LIMIT_DETAIL
    )
    rate_limiter.enforce(
        f"portal_chat_hourly:{email}:{agent_name}", PORTAL_CHAT_HOURLY_LIMIT, 3600,
        detail=_CHAT_LIMIT_DETAIL,
    )
    try:
        result = await service.portal_chat(agent_name, body.message, email, session_id=body.session_id,
                                          include_owned=include_owned,
                                          new_thread=body.new_thread)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return PortalChatResponse(**result)


@router.post("/agents/{agent_name}/ratings", response_model=PortalRatingResult)
def portal_submit_rating(agent_name: str, body: PortalRatingRequest,
                         background: BackgroundTasks,
                         principal: PortalPrincipal = Depends(get_portal_principal)):
    """Rate one agent message or deliverable (ent#366).

    Writes to `agent_evaluations` — the ent#206 referee surface — under a
    `workspace:<email>` evaluator. This is the **amendment to that write fence**
    the issue calls for: the fence exists so the graded agent never writes its
    own grade, and a Workspace principal is exactly the kind of writer it was
    built to admit. The rated agent still has no write path, and the route is
    roster-scoped with the target checked against the reader (an id alone proves
    nothing — see `_rating_target_is_visible`).

    Idempotent per person per target, so changing your mind updates rather than
    appends and a tally counts people, not clicks.

    Rate-limited per (client, agent) in TWO tiers, like `portal_chat`: the row
    is cheap, but a down-rating with a comment dispatches a full agent turn, so
    the hourly tier is what keeps this route inside the same budget the client
    has through chat. The dispatch itself is claimed per (evaluator, target) in
    the service, so re-rating one target is an update, never a new turn.
    """
    email = principal.email
    include_owned = principal.is_platform
    from services import rate_limiter
    # Burst first, so a client held at the per-minute limit does not also burn
    # their hourly budget (the `portal_chat` ordering and its rationale).
    rate_limiter.enforce(
        f"portal_rating:{email}:{agent_name}", PORTAL_RATING_BURST_LIMIT, 60,
        detail=_RATING_LIMIT_DETAIL,
    )
    rate_limiter.enforce(
        f"portal_rating_hourly:{email}:{agent_name}", PORTAL_RATING_HOURLY_LIMIT, 3600,
        detail=_RATING_LIMIT_DETAIL,
    )
    try:
        result = service.submit_rating(
            agent_name, email,
            target_kind=body.target_kind, target_id=body.target_id,
            rating=body.rating, comment=body.comment,
            include_owned=include_owned,
        )
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    # AC #2/#6: the words go further only when the agent can actually take them.
    # The rating is already durable at this point, so everything below is
    # additive — no branch here can lose the feedback.
    #
    # Dispatched as a BACKGROUND task: it is a whole agent turn, and a person
    # clicking "not what I needed" should not wait on the agent that just
    # disappointed them. `dispatched` therefore means "handed off", which is the
    # honest claim — the turn's own outcome is observable as an execution row.
    if result["comment_recorded"] and body.rating == "down":
        if service.agent_has_capture_feedback(agent_name):
            # One turn per person per target (ent#366 review). The row is
            # idempotent through the partial UNIQUE; without this the SIDE
            # EFFECT was not, so re-rating the same target with a tweaked
            # comment re-fired a full agent turn each time.
            _claim = service.claim_capture_feedback_dispatch(
                agent_name, email,
                target_kind=body.target_kind, target_id=body.target_id,
            )
            if _claim is not None:
                background.add_task(
                    service.dispatch_capture_feedback,
                    agent_name, email,
                    target_kind=body.target_kind, target_id=body.target_id,
                    comment=body.comment or "",
                    claim=_claim,
                )
                result["capture_feedback"] = "dispatched"
            else:
                # Honest, and distinct from "no skill": the words ARE recorded
                # (the update landed above), and this person has already spent
                # a handoff for this exact target inside the dedup window.
                #
                # It deliberately does NOT claim the agent can read the row —
                # `routers/evaluations.py` nulls `comment` for every machine
                # principal, so it cannot (review finding). What is true is that
                # the comment is stored and a second turn is not being spent.
                # A dispatch that FAILED releases its claim, so this branch now
                # means "already delivered", not "already attempted".
                result["capture_feedback"] = "already_dispatched"
        else:
            # Not a failure: the comment is recorded either way, and saying so
            # lets the UI thank the person for words that landed rather than
            # imply a follow-up that will not happen.
            result["capture_feedback"] = "skill_not_installed"
    return PortalRatingResult(**result)


@router.post("/agents/{agent_name}/tts")
async def portal_tts(agent_name: str, body: PortalTtsRequest,
                     principal: PortalPrincipal = Depends(get_portal_principal)):
    """Speak a reply in portal voice mode (#78) — returns `audio/mpeg` (MP3) for
    the given text via the shared ElevenLabs voice layer, using the agent's
    configured voice. Roster-scoped (miss → 404); 404 when voice isn't available,
    422 when synthesis fails / exceeds the cost cap (client keeps the text).
    Rate-limited per (client, agent) — it's a paid, client-facing surface."""
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can
    # SEE — `get_roster` unions owned agents for a platform session, so the
    # gate below has to as well, or an owner 404s on their own agent.
    include_owned = principal.is_platform
    from services import rate_limiter
    # 20 syntheses / minute per client+agent, on top of tts_service's char cap.
    rate_limiter.enforce(f"portal_tts:{email}:{agent_name}", 20, 60)
    try:
        audio = await service.synthesize_portal_tts(agent_name, email, body.text, include_owned=include_owned)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return Response(content=audio, media_type="audio/mpeg")


@router.post("/agents/{agent_name}/stt")
async def portal_stt(agent_name: str, file: UploadFile = File(...),
                     principal: PortalPrincipal = Depends(get_portal_principal)):
    """Speech-to-text for portal voice input (#78) — the Firefox/Safari fallback
    when the browser has no Web Speech API. Accepts a recorded audio clip, returns
    `{text}` via ElevenLabs Scribe. Roster-scoped (miss → 404); rate-limited per
    (client, agent); fail-soft (client just types on any error)."""
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can
    # SEE — `get_roster` unions owned agents for a platform session, so the
    # gate below has to as well, or an owner 404s on their own agent.
    include_owned = principal.is_platform
    from services import rate_limiter
    rate_limiter.enforce(f"portal_stt:{email}:{agent_name}", 20, 60)
    audio = await file.read()
    try:
        text = await service.transcribe_portal_audio(
            agent_name, email, file.filename or "audio.webm",
            file.content_type or "application/octet-stream", audio,
            include_owned=include_owned,
        )
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return {"text": text}


@router.get("/agents/{agent_name}/sessions", response_model=PortalSessions)
def portal_sessions(agent_name: str, principal: PortalPrincipal = Depends(get_portal_principal)):
    """The client's conversation threads with a rostered agent (most-recent first)
    — the chat-history list. Roster-scoped (miss → 404)."""
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can
    # SEE — `get_roster` unions owned agents for a platform session, so the
    # gate below has to as well, or an owner 404s on their own agent.
    include_owned = principal.is_platform
    try:
        return service.list_sessions(agent_name, email, include_owned=include_owned)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/agents/{agent_name}/sessions", response_model=PortalSessionSummary)
def portal_create_session(agent_name: str, principal: PortalPrincipal = Depends(get_portal_principal)):
    """Open a fresh conversation thread ("New chat"). Roster-scoped (miss → 404).
    Returns the empty session; its title fills in on the first message."""
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can
    # SEE — `get_roster` unions owned agents for a platform session, so the
    # gate below has to as well, or an owner 404s on their own agent.
    include_owned = principal.is_platform
    try:
        return service.create_session(agent_name, email, include_owned=include_owned)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.patch("/agents/{agent_name}/sessions/{session_id}", response_model=PortalSessionSummary)
def portal_rename_session(agent_name: str, session_id: str, body: PortalSessionRename,
                          principal: PortalPrincipal = Depends(get_portal_principal)):
    """Title a thread (ent#473). Roster-scoped, then session-scoped to the
    caller — both misses are the uniform 404. A refused title is a NAMED 400
    (`detail.code == "invalid_title"`, `detail.reason` the rule it broke,
    `detail.message` what to change), never a 422 about a schema."""
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can SEE.
    include_owned = principal.is_platform

    from services import rate_limiter

    # A write, bounded per viewer like every other portal write surface; the
    # keystroke-level edits happen client-side, one PATCH per committed rename.
    rate_limiter.enforce(f"portal_rename:{email}", 60, 60)
    try:
        return service.rename_session(agent_name, email, session_id, body.title,
                                      include_owned=include_owned)
    except InvalidChatTitle as e:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_title", "reason": e.reason, "message": e.detail},
        )
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/agents/{agent_name}/history", response_model=PortalHistory)
def portal_history(agent_name: str, session_id: str | None = None,
                   principal: PortalPrincipal = Depends(get_portal_principal)):
    """The client's persisted conversation with a rostered agent (oldest-first),
    so it survives a refresh / re-sign-in. With ``?session_id=`` returns that
    thread; without, the most-recent one. Roster-scoped (miss → 404).
    """
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can
    # SEE — `get_roster` unions owned agents for a platform session, so the
    # gate below has to as well, or an owner 404s on their own agent.
    include_owned = principal.is_platform
    try:
        return service.get_history(agent_name, email, session_id=session_id, include_owned=include_owned)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/agents/{agent_name}/documents", response_model=PortalDocuments)
def portal_documents(agent_name: str, principal: PortalPrincipal = Depends(get_portal_principal)):
    """Files a rostered agent has shared (FILES-001), with download URLs. Scoped
    to the caller's roster (miss → uniform 404). The `?sig=` token gates the
    public OSS download route, so no portal auth rides on the link.
    """
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can
    # SEE — `get_roster` unions owned agents for a platform session, so the
    # gate below has to as well, or an owner 404s on their own agent.
    include_owned = principal.is_platform
    try:
        return service.portal_documents(agent_name, email, include_owned=include_owned)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/agents/{agent_name}/documents", response_model=PortalUpload)
async def portal_upload(
    agent_name: str,
    file: UploadFile = File(...),
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Upload a file to a rostered agent (lands in `~/inbox/<client-email>/`).
    Scoped to the caller's roster (miss → 404); size-capped, filename sanitized.

    Rate-limited per client **across agents** in two tiers (ent#287) — the inbox
    quota bounds resident bytes, but a same-filename re-upload overwrites rather
    than accumulates, so without this the per-request work is unbounded. Keyed on
    email alone: the cost being bounded here is the operator's, not one agent's.
    """
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can
    # SEE — `get_roster` unions owned agents for a platform session, so the
    # gate below has to as well, or an owner 404s on their own agent.
    include_owned = principal.is_platform
    from services import rate_limiter

    # Both gates run before `read()`, which is what materialises the body in RAM,
    # and before the service call, which is the expensive part (two docker execs
    # + a tar + a container write).
    #
    # What this ordering does NOT save is the transfer: Starlette parses the
    # multipart form in `get_request_handler` BEFORE the endpoint function is
    # called, spooling anything over 1 MiB to a temp file. By the time we get
    # here the 25 MiB is already on disk. Rejecting earlier than this would need
    # middleware (or a Content-Length pre-check), not an in-handler gate — do not
    # let a comment here claim otherwise.
    _require_roster(agent_name, email, include_owned)
    rate_limiter.enforce(
        f"portal_upload:{email}", PORTAL_UPLOAD_BURST_LIMIT, 60, detail=_UPLOAD_LIMIT_DETAIL
    )
    rate_limiter.enforce(
        f"portal_upload_hourly:{email}", PORTAL_UPLOAD_HOURLY_LIMIT, 3600,
        detail=_UPLOAD_LIMIT_DETAIL,
    )
    data = await file.read()
    try:
        result = await service.portal_upload_document(agent_name, email, file.filename or "upload", data,
                                                       include_owned=include_owned)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return PortalUpload(**result)


@router.get("/agents/{agent_name}/uploads", response_model=PortalUploads)
async def portal_uploads(agent_name: str, principal: PortalPrincipal = Depends(get_portal_principal)):
    """Files the client has sent to this rostered agent (their inbox) — so they
    can review what they've uploaded. Roster-scoped (miss → 404); empty when the
    agent is offline.
    """
    email = principal.email
    # ent#358: the scope of what a caller can DO must equal what they can
    # SEE — `get_roster` unions owned agents for a platform session, so the
    # gate below has to as well, or an owner 404s on their own agent.
    include_owned = principal.is_platform
    try:
        return await service.list_client_uploads(agent_name, email, include_owned=include_owned)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# --- Streaming turns (ent#286) -----------------------------------------------

# How long to keep trying to attach to a turn the agent has not registered yet,
# and how often. The wait is bounded by the turn still being in flight, so this
# ceiling only matters when the marker outlives the agent-side execution.
_STREAM_ATTACH_TIMEOUT_S = 30.0
_STREAM_ATTACH_POLL_S = 0.4

@router.post("/agents/{agent_name}/chat/stream", response_model=PortalTurnStarted, status_code=202)
async def portal_chat_stream(
    agent_name: str,
    body: PortalChatRequest,
    request: Request,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Begin a turn and return its id immediately (202) so the client can watch it.

    A SEPARATE route rather than a mode on `POST .../chat`: ent#83 documented
    that endpoint as the integration surface for fully custom, API-only clients,
    and silently turning its 200-with-a-reply into a 202-with-an-id would break
    every one of them.

    Rate limited on the SAME keys as the synchronous route — otherwise this
    would be a way to buy unlimited turns by asking for them a different way.
    """
    from services import rate_limiter

    email = principal.email
    include_owned = principal.is_platform

    _require_roster(agent_name, email, include_owned)
    rate_limiter.enforce(
        f"portal_chat:{email}:{agent_name}", PORTAL_CHAT_BURST_LIMIT, 60, detail=_CHAT_LIMIT_DETAIL
    )
    rate_limiter.enforce(
        f"portal_chat_hourly:{email}:{agent_name}", PORTAL_CHAT_HOURLY_LIMIT, 3600,
        detail=_CHAT_LIMIT_DETAIL,
    )
    # Invariant #18: a producer boundary that creates an execution accepts an
    # Idempotency-Key. This route creates one, so a retried dispatch — a proxy
    # replay, a client timeout-and-resend — would otherwise run and BILL the
    # turn twice. Scoped per (agent, client) so two clients cannot collide on a
    # key, and fail-open: a dedup failure must never block a real turn.
    from services import idempotency_service

    # Read off the request rather than as a typed `Header(...)` param: this
    # module uses `from __future__ import annotations`, which turns the
    # annotation into a ForwardRef that pydantic cannot resolve for a header
    # (500 at request time, not import time — so it only shows up when called).
    idempotency_key = request.headers.get("Idempotency-Key")

    scope = f"portal_stream:{agent_name}:{email}"
    decision = idempotency_service.begin(scope, idempotency_key)
    if decision.replay:
        if decision.in_flight:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "request_in_progress",
                    "message": "A request with this Idempotency-Key is still being processed.",
                    "execution_id": decision.execution_id,
                },
            )
        return JSONResponse(
            content=decision.snapshot or {"execution_id": decision.execution_id},
            headers={"X-Idempotent-Replay": "true"},
            status_code=202,
        )

    try:
        started = await service.start_portal_turn(
            agent_name, body.message, email,
            session_id=body.session_id, include_owned=include_owned,
            # ent#451 — the streaming path is what the Workspace uses; the sync
            # one above is its fallback. A flag honoured by only one brings the
            # bug back exactly when streaming fails.
            new_thread=body.new_thread,
        )
    except ClientPortalError as e:
        idempotency_service.fail(decision)
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception:
        idempotency_service.fail(decision)
        raise

    idempotency_service.complete(decision, started.get("execution_id"), started)
    return PortalTurnStarted(**started)


@router.post("/agents/{agent_name}/executions/{execution_id}/terminate")
async def portal_terminate_execution(
    agent_name: str,
    execution_id: str,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Stop one of the caller's OWN in-flight turns (ent#155).

    The same three gates as the stream route, in the same order and for the
    same reason: the agent must be on the caller's roster, the execution must
    belong to that agent, and it must have been STARTED BY THIS CALLER. The
    third is load-bearing — executions are agent-scoped, so without it any
    client of a shared agent could stop another client's turn by guessing an
    id, which is strictly worse than reading one.

    A turn that has already finished answers `already_terminal` rather than a
    4xx: the client is racing its own reattach poll, and losing that race is
    not an error anyone can act on — the reply is on screen. Cancellation
    itself is the platform's existing CAS-guarded CANCELLED terminal
    (#679/#1332), so a cancel landing after the fact cannot overwrite it.

    The in-flight marker and the resume lock need no special handling here:
    `start_portal_turn`'s background task clears both in its `finally`, on
    every exit path including the one a SIGINT produces.
    """
    email = principal.email
    include_owned = principal.is_platform

    _require_roster(agent_name, email, include_owned)
    if not service.execution_belongs_to_caller(execution_id, agent_name, email):
        # Uniform 404, exactly like the roster miss — never confirm that an
        # execution exists to someone who may not touch it.
        raise HTTPException(status_code=404, detail="Execution not found")

    from services import rate_limiter
    rate_limiter.enforce(f"portal_cancel:{email}:{agent_name}", 30, 60,
                         detail="Too many cancellations.")

    try:
        return await service.terminate_portal_turn(agent_name, execution_id)
    except ClientPortalError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/agents/{agent_name}/executions/{execution_id}/stream")
async def portal_stream_execution(
    agent_name: str,
    execution_id: str,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Live SSE for one of the caller's OWN turns.

    Three gates, in order: the agent must be on the caller's roster, the
    execution must belong to that agent, and it must have been started by this
    caller. The third is the load-bearing one — executions are agent-scoped, so
    without it any client of a shared agent could watch another client's
    conversation by guessing an id.

    A completed execution still streams: the agent replays its buffered log and
    ends with `stream_end`, which is what makes a browser refresh mid-turn
    reattach instead of starting a second turn.
    """
    email = principal.email
    include_owned = principal.is_platform

    _require_roster(agent_name, email, include_owned)
    if not service.execution_belongs_to_caller(execution_id, agent_name, email):
        # Uniform 404, exactly like the roster miss above — never confirm that
        # an execution exists to someone who may not watch it.
        raise HTTPException(status_code=404, detail="Execution not found")

    container = get_agent_container(agent_name)
    if not container or container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    async def proxy_stream():
        # The agent answers 404 in TWO harmless situations, and treating either
        # as fatal is what made a mid-turn reload look broken:
        #
        #   too early — the client subscribes the instant it gets the 202, but
        #     the turn passes through admission, capacity and the resume lock
        #     before the agent registers it. Measured at ~1-2s on a warm agent.
        #   too late  — the turn is over and the agent has dropped its buffered
        #     log. Nothing is wrong; the reply is in history.
        #
        # So: retry briefly while the turn is still live, and end the stream
        # cleanly (never `error`) when it simply is not streamable. The client
        # reads the persisted reply on `stream_end` either way.
        agent_url = f"http://agent-{agent_name}:8000/api/executions/{execution_id}/stream"
        deadline = asyncio.get_event_loop().time() + _STREAM_ATTACH_TIMEOUT_S
        try:
            async with agent_httpx_client(agent_name, timeout=None) as client:
                while True:
                    async with client.stream("GET", agent_url) as response:
                        if response.status_code == 200:
                            async for chunk in response.aiter_text():
                                yield chunk
                            return
                        if response.status_code != 404:
                            yield f"data: {json.dumps({'type': 'error', 'message': f'Agent returned {response.status_code}'})}\n\n"
                            yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                            return
                    # 404 — is this turn still worth waiting for?
                    if not service.get_turn_inflight_matches(execution_id):
                        # Finished (or never ran). Not an error: end cleanly so
                        # the client goes and reads the reply.
                        yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                        return
                    if asyncio.get_event_loop().time() >= deadline:
                        logger.info(
                            "[PortalStream] gave up attaching to %s on %s after %ss",
                            execution_id, agent_name, _STREAM_ATTACH_TIMEOUT_S,
                        )
                        yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                        return
                    await asyncio.sleep(_STREAM_ATTACH_POLL_S)
        except httpx.ConnectError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Failed to connect to agent'})}\n\n"
            yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
        except Exception as e:  # noqa: BLE001 — the stream must always terminate cleanly
            logger.error("[PortalStream] error streaming %s from %s: %s", execution_id, agent_name, e)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Stream interrupted'})}\n\n"
            yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"

    return StreamingResponse(
        proxy_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
