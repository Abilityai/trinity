"""
WebSocket auth tickets (#550).

Closes the JWT-in-URL leak on ``/ws`` by swapping the long-lived bearer
token for a short-lived opaque ticket. Browser flow:

    1. Client calls ``POST /api/ws/ticket`` (JWT-authed, normal Bearer).
    2. Backend mints a 32-byte urlsafe ticket, stores it in Redis with
       a 30-second TTL, and returns it.
    3. Client connects to ``/ws?ticket=<ticket>``. Backend atomically
       GETDELs the Redis key — single-use — and resolves it to the
       authenticated subject before accepting the WebSocket.

Why this matters:

- The JWT no longer appears in nginx access logs, browser history,
  or upstream proxy logs.
- A leaked ticket is single-use and expires in 30s, so replay is
  effectively impossible.
- CSWSH is mitigated: a malicious page can't mint a ticket on behalf
  of the victim because ``POST /api/ws/ticket`` requires the JWT in
  an ``Authorization`` header (cross-origin requests would fail
  CORS preflight or lack the header entirely).

Redis is required. If Redis is unavailable ``mint_ticket`` raises;
``consume_ticket`` returns ``None`` and the WebSocket connection is
rejected. Failing closed is the right call for an auth path.

Every consumer names the surface it accepts (trinity-enterprise#595):
``consume_ticket(ticket, scope=...)`` has no default, and a ticket whose
scope differs is refused after it was consumed. The live scopes are
``user`` (``/ws``), ``voip:{call_id}`` (the VoIP media stream) and
``voice:{voice_session_id}`` (the voice socket).
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Mapping, Optional

from routers.auth import get_redis_client

logger = logging.getLogger(__name__)

_TICKET_TTL_SECONDS = 30
# Ceiling for caller-supplied TTLs (VoIP dial+ring); keeps a leaked ticket short-lived.
_TICKET_TTL_MAX_SECONDS = 600
_TICKET_KEY_PREFIX = "ws_ticket:"


# Claims a caller may not supply through ``claims`` — they are the ticket's
# identity and its surface, set only by the named arguments.
_RESERVED_CLAIMS = frozenset({"sub", "scope"})


class TicketStoreUnavailable(RuntimeError):
    """The ticket store could not be read or written (trinity-enterprise#595).

    A ``RuntimeError`` so every existing ``except RuntimeError`` caller keeps
    working. Its message is fixed: it never carries the ticket, the payload,
    the store URL or the underlying exception text.
    """


def _key(ticket: str) -> str:
    return f"{_TICKET_KEY_PREFIX}{ticket}"


def mint_ticket(
    subject: str,
    *,
    scope: str = "user",
    ttl_seconds: Optional[int] = None,
    claims: Optional[Mapping[str, Any]] = None,
) -> str:
    """Mint a single-use opaque WebSocket ticket.

    Args:
        subject: The authenticated principal (username for JWT users).
        scope: Identifier for the auth surface — ``"user"`` for the
            browser ``/ws`` flow, or a call-bound value like
            ``"voip:{call_id}"`` so the ticket only authenticates the
            one Media Streams URL it was minted for (VOIP-001, #1056).
        ttl_seconds: Override the default 30s TTL. The browser flow
            consumes the ticket in milliseconds, but the Twilio Media
            Streams socket only connects *after* PSTN dial + ring, which
            routinely exceeds 30s — VoIP mints at a wider TTL. Clamped to
            a sane ceiling so a caller can't mint a near-permanent ticket.
        claims: Supplemental JSON-serialisable claims stored beside
            ``sub``/``scope`` (the voice ticket's ``user_id`` and ``origin``,
            trinity-enterprise#595). They may not replace ``sub`` or
            ``scope`` (``ValueError``).

    Returns:
        The opaque ticket string. Hand to client; expect them to
        present it back on the WebSocket URL within the TTL.

    Raises:
        TicketStoreUnavailable: Redis unavailable or the write failed (a
            ``RuntimeError``). Auth must fail closed.
        ValueError: ``claims`` tried to replace ``sub`` or ``scope``.
    """
    extra = dict(claims or {})
    if _RESERVED_CLAIMS & set(extra):
        raise ValueError("ticket claims may not replace 'sub' or 'scope'")

    r = get_redis_client()
    if r is None:
        raise TicketStoreUnavailable("Redis unavailable — cannot mint WebSocket ticket")

    ttl = _TICKET_TTL_SECONDS if ttl_seconds is None else max(1, min(int(ttl_seconds), _TICKET_TTL_MAX_SECONDS))
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps({"sub": subject, "scope": scope, **extra})
    try:
        r.setex(_key(ticket), ttl, payload)
    except Exception:
        # Fixed words only: the exception text can carry the store URL.
        logger.warning("WebSocket ticket store write failed")
        raise TicketStoreUnavailable("Redis unavailable — cannot mint WebSocket ticket") from None
    return ticket


def consume_ticket(
    ticket: str, *, scope: str, raise_on_unavailable: bool = False
) -> Optional[dict]:
    """Atomically exchange a ticket for its principal — on one surface only.

    Uses Redis ``GETDEL`` (Redis 6.2+) so the ticket is single-use:
    the same ticket can never authenticate two connections, even
    under a race. Returns ``None`` if the ticket is missing,
    expired, or already consumed.

    The order is the contract (trinity-enterprise#595): the ticket is
    consumed FIRST, then its payload is checked. A ticket presented on a
    surface it was not minted for is therefore spent as well as refused, and
    cannot be retried where it belongs.

    Args:
        ticket: The opaque ticket the client presented.
        scope: The exact scope this consumer accepts (``"user"``,
            ``"voip:{call_id}"``, ``"voice:{voice_session_id}"``). Required:
            a consumer that does not name its surface would accept every
            other surface's tickets.
        raise_on_unavailable: Raise ``TicketStoreUnavailable`` instead of
            returning ``None`` when the store cannot be read, so a caller can
            tell the person "try again" rather than "start over".

    Returns:
        The decoded payload (``{"sub": ..., "scope": ...}`` plus any
        supplemental claims) when it is an object with a non-empty string
        ``sub`` and exactly ``scope``; ``None`` on every other outcome
        (missing, expired, used, wrong surface, malformed, Redis down).
    """
    if not ticket:
        return None

    r = get_redis_client()
    if r is None:
        logger.warning("Redis unavailable — rejecting WebSocket ticket exchange")
        if raise_on_unavailable:
            raise TicketStoreUnavailable("Redis unavailable — cannot exchange WebSocket ticket")
        return None

    try:
        raw = r.getdel(_key(ticket))
    except Exception:
        # Fixed words only: the exception text can carry the store URL or key.
        logger.warning("WebSocket ticket exchange failed: ticket store unavailable")
        if raise_on_unavailable:
            raise TicketStoreUnavailable("Redis unavailable — cannot exchange WebSocket ticket") from None
        return None

    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("WebSocket ticket payload was not valid JSON")
        return None

    if not isinstance(payload, dict):
        logger.warning("WebSocket ticket payload was not an object")
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        logger.warning("WebSocket ticket payload has no subject")
        return None
    if payload.get("scope") != scope:
        logger.warning("WebSocket ticket presented on a surface it was not minted for")
        return None
    return payload
