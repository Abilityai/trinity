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
        "mode": mode  # Track auth mode to prevent dev/prod token mixing
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
            return User(
                id=user["id"],
                username=user["username"],
                email=user.get("email"),
                role=user["role"],
                agent_name=agent_name,
                connector_agent=connector_agent,
            )

    # Both JWT and MCP key failed
    raise credentials_exception


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
        HTTPException(403): If user is not an admin
    """
    _reject_connector_principal(current_user)
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
        HTTPException(404): If agent does not exist
        HTTPException(403): If user cannot access the agent
    """
    # First check if agent exists
    if not db.get_agent_owner(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    _enforce_connector_scope(current_user, name, owner_op=False)
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
    _enforce_connector_scope(current_user, name, owner_op=True)
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
    _enforce_connector_scope(current_user, agent_name, owner_op=False)
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
    _enforce_connector_scope(current_user, agent_name, owner_op=True)
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
