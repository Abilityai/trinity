"""
FastAPI dependencies for the Trinity backend.
"""
from datetime import datetime, timedelta
from typing import Optional, Annotated
from fastapi import Depends, HTTPException, status, Request, Path
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from models import User
from config import SECRET_KEY, ALGORITHM
from database import db


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
    """Authenticate a user by username and password."""
    user = db.get_user_by_username(username)
    if not user:
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
        "mode": mode  # Track auth mode to prevent dev/prod token mixing
    })
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


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
            agent_name = mcp_key_info.get("agent_name") if mcp_key_info.get("scope") == "agent" else None
            return User(
                id=user["id"],
                username=user["username"],
                email=user.get("email"),
                role=user["role"],
                agent_name=agent_name
            )

    # Both JWT and MCP key failed
    raise credentials_exception


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """
    Dependency that requires the current user to be an admin.

    Raises:
        HTTPException(403): If user is not an admin
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user


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
    """
    def _require_role(current_user: User = Depends(get_current_user)) -> User:
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
        HTTPException(404): If agent does not exist
        HTTPException(403): If user cannot access the agent
    """
    # First check if agent exists
    if not db.get_agent_owner(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    # Then check if user has access
    if not db.can_user_access_agent(current_user.username, name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to agent"
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
        HTTPException(404): If agent does not exist
        HTTPException(403): If user is not owner/admin
    """
    # First check if agent exists
    if not db.get_agent_owner(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    # Then check if user has owner access
    if not db.can_user_share_agent(current_user.username, name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner access required"
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
        HTTPException(404): If agent does not exist
        HTTPException(403): If user cannot access the agent
    """
    # First check if agent exists
    if not db.get_agent_owner(agent_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    # Then check if user has access
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to agent"
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
        HTTPException(404): If agent does not exist
        HTTPException(403): If user is not owner/admin
    """
    # First check if agent exists
    if not db.get_agent_owner(agent_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    # Then check if user has owner access
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner access required"
        )
    return agent_name


# Type aliases for cleaner signatures
# For routes using {name} path parameter (schedules, credentials, chat)
AuthorizedAgent = Annotated[str, Depends(get_authorized_agent)]
OwnedAgent = Annotated[str, Depends(get_owned_agent)]

# For routes using {agent_name} path parameter (agents, git, sharing, public_links)
AuthorizedAgentByName = Annotated[str, Depends(get_authorized_agent_by_name)]
OwnedAgentByName = Annotated[str, Depends(get_owned_agent_by_name)]

# Current user type alias
CurrentUser = Annotated[User, Depends(get_current_user)]
