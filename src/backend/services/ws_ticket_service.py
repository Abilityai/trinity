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
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Optional

from routers.auth import get_redis_client

logger = logging.getLogger(__name__)

_TICKET_TTL_SECONDS = 30
# Ceiling for caller-supplied TTLs (VoIP dial+ring); keeps a leaked ticket short-lived.
_TICKET_TTL_MAX_SECONDS = 600
_TICKET_KEY_PREFIX = "ws_ticket:"


def _key(ticket: str) -> str:
    return f"{_TICKET_KEY_PREFIX}{ticket}"


def mint_ticket(subject: str, *, scope: str = "user", ttl_seconds: Optional[int] = None) -> str:
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

    Returns:
        The opaque ticket string. Hand to client; expect them to
        present it back on the WebSocket URL within the TTL.

    Raises:
        RuntimeError: Redis unavailable. Auth must fail closed.
    """
    r = get_redis_client()
    if r is None:
        raise RuntimeError("Redis unavailable — cannot mint WebSocket ticket")

    ttl = _TICKET_TTL_SECONDS if ttl_seconds is None else max(1, min(int(ttl_seconds), _TICKET_TTL_MAX_SECONDS))
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps({"sub": subject, "scope": scope})
    r.setex(_key(ticket), ttl, payload)
    return ticket


def consume_ticket(ticket: str) -> Optional[dict]:
    """Atomically exchange a ticket for its principal.

    Uses Redis ``GETDEL`` (Redis 6.2+) so the ticket is single-use:
    the same ticket can never authenticate two connections, even
    under a race. Returns ``None`` if the ticket is missing,
    expired, or already consumed.

    Args:
        ticket: The opaque ticket the client presented.

    Returns:
        ``{"sub": "<username>", "scope": "<scope>"}`` on success;
        ``None`` on any failure mode (missing, expired, used,
        Redis down, malformed).
    """
    if not ticket:
        return None

    r = get_redis_client()
    if r is None:
        logger.warning("Redis unavailable — rejecting WebSocket ticket exchange")
        return None

    try:
        raw = r.getdel(_key(ticket))
    except Exception as exc:
        logger.warning("WebSocket ticket exchange failed: %s", exc)
        return None

    if not raw:
        return None

    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("WebSocket ticket payload was not valid JSON")
        return None
