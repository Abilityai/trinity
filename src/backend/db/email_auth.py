"""
Email authentication operations for Trinity platform.

Handles:
- Email whitelist management
- Login code generation and verification
- User creation from email

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the ``email_whitelist``,
``email_login_codes``, and ``users`` tables in ``db/tables.py``
(dialect-agnostic expressions, no ``?`` placeholders, no SQLite-only time
functions), and the engine is resolved via ``db/engine.py``.
"""

import secrets
from datetime import datetime, timedelta
from typing import Optional, List

from sqlalchemy import select, insert, update, delete, func, and_, cast, Text

from .engine import get_engine
from .tables import email_whitelist, email_login_codes, users
from utils.helpers import utc_now_iso
from .users import EmailInUseError

# Valid role values for new users. Kept local to avoid a circular import with
# dependencies.py (which imports from database, which imports from this module).
# Must stay in sync with dependencies.ROLE_HIERARCHY.
_VALID_DEFAULT_ROLES = {"user", "operator", "creator", "admin"}


class EmailAuthOperations:
    """Handles email-based authentication operations."""

    def __init__(self, user_ops):
        """Initialize with user operations dependency."""
        self._user_ops = user_ops

    # =========================================================================
    # Email Whitelist Operations
    # =========================================================================

    def is_email_whitelisted(self, email: str) -> bool:
        """Check if an email is in the whitelist."""
        stmt = select(email_whitelist.c.id).where(
            func.lower(email_whitelist.c.email) == email.lower()
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).mappings().first() is not None

    def add_to_whitelist(
        self,
        email: str,
        added_by: str,
        source: str,
        *,
        default_role: str,
    ) -> bool:
        """
        Add an email to the whitelist.

        Args:
            email: Email address to whitelist
            added_by: Username of user adding this email
            source: Source of addition (manual, agent_sharing, access_request, cli)
            default_role: Role assigned on first email login. Required keyword-only
                so every callsite makes the role decision deliberately (#314).

        Returns:
            True if added, False if already exists

        Raises:
            ValueError: If default_role is not a recognized role.
        """
        if default_role not in _VALID_DEFAULT_ROLES:
            raise ValueError(
                f"Invalid default_role: {default_role!r}. "
                f"Must be one of {sorted(_VALID_DEFAULT_ROLES)}"
            )

        # Check if already exists
        if self.is_email_whitelisted(email):
            return False

        # Get user ID
        user = self._user_ops.get_user_by_username(added_by)
        if not user:
            raise ValueError(f"User not found: {added_by}")

        stmt = insert(email_whitelist).values(
            email=email.lower(),
            added_by=user["id"],
            added_at=utc_now_iso(),
            source=source,
            default_role=default_role,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)
        return True

    def get_whitelist_default_role(self, email: str) -> Optional[str]:
        """Return the default_role for a whitelisted email, or None if not found."""
        stmt = select(email_whitelist.c.default_role).where(
            func.lower(email_whitelist.c.email) == email.lower()
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        return row["default_role"] if row["default_role"] else None

    def remove_from_whitelist(self, email: str) -> bool:
        """
        Remove an email from the whitelist.

        Returns:
            True if removed, False if not found
        """
        stmt = delete(email_whitelist).where(
            func.lower(email_whitelist.c.email) == email.lower()
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
        return result.rowcount > 0

    def list_whitelist(self, limit: int = 100) -> List[dict]:
        """Get all whitelisted emails with metadata."""
        stmt = (
            select(
                email_whitelist.c.id,
                email_whitelist.c.email,
                email_whitelist.c.added_by,
                users.c.username.label("added_by_username"),
                email_whitelist.c.added_at,
                email_whitelist.c.source,
                email_whitelist.c.default_role,
            )
            .select_from(
                # added_by is TEXT (stringified user id) but users.id is INTEGER;
                # cast so the JOIN works on PostgreSQL too (#300, text=integer).
                email_whitelist.outerjoin(
                    users, email_whitelist.c.added_by == cast(users.c.id, Text)
                )
            )
            .order_by(email_whitelist.c.added_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    # =========================================================================
    # Login Code Operations
    # =========================================================================

    def create_login_code(self, email: str, expiry_minutes: int = 10,
                          purpose: Optional[str] = None) -> dict:
        """
        Generate a 6-digit login code for an email.

        purpose (ent#720): NULL is a sign-in code. An email-bind code carries
        `email_bind:<user id>` so it can only complete THAT account's bind, and
        neither kind of code is accepted where the other is expected.

        Returns:
            dict with code_id, code, expires_at
        """
        # Generate 6-digit code
        code = f"{secrets.randbelow(1000000):06d}"
        code_id = secrets.token_urlsafe(16)
        created_at = datetime.utcnow()
        expires_at = created_at + timedelta(minutes=expiry_minutes)

        stmt = insert(email_login_codes).values(
            id=code_id,
            email=email.lower(),
            code=code,
            created_at=created_at.isoformat(),
            expires_at=expires_at.isoformat(),
            verified=0,
            purpose=purpose,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return {
            "code_id": code_id,
            "code": code,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "expires_in_seconds": expiry_minutes * 60
        }

    def verify_login_code(self, email: str, code: str,
                          purpose: Optional[str] = None) -> Optional[dict]:
        """
        Verify a login code for an email. `purpose` must match exactly — the
        default (None) accepts only sign-in codes (ent#720).

        Returns:
            dict with code verification result, or None if invalid
        """
        with get_engine().begin() as conn:
            # Find unused code for this email
            select_stmt = (
                select(
                    email_login_codes.c.id,
                    email_login_codes.c.code,
                    email_login_codes.c.expires_at,
                    email_login_codes.c.verified,
                )
                .where(
                    and_(
                        func.lower(email_login_codes.c.email) == email.lower(),
                        email_login_codes.c.code == code,
                        email_login_codes.c.verified == 0,
                        (email_login_codes.c.purpose.is_(None) if purpose is None
                         else email_login_codes.c.purpose == purpose),
                    )
                )
                .order_by(email_login_codes.c.created_at.desc())
                .limit(1)
            )

            row = conn.execute(select_stmt).mappings().first()
            if not row:
                return None

            code_record = dict(row)

            # Check if expired
            expires_at = datetime.fromisoformat(code_record["expires_at"])
            if datetime.utcnow() > expires_at:
                return None

            # Mark as verified
            conn.execute(
                update(email_login_codes)
                .where(email_login_codes.c.id == code_record["id"])
                .values(verified=1, used_at=utc_now_iso())
            )

            return {
                "code_id": code_record["id"],
                "email": email.lower(),
                "verified": True
            }

    def count_recent_code_requests(self, email: str, minutes: int = 10) -> int:
        """Count recent SIGN-IN code requests for this email (`purpose IS NULL`).

        ent#720: bind codes are excluded. Counting them would let any signed-in
        user request bind codes for someone else's address and keep its owner's
        sign-in codes suppressed; the bind route limits its caller instead
        (`count_recent_codes_for_purpose`)."""
        cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
        stmt = select(func.count().label("count")).where(
            and_(
                func.lower(email_login_codes.c.email) == email.lower(),
                email_login_codes.c.purpose.is_(None),
                email_login_codes.c.created_at > cutoff,
            )
        )
        with get_engine().connect() as conn:
            result = conn.execute(stmt).mappings().first()
        return result["count"] if result else 0

    def count_recent_codes_for_purpose(self, purpose: str, minutes: int = 10) -> int:
        """Count recent codes minted for one purpose, across every address (ent#720:
        the per-account bind allowance, `email_bind:<user id>`)."""
        cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
        stmt = select(func.count().label("count")).where(
            and_(
                email_login_codes.c.purpose == purpose,
                email_login_codes.c.created_at > cutoff,
            )
        )
        with get_engine().connect() as conn:
            result = conn.execute(stmt).mappings().first()
        return result["count"] if result else 0

    def cleanup_old_codes(self, days: int = 1):
        """Delete old verification codes."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        stmt = delete(email_login_codes).where(
            email_login_codes.c.created_at < cutoff
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
        return result.rowcount

    # =========================================================================
    # User Management for Email Auth
    # =========================================================================

    def get_or_create_email_user(self, email: str) -> dict:
        """
        Get or create a user account for email authentication.

        Email becomes the username. No password is set (email auth only).

        New users inherit the role recorded on their whitelist row, falling
        back to "user" when no whitelist entry exists (safer default — see
        #314). Previously this hardcoded "creator", which silently promoted
        anyone whitelisted via access grants to full agent-creation rights.
        """
        # Try to get existing user by email
        user = self._user_ops.get_user_by_email(email)
        if user:
            return user

        # Resolve intended role from whitelist row; "user" if no row or NULL.
        role = self.get_whitelist_default_role(email) or "user"
        if role not in _VALID_DEFAULT_ROLES:
            role = "user"

        # Create new user — through the one checked writer (ent#720), which
        # also keeps a reclaimed `username = email` from becoming a 500.
        try:
            return self._user_ops.insert_email_user(email, role)
        except EmailInUseError:
            # Lost a race with a concurrent first sign-in for the same address:
            # that account is this person's account.
            return self._user_ops.get_user_by_email(email)
