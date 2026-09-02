"""``/ws`` client identity and agent scope (ent#467).

``/ws`` used to need no identity at all: it was ``SCOPE_ALL`` and unfiltered,
so a valid ticket was the whole authorization story. Once its events are
scoped to the agents a user may see, the socket has to answer *who is this*
before it accepts — which is exactly the router → service → db split
(Invariant #1), with ``main.py``'s WebSocket endpoint as the thin router.

Both functions are total: they never raise for a missing user, a missing
email or a DB read that returns nothing. They are also the only place the
``/ws`` scope policy lives, so the connect-time resolution and the live
roster refresh cannot drift apart.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def accessible_agents_for(email: str) -> List[str]:
    """Agent names a non-admin ``email`` may observe. Empty for a falsy email.

    Used both at connect and by the dispatcher's background roster refresh,
    so a share granted mid-connection reaches a live socket.
    """
    from database import db

    if not email:
        return []
    return db.get_accessible_agent_names(email, False)


def resolve_ws_identity(username: str) -> Optional[Dict[str, Any]]:
    """Resolve a ``/ws`` ticket subject to the agent scope it may observe.

    Returns ``None`` — which the endpoint turns into a closed socket — for
    anything that is not an active Trinity user: an unknown username, a
    suspended account (#995; a ticket can be minted seconds before the
    suspension lands and stays valid for its 30s TTL), or a user lookup that
    raised. Refusing beats registering an unidentified client, and the
    frontend does not retry a 4001, so this must stay narrow.

    A non-admin row with **no email** is a resolved identity with an EMPTY
    roster, not a refusal: `agent_ownership` is joined to `users.email` and
    `agent_sharing` is keyed on it, so such a user genuinely can access no
    agent — the empty set is the exact answer, and it leaves the socket alive
    for the agent-less events (`resync_required`, bulk clears) instead of
    permanently dark.

    The admin branch deliberately resolves **no** roster. Admins are exempt
    from filtering, and a snapshot taken here would invite a later reader to
    start filtering on it — at which point every agent created after the page
    loaded goes silent for the operator most likely to be watching one being
    created.
    """
    from database import db

    if not username:
        return None
    try:
        user = db.get_user_by_username(username)
    except Exception:  # noqa: BLE001 — an unresolvable identity must close the socket
        logger.warning("[/ws] user lookup failed for ticket subject", exc_info=True)
        return None
    if not user:
        return None
    if user.get("suspended_at"):
        return None
    is_admin = user.get("role") == "admin"
    email = user.get("email") or ""
    if is_admin:
        return {"email": email, "is_admin": True, "accessible_agents": []}
    if not email:
        return {"email": "", "is_admin": False, "accessible_agents": []}
    try:
        agents = accessible_agents_for(email)
    except Exception:  # noqa: BLE001
        logger.warning("[/ws] accessible-agent lookup failed", exc_info=True)
        return None
    return {"email": email, "is_admin": False, "accessible_agents": list(agents or [])}
