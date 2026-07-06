"""
Authentication routes for the Trinity backend.
"""
import asyncio
import logging
from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt
import redis

from models import Token
from services.platform_audit_service import platform_audit_service, AuditEventType
from config import (
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    EMAIL_AUTH_ENABLED,
    PUBLIC_ACCESS_REQUESTS_ENABLED,
    REDIS_URL,
)
from database import db
from dependencies import (
    authenticate_user,
    create_access_token,
    get_current_user,
    is_token_revoked,
    oauth2_scheme,
    revoke_token_jti,
)
from models import User

logger = logging.getLogger(__name__)

# Strong refs for fire-and-forget email dispatch tasks (#186) so an in-flight
# send isn't garbage-collected before it completes (the asyncio GC footgun).
_email_dispatch_tasks: set = set()

# Login rate limiting — split per-account (tight) + per-IP (loose).
#
# Issue #591 (AISEC-H2): the previous design used a single per-IP bucket
# at 5 fails / 10 min, which doubled as a platform-wide DoS primitive —
# any user behind the same NAT/VPN/CDN got locked out for 10 minutes by
# someone else's bad attempts, and an attacker with a rotating proxy
# could keep an organisation locked out indefinitely. The split buckets
# limit credential-stuffing on a single account without affecting other
# users sharing an egress IP.
LOGIN_ACCOUNT_LIMIT = 5     # fails per account before lockout
LOGIN_ACCOUNT_WINDOW = 900  # 15 minutes
LOGIN_IP_LIMIT = 30         # fails per source IP before lockout (high enough for shared NAT)
LOGIN_IP_WINDOW = 300       # 5 minutes

# Backwards-compat aliases — referenced by tests and other modules.
LOGIN_RATE_LIMIT = LOGIN_IP_LIMIT
LOGIN_RATE_WINDOW = LOGIN_IP_WINDOW

# OTP verification rate limiting (pentest finding 3.1.5)
OTP_MAX_ATTEMPTS = 5   # Max failed OTP attempts before lockout
OTP_RATE_WINDOW = 600  # 10 minutes in seconds

# Redis client for rate limiting
_redis_client = None

def get_redis_client():
    """Get or create Redis client for rate limiting."""
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            _redis_client.ping()
        except Exception as e:
            logger.warning(f"Redis unavailable for rate limiting: {e}")
            return None
    return _redis_client


def _account_key(account: Optional[str]) -> Optional[str]:
    """Normalised per-account Redis key; returns None when no account given."""
    if not account:
        return None
    return f"login_attempts_acct:{account.strip().lower()}"


def _ip_key(client_ip: str) -> str:
    return f"login_attempts_ip:{client_ip}"


def check_login_rate_limit(client_ip: str, account: Optional[str] = None) -> bool:
    """Check IP- and account-scoped login rate limits.

    Returns True if allowed; raises 429 if either bucket is exhausted.

    Two independent buckets (issue #591):
      * per-account (tight) — 5 fails / 15 min: limits credential stuffing
        against one account without affecting other accounts.
      * per-IP (loose)      — 30 fails / 5 min: catches single-source abuse
        but stays well above the legitimate-traffic threshold for users
        sharing a NAT/VPN/CDN egress.

    ``account`` may be omitted for endpoints where no account context exists
    yet (e.g. public access-request); only the per-IP bucket is checked.
    """
    r = get_redis_client()
    if r is None:
        logger.warning("Rate limiting unavailable - Redis not connected")
        return True

    try:
        ip_key = _ip_key(client_ip)
        ip_attempts = r.get(ip_key)
        if ip_attempts is not None and int(ip_attempts) >= LOGIN_IP_LIMIT:
            ttl = r.ttl(ip_key)
            logger.warning(
                "[Auth] Per-IP login lockout triggered: ip=%s attempts=%s ttl=%ss",
                client_ip, ip_attempts, ttl,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many login attempts from this network. Try again in {ttl} seconds.",
            )

        acct_key = _account_key(account)
        if acct_key is not None:
            acct_attempts = r.get(acct_key)
            if acct_attempts is not None and int(acct_attempts) >= LOGIN_ACCOUNT_LIMIT:
                ttl = r.ttl(acct_key)
                logger.warning(
                    "[Auth] Per-account login lockout triggered: account=%s ip=%s attempts=%s ttl=%ss",
                    account, client_ip, acct_attempts, ttl,
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Too many failed attempts for this account. Try again in {ttl} seconds.",
                )
        return True
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Rate limit check failed: {e}")
        return True


def record_login_attempt(client_ip: str, success: bool, account: Optional[str] = None):
    """Update per-IP and (optionally) per-account login attempt counters.

    Failed attempts increment both buckets; successful login clears both.
    """
    r = get_redis_client()
    if r is None:
        return

    ip_key = _ip_key(client_ip)
    acct_key = _account_key(account)

    try:
        if success:
            r.delete(ip_key)
            if acct_key:
                r.delete(acct_key)
            return

        pipe = r.pipeline()
        pipe.incr(ip_key)
        pipe.expire(ip_key, LOGIN_IP_WINDOW)
        if acct_key:
            pipe.incr(acct_key)
            pipe.expire(acct_key, LOGIN_ACCOUNT_WINDOW)
        pipe.execute()
    except Exception as e:
        logger.warning(f"Failed to record login attempt: {e}")


def check_otp_rate_limit(email: str) -> bool:
    """
    Check if OTP verification attempts for this email are within rate limit.
    Returns True if allowed, raises HTTPException if rate limited.

    Security fix (pentest 3.1.5): Prevents brute-force of 6-digit OTP codes.
    After OTP_MAX_ATTEMPTS failures the current code is effectively invalidated.
    """
    r = get_redis_client()
    if r is None:
        logger.warning("Rate limiting unavailable - Redis not connected")
        return True

    key = f"otp_attempts:{email}"
    try:
        attempts = r.get(key)
        if attempts and int(attempts) >= OTP_MAX_ATTEMPTS:
            ttl = r.ttl(key)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many verification attempts. Request a new code or try again in {ttl} seconds."
            )
        return True
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"OTP rate limit check failed: {e}")
        return True


def record_otp_attempt(email: str, success: bool):
    """
    Record an OTP verification attempt for an email.
    Failed attempts increment counter; success clears it.
    """
    r = get_redis_client()
    if r is None:
        return

    key = f"otp_attempts:{email}"
    try:
        if success:
            r.delete(key)
        else:
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, OTP_RATE_WINDOW)
            pipe.execute()
    except Exception as e:
        logger.warning(f"Failed to record OTP attempt: {e}")


def is_setup_completed() -> bool:
    """Check if initial setup is completed."""
    return db.get_setting_value('setup_completed', 'false') == 'true'

router = APIRouter()


@router.get("/api/auth/mode")
async def get_auth_mode():
    """
    Get authentication mode configuration.

    This endpoint requires NO authentication - it's called before login
    to determine which login options to show.

    Returns:
        - email_auth_enabled: Whether email-based login is enabled
        - setup_completed: Whether first-time setup is complete
    """
    # Check if email auth is enabled (can be overridden via settings)
    email_auth_setting = db.get_setting_value("email_auth_enabled", str(EMAIL_AUTH_ENABLED).lower())
    email_auth_enabled = email_auth_setting.lower() == "true"

    return {
        "email_auth_enabled": email_auth_enabled,
        "setup_completed": is_setup_completed()
    }


@router.post("/token", response_model=Token)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    """Login with username/password and get JWT token.

    Used for admin login (username 'admin' with password).
    Regular users should use email authentication.

    Rate limited (issue #591): per-account (5 fails / 15 min) plus
    per-IP (30 fails / 5 min). The two-bucket design prevents one user's
    bad attempts from locking out other users sharing the same egress IP.
    """
    # Get client IP for rate limiting
    client_ip = request.client.host if request.client else "unknown"
    account = (form_data.username or "").strip().lower()

    # Check rate limit before processing (#591)
    check_login_rate_limit(client_ip, account=account)

    # Block login if setup is not completed
    if not is_setup_completed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="setup_required"
        )

    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        # Record failed attempt
        record_login_attempt(client_ip, success=False, account=account)
        # SEC-001: audit failed admin login
        await platform_audit_service.log(
            event_type=AuditEventType.AUTHENTICATION,
            event_action="login_failed",
            source="api",
            actor_ip=client_ip,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"method": "admin", "username": form_data.username},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Record successful login (clears attempt counter)
    record_login_attempt(client_ip, success=True, account=account)

    # Update last login timestamp
    db.update_last_login(user["username"])

    # #5 — enterprise 2FA gate. Password is the first factor; if a second
    # factor is required (user enrolled OR policy mandates it for the role)
    # return a challenge instead of an access token. OSS-only builds have no
    # provider registered → returns None → unchanged behaviour.
    from services import mfa_gate
    challenge = mfa_gate.gate_login(user, mode="admin")
    if challenge:
        await platform_audit_service.log(
            event_type=AuditEventType.AUTHENTICATION,
            event_action="mfa_challenge_issued",
            source="api",
            actor_ip=client_ip,
            target_type="user",
            target_id=user["username"],
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"method": "admin"},
        )
        return challenge

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]},
        expires_delta=access_token_expires,
        mode="admin"  # Mark as admin login token
    )

    # SEC-001: audit successful admin login
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHENTICATION,
        event_action="login_success",
        source="api",
        actor_ip=client_ip,
        target_type="user",
        target_id=user["username"],
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"method": "admin"},
    )

    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/api/token", response_model=Token)
async def login_api(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    """Alias for /token endpoint."""
    return await login(request, form_data)


@router.get("/api/auth/validate")
async def validate_token(request: Request):
    """
    Validate JWT token for nginx auth_request.
    Returns 200 if valid, 401 if invalid.

    Accepts token via:
    - Authorization header: Bearer <token>
    - Cookie: token=<token>
    - Query param: ?token=<token>
    """
    token = None

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]

    if not token:
        token = request.cookies.get("token")

    if not token:
        token = request.query_params.get("token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No token provided",
            headers={"WWW-Authenticate": "Bearer"}
        )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        user = db.get_user_by_username(username) if username else None
        if username is None or user is None or is_token_revoked(payload.get("jti")):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"}
            )
        return {"status": "valid", "user": username}
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"}
        )


@router.post("/api/auth/logout")
async def logout(
    current_user: User = Depends(get_current_user),
    token: str = Depends(oauth2_scheme),
):
    """Revoke the caller's JWT so it can no longer be used (#187).

    `get_current_user` guarantees the bearer is a valid, non-revoked session
    token (an MCP API key authenticates too, but carries no `jti` — logging out
    a key is a no-op here; keys are revoked via key management). We then stamp
    its `jti` into the Redis blacklist until the token's own expiry, so an
    exfiltrated 7-day token stops working immediately on logout. Idempotent.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        # Not a JWT (e.g. an MCP API key) — nothing to revoke by jti.
        return {"status": "logged_out"}
    revoke_token_jti(payload.get("jti"), payload.get("exp"))
    return {"status": "logged_out"}


# =========================================================================
# Email-Based Authentication Endpoints (Phase 12.4)
# =========================================================================

@router.post("/api/auth/email/request")
async def request_email_login_code(request: Request):
    """
    Request a login code via email.

    Unauthenticated endpoint. Sends a 6-digit code to the provided email
    if it's in the whitelist.

    Rate limit: 3 requests per 10 minutes per email.
    """
    from database import EmailLoginRequest
    from services.email_service import EmailService

    # Block if setup is not completed
    if not is_setup_completed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="setup_required"
        )

    # Check if email auth is enabled
    email_auth_setting = db.get_setting_value("email_auth_enabled", str(EMAIL_AUTH_ENABLED).lower())
    if email_auth_setting.lower() != "true":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email authentication is disabled"
        )

    # Parse request
    body = await request.json()
    login_request = EmailLoginRequest(**body)
    email = login_request.email.lower()

    # #186: every branch below returns a byte-identical body + status, and the
    # email send is dispatched fire-and-forget, so a caller cannot distinguish a
    # whitelisted (registered) email from a non-whitelisted one by body, status,
    # or response latency. Do NOT reintroduce a distinct message, extra fields
    # (e.g. expires_in_seconds), a 429, or a blocking send — each re-opens the
    # enumeration oracle (pentest 3.3.3).
    generic_response = {
        "success": True,
        "message": "If your email is registered, you'll receive a code shortly",
    }

    # Non-whitelisted: return the generic body without revealing membership.
    if not db.is_email_whitelisted(email):
        return generic_response

    # Rate limit: over-limit returns the SAME generic body (not a 429) so a
    # repeat-request status differential can't be used as a membership oracle.
    # WARN server-side (fail-loud) so ops can still distinguish a rate-limit
    # suppression from a mail outage.
    recent_requests = db.count_recent_code_requests(email, minutes=10)
    if recent_requests >= 3:
        logger.warning("email-code suppressed: rate limit for %s", email)
        return generic_response

    # Generate code
    code_data = db.create_login_code(email, expiry_minutes=10)

    # Dispatch the email fire-and-forget so the whitelisted path's latency matches
    # the immediate-return non-whitelisted / rate-limited paths (#186 timing oracle).
    async def _dispatch_code(target_email: str, code: str) -> None:
        try:
            email_service = EmailService()
            await email_service.send_verification_code(
                target_email, code, context_label="Trinity login"
            )
        except Exception:
            logger.exception("Failed to send email login code")

    task = asyncio.create_task(_dispatch_code(email, code_data["code"]))
    _email_dispatch_tasks.add(task)
    task.add_done_callback(_email_dispatch_tasks.discard)

    return generic_response


@router.post("/api/auth/email/verify")
async def verify_email_login_code(request: Request):
    """
    Verify email login code and get JWT token.

    Unauthenticated endpoint. Verifies the code and creates/returns user session.
    """
    from database import EmailLoginVerify, EmailLoginResponse

    # Block if setup is not completed
    if not is_setup_completed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="setup_required"
        )

    # Check if email auth is enabled
    email_auth_setting = db.get_setting_value("email_auth_enabled", str(EMAIL_AUTH_ENABLED).lower())
    if email_auth_setting.lower() != "true":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email authentication is disabled"
        )

    # Get client IP for rate limiting
    client_ip = request.client.host if request.client else "unknown"

    # Parse request first so we can scope rate-limit checks per-account (#591)
    body = await request.json()
    verify_request = EmailLoginVerify(**body)
    email = verify_request.email.lower()
    code = verify_request.code

    # Check per-account + per-IP login rate limit (#591)
    check_login_rate_limit(client_ip, account=email)

    # Check per-email OTP attempt rate limit (pentest 3.1.5)
    check_otp_rate_limit(email)

    # Verify code
    verification = db.verify_login_code(email, code)
    if not verification:
        record_login_attempt(client_ip, success=False, account=email)
        record_otp_attempt(email, success=False)
        # SEC-001: audit failed email login
        await platform_audit_service.log(
            event_type=AuditEventType.AUTHENTICATION,
            event_action="login_failed",
            source="api",
            actor_ip=client_ip,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"method": "email", "email": email},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired verification code"
        )

    # Clear rate limit counters on successful verification
    record_login_attempt(client_ip, success=True, account=email)
    record_otp_attempt(email, success=True)

    # Get or create user
    user = db.get_or_create_email_user(email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user account"
        )

    # Update last login
    db.update_last_login(user["username"])

    # #5 — enterprise 2FA gate. The verified email code is the first factor;
    # if a second factor is required, return a challenge instead of a token.
    # OSS-only builds have no provider → returns None → unchanged behaviour.
    from services import mfa_gate
    challenge = mfa_gate.gate_login(user, mode="email")
    if challenge:
        await platform_audit_service.log(
            event_type=AuditEventType.AUTHENTICATION,
            event_action="mfa_challenge_issued",
            source="api",
            actor_ip=client_ip,
            target_type="user",
            target_id=user["username"],
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"method": "email", "email": email},
        )
        return challenge

    # Create JWT token
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]},
        expires_delta=access_token_expires,
        mode="email"  # Mark as email auth token
    )

    # SEC-001: audit successful email login
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHENTICATION,
        event_action="login_success",
        source="api",
        actor_ip=client_ip,
        target_type="user",
        target_id=user["username"],
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"method": "email", "email": email},
    )

    return EmailLoginResponse(
        access_token=access_token,
        token_type="bearer",
        user={
            "username": user["username"],
            "email": user["email"],
            "role": user["role"],
            "name": user.get("name"),
            "picture": user.get("picture")
        }
    )


# =========================================================================
# Public Access Request (CLI onboarding)
# =========================================================================

@router.post("/api/access/request")
async def request_access(request: Request):
    """
    Public self-signup for this Trinity instance (CLI onboarding).

    Unauthenticated. **Disabled by default** (trinity-enterprise#10): when the
    `public_access_requests_enabled` setting / `PUBLIC_ACCESS_REQUESTS_ENABLED`
    env is not explicitly enabled, this returns 403 and does NOT whitelist the
    email — the email whitelist stays authoritative against self-enrollment.
    When an operator opts in, the submitted email is auto-added to the login
    whitelist (role `user`) for frictionless onboarding. Idempotent.

    This does not affect login-code requests for already-whitelisted emails
    (POST /api/auth/email/request), which remain available regardless of the flag.

    Rate limit: 5 requests per 10 minutes per IP.
    """
    # Block if setup is not completed
    if not is_setup_completed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="setup_required"
        )

    # Secure default (trinity-enterprise#10): public self-signup is OFF unless the
    # operator explicitly enables it. Env default via PUBLIC_ACCESS_REQUESTS_ENABLED;
    # overridable at runtime via the system_settings key. When off, do NOT
    # auto-whitelist — return 403 so the whitelist remains the real access gate.
    self_signup_setting = db.get_setting_value(
        "public_access_requests_enabled", str(PUBLIC_ACCESS_REQUESTS_ENABLED).lower()
    )
    if self_signup_setting.lower() != "true":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public access requests are disabled on this instance. "
                   "Ask an administrator to add your email to the whitelist."
        )

    # Check if email auth is enabled (access request only makes sense with email auth)
    email_auth_setting = db.get_setting_value("email_auth_enabled", str(EMAIL_AUTH_ENABLED).lower())
    if email_auth_setting.lower() != "true":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email authentication is disabled"
        )

    # Rate limit by IP
    client_ip = request.client.host if request.client else "unknown"
    check_login_rate_limit(client_ip)

    # Parse request
    body = await request.json()
    email = (body.get("email") or "").lower().strip()
    if not email or "@" not in email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Valid email address required"
        )

    # Auto-approve: add to whitelist if not already present
    if db.is_email_whitelisted(email):
        record_login_attempt(client_ip, success=True)
        return {"success": True, "message": "Email already on the access whitelist", "already_registered": True}

    try:
        # Public self-signup — default to `user`. Owners who want a collaborator
        # to create agents can promote them via `PUT /api/users/{username}/role`.
        # Granting `creator` here would recreate the bug fixed in #314.
        db.add_to_whitelist(email, added_by="admin", source="cli", default_role="user")
    except Exception as e:
        logger.error(f"Failed to add {email} to whitelist: {e}")
        record_login_attempt(client_ip, success=False)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to grant access"
        )

    record_login_attempt(client_ip, success=True)
    logger.info(f"CLI self-signup (operator-enabled): {email} added to access whitelist")
    return {"success": True, "message": "Email added to the access whitelist", "already_registered": False}
