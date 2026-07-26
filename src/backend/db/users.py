"""
User management database operations.

Handles user CRUD, authentication, and profile management.

Pilot module for the configurable database backend (#300 Phase 2): converted
from raw sqlite3 to SQLAlchemy Core so it runs unchanged on both SQLite and
PostgreSQL. Queries are built from the ``users`` table in ``db/tables.py``
(dialect-agnostic expressions, no ``?``/``%s`` placeholders), and the engine is
resolved from ``DATABASE_URL`` via ``db/engine.py``. The public API of
``UserOperations`` is unchanged — callers (and the ``DatabaseManager`` facade)
are unaffected.
"""

from typing import Optional, Dict, List, Any

from sqlalchemy import insert, select, update

from .engine import get_engine
from .tables import users
from db_models import UserCreate
from utils.helpers import utc_now_iso


class UserOperations:
    """User database operations."""

    # Columns returned for a full user record (includes password_hash).
    _USER_COLUMNS = (
        users.c.id,
        users.c.username,
        users.c.password_hash,
        users.c.role,
        users.c.auth0_sub,
        users.c.name,
        users.c.picture,
        users.c.email,
        users.c.created_at,
        users.c.updated_at,
        users.c.last_login,
        users.c.suspended_at,
    )

    @staticmethod
    def _row_to_user_dict(row) -> Dict:
        """Convert a user row (RowMapping) to a dictionary."""
        return {
            "id": row["id"],
            "username": row["username"],
            "password": row["password_hash"],  # Keep as "password" for backward compat
            "role": row["role"],
            "auth0_sub": row["auth0_sub"],
            "name": row["name"],
            "picture": row["picture"],
            "email": row["email"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login": row["last_login"],
            "suspended_at": row["suspended_at"],  # #995 — NULL = active
        }

    def _get_user_by_field(self, field: str, value: Any) -> Optional[Dict]:
        """Generic user lookup by any column."""
        column = users.c[field]
        stmt = select(*self._USER_COLUMNS).where(column == value)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_user_dict(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[Dict]:
        """Get user by username."""
        return self._get_user_by_field("username", username)

    def get_user_by_auth0_sub(self, auth0_sub: str) -> Optional[Dict]:
        """Get user by Auth0 subject ID."""
        return self._get_user_by_field("auth0_sub", auth0_sub)

    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        """Get user by ID."""
        return self._get_user_by_field("id", user_id)

    def get_user_by_email(self, email: str) -> Optional[Dict]:
        """Get user by email address."""
        return self._get_user_by_field("email", email)

    def create_user(self, user_data: UserCreate) -> Dict:
        """Create a new user."""
        now = utc_now_iso()
        email = user_data.email or user_data.username  # Use username as email if not provided

        stmt = insert(users).values(
            username=user_data.username,
            password_hash=user_data.password,
            role=user_data.role,
            auth0_sub=user_data.auth0_sub,
            name=user_data.name,
            picture=user_data.picture,
            email=email,
            created_at=now,
            updated_at=now,
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            user_id = result.inserted_primary_key[0]

        return {
            "id": user_id,
            "username": user_data.username,
            "password": user_data.password,
            "role": user_data.role,
            "auth0_sub": user_data.auth0_sub,
            "name": user_data.name,
            "picture": user_data.picture,
            "email": email,
            "created_at": now,
            "updated_at": now,
            "last_login": None,
        }

    def update_user(self, username: str, updates: Dict) -> Optional[Dict]:
        """Update user fields."""
        values = {
            key: value
            for key, value in updates.items()
            if key in ("name", "picture", "role", "email")
        }
        if not values:
            return self.get_user_by_username(username)

        values["updated_at"] = utc_now_iso()
        stmt = update(users).where(users.c.username == username).values(**values)
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return self.get_user_by_username(username)

    def update_user_password(self, username: str, hashed_password: str) -> bool:
        """Update user's password hash, creating the user if it doesn't exist.

        For the admin user during first-time setup, this will create the user
        if it doesn't exist yet.

        Args:
            username: The username to update
            hashed_password: The bcrypt-hashed password

        Returns:
            True if the user was updated or created successfully
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            # Try to update existing user
            result = conn.execute(
                update(users)
                .where(users.c.username == username)
                .values(password_hash=hashed_password, updated_at=now)
            )
            if result.rowcount > 0:
                return True

            # User doesn't exist - create it (for admin user during first-time setup)
            result = conn.execute(
                insert(users).values(
                    username=username,
                    password_hash=hashed_password,
                    role="admin",
                    email=username,
                    created_at=now,
                    updated_at=now,
                )
            )
            return result.rowcount > 0

    def update_last_login(self, username: str):
        """Update user's last login timestamp."""
        now = utc_now_iso()
        stmt = (
            update(users)
            .where(users.c.username == username)
            .values(last_login=now, updated_at=now)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def get_or_create_auth0_user(self, auth0_sub: str, email: str, name: str = None, picture: str = None) -> Dict:
        """Get or create a user from Auth0 authentication."""
        # First try to find by auth0_sub
        user = self.get_user_by_auth0_sub(auth0_sub)
        if user:
            # Update profile info if changed
            updates = {}
            if name and name != user.get("name"):
                updates["name"] = name
            if picture and picture != user.get("picture"):
                updates["picture"] = picture
            if updates:
                self.update_user(user["username"], updates)
                user = self.get_user_by_username(user["username"])
            return user

        # Try to find by email (username)
        user = self.get_user_by_username(email)
        if user:
            # Link auth0_sub to existing user
            stmt = (
                update(users)
                .where(users.c.username == email)
                .values(
                    auth0_sub=auth0_sub,
                    name=name,
                    picture=picture,
                    updated_at=utc_now_iso(),
                )
            )
            with get_engine().begin() as conn:
                conn.execute(stmt)
            return self.get_user_by_username(email)

        # Create new user
        user_data = UserCreate(
            username=email,
            password=None,  # Auth0 users don't have local passwords
            role="user",
            auth0_sub=auth0_sub,
            name=name,
            picture=picture,
            email=email
        )
        return self.create_user(user_data)

    def list_users(self) -> List[Dict]:
        """List all users (admin only)."""
        stmt = select(
            users.c.id,
            users.c.username,
            users.c.role,
            users.c.auth0_sub,
            users.c.name,
            users.c.picture,
            users.c.email,
            users.c.created_at,
            users.c.updated_at,
            users.c.last_login,
            users.c.suspended_at,
        ).order_by(users.c.created_at.desc())
        with get_engine().connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def update_user_role(self, username: str, role: str) -> Optional[Dict]:
        """Update a user's role. Returns updated user or None if not found."""
        valid_roles = {"admin", "creator", "operator", "user"}
        if role not in valid_roles:
            raise ValueError(f"Invalid role '{role}'. Must be one of: {', '.join(sorted(valid_roles))}")
        stmt = (
            update(users)
            .where(users.c.username == username)
            .values(role=role, updated_at=utc_now_iso())
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            if result.rowcount == 0:
                return None
        return self.get_user_by_username(username)

    # =========================================================================
    # Per-user GitHub PAT (ent#162)
    #
    # A user configures one GitHub token in their own settings; the agent-create
    # resolver (services/settings_service.resolve_github_pat) reads it live by
    # owner_id, so a non-admin is not confined to the admin PAT's repo scope.
    # Stored AES-256-GCM (Invariant #12), same envelope as the per-agent PAT.
    # Deliberately NOT part of _USER_COLUMNS / _row_to_user_dict — the token must
    # never ride the general user dict that flows to /api/users/me and friends;
    # only these dedicated accessors touch the column.
    # =========================================================================

    def _get_encryption_service(self):
        """Lazy-load encryption service (same pattern as GitPATMixin)."""
        from services.credential_encryption import CredentialEncryptionService
        return CredentialEncryptionService()

    def _encrypt_github_pat(self, pat: str) -> str:
        svc = self._get_encryption_service()
        return svc.encrypt({"github_pat": pat})

    def _decrypt_github_pat(self, encrypted: str) -> Optional[str]:
        try:
            svc = self._get_encryption_service()
            return svc.decrypt(encrypted).get("github_pat")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to decrypt per-user GitHub PAT: {e}"
            )
            return None

    def get_user_github_pat(self, user_id: int) -> Optional[str]:
        """Decrypted per-user GitHub PAT, or None if unset / undecryptable."""
        stmt = select(users.c.github_pat_encrypted).where(users.c.id == user_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row or not row["github_pat_encrypted"]:
            return None
        return self._decrypt_github_pat(row["github_pat_encrypted"])

    def set_user_github_pat(self, user_id: int, pat: str) -> bool:
        """Store an encrypted per-user PAT. True if the user row was updated."""
        encrypted = self._encrypt_github_pat(pat)
        stmt = (
            update(users)
            .where(users.c.id == user_id)
            .values(github_pat_encrypted=encrypted, updated_at=utc_now_iso())
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

    def clear_user_github_pat(self, user_id: int) -> bool:
        """Clear the per-user PAT (revert this user to the global PAT)."""
        stmt = (
            update(users)
            .where(users.c.id == user_id)
            .values(github_pat_encrypted=None, updated_at=utc_now_iso())
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

    def has_user_github_pat(self, user_id: int) -> bool:
        """True if the user has a personal GitHub PAT configured."""
        stmt = select(
            users.c.github_pat_encrypted.isnot(None).label("has_pat")
        ).where(users.c.id == user_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return bool(row and row["has_pat"])
