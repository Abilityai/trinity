"""Voice socket authentication (trinity-enterprise#595, VOICE-002).

The voice socket authenticates with a single-use, short-lived credential; no
session token appears in any URL. Three steps live here so the router keeps no
auth logic (Invariant #1):

1. **Issuance** (`issue_voice_ticket`): both voice start routes mint a ticket
   for the session they just created and the caller they resolved. The ticket
   is Redis state only (`ws_ticket_service`, `GETDEL`, 30 s), so any worker can
   consume it.
2. **First-frame authentication** (`authenticate_voice_socket`): after the
   transport is accepted, the client's first text frame
   `{"type": "auth", "ticket": "..."}` is read within a bound, the ticket is
   consumed under the exact scope `voice:{voice_session_id}`, and the account
   it names is re-read.
3. **Current-account validation** (`_resolve_voice_ticket_user`): the one seam
   that turns a consumed ticket into a `User`. The role comes from the
   account's row as it is now, never from the ticket.

Grant lifetime: a ticket is an independent grant, bounded by its expiry and by
single use. It carries no claim of the session token it was minted from, so a
sign-out or a signing-key change after minting does not withdraw it; it is
usable only until it is consumed or expires. Session lookup, the owner gate and
every effect of the call happen in the router, after this module returned a
user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable, Mapping, Optional

from models import User
from services.ws_ticket_service import TicketStoreUnavailable, consume_ticket, mint_ticket

logger = logging.getLogger(__name__)

VOICE_TICKET_TTL_SECONDS = 30
AUTH_FRAME_TIMEOUT_SECONDS = 5.0
AUTH_FRAME_MAX_BYTES = 1024

ORIGIN_OPERATOR = "operator"
ORIGIN_WORKSPACE = "workspace"
_ORIGINS = frozenset({ORIGIN_OPERATOR, ORIGIN_WORKSPACE})

# `secrets.token_urlsafe(32)` → 43 URL-safe base64 characters, no padding.
_TICKET_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")

# Close codes keep their #600 / ent#534 meanings; they are observable now that
# every refusal happens after the transport was accepted.
CLOSE_UNAUTHENTICATED = 4001
CLOSE_FORBIDDEN = 4003
CLOSE_NOT_FOUND = 4004

MSG_NOT_VERIFIED = "The voice connection expired or could not be verified. Start a new call."
MSG_RELOAD = "Voice was updated. Reload this page, then start a new call."
MSG_UNAVAILABLE = "Voice is unavailable right now. Try again shortly."
MSG_SESSION_GONE = "This voice call has already ended. Start a new call."
MSG_NOT_OWNER = "This voice call belongs to another account. Start your own call."

#: The 503 detail both start routes return when a session or its ticket cannot
#: be stored. Fixed words: never the underlying exception text.
START_UNAVAILABLE_DETAIL = MSG_UNAVAILABLE


class VoiceAuthRefused(Exception):
    """A refused voice socket: the close code, and the words the person sees."""

    def __init__(self, code: int, message: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.message = message
        self.reason = reason


class VoiceTicketIssueError(RuntimeError):
    """The voice ticket could not be stored; the start must answer 503."""


def voice_ticket_scope(voice_session_id: str) -> str:
    """The one scope a voice ticket opens: its own session's socket."""
    return f"voice:{voice_session_id}"


async def issue_voice_ticket(
    user: User,
    voice_session_id: str,
    *,
    origin: str,
    remove_session: Callable[[str], Awaitable[Any]],
) -> dict:
    """Mint the socket credential for a session the caller just created.

    Returns the two response fields. On a store failure the new session is
    removed (best effort — during a Redis outage its metadata TTL is the
    fallback) and `VoiceTicketIssueError` is raised; no provider connection or
    Workspace lease exists yet at this point, so nothing else needs undoing.
    """
    if origin not in _ORIGINS:
        raise ValueError(f"unknown voice ticket origin {origin!r}")
    try:
        ticket = mint_ticket(
            user.username,
            scope=voice_ticket_scope(voice_session_id),
            ttl_seconds=VOICE_TICKET_TTL_SECONDS,
            claims={"user_id": user.id, "origin": origin},
        )
    except TicketStoreUnavailable:
        logger.warning("voice start: ticket store unavailable; removing the new session")
        try:
            await remove_session(voice_session_id)
        except Exception:  # noqa: BLE001 — the session TTL is the fallback
            logger.warning("voice start: could not remove the session after a ticket failure")
        raise VoiceTicketIssueError(START_UNAVAILABLE_DETAIL) from None
    return {
        "websocket_ticket": ticket,
        "websocket_ticket_expires_in": VOICE_TICKET_TTL_SECONDS,
    }


def _refused(message: str, reason: str, code: int = CLOSE_UNAUTHENTICATED) -> VoiceAuthRefused:
    return VoiceAuthRefused(code=code, message=message, reason=reason)


async def _receive_auth_ticket(websocket) -> str:
    """Read the one authentication frame, bounded in time and size.

    Its content is never logged: it is a credential.
    """
    try:
        message = await asyncio.wait_for(websocket.receive(), timeout=AUTH_FRAME_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise _refused(MSG_NOT_VERIFIED, "Authentication timed out") from None
    except Exception:  # noqa: BLE001 — any transport failure ends the attempt
        raise _refused(MSG_NOT_VERIFIED, "Authentication required") from None

    if not isinstance(message, Mapping) or message.get("type") != "websocket.receive":
        raise _refused(MSG_NOT_VERIFIED, "Authentication required")
    text = message.get("text")
    if not isinstance(text, str):
        raise _refused(MSG_NOT_VERIFIED, "Authentication must be a text frame")
    if len(text.encode("utf-8")) > AUTH_FRAME_MAX_BYTES:
        raise _refused(MSG_NOT_VERIFIED, "Authentication frame too large")
    try:
        frame = json.loads(text)
    except (TypeError, ValueError):
        raise _refused(MSG_NOT_VERIFIED, "Malformed authentication frame") from None
    if not isinstance(frame, dict) or frame.get("type") != "auth":
        raise _refused(MSG_NOT_VERIFIED, "Malformed authentication frame")
    ticket = frame.get("ticket")
    if not isinstance(ticket, str) or not _TICKET_SHAPE.fullmatch(ticket):
        raise _refused(MSG_NOT_VERIFIED, "Malformed authentication frame")
    return ticket


def _resolve_voice_ticket_user(payload: Mapping[str, Any]) -> Optional[User]:
    """The account a consumed voice ticket names, as it is NOW — or None.

    The single attach-time validation seam. Refuses a missing or malformed id,
    an unknown origin, a deleted account, a username that no longer matches the
    id, a suspended account (#995), and an operator-origin grant whose account
    is now Workspace-only (ent#837). A Workspace-origin grant keeps
    Workspace-only eligibility. The returned `User` is built from the row, so
    no role travels inside the ticket.
    """
    user_id = payload.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return None
    origin = payload.get("origin")
    if origin not in _ORIGINS:
        return None

    from database import db
    from dependencies import is_workspace_only_role

    row = db.get_user_by_id(user_id)
    if not row:
        return None
    if row.get("id") != user_id or row.get("username") != payload.get("sub"):
        return None
    if row.get("suspended_at"):
        return None
    role = row.get("role")
    if origin == ORIGIN_OPERATOR and is_workspace_only_role(role):
        return None
    return User(id=row["id"], username=row["username"], email=row.get("email"), role=role or "user")


async def authenticate_voice_socket(websocket, voice_session_id: str) -> User:
    """Authenticate an ACCEPTED voice socket, before its session is looked up.

    Raises `VoiceAuthRefused` (always 4001 here) for a query-string credential,
    a missing, late, oversized, binary or malformed auth frame, an expired,
    reused, wrong-session or wrong-surface ticket, an unreadable ticket store,
    and an account that no longer qualifies. The decision never depends on
    whether `voice_session_id` names a live session.
    """
    params = websocket.query_params
    if "token" in params or "ticket" in params:
        # A tab from before this protocol. The values are never read or logged.
        raise _refused(MSG_RELOAD, "Reload required")

    ticket = await _receive_auth_ticket(websocket)
    try:
        payload = consume_ticket(
            ticket, scope=voice_ticket_scope(voice_session_id), raise_on_unavailable=True,
        )
    except TicketStoreUnavailable:
        raise _refused(MSG_UNAVAILABLE, "Voice unavailable") from None
    if payload is None:
        raise _refused(MSG_NOT_VERIFIED, "Invalid or expired voice ticket")

    user = _resolve_voice_ticket_user(payload)
    if user is None:
        logger.warning("voice_ws: a consumed ticket names an account that no longer qualifies")
        raise _refused(MSG_NOT_VERIFIED, "Voice ticket account is not eligible")
    return user


def refusal_frame(message: str) -> dict:
    """The existing `status` frame shape, so the client shows the words."""
    return {"type": "status", "state": "ended", "reason": "error", "message": message}


async def refuse_voice_socket(websocket, refused: VoiceAuthRefused) -> None:
    """Say why, then close with the refusal's code. A client that already went
    away gets neither; its own close handler supplies the words."""
    try:
        await websocket.send_json(refusal_frame(refused.message))
    except Exception:  # noqa: BLE001
        pass
    try:
        await websocket.close(code=refused.code, reason=refused.reason)
    except Exception:  # noqa: BLE001
        pass
