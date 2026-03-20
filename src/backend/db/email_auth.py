"""
Email authentication operations for Trinity platform.

Handles:
- Email whitelist management
- Login code generation and verification
- User creation from email
"""

import secrets
from datetime import datetime, timedelta
from typing import Optional, List
from db.connection import get_db_connection


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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM email_whitelist WHERE LOWER(email) = ?",
                (email.lower(),)
            )
            return cursor.fetchone() is not None

    def add_to_whitelist(self, email: str, added_by: str, source: str = "manual") -> bool:
        """
        Add an email to the whitelist.

        Args:
            email: Email address to whitelist
            added_by: Username of user adding this email
            source: Source of addition (manual, agent_sharing, etc.)

        Returns:
            True if added, False if already exists
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Check if already exists
            if self.is_email_whitelisted(email):
                return False

            # Get user ID
            user = self._user_ops.get_user_by_username(added_by)
            if not user:
                raise ValueError(f"User not found: {added_by}")

            cursor.execute("""
                INSERT INTO email_whitelist (email, added_by, added_at, source)
                VALUES (?, ?, ?, ?)
            """, (email.lower(), user["id"], datetime.utcnow().isoformat(), source))

            conn.commit()
            return True

    def remove_from_whitelist(self, email: str) -> bool:
        """
        Remove an email from the whitelist.

        Returns:
            True if removed, False if not found
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM email_whitelist WHERE LOWER(email) = ?",
                (email.lower(),)
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_whitelist(self, limit: int = 100) -> List[dict]:
        """Get all whitelisted emails with metadata."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    w.id,
                    w.email,
                    w.added_by,
                    u.username as added_by_username,
                    w.added_at,
                    w.source
                FROM email_whitelist w
                LEFT JOIN users u ON w.added_by = u.id
                ORDER BY w.added_at DESC
                LIMIT ?
            """, (limit,))

            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    # =========================================================================
    # Login Code Operations
    # =========================================================================

    def create_login_code(self, email: str, expiry_minutes: int = 10) -> dict:
        """
        Generate a 6-digit login code for an email.

        Returns:
            dict with code_id, code, expires_at
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Generate 6-digit code
            code = f"{secrets.randbelow(1000000):06d}"
            code_id = secrets.token_urlsafe(16)
            created_at = datetime.utcnow()
            expires_at = created_at + timedelta(minutes=expiry_minutes)

            cursor.execute("""
                INSERT INTO email_login_codes (id, email, code, created_at, expires_at, verified)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (
                code_id,
                email.lower(),
                code,
                created_at.isoformat(),
                expires_at.isoformat()
            ))

            conn.commit()

            return {
                "code_id": code_id,
                "code": code,
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "expires_in_seconds": expiry_minutes * 60
            }

    def verify_login_code(self, email: str, code: str) -> Optional[dict]:
        """
        Verify a login code for an email.

        Returns:
            dict with code verification result, or None if invalid
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Find unused code for this email
            cursor.execute("""
                SELECT id, code, expires_at, verified
                FROM email_login_codes
                WHERE LOWER(email) = ? AND code = ? AND verified = 0
                ORDER BY created_at DESC
                LIMIT 1
            """, (email.lower(), code))

            row = cursor.fetchone()
            if not row:
                return None

            code_record = dict(row)

            # Check if expired
            expires_at = datetime.fromisoformat(code_record["expires_at"])
            if datetime.utcnow() > expires_at:
                return None

            # Mark as verified
            cursor.execute("""
                UPDATE email_login_codes
                SET verified = 1, used_at = ?
                WHERE id = ?
            """, (datetime.utcnow().isoformat(), code_record["id"]))

            conn.commit()

            return {
                "code_id": code_record["id"],
                "email": email.lower(),
                "verified": True
            }

    def count_recent_code_requests(self, email: str, minutes: int = 10) -> int:
        """Count how many code requests were made for this email recently."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()

            cursor.execute("""
                SELECT COUNT(*) as count
                FROM email_login_codes
                WHERE LOWER(email) = ? AND created_at > ?
            """, (email.lower(), cutoff))

            result = cursor.fetchone()
            return result["count"] if result else 0

    def cleanup_old_codes(self, days: int = 1):
        """Delete old verification codes."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

            cursor.execute("""
                DELETE FROM email_login_codes
                WHERE created_at < ?
            """, (cutoff,))

            deleted = cursor.rowcount
            conn.commit()
            return deleted

    # =========================================================================
    # User Management for Email Auth
    # =========================================================================

    def get_or_create_email_user(self, email: str) -> dict:
        """
        Get or create a user account for email authentication.

        Email becomes the username. No password is set (email auth only).
        """
        # Try to get existing user by email
        user = self._user_ops.get_user_by_email(email)
        if user:
            return user

        # Create new user
        now = datetime.utcnow().isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Username = email (lowercase)
            username = email.lower()

            cursor.execute("""
                INSERT INTO users (username, email, role, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (username, email.lower(), "creator", now, now))

            conn.commit()

            # Return the created user
            return self._user_ops.get_user_by_email(email)
