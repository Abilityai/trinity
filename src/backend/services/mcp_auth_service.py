"""MCP inline email auth (#848) — all logic behind ``/api/internal/mcp-auth/*``.

Lets an external user sign in to Trinity from inside their MCP client with the
existing 6-digit email-code flow: no pre-minted API key, no web-UI visit. The
MCP server relays four calls here on behalf of a keyless session, authenticating
itself with ``X-Internal-Secret``.

Spec: ``docs/memory/requirements/mcp.md`` §7.6.

Two properties this module exists to hold:

1. **``request_login`` is not an open email relay.** The endpoint is reachable
   by an unauthenticated MCP client, so a code is only ever generated for an
   address Trinity already knows (a ``users`` row, or an ``agent_sharing``
   allow-list entry). Everything else silently does nothing.

2. **The unknown-address branch is indistinguishable from the known one.** Same
   status, same bytes, and — deliberately — no audit row. Latency too, but only
   because NOTHING branch-dependent runs on the request path: the known-check,
   the cap read and the code INSERT all run in a background task, after the 202
   has been flushed (see ``schedule_login_code``). Any
   per-branch signal is an oracle for "is this address registered here". This is
   the #186 property, mirrored from ``routers/auth.py::request_email_login_code``.

The internal secret authenticates the CALLER, never the action: ``playbooks``
and ``chat`` re-gate on ``db.email_has_agent_access(agent, email)`` per call, so
a compromised MCP server still cannot reach an agent the asserted email cannot.
Nothing here ever returns a credential — the session binding lives only in the
MCP server's memory and dies with the connection.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import BackgroundTasks, HTTPException

from database import db
from models import McpInlineAgent, McpInlineVerifyResponse
from services import rate_limiter
from services.connector_service import fetch_live_playbooks, resolve_exposed_playbooks

logger = logging.getLogger(__name__)

# Codes requested per email per window before we stop sending. Mirrors the web
# path (``routers/auth.py``: 3 per 10 minutes) — over-limit is a silent skip,
# never a distinct status, or the limiter itself becomes the oracle.
CODE_REQUESTS_PER_WINDOW = 3
CODE_REQUEST_WINDOW_MINUTES = 10

# Coarse global ceiling on /request across the whole surface. The per-address
# cap above only applies AFTER the known-check, so it never bounds lookups for
# unknown addresses; this does. Sized well above any plausible legitimate rate
# (inline sign-in is a rare, human-paced act) so it only ever trips on abuse.
INLINE_REQUEST_GLOBAL_LIMIT = 60
INLINE_REQUEST_GLOBAL_WINDOW = 60

# Login codes live 10 minutes, same as the web email flow.
CODE_EXPIRY_MINUTES = 10

# Chat dispatched on behalf of an inline-authenticated user. Matches the ceiling
# the public path uses, and lands in the "MCP" analytics bucket.
CHAT_TIMEOUT_SECONDS = 900
CHAT_TRIGGERED_BY = "mcp"

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# ---------------------------------------------------------------------------
# request — send a code, but only to an address Trinity already knows
# ---------------------------------------------------------------------------

def _email_is_known(email: str) -> bool:
    """True iff Trinity already knows this address.

    Two sources, both cheap: an existing account, or membership in any agent's
    sharing allow-list (the address an owner invited but who has never logged
    in — precisely the user inline auth exists to onboard).

    Deliberately NOT the email whitelist: §7.6 bypasses it, because the
    whitelist-gated ``/api/auth/email/request`` silently no-ops for exactly the
    external addresses this feature serves. Authorization is the per-agent
    access gate at ``playbooks``/``chat`` instead, never this membership test —
    this only decides whether an email is worth sending.

    Fail-CLOSED on a DB error: an inability to answer "do we know this address"
    must not degrade into "email anyone who asks".
    """
    try:
        if db.get_user_by_email(email):
            return True
        return bool(db.get_agents_shared_with_email(email))
    except Exception:
        logger.exception("[#848] known-email lookup failed; suppressing code send")
        return False


async def _dispatch_code_email(email: str, code: str) -> None:
    from services.email_service import EmailService

    try:
        await EmailService().send_verification_code(
            email, code, context_label="Trinity MCP login"
        )
    except Exception:
        logger.exception("[#848] failed to send inline login code")


def _resolve_and_create_code(email: str, session_id: Optional[str]) -> Optional[str]:
    """The blocking half: known-check, per-address cap, code row.

    Returns the code to send, or None if nothing should be sent. Never raises —
    every failure is a silent suppression, because the caller has already
    answered 202 and there is no channel left to signal on.
    """
    if not _email_is_known(email):
        logger.info("[#848] inline login code suppressed: unknown address (session=%s)", session_id)
        return None

    try:
        recent = db.count_recent_code_requests(email, minutes=CODE_REQUEST_WINDOW_MINUTES)
    except Exception:
        logger.exception("[#848] rate-limit read failed; suppressing code send")
        return None

    if recent >= CODE_REQUESTS_PER_WINDOW:
        logger.warning("[#848] inline login code suppressed: rate limit for %s", email)
        return None

    try:
        return db.create_login_code(email, expiry_minutes=CODE_EXPIRY_MINUTES)["code"]
    except Exception:
        logger.exception("[#848] failed to create inline login code")
        return None


async def _process_login_request(email: str, session_id: Optional[str]) -> None:
    """Everything branch-dependent, run after the response has been sent.

    The DB work stays on the request thread on purpose. An earlier revision
    pushed it to ``asyncio.to_thread`` and every send silently stopped: SQLite
    connections are thread-affine, so ``_email_is_known`` raised in the worker,
    hit its own fail-closed handler and suppressed the code — a total outage of
    the feature that presented as "the email just never arrives". Starlette runs
    background tasks after the response is flushed, which is all the
    constant-time property needs; it does not need another thread.
    """
    code = _resolve_and_create_code(email, session_id)
    if code:
        await _dispatch_code_email(email, code)


def schedule_login_code(
    background_tasks: "BackgroundTasks",
    email: str,
    session_id: Optional[str] = None,
) -> None:
    """Queue a login-code send to run after the response. Constant time.

    The caller returns one constant body no matter what happens — that is the
    whole enumeration-safety contract, so this must never signal its branch
    through a return value, an exception, an audit row, **or latency**.

    Latency is why *nothing* branch-dependent happens inline. An earlier version
    did the known-check, the per-address cap read and the code INSERT
    synchronously, and only the email send was deferred. That still leaked: the
    known branch performed a committing write the unknown branch did not, which
    measured ~1.9x (~1ms) even on in-process SQLite, and the gap widens with a
    real database because a durable commit is the dominant term. Identical bytes
    do not help if the response arrives at a measurably different time (#186 is
    about the oracle, not the body).

    So the request path now does exactly two things regardless of input:
    normalize, and enqueue. Starlette runs the queued task only after the
    response has been flushed, so no branch-dependent work can contribute to
    measured latency.

    Rate limiting is keyed on the EMAIL, not the caller IP: every request here
    arrives from the MCP server, so one IP bucket would be shared by the whole
    fleet of clients — meaningless as a limit and a trivial fleet-wide DoS.
    ``session_id`` is logged as a secondary signal only (the MCP server applies
    its own per-session cap); it is never a limiter key here, because a client
    controls its own session ids and could rotate past any cap keyed on one.
    """
    email = normalize_email(email)
    if not email:
        return

    # Coarse global bound (#848 H3). The per-address cap lives behind the
    # known-check, so an UNKNOWN address is never counted by it — leaving an
    # unauthenticated caller free to pump unlimited lookups. This bounds the
    # whole surface regardless of which branch a request takes, and is
    # deliberately checked BEFORE the branch so it cannot itself become a
    # differential. Over-limit is a silent skip: the caller still gets the same
    # 202, because a 429 here would be its own oracle.
    if not rate_limiter.check(
        "mcp_inline_auth:request", INLINE_REQUEST_GLOBAL_LIMIT, INLINE_REQUEST_GLOBAL_WINDOW
    ).allowed:
        logger.warning("[#848] inline login request suppressed: global rate limit")
        return

    background_tasks.add_task(_process_login_request, email, session_id)


# ---------------------------------------------------------------------------
# verify — check the code, resolve what the address may reach
# ---------------------------------------------------------------------------

def verify_login_code(email: str, code: str) -> Optional[dict]:
    """Consume the code. Returns the user dict on success, None on failure.

    Rate limiting is the ROUTER's job (it owns the client IP and the
    ``record_*`` bookkeeping); this is the verification proper.
    """
    email = normalize_email(email)
    if not email or not code:
        return None

    if not db.verify_login_code(email, code):
        return None

    # #314: role comes from the whitelist row's default_role, falling back to
    # "user". Never force a role here — passing "creator" is the exact silent
    # promotion #314 closed.
    user = db.get_or_create_email_user(email)
    if not user:
        logger.error("[#848] verified code but failed to resolve user for %s", email)
        return None

    try:
        db.update_last_login(user["username"])
    except Exception:
        logger.warning("[#848] update_last_login failed for %s", user["username"])

    return user


def resolve_accessible_agents(email: str) -> List[McpInlineAgent]:
    """Connector-enabled agents this email may reach.

    Both conditions are required and both are re-checked per call at the
    ``playbooks``/``chat`` gates — this listing is a convenience for the client,
    never the authorization itself:

      * the agent's connector is ENABLED (otherwise it is not an MCP surface at
        all, and listing it would advertise an agent the user cannot use), and
      * ``email_has_agent_access`` — owner, admin, or on the sharing allow-list.

    Iterating connector-enabled agents (a small set) rather than the fleet keeps
    this bounded. An empty list is a normal, expected result: the address
    verified but no owner has granted it anything yet.
    """
    from services.agent_service.mcp_tool_names import build_tool_description

    email = normalize_email(email)
    if not email:
        return []

    try:
        candidates = db.list_connector_enabled_agents()
    except Exception:
        logger.exception("[#848] failed to list connector-enabled agents")
        return []

    out: List[McpInlineAgent] = []
    for agent_name in candidates:
        try:
            if not db.email_has_agent_access(agent_name, email):
                continue
        except Exception:
            # Fail closed per agent — an unreadable access check is not access.
            logger.exception("[#848] access check failed for %s", agent_name)
            continue
        out.append(
            McpInlineAgent(name=agent_name, description=build_tool_description(agent_name))
        )
    return out


def build_verify_response(user: dict, email: str) -> McpInlineVerifyResponse:
    """The success payload. Carries no credential — by design and by test."""
    return McpInlineVerifyResponse(
        verified=True,
        username=user.get("username"),
        agents=resolve_accessible_agents(email),
    )


# ---------------------------------------------------------------------------
# the per-call authorization gate
# ---------------------------------------------------------------------------

def assert_email_may_reach_agent(email: str, agent: str) -> Optional[dict]:
    """403 unless the email may reach this agent through the connector surface.

    Applied on EVERY inline-auth data call, before anything else. The internal
    secret proved who is asking; it proves nothing about what they may reach,
    and the MCP server's claim that a session is verified is not authorization
    either — only the email's own standing in Trinity is.

    Both failures return the same 403 body: an "agent exists but you lack
    access" vs "no such connector" split is an enumeration oracle over the
    fleet (Invariant #8, self-uniform handlers).
    """
    email = normalize_email(email)
    denied = HTTPException(
        status_code=403,
        detail="This address cannot access that agent.",
    )
    if not email or not agent:
        raise denied

    try:
        cfg = db.get_connector_config(agent)
        has_access = db.email_has_agent_access(agent, email)
    except Exception:
        logger.exception("[#848] gate lookup failed for agent=%s", agent)
        raise denied

    if not cfg or not cfg.get("enabled"):
        raise denied
    if not has_access:
        raise denied

    # Returned so callers can reuse it instead of re-reading (see
    # list_exposed_playbooks) — the gate is the single read point.
    return cfg


# ---------------------------------------------------------------------------
# playbooks + chat
# ---------------------------------------------------------------------------

async def list_exposed_playbooks(email: str, agent: str) -> List:
    """Exposed playbooks (allow-list ∩ ``user_invocable``) for a verified email.

    Uses the config the gate already read rather than re-reading it. A second
    read would open a TOCTOU window spanning a DB round-trip plus the container
    call in ``fetch_live_playbooks`` (10s timeout), during which a revoke could
    land and still yield playbooks from a now-disabled connector — and, being
    unwrapped, would surface a DB error as a 500 where the gate fails closed.
    """
    cfg = assert_email_may_reach_agent(email, agent)

    live = await fetch_live_playbooks(agent)
    allow = cfg.get("exposed_playbooks") if cfg else None
    return resolve_exposed_playbooks(live, allow)


async def dispatch_chat(email: str, agent: str, message: str) -> "object":
    """Run one turn against the agent, attributed to the verified email.

    Goes through ``TaskExecutionService`` like every other trigger, so the turn
    gets an execution row, activity tracking, slot management, and credential
    sanitization — an inline-auth chat is not a side door around the execution
    path. The user account is resolved (and created on first sign-in) so the
    turn is attributed to a real identity rather than the MCP server.
    """
    assert_email_may_reach_agent(email, agent)
    email = normalize_email(email)

    from services.task_execution_service import get_task_execution_service

    result = await get_task_execution_service().execute_task(
        agent_name=agent,
        message=message,
        triggered_by=CHAT_TRIGGERED_BY,
        source_user_email=email,
        timeout_seconds=CHAT_TIMEOUT_SECONDS,
        # #894: the inline-auth caller is an external user reaching the agent
        # over a public-facing surface, so it takes the public-channel model
        # override, not the owner's default.
        model=db.get_public_channel_model(agent),
    )

    if result.status in ("failed", "cancelled"):
        error = result.error or ""
        if "at capacity" in error:
            raise HTTPException(status_code=429, detail="Agent is busy. Please try again later.")
        if "timed out" in error:
            raise HTTPException(status_code=504, detail="Request timed out. Please try again.")
        logger.error("[#848] inline chat failed for %s: %s", agent, error)
        raise HTTPException(status_code=502, detail="Failed to process your request.")

    return result
