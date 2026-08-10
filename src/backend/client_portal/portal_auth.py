"""Portal identity resolution for the Client Portal (#78).

`get_portal_identity` resolves the caller to a single email, accepting EITHER:
  * a portal session token (a verified email, no platform account — the real
    client path), OR
  * a normal platform user (operator preview) → that user's email.

So the roster works both for a signed-in client and for an operator previewing.
The portal token is fenced out of every platform endpoint by OSS
`get_current_user`; only the entitled portal endpoints accept it, via this
dependency.

A blocked client (ent#281) is refused here as well as at sign-in. Both layers
are needed and they fail differently: session revocation is a Redis cutoff and
is best-effort, so with Redis down a blocked client's live token would still
decode. This check reads the durable block row, so the block holds regardless —
it is the layer that makes "blocked" mean blocked.
"""
from __future__ import annotations

import logging

from typing import NamedTuple

from fastapi import Depends, HTTPException, Request

from dependencies import oauth2_scheme, decode_portal_session, get_current_user

logger = logging.getLogger(__name__)


def _reject_if_blocked(email: str) -> None:
    """403 a blocked client. Fail-closed — a lookup failure denies.

    Denying on a DB error costs nothing extra in practice: every portal endpoint
    downstream of this dependency reads the same database (roster scoping at
    minimum), so a DB that cannot answer this question cannot serve the request
    either. It would only turn a 500 into a 403.
    """
    from . import db as portal_db

    try:
        blocked = portal_db.is_client_blocked(email)
    except Exception as exc:  # noqa: BLE001 — a block must not evaporate on error
        logger.error("[#281] block lookup failed for %s; denying: %s", email, exc)
        raise HTTPException(status_code=403, detail="Access has been revoked.")
    if blocked:
        raise HTTPException(status_code=403, detail="Access has been revoked.")


class PortalPrincipal(NamedTuple):
    """Who is asking, and by which credential (ent#357).

    Every route below cared only about the email until the Workspace rename made
    a signed-in PLATFORM user a first-class audience. The roster is built from
    `agent_sharing`, and Trinity refuses to share an agent with its own owner
    ("Cannot share an agent with yourself"), so an owner's roster is *always*
    empty — they reach the surface in one click and land on nothing.

    Widening the roster for everyone would be wrong: an external client must
    keep seeing exactly what was shared with them and nothing else. So the
    principal KIND has to travel with the email, and only the platform path
    unlocks the owned-agents union.
    """
    email: str
    is_platform: bool


async def get_portal_principal(
    request: Request, token: str = Depends(oauth2_scheme)
) -> PortalPrincipal:
    """Resolve the caller to (email, is_platform). See `PortalPrincipal`."""
    # Portal session token → the verified email it carries.
    email = decode_portal_session(token)
    if email:
        _reject_if_blocked(email)
        return PortalPrincipal(email, False)

    # Otherwise a platform principal (operator preview / signed-in user) →
    # resolve their email.
    user = await get_current_user(request, token)  # raises 401 if the token is invalid
    from database import db
    row = db.get_user_by_username(user.username)
    email = (row or {}).get("email") if row else None
    if not email:
        raise HTTPException(status_code=403, detail="No email is associated with this account")
    email = email.lower()
    # The operator-preview path is gated too: previewing as a blocked client must
    # show what that client sees, and an operator whose OWN email is blocked has
    # been blocked as a client — the block is on the identity, not on the route.
    _reject_if_blocked(email)
    return PortalPrincipal(email, True)


async def get_portal_identity(
    principal: PortalPrincipal = Depends(get_portal_principal),
) -> str:
    """The caller's email, for the routes that need identity and nothing else.

    Kept as the dependency the other 13 routes use, so this change adds a
    capability without touching their signatures or their behaviour.
    """
    return principal.email
