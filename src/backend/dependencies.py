"""
FastAPI dependencies for the Trinity backend.
"""
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Annotated
from fastapi import Depends, HTTPException, status, Request, Path
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from models import User
from config import SECRET_KEY, ALGORITHM
from database import db
from redis_breaker_util import get_breaker_redis

logger = logging.getLogger(__name__)

# JWT revocation (#187): a logged-out token's `jti` is stored in Redis until it
# would have expired anyway, so the 7-day token can be killed early. Fail-open
# — if Redis is unreachable the check is skipped (a revoked token would then
# pass until natural expiry), matching the platform-wide fail-open posture; the
# threat (UnderDefense 3.3.4) is Low/CVSS 2.0 and backend restarts already
# rotate SECRET_KEY (invalidating every token).
_JWT_REVOKED_PREFIX = "jwt:revoked:"

# Bulk portal-session revocation (trinity-enterprise#281): "log this client out
# everywhere, now". A per-`jti` blacklist cannot express it — `jti` is random per
# token and nothing indexes email → issued jtis, so answering "which tokens does
# this email hold?" would need a write-side index maintained at every mint.
#
# Instead store ONE cutoff timestamp per email and reject any portal token issued
# at or before it. O(1) to write, O(1) to check, nothing to enumerate, and it
# covers every mint path by construction — interactive email-OTP sign-in and the
# ent#163 delegated exchange both go through `create_portal_session_token`, so
# neither can be forgotten here (the failure mode a hand-maintained index has).
#
# The TTL is the max session lifetime: a token older than that is expired anyway,
# so the key self-expires exactly when it stops mattering — same bounded-growth
# property as the `jti` blacklist above, no sweep.
#
# Fail-open on Redis, matching #187 and the platform posture. This is deliberate
# and is why revocation is NOT the whole feature: a *blocked* client is refused
# from a durable DB row on the enterprise side, which keeps working with Redis
# down. Revocation is the fast path; the block is the load-bearing one.
_PORTAL_REVOKED_PREFIX = "portal:revoked_before:"


def _portal_revoked_key(email: str) -> str:
    return f"{_PORTAL_REVOKED_PREFIX}{email.strip().lower()}"


def _portal_session_policy() -> tuple:
    """`(idle_seconds, absolute_seconds)` — the sliding-session policy (ent#375).

    Imported lazily: `settings_service` imports `db`, and `dependencies` is
    imported by nearly everything, so a module-level import here would make an
    import cycle out of what is one settings read.
    """
    try:
        from services.settings_service import settings_service
        return settings_service.get_portal_session_policy()
    except Exception:  # pragma: no cover — settings unavailable
        # Auth path: degrade to the shipped policy rather than 500 the request.
        from config import (
            PORTAL_SESSION_ABSOLUTE_DAYS_DEFAULT,
            PORTAL_SESSION_IDLE_DAYS_DEFAULT,
        )
        return (
            int(PORTAL_SESSION_IDLE_DAYS_DEFAULT * 86400),
            int(PORTAL_SESSION_ABSOLUTE_DAYS_DEFAULT * 86400),
        )


def revoke_portal_sessions_for_email(email: str) -> bool:
    """Invalidate every portal session token already issued to ``email``.

    Edition-agnostic primitive: OSS owns the mint, the decode, and this bulk
    revoke; the entitled module decides *when* to call it (same split as the
    ent#163 mint). Returns True when the cutoff was durably written — the caller
    reports an honest outcome rather than claiming success on a Redis outage.
    """
    if not email:
        return False
    r = get_breaker_redis()
    if r is None:
        logger.warning("[auth] portal session revoke skipped — Redis unavailable")
        return False
    # Cutoff is `now`, and the check below rejects `iat <= cutoff`, so a token
    # minted in the same second as the revoke is killed too. For a kill switch,
    # rounding toward revoking is the only safe direction.
    now = int(datetime.now(timezone.utc).timestamp())
    # TTL must outlive the LONGEST-LIVED token this cutoff has to kill, which is
    # the absolute cap — not the idle window and (ent#375) no longer the old flat
    # 12 hours. Getting this wrong resurrects revoked sessions: with sliding
    # sessions capped at 30 days, a 12-hour cutoff evaporates while tokens minted
    # before the revoke are still valid, and the next request sails through
    # because there is no cutoff left to compare against. Read the policy so the
    # TTL tracks the cap automatically instead of restating it.
    _, absolute_s = _portal_session_policy()
    ttl = absolute_s + 60  # + slack for clock skew
    try:
        r.setex(_portal_revoked_key(email), ttl, str(now))
        return True
    except Exception as exc:  # pragma: no cover — fail-open
        logger.warning(f"[auth] revoke_portal_sessions_for_email failed: {exc}")
        return False


def portal_sessions_revoked_at(email: str) -> Optional[int]:
    """The active revocation cutoff for ``email``, or None. Fail-open → None."""
    if not email:
        return None
    r = get_breaker_redis()
    if r is None:
        return None
    try:
        raw = r.get(_portal_revoked_key(email))
    except Exception as exc:  # pragma: no cover — fail-open
        logger.warning(f"[auth] portal_sessions_revoked_at failed: {exc}")
        return None
    if raw is None:
        return None
    try:
        return int(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, AttributeError):
        return None


def revoke_token_jti(jti: str, exp_ts: Optional[int]) -> None:
    """Blacklist a token's `jti` until its own expiry (best-effort, fail-open).

    TTL is the token's remaining lifetime, so the key self-expires exactly when
    the token would — no unbounded growth, no separate sweep.
    """
    if not jti:
        return
    r = get_breaker_redis()
    if r is None:
        return
    now = int(datetime.now(timezone.utc).timestamp())
    ttl = (int(exp_ts) - now) if exp_ts else 0
    if ttl <= 0:
        return  # already expired — nothing to revoke
    try:
        r.setex(f"{_JWT_REVOKED_PREFIX}{jti}", ttl, "1")
    except Exception as exc:  # pragma: no cover — fail-open
        logger.warning(f"[auth] revoke_token_jti failed (fail-open): {exc}")


def is_token_revoked(jti: Optional[str]) -> bool:
    """True if this `jti` was revoked via logout. Fail-open → False on no jti
    (legacy token minted before #187) or Redis error."""
    if not jti:
        return False
    r = get_breaker_redis()
    if r is None:
        return False
    try:
        return r.exists(f"{_JWT_REVOKED_PREFIX}{jti}") > 0
    except Exception as exc:  # pragma: no cover — fail-open
        logger.warning(f"[auth] is_token_revoked failed (fail-open): {exc}")
        return False


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, stored_password: str) -> bool:
    """Verify password against stored bcrypt hash.

    Security: Plaintext fallback removed (M-003, 2026-02-23).
    All passwords must be bcrypt hashed.
    """
    try:
        return pwd_context.verify(plain_password, stored_password)
    except Exception:
        # Invalid hash format - reject
        return False


def authenticate_user(username: str, password: str):
    """Authenticate a user by username — or, for the admin, registered email.

    #82 Phase 1: the admin may sign in with the email they registered at
    first-run setup (or via Settings) instead of the fixed 'admin' username.
    When the identifier looks like an email and no username matches, we fall
    back to an email lookup. This is safe for password auth because only an
    account that actually has a password hash can pass `verify_password` below —
    email-code-only users have none, so they can never authenticate this way
    even if matched by email.
    """
    user = db.get_user_by_username(username)
    if not user and username and "@" in username:
        user = db.get_user_by_email(username.strip().lower())
    if not user:
        return False
    if not user.get("password"):
        # No password hash (email-code-only account) — never password-authenticate.
        return False
    if not verify_password(password, user["password"]):
        return False
    return user


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None, mode: str = "prod") -> str:
    """Create a JWT access token.

    Args:
        data: Claims to encode in the token
        expires_delta: Token expiration time
        mode: Authentication mode - "dev" for local login, "prod" for Auth0
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({
        "exp": expire,
        "mode": mode,  # Track auth mode to prevent dev/prod token mixing
        "jti": secrets.token_urlsafe(16),  # #187: per-token id for revocation
    })
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


# Scope marker for the short-lived token issued between password/email
# verification and second-factor completion (enterprise 2FA, #5). A token
# carrying this scope is NOT a valid access token — it only authorizes the
# /api/enterprise/2fa/login/* endpoints.
MFA_CHALLENGE_SCOPE = "mfa_challenge"
MFA_CHALLENGE_EXPIRE_MINUTES = 5


def create_mfa_challenge_token(username: str, mode: str = "prod") -> str:
    """Mint a short-lived challenge token binding a half-authenticated session
    to its eventual login ``mode``. Generic (OSS) — the enterprise module
    decides *whether* to require it; this only encodes it. The carried ``mode``
    is replayed into the final access token so admin/email tokens keep their
    original mode after the second factor."""
    return create_access_token(
        data={"sub": username, "scope": MFA_CHALLENGE_SCOPE},
        expires_delta=timedelta(minutes=MFA_CHALLENGE_EXPIRE_MINUTES),
        mode=mode,
    )


def decode_mfa_challenge(token: str) -> Optional[dict]:
    """Validate a challenge token. Returns ``{"username", "mode"}`` if the
    token is a non-expired challenge token for an existing, non-suspended
    user; ``None`` otherwise. Used by the enterprise 2FA login endpoints to
    resolve the half-authenticated identity before minting the real token."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if payload.get("scope") != MFA_CHALLENGE_SCOPE:
        return None
    username = payload.get("sub")
    if not username:
        return None
    user = db.get_user_by_username(username)
    if not user or user.get("suspended_at"):
        return None
    return {"username": username, "mode": payload.get("mode", "prod")}


# Scope marker for the Client Portal session token (enterprise `client_portal`,
# epic #78). A portal client is a *verified email*, NOT a `users` row — this
# token carries only the email and is fenced OUT of every platform endpoint
# (get_current_user / decode_token reject it, mirroring MFA_CHALLENGE_SCOPE). It
# only authorizes the entitled portal endpoints, which resolve identity via
# `decode_portal_session`. Edition-agnostic: OSS owns the mint/decode primitive
# + the fence; the enterprise module decides *when* to mint one (after email-code
# verification of a client whose email has a share). No new secret — same
# SECRET_KEY/ALGORITHM, so a backend restart invalidates portal sessions too.
PORTAL_SESSION_SCOPE = "portal_session"

# RETIRED as a lifetime (ent#375). The session now slides: `_portal_session_policy()`
# supplies an idle window and an absolute cap, and every consumer reads those.
#
# Deleted rather than left at 12: a stale constant beside a live policy is the
# hazard this feature already tripped over once. `revoke_portal_sessions_for_email`
# derived its Redis TTL from it, so extending sessions to 30 days while it still
# said 12 hours would have expired the revoke cutoff 29.5 days before the tokens
# it exists to kill — silently resurrecting revoked sessions. Anything still
# reading a fixed portal lifetime should read the policy instead, and a NameError
# is the right way to find that out.

# --- Delegated portal identity (ent#163) ---------------------------------
#
# A `portal_delegate` MCP key lets a TRUSTED backend assert which of *its* end
# users a request is for, and exchange that assertion for a portal session
# token. It is how a licensee runs their own customer portal against Trinity
# while keeping their own IdP: they already authenticated bob@example.com and
# want Trinity to act as bob, not as the key owner.
#
# Why a dedicated scope rather than reusing `scope='user'`: this capability
# reads another person's chat history. Riding it on an ordinary user key would
# silently turn EVERY user key into a fleet-wide impersonation key. It is
# admin-issued, and revoking the key stops delegation immediately.
#
# Why a mint rather than a per-request `X-On-Behalf-Of` header: a header puts
# impersonation in the auth path of every current *and future* portal endpoint —
# an ambient capability each new route silently inherits. A mint is one
# auditable event, and everything downstream keeps using the portal session
# token it already understands.
#
# Edition-agnostic, exactly like PORTAL_SESSION_SCOPE above: OSS owns the scope,
# the containment fence, and the mint primitive; the entitled module owns the
# endpoint that decides *whether* this email may be delegated. In an OSS-only
# build the fenced path is not registered, so such a key can reach nothing.
PORTAL_DELEGATE_SCOPE = "portal_delegate"

# The ONLY (method, path) a portal_delegate key may reach. Deliberately a single
# exchange route, not a prefix: the minted portal session — not this key — is
# what drives the portal surface afterwards, so this key never needs breadth.
PORTAL_DELEGATE_ALLOWED_ROUTES = {
    ("POST", "/api/enterprise/client-portal/auth/exchange"),
}


def create_portal_session_token(
    email: str, mode: str = "prod", session_start: Optional[int] = None
) -> str:
    """Mint a Workspace session token for a verified email. Carries no ``sub``
    (no platform identity) — only the email + the portal scope.

    Carries an explicit ``iat`` so bulk revocation (ent#281) can date the token
    against the per-email cutoff. Set here rather than in ``create_access_token``
    to keep the claim set of every other token type unchanged.

    ent#375 — the session SLIDES, so two clocks are needed and ``iat`` can only
    be one of them. ``iat`` moves on every rotation (it is what the revoke cutoff
    dates against, so it MUST move, otherwise a rotated token would look older
    than the revoke that should have killed it). ``sst`` — session start — is
    carried through rotations unchanged and is what the absolute cap measures
    from. With only ``iat``, rotation would reset the cap on every request and
    the session would live forever, which is the one thing an absolute cap
    exists to prevent.

    ``exp`` is the idle deadline, additionally clamped to the absolute cap so a
    token can never advertise a validity its own session no longer has.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    start = int(session_start) if session_start else now
    idle_s, absolute_s = _portal_session_policy()

    idle_deadline = now + idle_s
    absolute_deadline = start + absolute_s
    expires_in = max(1, min(idle_deadline, absolute_deadline) - now)

    return create_access_token(
        data={
            "scope": PORTAL_SESSION_SCOPE,
            "email": email.lower(),
            "iat": now,
            "sst": start,
        },
        expires_delta=timedelta(seconds=expires_in),
        mode=mode,
    )


def decode_portal_session(token: str) -> Optional[str]:
    """Validate a portal session token. Returns the verified email if the token
    is a non-expired, non-revoked portal-scoped token; ``None`` otherwise. Used
    by the entitled portal endpoints to resolve the client identity."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if payload.get("scope") != PORTAL_SESSION_SCOPE:
        return None
    if is_token_revoked(payload.get("jti")):
        return None
    email = payload.get("email")
    if not email:
        return None
    email = email.lower()

    # ent#281 bulk revoke: reject anything issued at or before the cutoff.
    cutoff = portal_sessions_revoked_at(email)
    if cutoff is not None:
        iat = payload.get("iat")
        # A token with no `iat` cannot be dated, so it cannot be shown to
        # post-date the revoke — treat it as revoked. Fail CLOSED here, unlike
        # the Redis read above: the only tokens this affects are ones minted
        # before ent#281 shipped (all expired within
        # PORTAL_SESSION_EXPIRE_HOURS of the upgrade), and only for an email an
        # operator has actively revoked. Letting an undatable token survive an
        # explicit kill switch is the worse failure.
        if iat is None:
            return None
        try:
            if int(iat) <= cutoff:
                return None
        except (TypeError, ValueError):
            return None  # malformed `iat` — same reasoning as missing

    # ent#375 absolute cap. `exp` is clamped to it at mint, so this is redundant
    # for a token minted under the CURRENT policy — and load-bearing for one
    # minted under a wider policy that an operator has since narrowed. Enforcing
    # it on read is what makes shortening the cap take effect immediately
    # instead of only for sessions started afterwards.
    #
    # A token with no `sst` predates ent#375. Fall back to `iat`: those tokens
    # were minted under the flat 12-hour lifetime and have already expired, so
    # the fallback is unreachable in practice and cannot widen anything.
    _, absolute_s = _portal_session_policy()
    started = payload.get("sst", payload.get("iat"))
    if started is not None:
        try:
            if int(started) + absolute_s <= int(datetime.now(timezone.utc).timestamp()):
                return None
        except (TypeError, ValueError):
            return None
    return email


def portal_session_needs_rotation(token: str) -> bool:
    """True when `token` is far enough into its idle window to be worth
    re-minting (ent#375).

    Pure and side-effect-free — a caller can ask without committing to rotate.
    Rotating on every request would revoke a token that concurrent in-flight
    requests are still carrying; rotating only past the threshold keeps that
    window rare.
    """
    from config import PORTAL_SESSION_ROTATE_AFTER_FRACTION

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return False
    if payload.get("scope") != PORTAL_SESSION_SCOPE:
        return False
    iat = payload.get("iat")
    if iat is None:
        return False
    idle_s, _ = _portal_session_policy()
    try:
        age = int(datetime.now(timezone.utc).timestamp()) - int(iat)
    except (TypeError, ValueError):
        return False
    return age >= idle_s * PORTAL_SESSION_ROTATE_AFTER_FRACTION


def renew_portal_session(token: str) -> Optional[str]:
    """Rotate a live Workspace session token, or None if it may not be renewed.

    Renewal is a NEW grant, so it is held to a stricter standard than reading an
    existing one:

    * **Fails CLOSED on the revocation store.** `is_token_revoked` and
      `portal_sessions_revoked_at` both fail OPEN — correct for a read (a Redis
      blip must not sign every client out), indefensible for a mint. Failing open
      here would hand a *fresh* token to a session an operator just killed, and
      the new token post-dates the cutoff, so the kill would not re-apply when
      Redis came back: one blip at the wrong moment permanently resurrects a
      revoked session. The issue calls this out and it is the sharpest edge in
      the feature.
    * **Never extends past the absolute cap.** `sst` is carried through
      unchanged, so the cap measures from the original sign-in however many
      rotations have happened.
    * **Revokes the token it replaces**, after a short grace, so a superseded
      token is not left independently valid for its full remaining lifetime.

    Returns the new token; None means "do not renew" and the caller keeps
    serving the request with the existing one (renewal is opportunistic — a
    failed renewal must never break a request that was otherwise authorised).
    """
    from config import PORTAL_SESSION_ROTATION_GRACE_SECONDS

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if payload.get("scope") != PORTAL_SESSION_SCOPE:
        return None
    email = payload.get("email")
    if not email:
        return None
    email = email.lower()

    # --- fail-CLOSED revocation checks -------------------------------------
    r = get_breaker_redis()
    if r is None:
        return None  # cannot verify -> do not mint
    jti = payload.get("jti")
    try:
        if jti and r.exists(f"{_JWT_REVOKED_PREFIX}{jti}") > 0:
            return None
        raw_cutoff = r.get(_portal_revoked_key(email))
    except Exception as exc:
        logger.warning(f"[auth] renew_portal_session: revocation read failed, refusing: {exc}")
        return None

    iat = payload.get("iat")
    if raw_cutoff is not None:
        try:
            cutoff = int(raw_cutoff.decode() if isinstance(raw_cutoff, (bytes, bytearray)) else raw_cutoff)
        except (ValueError, AttributeError):
            return None  # unreadable cutoff -> assume revoked
        if iat is None:
            return None
        try:
            if int(iat) <= cutoff:
                return None
        except (TypeError, ValueError):
            return None

    # --- absolute cap -------------------------------------------------------
    now = int(datetime.now(timezone.utc).timestamp())
    _, absolute_s = _portal_session_policy()
    started = payload.get("sst", iat)
    if started is None:
        return None
    try:
        started = int(started)
    except (TypeError, ValueError):
        return None
    if started + absolute_s <= now:
        return None  # capped out — a fresh sign-in is required

    new_token = create_portal_session_token(email, session_start=started)

    # Retire the old jti, but only after a grace: requests already in flight are
    # still carrying it, and a hard revoke here would 401 them mid-rotation.
    if jti:
        try:
            r.setex(
                f"{_JWT_REVOKED_PREFIX}{jti}",
                PORTAL_SESSION_ROTATION_GRACE_SECONDS,
                "1",
            )
        except Exception as exc:  # pragma: no cover
            # Best-effort: the superseded token still expires on its own `exp`.
            logger.warning(f"[auth] renew_portal_session: could not retire old jti: {exc}")

    return new_token


def decode_token(token: str) -> Optional[dict]:
    """
    Decode a JWT token without FastAPI dependency.

    Returns the token payload with user info if valid, None if invalid.
    Useful for WebSocket authentication where Depends() doesn't work.

    Returns:
        dict with keys: sub, email, role, exp, mode (if valid)
        None if token is invalid or expired
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            return None

        # #5 — a half-authenticated 2FA challenge token is not a session token.
        if payload.get("scope") == MFA_CHALLENGE_SCOPE:
            return None

        # #78 — a Client Portal session token is not a platform session. It only
        # authorizes the entitled portal endpoints (via decode_portal_session).
        if payload.get("scope") == PORTAL_SESSION_SCOPE:
            return None

        # #187 — a token revoked via logout is no longer valid (also for WS).
        if is_token_revoked(payload.get("jti")):
            return None

        # Get full user record from database
        user = db.get_user_by_username(username)
        if not user:
            return None

        return {
            "sub": username,
            "email": user.get("email"),
            "role": user.get("role"),
            "exp": payload.get("exp"),
            "mode": payload.get("mode")
        }
    except JWTError:
        return None


async def get_current_user(request: Request, token: str = Depends(oauth2_scheme)) -> User:
    """
    FastAPI dependency to get the current authenticated user.

    Validates JWT token OR MCP API key and returns User object.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Try JWT token first
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception

        # #5 — reject a 2FA challenge token used as a session token. It only
        # authorizes /api/enterprise/2fa/login/*; the second factor must be
        # completed there to obtain a real access token.
        if payload.get("scope") == MFA_CHALLENGE_SCOPE:
            raise credentials_exception

        # #78 — a Client Portal session token is fenced OUT of every platform
        # endpoint. It carries no `sub` (so the check below would reject it
        # anyway), but reject explicitly so a portal token can never resolve to
        # a platform principal even if the claim shape changes.
        if payload.get("scope") == PORTAL_SESSION_SCOPE:
            raise credentials_exception

        # #187 — reject a token revoked via logout.
        if is_token_revoked(payload.get("jti")):
            raise credentials_exception

        user = db.get_user_by_username(username)
        if user is None:
            raise credentials_exception

        # #995 — deactivation primitive: reject suspended users here, so
        # setting users.suspended_at invalidates live tokens on the next
        # request (not only new logins). Edition-agnostic; only the
        # enterprise user-management knob sets/clears the column.
        if user.get("suspended_at"):
            raise credentials_exception

        return User(
            id=user["id"],
            username=user["username"],
            email=user.get("email"),
            role=user["role"]
        )
    except JWTError:
        # JWT failed, try MCP API key
        pass

    # Try MCP API key authentication
    mcp_key_info = db.validate_mcp_api_key(token)
    if mcp_key_info:  # validate_mcp_api_key returns dict if valid, None if invalid
        user_email = mcp_key_info.get("user_email")
        user_id = mcp_key_info.get("user_id")  # This is actually username, not DB id

        # Get full user record - try email first, then username
        # Note: user_id from MCP key is the username string, not the database id
        user = db.get_user_by_email(user_email) if user_email else db.get_user_by_username(user_id)
        if user and not user.get("suspended_at"):  # #995 — suspended users blocked here too
            # For agent-scoped keys, include the agent_name
            scope = mcp_key_info.get("scope")
            agent_name = mcp_key_info.get("agent_name") if scope == "agent" else None
            # Connector-scoped keys: consumption-only principal fenced to one
            # agent (see _enforce_connector_scope). The key is minted by an
            # entitled module; core only recognizes + enforces the scope.
            connector_agent = mcp_key_info.get("agent_name") if scope == "connector" else None
            # ent#163: a delegated-portal key is fenced to the single exchange
            # route. Enforced HERE at the one auth entry point — not only in the
            # portal router — for the same reason as the connector fence above:
            # this principal resolves to the key OWNER, so any endpoint doing an
            # inline access check would otherwise treat it as that human. The
            # key's whole job is to mint a portal session; it never needs to
            # reach anything else, including the portal endpoints themselves.
            portal_delegate = scope == PORTAL_DELEGATE_SCOPE
            if portal_delegate and (
                (request.method.upper(), request.url.path) not in PORTAL_DELEGATE_ALLOWED_ROUTES
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Portal delegate keys may only exchange an end-user email "
                        "for a portal session"
                    ),
                )
            if connector_agent:
                # Central containment (ent#46): a connector key may reach ONLY
                # its bound agent's chat + connector playbook list. Enforced here
                # at the single auth entry point — NOT only in the agent path-
                # deps — so the many endpoints that do inline access checks (and
                # resolve this principal to the owner) can't be reached by a
                # leaked connector snippet. The allowlist is the exact set of
                # backend routes the connector MCP tools call.
                allowed = {
                    ("POST", f"/api/agents/{connector_agent}/chat"),
                    ("GET", f"/api/agents/{connector_agent}/connector/playbooks"),
                }
                if (request.method.upper(), request.url.path) not in allowed:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Connector keys may only chat their bound agent and list its playbooks",
                    )
            # trinity-enterprise#69: ephemeral ("ghost") agent containment.
            # An agent-scoped key resolves to the OWNER user on REST — for a
            # ghost running an arbitrary/untrusted workspace that breadth is a
            # fleet skeleton key (prompt-injected ghost reads sibling files,
            # drives schedules, spawns agents). Fence it here at the single
            # auth entry point, keyed off the agent row's is_ephemeral flag
            # (the flag dies with the ghost — no key-schema change, and
            # heartbeat/report/callback auth keeps working since scope stays
            # "agent").
            if agent_name:
                _enforce_ephemeral_key_fence(request, agent_name)
            # #2323 — bounded ops credential. Enforced here for the same reason
            # as the connector and portal_delegate fences above: this principal
            # resolves to the key OWNER, so any endpoint doing an inline access
            # check would otherwise treat it as that human.
            if scope == OPS_SCOPE:
                _enforce_ops_key_fence(request)
            return User(
                id=user["id"],
                username=user["username"],
                email=user.get("email"),
                role=user["role"],
                agent_name=agent_name,
                connector_agent=connector_agent,
                portal_delegate=portal_delegate,
                # #1854: carry the RAW scope, unconditionally. Every field above
                # is set only for its own scope, so a principal outside those
                # three (today: `user`, `system`; tomorrow: whatever a future PR
                # adds to this free-text column) is indistinguishable from a
                # browser session downstream. `reject_non_interactive_principal`
                # allowlists on this being None, so a NULL scope column must
                # NOT read as "interactive" — coerce it the same way
                # `validate_mcp_api_key` does.
                mcp_scope=scope or "user",
                # #2323 — the key identity `validate_mcp_api_key` already
                # returned and this constructor already discarded. Derived from
                # the PRESENTED bearer, so it is the one trustworthy answer to
                # "which credential did this"; the `X-MCP-Key-Id` header that
                # previously fed the audit log is client-supplied and validated
                # nowhere.
                mcp_key_id=mcp_key_info.get("key_id"),
                mcp_key_name=mcp_key_info.get("key_name"),
            )

    # Both JWT and MCP key failed
    raise credentials_exception


# trinity-enterprise#69: the exact backend surface an EPHEMERAL agent's own
# key may reach — liveness, terminal delivery, structured output, and
# self-identity. Everything else (sibling files/chat/schedules/credentials,
# agent creation → chain-spawn) is 403. `{name}` groups must equal the key's
# own agent. Kept deliberately tiny; widen only with a recorded decision.
_EPHEMERAL_ALLOWED_ROUTES = (
    ("POST", re.compile(r"^/api/agents/(?P<name>[^/]+)/heartbeat$")),
    ("POST", re.compile(r"^/api/agents/(?P<name>[^/]+)/executions/[^/]+/result$")),
    ("POST", re.compile(r"^/api/agents/(?P<name>[^/]+)/reports$")),
    ("POST", re.compile(r"^/api/notifications$")),
    ("GET", re.compile(r"^/api/agents/(?P<name>[^/]+)$")),
    ("GET", re.compile(r"^/api/agents/(?P<name>[^/]+)/info$")),
)


def _enforce_ephemeral_key_fence(request: Request, agent_name: str) -> None:
    """Containment fence for ephemeral agents' own keys (trinity-enterprise#69).

    Mirrors the connector fence above: enforced at the single auth entry
    point so inline-access endpoints (which resolve agent keys to the owner)
    can't be reached by a hostile ghost workspace. Fail-open on a DB read
    error — the fence is a hardening layer, and a transient DB failure must
    not take down heartbeats fleet-wide.
    """
    try:
        info = db.get_agent_ephemeral_info(agent_name)
    except Exception:
        return
    if not isinstance(info, dict) or not info.get("is_ephemeral"):
        return
    method = request.method.upper()
    path = request.url.path
    for allowed_method, pattern in _EPHEMERAL_ALLOWED_ROUTES:
        if method != allowed_method:
            continue
        match = pattern.fullmatch(path)
        if match:
            bound_name = match.groupdict().get("name")
            if bound_name is None or bound_name == agent_name:
                return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Ephemeral agent keys are restricted to heartbeat, result delivery, reports, notifications, and self-info",
    )


# --------------------------------------------------------------------------- #
# #2323 — the `ops` scope: a machine credential NARROWER than its owner.
# --------------------------------------------------------------------------- #
# Trinity already had a machine identity for admin/ops surfaces: a `user`-scoped
# MCP key owned by an admin reaches every admin gate, is exempt from interactive
# 2FA (the MFA gate lives on the two login routes; key validation never passes
# through it), is revocable, and rotates by minting a second key. What it is NOT
# is bounded: it carries the owner's full role, so the only way to keep a
# monitoring dashboard alive under enforced 2FA was to hand it a permanent,
# unattributable, unlimited admin credential.
#
# `ops` is the bounded alternative. Two independent layers, both load-bearing:
#
#   1. This route allowlist, enforced HERE at the single auth entry point rather
#      than per-endpoint — the ent#293/#297 lesson (five occurrences meant the
#      GATE was wrong, not the endpoints), and the same placement as the
#      connector, ephemeral and portal_delegate fences.
#   2. `ADMIN_GATE_SCOPES` above, which keeps `ops` OUT of the admin gates by
#      default; an admin-gated ops route opts in explicitly with
#      `assert_admin(..., allow_scopes={"ops"})`.
#
# Layer 2 is what makes this a machine identity rather than a human's proxy:
# authority comes from being an ops key, not from who owns it. It also flips the
# failure direction — a new ops route is INACCESSIBLE until someone opts it in,
# noticed at once by the integration owner, instead of silently reachable and
# noticed by whoever finds it first.
#
# EVERY ENTRY IS A `GET`, and a test asserts it. The method belt is not
# decorative: without it a future `POST /api/ops/<anything>` under an allowlisted
# prefix would be admitted, and `/api/ops` is where `emergency-stop` and
# `fleet/stop` live.
#
# The set is deliberately fail-CLOSED on a route that moves or is added: legit
# traffic 403s and an operator notices. Do NOT "fix" that with a prefix match.
OPS_SCOPE = "ops"

_OPS_ALLOWED_ROUTES = (
    # Platform identity + fleet posture
    ("GET", re.compile(r"^/api/version$")),
    ("GET", re.compile(r"^/api/ops/fleet/status$")),
    ("GET", re.compile(r"^/api/ops/fleet/health$")),
    ("GET", re.compile(r"^/api/ops/schedules$")),
    ("GET", re.compile(r"^/api/ops/alerts$")),
    ("GET", re.compile(r"^/api/ops/costs$")),
    ("GET", re.compile(r"^/api/ops/auth-report$")),
    ("GET", re.compile(r"^/api/monitoring/status$")),
    # Host + container telemetry
    ("GET", re.compile(r"^/api/telemetry/host$")),
    ("GET", re.compile(r"^/api/telemetry/containers$")),
    # Fleet roster + capacity
    ("GET", re.compile(r"^/api/agents$")),
    ("GET", re.compile(r"^/api/agents/execution-stats$")),
    ("GET", re.compile(r"^/api/agents/slots$")),
    # Execution history + live log relay
    ("GET", re.compile(r"^/api/executions$")),
    ("GET", re.compile(r"^/api/executions/stats$")),
    ("GET", re.compile(r"^/api/agents/[^/]+/executions$")),
    ("GET", re.compile(r"^/api/agents/[^/]+/executions/[^/]+/stream$")),
    # Subscription pressure
    ("GET", re.compile(r"^/api/subscriptions$")),
    ("GET", re.compile(r"^/api/subscriptions/[^/]+/usage$")),
)


def _enforce_ops_key_fence(request: Request) -> None:
    """Route containment for `ops`-scoped keys (#2323).

    Reads only the method and path — no DB, no Redis — so unlike the ephemeral
    fence there is nothing here that can fail open. Keep it that way: never make
    membership a settings lookup.
    """
    method = request.method.upper()
    path = request.url.path
    for allowed_method, pattern in _OPS_ALLOWED_ROUTES:
        if method == allowed_method and pattern.fullmatch(path):
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Ops keys are read-only and limited to fleet health, telemetry, execution and subscription reads",
    )


def reject_agent_principal(current_user: User) -> None:
    """Human-only operation guard (trinity-enterprise#69 Part 2).

    Agent-scoped keys resolve to the owner user on REST, which incidentally
    made sharing, permission grants, rename, and credential ops reachable by
    an agent. These are human decisions — a parent agent is a *controller* of
    the agents it spawns, never an owner. No-op for JWT / user-scoped /
    system-scoped principals (``User.agent_name`` is set only for
    scope="agent").
    """
    if current_user.agent_name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This operation is human-only; agent-scoped keys cannot perform it",
        )


def reject_non_interactive_principal(current_user: User) -> None:
    """Interactive-human-only guard — an ALLOWlist, not a denylist (#1854).

    `reject_agent_principal` + `_reject_connector_principal` together cover only
    two of the five live `mcp_api_keys.scope` values. For a `scope='system'` key
    BOTH are no-ops (`agent_name` is set only for scope='agent',
    `connector_agent` only for scope='connector'), and the principal still
    resolves to the key OWNER carrying the owner's role — on a default
    admin-owned install `can_user_share_agent` is then True for every agent in
    the fleet. `scope` is a free-text column with no CHECK constraint, so any
    denylist is open at the top.

    This inverts it: pass ONLY when the caller authenticated interactively (JWT
    ⇒ `mcp_scope is None`). Fail-closed against `user`, `agent`, `system`,
    `connector`, `portal_delegate` and whatever scope ships next.

    Use for credential-lifecycle operations whose whole point is that a human
    decided — never for read/chat surfaces an MCP key legitimately drives.
    """
    if current_user.mcp_scope is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This operation requires an interactive session; "
                "MCP API keys cannot perform it"
            ),
        )


def enforce_agent_spawn_scope(current_user: User, target_agent: str) -> None:
    """Lifecycle-mutation gate for agent-scoped callers
    (trinity-enterprise#69 Part 2) — INTERIM until #948 capability tokens.

    An agent may start/stop/delete ONLY agents it spawned: the target's
    ``spawned_by_agent`` must equal the caller AND ``spawned_by_key_id`` must
    match the caller's current agent key (a name-only match is forgeable via
    name reuse; the key id is stable and cascade-deletes with the parent).
    No-op for human/system principals. #948 workflow-scoped capability tokens
    will subsume this parenthood check; ``spawned_by_*`` stays as provenance.
    """
    if not current_user.agent_name:
        return
    denied = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Agent-scoped keys may only manage agents they spawned",
    )
    try:
        info = db.get_agent_ephemeral_info(target_agent)
    except Exception:
        raise denied
    if not isinstance(info, dict) or info.get("spawned_by_agent") != current_user.agent_name:
        raise denied
    expected_key_id = info.get("spawned_by_key_id")
    if not expected_key_id:
        raise denied
    try:
        parent_key = db.get_agent_mcp_api_key(current_user.agent_name)
    except Exception:
        raise denied
    if not parent_key or parent_key.id != expected_key_id:
        raise denied


def _reject_connector_principal(current_user: User) -> None:
    """Connector-scoped keys are consumption-only — never role-bearing.

    Blocks a leaked connector key from reaching any role-gated endpoint
    (create-agent, admin settings, …) even though it resolves to the owner.
    Edition-agnostic enforcement primitive (the key is minted by an entitled
    module).
    """
    if current_user.connector_agent:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Connector keys are consumption-only and cannot perform this operation",
        )


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """
    Dependency that requires the current user to be an admin.

    Raises:
        HTTPException(403): If user is not an admin, or is an agent/connector
            principal.

    ent#293 — an ADMIN gate is never agent-callable.

    An agent-scoped key resolves to its owner CARRYING THE OWNER'S ROLE, so on a
    default admin-owned install every agent's injected `TRINITY_MCP_API_KEY`
    satisfied this gate. ent#297 traced FIVE occurrences of one class —
    trinity-ops-agent#232, #1644 (retention acknowledge), #1816 (system-agent
    restart), ent#236 and ent#293 (skills-library repointing) — each previously
    closed by bolting `reject_agent_principal` onto one more endpoint, 18 of
    them, against 114 admin-gated call sites. Five occurrences means the GATE
    was wrong, not the endpoints, so the rejection moves here.

    Note this closes the class for THIS gate and `assert_admin`. It does not
    reach `require_role("admin")`, a third spelling that must not exist — see
    `require_role`'s docstring for why it stays permissive and the guard that
    keeps the spelling from coming back.

    Safe by construction, verified rather than assumed: the agent-key flows that
    must keep working — heartbeat, structured reports, the #1083 result callback
    — authorize on `current_user.agent_name` self-checks and never touch an admin
    gate. System-scoped keys are unaffected because `User.agent_name` is set only
    for `scope == "agent"`, so `trinity-system` still passes.

    This is the grant-vs-use line from `learnings.md`: the endpoint that USES a
    capability may be agent-callable; the endpoint that GRANTS one is human-only.
    """
    _reject_connector_principal(current_user)
    reject_agent_principal(current_user)
    _reject_scope_at_admin_gate(current_user)
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user


# ent#293/#297 closed the admin gate against `agent` and `connector` principals by
# NAMING them. That is a denylist over a free-text column: `mcp_api_keys.scope`
# carries no CHECK constraint, and any scope that sets neither `agent_name` nor
# `connector_agent` walks both rejections and inherits the OWNER'S ROLE in full —
# on a default admin-owned install, that is every admin gate in the tree.
# `models.User.mcp_scope`'s own docstring predicted this: "fail-closed against a
# sixth scope a future PR invents."
#
# This is that allowlist. A scope not named here cannot satisfy an admin gate on
# the strength of its owner's role, whether or not anyone remembered it existed.
#
#   None     - the JWT branch: an interactive human. The only principal the role
#              check was ever written for.
#   "user"   - a human's own user-scoped MCP key. It legitimately drives admin
#              endpoints today (ops dashboards, trinity-ops-agent), so removing it
#              would be a behaviour change. Deliberately IN the set; #2323 adds a
#              bounded alternative rather than narrowing this one.
#   "system" - trinity-system. `require_admin`'s docstring already documents that
#              it passes; removing it breaks the orchestrator.
#
# `agent` and `connector` are absent but already rejected one line earlier by the
# named guards, so their 403 is unchanged. `portal_delegate` is absent and IS a
# real narrowing: it passes both named guards today and is contained only by its
# route fence in `get_current_user`. It has no business at an admin gate, its
# fence already forbids every route that has one, so this clause is unreachable in
# normal operation — it fires only if that fence is ever holed. Which is the point.
#
# NULL/empty scope columns are double-coerced to "user" (`db/mcp_keys.py` and
# `get_current_user` below), so no legacy row is stranded outside the set.
#
# Deliberately NOT consulted by `require_role` — see its docstring: agent-spawned
# agent creation through `require_role("creator")` is supported ent#69 behaviour.
ADMIN_GATE_SCOPES = frozenset({None, "user", "system"})

# Sentinel for "this object has no `mcp_scope` at all" — deliberately not `None`,
# which is the JWT-human value and would make an absent attribute the privileged
# one.
_SCOPE_ABSENT = object()


def _reject_scope_at_admin_gate(
    current_user: User, allow_scopes: Optional[frozenset] = None
) -> None:
    """Allowlist half of the admin gate (#2323).

    `allow_scopes` lets an individual endpoint opt a bounded scope in — the
    per-route grant that makes a machine credential authorized because of WHAT IT
    IS rather than who owns it. Absent, only `ADMIN_GATE_SCOPES` passes.
    """
    permitted = ADMIN_GATE_SCOPES if not allow_scopes else (ADMIN_GATE_SCOPES | frozenset(allow_scopes))
    # A principal that does not carry `mcp_scope` at all fails CLOSED. The
    # tempting `getattr(..., None)` would default to the JWT value — the most
    # privileged member of the set — which is the documented trap: when a
    # getattr default reads an authorization discriminator across principal
    # types, the default must be the UNPRIVILEGED answer. `models.User` always
    # declares the field, so the only objects this can reject are stand-ins that
    # do not match the real principal, and a stand-in that does not match the
    # real principal is exactly how #2323 found two live bugs.
    scope = getattr(current_user, "mcp_scope", _SCOPE_ABSENT)
    if scope is _SCOPE_ABSENT:
        # Names the cause, because the alternative is a puzzling 403 on a
        # principal that "looks like" an admin. In production this is
        # unreachable (`models.User` declares the field); in a test it means the
        # stand-in does not match the real principal — which is the defect this
        # allowlist exists to make visible, not an inconvenience to route around.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Principal carries no mcp_scope; cannot satisfy an admin gate",
        )
    if scope not in permitted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This key scope cannot satisfy an admin gate",
        )


# Role hierarchy: admin > creator > operator > user
ROLE_HIERARCHY = ["user", "operator", "creator", "admin"]


def require_role(min_role: str):
    """
    Dependency factory that requires the current user to have at least `min_role`.

    Usage:
        @router.post("/agents")
        async def create(current_user: User = Depends(require_role("creator"))):
            ...

    Raises:
        HTTPException(403): If user's role is below the minimum required

    **Deliberately does NOT call `reject_agent_principal`** — read this before
    "fixing" it to match `require_admin`/`assert_admin` (ent#293/ent#297).

    Those two gates reject agent principals because an admin gate is never
    agent-callable. This one is different: its main consumer is
    `require_role("creator")` on `POST /api/agents`, and agent-spawned agent
    creation is a SUPPORTED feature (ent#69 Part 2 — the spawn persists
    `spawned_by_agent`, auto-grants the parent→child permission edge, and is
    bounded by `enforce_agent_spawn_scope` + the per-parent spawn rate limit).
    Adding a blanket rejection here would break ghost spawning outright.

    The corollary, and the trap ent#297 walked into: `require_role("admin")` was
    a THIRD spelling of an admin gate that this permissiveness left open after
    the other two were closed. Do not write `require_role("admin")` — use
    `require_admin`, which is equivalent (`admin` is last in ROLE_HIERARCHY) and
    carries the agent rejection. `tests/unit/test_293_admin_gate_rejects_agent_keys.py`
    fails the build if a `require_role("admin")` reappears.
    """
    def _require_role(current_user: User = Depends(get_current_user)) -> User:
        _reject_connector_principal(current_user)
        user_level = ROLE_HIERARCHY.index(current_user.role) if current_user.role in ROLE_HIERARCHY else -1
        min_level = ROLE_HIERARCHY.index(min_role) if min_role in ROLE_HIERARCHY else len(ROLE_HIERARCHY)
        if user_level < min_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{min_role}' or above required"
            )
        return current_user
    return _require_role


def requires_entitlement(feature_id: str):
    """Dependency factory: require an entitlement for the named enterprise feature.

    Issue #847 — Phase 0 seam. Consults the ``EntitlementService`` (stub
    today, license-checked in a later phase) to decide whether the
    request is allowed to use a paid feature.

    Usage:
        from dependencies import requires_entitlement

        @router.get("/some-enterprise-endpoint")
        async def handler(_: None = Depends(requires_entitlement("sso"))):
            ...

    The dependency returns nothing on success. On failure raises HTTP
    403 with detail naming the missing entitlement so the UI can surface
    a "license required" message and the operator can correlate with
    `system_settings`.

    The stub implementation in ``services.entitlement_service`` returns
    True for every feature_id in the OSS build — the seam exists so that
    enterprise routers can be wired today without conditionally adding
    a guard later. When a license check lands, all gated endpoints get
    real enforcement with zero diff at the call site.
    """
    def _requires_entitlement():
        # Lazy import: keeps `dependencies.py` importable even when the
        # entitlement module isn't loaded yet (e.g. during partial
        # module init in tests).
        from services.entitlement_service import entitlement_service
        if not entitlement_service.is_entitled(feature_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Enterprise feature '{feature_id}' is not licensed for "
                    "this instance. Contact your administrator."
                ),
            )
        return None
    return _requires_entitlement


# ============================================================================
# Agent Access Control Dependencies
# ============================================================================
# These dependencies validate user access to agents via path parameters.
# Two sets exist to support different path parameter naming conventions:
#   - {name}: Used by schedules, credentials, chat routers
#   - {agent_name}: Used by agents, git, sharing, public_links routers
# ============================================================================


def _enforce_connector_scope(current_user: User, agent_name: str, *, owner_op: bool) -> None:
    """Fence connector-scoped MCP keys (consumption-only, bound to one agent).

    A connector key resolves to the owner user but must NOT be owner-equivalent:
      - owner operations (OwnedAgent* dependencies) are refused outright, and
      - read/chat is allowed ONLY against the key's bound agent.
    No-op for ordinary (non-connector) principals. Edition-agnostic — the key
    is minted by an entitled module; core recognizes + enforces the scope.
    """
    if not current_user.connector_agent:
        return
    if owner_op:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Connector keys are consumption-only and cannot perform owner operations",
        )
    if agent_name != current_user.connector_agent:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Connector key is scoped to a different agent",
        )


def get_authorized_agent(
    name: str = Path(..., description="Agent name from path"),
    current_user: User = Depends(get_current_user)
) -> str:
    """
    Dependency that validates user has access to an agent.
    For routes using {name} path parameter.

    Used for endpoints that require read access to an agent.
    Returns the agent name if authorized.

    Raises:
        HTTPException(403): If a connector key is scoped to a different agent
        HTTPException(404): If the agent does not exist OR the user cannot access
            it — a uniform 404 so a non-existent and an inaccessible agent are
            indistinguishable (enumeration-safe, #186).
    """
    # Connector scope first: fires before any existence lookup so a connector key
    # gets a uniform 403 across all non-bound names, existent or not (#186).
    _enforce_connector_scope(current_user, name, owner_op=False)
    # Evaluate existence AND access before branching so the query count (hence
    # timing) is identical for the non-existent and inaccessible cases (#186).
    exists = db.get_agent_owner(name) is not None
    allowed = db.can_user_access_agent(current_user.username, name)
    if not (exists and allowed):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    return name


def get_owned_agent(
    name: str = Path(..., description="Agent name from path"),
    current_user: User = Depends(get_current_user)
) -> str:
    """
    Dependency that validates user owns or can share an agent.
    For routes using {name} path parameter.

    Used for endpoints that require owner-level access (delete, share, configure).
    Returns the agent name if authorized.

    Raises:
        HTTPException(403): If a connector key attempts an owner operation
        HTTPException(404): If the agent does not exist OR the user is not
            owner/admin — a uniform 404 so a non-existent and an unowned agent
            are indistinguishable (enumeration-safe, #186).
    """
    # Connector keys can never perform owner ops; fires before existence lookup.
    _enforce_connector_scope(current_user, name, owner_op=True)
    # Evaluate existence AND owner-access before branching (equal timing, #186).
    exists = db.get_agent_owner(name) is not None
    allowed = db.can_user_share_agent(current_user.username, name)
    if not (exists and allowed):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    return name


def get_authorized_agent_by_name(
    agent_name: str = Path(..., description="Agent name from path"),
    current_user: User = Depends(get_current_user)
) -> str:
    """
    Dependency that validates user has access to an agent.
    For routes using {agent_name} path parameter.

    Used for endpoints that require read access to an agent.
    Returns the agent name if authorized.

    Raises:
        HTTPException(403): If a connector key is scoped to a different agent
        HTTPException(404): If the agent does not exist OR the user cannot access
            it — a uniform 404 so a non-existent and an inaccessible agent are
            indistinguishable (enumeration-safe, #186).
    """
    _enforce_connector_scope(current_user, agent_name, owner_op=False)
    exists = db.get_agent_owner(agent_name) is not None
    allowed = db.can_user_access_agent(current_user.username, agent_name)
    if not (exists and allowed):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    return agent_name


def get_owned_agent_by_name(
    agent_name: str = Path(..., description="Agent name from path"),
    current_user: User = Depends(get_current_user)
) -> str:
    """
    Dependency that validates user owns or can share an agent.
    For routes using {agent_name} path parameter.

    Used for endpoints that require owner-level access (delete, share, configure).
    Returns the agent name if authorized.

    Raises:
        HTTPException(403): If a connector key attempts an owner operation
        HTTPException(404): If the agent does not exist OR the user is not
            owner/admin — a uniform 404 so a non-existent and an unowned agent
            are indistinguishable (enumeration-safe, #186).
    """
    _enforce_connector_scope(current_user, agent_name, owner_op=True)
    exists = db.get_agent_owner(agent_name) is not None
    allowed = db.can_user_share_agent(current_user.username, agent_name)
    if not (exists and allowed):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    return agent_name


# ============================================================================
# Imperative auth-guard family (INV-8, #1310)
# ============================================================================
# Callable from any router BODY (not a Depends), for the sites where the agent
# name is DERIVED from a resolved resource (a session / notification /
# subscription / execution row) or the gate is COMPOSITE — where a
# path-dependency can't reach. Each raises 403 and is access-first (no existence
# lookup → self-uniform per #186); the agent-name helpers run
# _enforce_connector_scope first, matching the path-dependencies' second fence.
#
# Rule of thumb: agent name IN the path → prefer the path-dependency
# (AuthorizedAgent[ByName] / OwnedAgent[ByName], uniform-404). Agent name
# DERIVED / composite → use an imperative helper here (403, self-uniform).
# ============================================================================


def assert_admin(
    current_user: User,
    *,
    detail: str = "Admin access required",
    allow_scopes: Optional[frozenset] = None,
) -> None:
    """Imperative admin gate — parity with the ``require_admin`` Depends form.

    Rejects connector AND agent principals (neither is a human admin), then
    requires ``role == "admin"``. Raises 403 on failure; returns None on success.
    Use in a router body where the admin check is inline rather than a ``Depends``.

    ent#293 — an ADMIN gate is never agent-callable.

    An agent-scoped key resolves to its owner CARRYING THE OWNER'S ROLE, so on a
    default admin-owned install every agent's injected `TRINITY_MCP_API_KEY`
    satisfied this gate. ent#297 traced FIVE occurrences of one class —
    trinity-ops-agent#232, #1644 (retention acknowledge), #1816 (system-agent
    restart), ent#236 and ent#293 (skills-library repointing) — each previously
    closed by bolting `reject_agent_principal` onto one more endpoint, 18 of
    them, against 114 admin-gated call sites. Five occurrences means the GATE
    was wrong, not the endpoints, so the rejection moves here.

    Note this closes the class for THIS gate and `assert_admin`. It does not
    reach `require_role("admin")`, a third spelling that must not exist — see
    `require_role`'s docstring for why it stays permissive and the guard that
    keeps the spelling from coming back.

    Safe by construction, verified rather than assumed: the agent-key flows that
    must keep working — heartbeat, structured reports, the #1083 result callback
    — authorize on `current_user.agent_name` self-checks and never touch an admin
    gate. System-scoped keys are unaffected because `User.agent_name` is set only
    for `scope == "agent"`, so `trinity-system` still passes.

    This is the grant-vs-use line from `learnings.md`: the endpoint that USES a
    capability may be agent-callable; the endpoint that GRANTS one is human-only.
    """
    _reject_connector_principal(current_user)
    reject_agent_principal(current_user)
    _reject_scope_at_admin_gate(current_user, allow_scopes)
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def assert_agent_access(current_user: User, agent_name: str, *, detail: str = "Access denied") -> None:
    """Imperative agent read-access gate — 403 unless the caller can access
    ``agent_name`` (``db.can_user_access_agent``; admin short-circuits True).

    Access-first → self-uniform (403 before any existence lookup; no
    404-then-403 enumeration oracle, #186). Runs ``_enforce_connector_scope``
    first so the connector boundary is enforced identically to the path-deps.
    """
    _enforce_connector_scope(current_user, agent_name, owner_op=False)
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def assert_agent_owner(current_user: User, agent_name: str, *, detail: str = "Not authorized") -> None:
    """Imperative agent owner gate — 403 unless the caller owns ``agent_name``
    (``db.can_user_share_agent``; owner-or-admin).

    **NOT delete-authorization.** ``can_user_share_agent`` does NOT carry the
    ``is_system`` guard that ``can_user_delete_agent`` (``db/agents.py``) does —
    a delete path must keep using the delete predicate, never this helper. Runs
    the connector owner-op fence first (connectors can never own).
    """
    _enforce_connector_scope(current_user, agent_name, owner_op=True)
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def assert_owns_or_admin(current_user: User, owner_id: int, *, detail: str = "Not authorized") -> None:
    """Imperative strict-self-OR-admin gate for a resource keyed by a user id
    (a session/preview ``user_id``). 403 unless the caller IS the owner OR an
    admin. The ``and`` is load-bearing — an ``or`` would admit everyone.
    """
    if current_user.id != owner_id and current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def assert_owns(
    current_user: User, owner_id: int, *, detail: str = "You don't have access to this session"
) -> None:
    """Imperative strict-self gate — id-only, **NO admin bypass**. 403 unless the
    caller IS the owner.

    Distinct from ``assert_owns_or_admin``: use where an admin must NOT be able
    to read another user's resource (e.g. a public-link chat session — "owners
    cannot see other users' sessions"). Mapping such a site to
    ``assert_owns_or_admin`` would WIDEN access (the #1310 regression guard).
    """
    if current_user.id != owner_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


# Type aliases for cleaner signatures
# For routes using {name} path parameter (schedules, credentials, chat)
AuthorizedAgent = Annotated[str, Depends(get_authorized_agent)]
OwnedAgent = Annotated[str, Depends(get_owned_agent)]

# For routes using {agent_name} path parameter (agents, git, sharing, public_links)
AuthorizedAgentByName = Annotated[str, Depends(get_authorized_agent_by_name)]
OwnedAgentByName = Annotated[str, Depends(get_owned_agent_by_name)]

# Current user type alias
CurrentUser = Annotated[User, Depends(get_current_user)]
