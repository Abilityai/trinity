"""
Authentication routes for the Trinity backend.
"""
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt
import httpx

from models import Token, Auth0TokenExchange
from config import (
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    AUTH0_DOMAIN,
    AUTH0_ALLOWED_DOMAIN,
    DEV_MODE_ENABLED,
)
from database import db
from dependencies import authenticate_user, create_access_token
from services.audit_service import log_audit_event


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
        - dev_mode_enabled: Whether local username/password login is allowed
        - auth0_configured: Whether Auth0 OAuth is available
        - allowed_domain: The email domain restriction (for display)
        - setup_completed: Whether first-time setup is complete
    """
    return {
        "dev_mode_enabled": DEV_MODE_ENABLED,
        "auth0_configured": bool(AUTH0_DOMAIN),
        "allowed_domain": AUTH0_ALLOWED_DOMAIN,
        "setup_completed": is_setup_completed()
    }


@router.post("/token", response_model=Token)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    """Login with username/password and get JWT token.

    This endpoint is only available when DEV_MODE_ENABLED=true.
    In production, use Auth0 OAuth via /api/auth/exchange.
    """
    # Block login if setup is not completed
    if not is_setup_completed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="setup_required"
        )

    # Gate this endpoint in production
    if not DEV_MODE_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Local login is disabled. Use 'Sign in with Google' instead."
        )

    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        await log_audit_event(
            event_type="authentication",
            action="login",
            user_id=form_data.username,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            result="failed",
            severity="warning"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]},
        expires_delta=access_token_expires,
        mode="dev"  # Mark as dev mode token
    )

    await log_audit_event(
        event_type="authentication",
        action="dev_login",  # Distinguish from Auth0 login in audit logs
        user_id=user["username"],
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        result="success"
    )

    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/api/token", response_model=Token)
async def login_api(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    """Alias for /token endpoint."""
    return await login(request, form_data)


@router.post("/api/auth/exchange", response_model=Token)
async def exchange_auth0_token(request: Request, token_data: Auth0TokenExchange):
    """
    Exchange an Auth0 access token for a backend JWT.

    Validates the Auth0 token by calling Auth0's /userinfo endpoint,
    checks domain restrictions, and returns a backend-signed JWT.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://{AUTH0_DOMAIN}/userinfo",
                headers={"Authorization": f"Bearer {token_data.auth0_token}"},
                timeout=10.0
            )

            if response.status_code != 200:
                await log_audit_event(
                    event_type="authentication",
                    action="auth0_exchange",
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                    result="failed",
                    severity="warning",
                    details={"error": "Invalid Auth0 token", "status": response.status_code}
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid Auth0 token",
                    headers={"WWW-Authenticate": "Bearer"}
                )

            user_info = response.json()

        email = user_info.get("email")
        if not email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No email found in Auth0 profile"
            )

        email_domain = email.split("@")[1] if "@" in email else ""
        if email_domain != AUTH0_ALLOWED_DOMAIN:
            await log_audit_event(
                event_type="authentication",
                action="auth0_exchange",
                user_id=email,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                result="denied",
                severity="warning",
                details={"error": f"Domain {email_domain} not allowed"}
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access restricted to @{AUTH0_ALLOWED_DOMAIN} domain users only"
            )

        if not user_info.get("email_verified", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Email not verified"
            )

        user = db.get_or_create_auth0_user(
            auth0_sub=user_info.get("sub"),
            email=email,
            name=user_info.get("name"),
            picture=user_info.get("picture")
        )
        username = user["username"]
        db.update_last_login(username)

        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": username},
            expires_delta=access_token_expires,
            mode="prod"  # Mark as production (Auth0) token
        )

        await log_audit_event(
            event_type="authentication",
            action="auth0_exchange",
            user_id=username,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            result="success",
            details={"name": user_info.get("name")}
        )

        return {"access_token": access_token, "token_type": "bearer"}

    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Auth0 token exchange failed: {e}")
        await log_audit_event(
            event_type="authentication",
            action="auth0_exchange",
            ip_address=request.client.host if request.client else None,
            result="error",
            severity="error",
            details={"error_type": type(e).__name__}  # Log type only, not message
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication failed. Please try again."
        )


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
        if username is None or user is None:
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
