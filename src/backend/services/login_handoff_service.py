"""One-time sign-in hand-off from the Workspace code page to `/login` (#4157).

#3922 sends a platform account that signs in on the Workspace's code page to
`/login`, so the platform session is minted only by the platform door. The
portal code it entered was correct and is already spent by then, so without a
hand-off the person has to request and enter a second code.

After a CORRECT portal code the portal door mints one of these tokens. `/login`
redeems it at `POST /api/auth/email/handoff`, which runs the platform email
login's checks in place of the code check. The token:

* is opaque (`secrets.token_urlsafe(32)`) and lives only in the browser's memory;
* is stored in Redis as a SHA-256 hash, so a Redis read does not yield a
  usable token, and is never logged;
* is bound to one email and expires after ``HANDOFF_TTL_SECONDS``;
* is single-use: redemption is an atomic ``GETDEL``, and a token presented for
  another address is spent and refused.

It is not a session and not a login code: it does not live in
`email_login_codes`, and `/api/auth/email/verify` does not accept it.

Redis is required. Without it nothing is minted (the person falls back to a
second code on `/login`) and nothing redeems: an auth path fails closed.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Optional

logger = logging.getLogger(__name__)

HANDOFF_TTL_SECONDS = 300
_KEY_PREFIX = "login_handoff:"


def _redis():
    # Late import: the shared auth Redis client (also what tests patch).
    from routers import auth as auth_router
    return auth_router.get_redis_client()


def _key(token: str) -> str:
    return _KEY_PREFIX + hashlib.sha256(token.encode()).hexdigest()


def _norm(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def mint(email: Optional[str]) -> Optional[str]:
    """Mint a hand-off token for ``email``; None when it cannot (no Redis)."""
    addr = _norm(email)
    if not addr:
        return None
    r = _redis()
    if r is None:
        logger.warning("[#4157] Redis unavailable; no sign-in hand-off minted")
        return None
    token = secrets.token_urlsafe(32)
    try:
        r.setex(_key(token), HANDOFF_TTL_SECONDS, addr)
    except Exception as exc:  # noqa: BLE001 — fall back to the second-code path
        logger.warning("[#4157] could not store sign-in hand-off: %s", type(exc).__name__)
        return None
    return token


def redeem(token: Optional[str], email: Optional[str]) -> bool:
    """Consume ``token``; True only if it was live and bound to ``email``."""
    addr = _norm(email)
    if not token or not addr:
        return False
    r = _redis()
    if r is None:
        logger.warning("[#4157] Redis unavailable; refusing sign-in hand-off")
        return False
    try:
        bound = r.getdel(_key(token))
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("[#4157] sign-in hand-off redemption failed: %s", type(exc).__name__)
        return False
    if isinstance(bound, bytes):
        bound = bound.decode("utf-8", "replace")
    return bool(bound) and hmac.compare_digest(bound, addr)
