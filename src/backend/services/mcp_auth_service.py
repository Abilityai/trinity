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
   status, same bytes, same latency, and — deliberately — no audit row. Any
   per-branch signal is an oracle for "is this address registered here". This is
   the #186 property, mirrored from ``routers/auth.py::request_email_login_code``.

The internal secret authenticates the CALLER, never the action: ``playbooks``
and ``chat`` re-gate on ``db.email_has_agent_access(agent, email)`` per call, so
a compromised MCP server still cannot reach an agent the asserted email cannot.
Nothing here ever returns a credential — the session binding lives only in the
MCP server's memory and dies with the connection.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from fastapi import HTTPException

from database import db
from models import McpInlineAgent, McpInlineVerifyResponse
from services.connector_service import fetch_live_playbooks, resolve_exposed_playbooks

logger = logging.getLogger(__name__)

# Codes requested per email per window before we stop sending. Mirrors the web
# path (``routers/auth.py``: 3 per 10 minutes) — over-limit is a silent skip,
# never a distinct status, or the limiter itself becomes the oracle.
CODE_REQUESTS_PER_WINDOW = 3
CODE_REQUEST_WINDOW_MINUTES = 10

# Login codes live 10 minutes, same as the web email flow.
CODE_EXPIRY_MINUTES = 10

# Chat dispatched on behalf of an inline-authenticated user. Matches the ceiling
# the public path uses, and lands in the "MCP" analytics bucket.
CHAT_TIMEOUT_SECONDS = 900
CHAT_TRIGGERED_BY = "mcp"

# Strong refs for fire-and-forget email dispatch (the asyncio GC footgun — a
# task with no live reference can be collected mid-send). Same guard as
# ``routers/auth.py::_email_dispatch_tasks``.
_email_dispatch_tasks: set = set()


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


def request_login_code(email: str, session_id: Optional[str] = None) -> None:
    """Email a login code iff the address is known. Always returns None.

    The caller returns one constant body no matter what happened in here — that
    is the whole enumeration-safety contract, so this function must never signal
    its branch through a return value, an exception, or an audit row.

    Every send is dispatched fire-and-forget so the known-address path returns
    as fast as the unknown one. A blocking send would leak membership through
    latency even with identical bodies (#186 timing oracle).

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

    if not _email_is_known(email):
        # Silent, and identical to every other branch from the caller's view.
        logger.info("[#848] inline login code suppressed: unknown address (session=%s)", session_id)
        return

    try:
        recent = db.count_recent_code_requests(email, minutes=CODE_REQUEST_WINDOW_MINUTES)
    except Exception:
        logger.exception("[#848] rate-limit read failed; suppressing code send")
        return

    if recent >= CODE_REQUESTS_PER_WINDOW:
        # WARN server-side (fail-loud for ops) but return the same nothing, so a
        # repeat-request differential can't distinguish "limited" from "unknown".
        logger.warning("[#848] inline login code suppressed: rate limit for %s", email)
        return

    try:
        code_data = db.create_login_code(email, expiry_minutes=CODE_EXPIRY_MINUTES)
    except Exception:
        logger.exception("[#848] failed to create inline login code")
        return

    task = asyncio.create_task(_dispatch_code_email(email, code_data["code"]))
    _email_dispatch_tasks.add(task)
    task.add_done_callback(_email_dispatch_tasks.discard)


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

def assert_email_may_reach_agent(email: str, agent: str) -> None:
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


# ---------------------------------------------------------------------------
# playbooks + chat
# ---------------------------------------------------------------------------

async def list_exposed_playbooks(email: str, agent: str) -> List:
    """Exposed playbooks (allow-list ∩ ``user_invocable``) for a verified email."""
    assert_email_may_reach_agent(email, agent)

    cfg = db.get_connector_config(agent)
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
