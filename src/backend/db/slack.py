"""
Database operations for Slack integration (SLACK-001).

Handles:
- Slack connection CRUD (workspace to public link)
- User verification management
- Pending verification state machine

Bot tokens are encrypted at rest via `services.credential_encryption`
(AES-256-GCM, JSON envelope) — same pattern as `db/slack_channels.py`,
`db/telegram_channels.py`, and `db/whatsapp_channels.py`. See #453.
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, List

from db.connection import get_db_connection
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


class SlackOperations:
    """Operations for managing Slack integrations."""

    # =========================================================================
    # Encryption helpers (same pattern as SlackChannelOperations,
    # TelegramChannelOperations, WhatsAppChannelOperations)
    # =========================================================================

    def _get_encryption_service(self):
        """Lazy-load encryption service.

        Raises ValueError on first call if CREDENTIAL_ENCRYPTION_KEY is unset.
        Write paths fail loudly; read paths catch and return None.
        """
        from services.credential_encryption import CredentialEncryptionService
        return CredentialEncryptionService()

    def _encrypt_token(self, token: str) -> str:
        svc = self._get_encryption_service()
        return svc.encrypt({"bot_token": token})

    def _decrypt_token(self, encrypted: str) -> Optional[str]:
        try:
            svc = self._get_encryption_service()
            decrypted = svc.decrypt(encrypted)
            return decrypted.get("bot_token")
        except Exception as e:
            logger.error(f"Failed to decrypt Slack bot token: {e}")
            # Fallback: legacy plaintext rows (Slack bot tokens always start xoxb-).
            # The one-shot migration in db/migrations.py re-encrypts these on the
            # next backend restart; this fallback keeps runtime working in the
            # interim and on developer machines that haven't run the migration.
            if encrypted and encrypted.startswith("xoxb-"):
                logger.warning(
                    "Slack bot token stored in plaintext — will re-encrypt on next migration"
                )
                return encrypted
            return None

    # =========================================================================
    # Slack Connection Operations
    # =========================================================================

    def create_slack_connection(
        self,
        link_id: str,
        slack_team_id: str,
        slack_team_name: Optional[str],
        slack_bot_token: str,
        connected_by: str
    ) -> dict:
        """Create a new Slack connection for a public link.

        Bot token is encrypted at rest (#453).
        """
        connection_id = secrets.token_urlsafe(16)
        now = utc_now_iso()
        encrypted_token = self._encrypt_token(slack_bot_token)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO slack_link_connections
                (id, link_id, slack_team_id, slack_team_name, slack_bot_token, connected_by, connected_at, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """, (connection_id, link_id, slack_team_id, slack_team_name, encrypted_token, connected_by, now))
            conn.commit()

        return self.get_slack_connection(connection_id)

    def get_slack_connection(self, connection_id: str) -> Optional[dict]:
        """Get a Slack connection by ID."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, link_id, slack_team_id, slack_team_name, slack_bot_token,
                       connected_by, connected_at, enabled
                FROM slack_link_connections
                WHERE id = ?
            """, (connection_id,))
            row = cursor.fetchone()

        if not row:
            return None

        return self._row_to_connection(row)

    def get_slack_connection_by_link(self, link_id: str) -> Optional[dict]:
        """Get a Slack connection by public link ID."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, link_id, slack_team_id, slack_team_name, slack_bot_token,
                       connected_by, connected_at, enabled
                FROM slack_link_connections
                WHERE link_id = ?
            """, (link_id,))
            row = cursor.fetchone()

        if not row:
            return None

        return self._row_to_connection(row)

    def get_slack_connection_by_team(self, slack_team_id: str) -> Optional[dict]:
        """Get a Slack connection by Slack team/workspace ID."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT c.id, c.link_id, c.slack_team_id, c.slack_team_name, c.slack_bot_token,
                       c.connected_by, c.connected_at, c.enabled,
                       l.agent_name, l.require_email
                FROM slack_link_connections c
                JOIN agent_public_links l ON c.link_id = l.id
                WHERE c.slack_team_id = ? AND c.enabled = 1 AND l.enabled = 1
            """, (slack_team_id,))
            row = cursor.fetchone()

        if not row:
            return None

        connection = self._row_to_connection(row[:8])
        connection["agent_name"] = row[8]
        connection["require_email"] = bool(row[9])
        return connection

    def update_slack_connection(
        self,
        connection_id: str,
        enabled: Optional[bool] = None,
        slack_team_name: Optional[str] = None
    ) -> Optional[dict]:
        """Update a Slack connection."""
        updates = []
        values = []

        if enabled is not None:
            updates.append("enabled = ?")
            values.append(1 if enabled else 0)
        if slack_team_name is not None:
            updates.append("slack_team_name = ?")
            values.append(slack_team_name)

        if not updates:
            return self.get_slack_connection(connection_id)

        values.append(connection_id)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE slack_link_connections
                SET {", ".join(updates)}
                WHERE id = ?
            """, values)
            conn.commit()

        return self.get_slack_connection(connection_id)

    def delete_slack_connection(self, connection_id: str) -> bool:
        """Delete a Slack connection."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Get link_id for cleanup
            cursor.execute("SELECT link_id FROM slack_link_connections WHERE id = ?", (connection_id,))
            row = cursor.fetchone()
            if row:
                link_id = row[0]
                # Delete related verifications
                cursor.execute("DELETE FROM slack_user_verifications WHERE link_id = ?", (link_id,))
                cursor.execute("DELETE FROM slack_pending_verifications WHERE link_id = ?", (link_id,))
            # Delete the connection
            cursor.execute("DELETE FROM slack_link_connections WHERE id = ?", (connection_id,))
            conn.commit()
            return cursor.rowcount > 0

    def delete_slack_connection_by_link(self, link_id: str) -> bool:
        """Delete a Slack connection by public link ID."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Delete related verifications
            cursor.execute("DELETE FROM slack_user_verifications WHERE link_id = ?", (link_id,))
            cursor.execute("DELETE FROM slack_pending_verifications WHERE link_id = ?", (link_id,))
            # Delete the connection
            cursor.execute("DELETE FROM slack_link_connections WHERE link_id = ?", (link_id,))
            conn.commit()
            return cursor.rowcount > 0

    def _row_to_connection(self, row) -> dict:
        """Convert a database row to a connection dict.

        Bot token is decrypted before return (#453). Callers (`routers/slack.py`,
        `adapters/slack_adapter.py`) receive plaintext tokens — same API
        surface as before encryption was added.
        """
        return {
            "id": row[0],
            "link_id": row[1],
            "slack_team_id": row[2],
            "slack_team_name": row[3],
            "slack_bot_token": self._decrypt_token(row[4]),
            "connected_by": row[5],
            "connected_at": row[6],
            "enabled": bool(row[7])
        }

    # =========================================================================
    # User Verification Operations
    # =========================================================================

    def get_user_verification(
        self,
        link_id: str,
        slack_user_id: str,
        slack_team_id: str
    ) -> Optional[dict]:
        """Check if a Slack user is verified for a link."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, link_id, slack_user_id, slack_team_id, verified_email,
                       verification_method, verified_at
                FROM slack_user_verifications
                WHERE link_id = ? AND slack_user_id = ? AND slack_team_id = ?
            """, (link_id, slack_user_id, slack_team_id))
            row = cursor.fetchone()

        if not row:
            return None

        return {
            "id": row[0],
            "link_id": row[1],
            "slack_user_id": row[2],
            "slack_team_id": row[3],
            "verified_email": row[4],
            "verification_method": row[5],
            "verified_at": row[6]
        }

    def create_user_verification(
        self,
        link_id: str,
        slack_user_id: str,
        slack_team_id: str,
        verified_email: str,
        verification_method: str
    ) -> dict:
        """Create a user verification record."""
        verification_id = secrets.token_urlsafe(16)
        now = utc_now_iso()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Upsert - replace if exists
            cursor.execute("""
                INSERT OR REPLACE INTO slack_user_verifications
                (id, link_id, slack_user_id, slack_team_id, verified_email, verification_method, verified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (verification_id, link_id, slack_user_id, slack_team_id, verified_email, verification_method, now))
            conn.commit()

        return self.get_user_verification(link_id, slack_user_id, slack_team_id)

    # =========================================================================
    # Pending Verification Operations (State Machine)
    # =========================================================================

    def get_pending_verification(
        self,
        slack_user_id: str,
        slack_team_id: str
    ) -> Optional[dict]:
        """Get a pending verification for a Slack user."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, link_id, slack_user_id, slack_team_id, email, code,
                       created_at, expires_at, state
                FROM slack_pending_verifications
                WHERE slack_user_id = ? AND slack_team_id = ?
                AND expires_at > ?
            """, (slack_user_id, slack_team_id, utc_now_iso()))
            row = cursor.fetchone()

        if not row:
            return None

        return self._row_to_pending(row)

    def create_pending_verification(
        self,
        link_id: str,
        slack_user_id: str,
        slack_team_id: str,
        email: Optional[str] = None,
        code: Optional[str] = None,
        state: str = "awaiting_email"
    ) -> dict:
        """Create a pending verification entry."""
        pending_id = secrets.token_urlsafe(16)
        now = datetime.utcnow()
        expires_at = (now + timedelta(minutes=10)).isoformat()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Delete any existing pending verification for this user
            cursor.execute("""
                DELETE FROM slack_pending_verifications
                WHERE slack_user_id = ? AND slack_team_id = ?
            """, (slack_user_id, slack_team_id))
            # Create new one
            cursor.execute("""
                INSERT INTO slack_pending_verifications
                (id, link_id, slack_user_id, slack_team_id, email, code, created_at, expires_at, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (pending_id, link_id, slack_user_id, slack_team_id, email, code, now.isoformat(), expires_at, state))
            conn.commit()

        return self.get_pending_verification(slack_user_id, slack_team_id)

    def update_pending_verification(
        self,
        slack_user_id: str,
        slack_team_id: str,
        email: Optional[str] = None,
        code: Optional[str] = None,
        state: Optional[str] = None
    ) -> Optional[dict]:
        """Update a pending verification (transition state machine)."""
        updates = []
        values = []

        if email is not None:
            updates.append("email = ?")
            values.append(email)
        if code is not None:
            updates.append("code = ?")
            values.append(code)
        if state is not None:
            updates.append("state = ?")
            values.append(state)

        # Reset expiration
        updates.append("expires_at = ?")
        values.append((datetime.utcnow() + timedelta(minutes=10)).isoformat())

        values.extend([slack_user_id, slack_team_id])

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE slack_pending_verifications
                SET {", ".join(updates)}
                WHERE slack_user_id = ? AND slack_team_id = ?
            """, values)
            conn.commit()

        return self.get_pending_verification(slack_user_id, slack_team_id)

    def delete_pending_verification(
        self,
        slack_user_id: str,
        slack_team_id: str
    ) -> bool:
        """Delete a pending verification (after successful verification)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM slack_pending_verifications
                WHERE slack_user_id = ? AND slack_team_id = ?
            """, (slack_user_id, slack_team_id))
            conn.commit()
            return cursor.rowcount > 0

    def cleanup_expired_pending_verifications(self) -> int:
        """Clean up expired pending verifications."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM slack_pending_verifications
                WHERE expires_at < ?
            """, (utc_now_iso(),))
            conn.commit()
            return cursor.rowcount

    def _row_to_pending(self, row) -> dict:
        """Convert a database row to a pending verification dict."""
        return {
            "id": row[0],
            "link_id": row[1],
            "slack_user_id": row[2],
            "slack_team_id": row[3],
            "email": row[4],
            "code": row[5],
            "created_at": row[6],
            "expires_at": row[7],
            "state": row[8]
        }
