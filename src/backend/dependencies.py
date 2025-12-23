"""
FastAPI dependencies for the Trinity backend.
"""
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from models import User
from config import SECRET_KEY, ALGORITHM
from database import db
from services.audit_service import log_audit_event


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, stored_password: str) -> bool:
    """Verify password against stored hash.

    For backward compatibility, also checks plaintext passwords.
    """
    # First try bcrypt verification
    try:
        if pwd_context.verify(plain_password, stored_password):
            return True
    except Exception:
        pass

    # Fall back to plaintext comparison for legacy passwords
    return plain_password == stored_password


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

        await log_audit_event(
            event_type="authentication",
            action="token_validation",
            user_id=username,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            result="success"
        )

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
        if user:
            await log_audit_event(
                event_type="authentication",
                action="mcp_key_validation",
                user_id=user.get("username"),
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                result="success",
                details=f"MCP API key: {mcp_key_info.get('key_name')}"
            )

            return User(
                id=user["id"],
                username=user["username"],
                email=user.get("email"),
                role=user["role"]
            )

    # Both JWT and MCP key failed
    raise credentials_exception
