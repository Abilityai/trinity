"""
Database operations for VoIP telephony bindings and call logs (VOIP-001, #1056).

Handles:
- Voice bindings (one Twilio voice sender per agent; AuthToken encrypted at rest).
  Separate from whatsapp_bindings — voice and messaging are different Twilio
  products with different setup (a Voice-capable number / TwiML app).
- Call logs (one row per outbound call) — backs the durable per-agent daily
  call cap and seeds Phase 3 observability.

Encryption mirrors the WhatsApp/Slack/Telegram pattern: the AuthToken is wrapped
in an AES-256-GCM JSON envelope via CredentialEncryptionService (Invariant #12).
"""

import logging
import secrets
from typing import List, Optional

from db.connection import get_db_connection
from utils.helpers import utc_now_iso, iso_cutoff

logger = logging.getLogger(__name__)


class VoipOperations:
    """Operations for VoIP/Twilio-voice bindings and call logs."""

    # =========================================================================
    # Encryption helpers (same pattern as WhatsApp/Slack/Telegram)
    # =========================================================================

    def _get_encryption_service(self):
        from services.credential_encryption import CredentialEncryptionService
        return CredentialEncryptionService()

    def _encrypt_auth_token(self, auth_token: str) -> str:
        svc = self._get_encryption_service()
        return svc.encrypt({"auth_token": auth_token})

    def _decrypt_auth_token(self, encrypted: str) -> Optional[str]:
        try:
            svc = self._get_encryption_service()
            decrypted = svc.decrypt(encrypted)
            return decrypted.get("auth_token")
        except Exception as e:
            logger.error(f"Failed to decrypt Twilio voice AuthToken: {e}")
            return None

    # =========================================================================
    # Binding Operations
    # =========================================================================

    def create_binding(
        self,
        agent_name: str,
        account_sid: str,
        auth_token: str,
        from_number: str,
        daily_call_cap: Optional[int] = None,
        display_name: Optional[str] = None,
        inbound_number: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> dict:
        """Create or replace a Twilio-voice binding for an agent."""
        webhook_secret = secrets.token_urlsafe(32)
        now = utc_now_iso()
        encrypted_token = self._encrypt_auth_token(auth_token)
        cap = daily_call_cap if daily_call_cap is not None else 50

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO voip_bindings
                (agent_name, account_sid, auth_token_encrypted, from_number,
                 inbound_number, webhook_secret, daily_call_cap, display_name,
                 enabled, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(agent_name) DO UPDATE SET
                    account_sid = excluded.account_sid,
                    auth_token_encrypted = excluded.auth_token_encrypted,
                    from_number = excluded.from_number,
                    inbound_number = excluded.inbound_number,
                    webhook_secret = excluded.webhook_secret,
                    daily_call_cap = excluded.daily_call_cap,
                    display_name = excluded.display_name,
                    enabled = 1,
                    updated_at = excluded.updated_at
            """, (agent_name, account_sid, encrypted_token, from_number,
                  inbound_number, webhook_secret, cap, display_name,
                  created_by, now, now))
            conn.commit()

        return self.get_binding_by_agent(agent_name)

    def get_binding_by_agent(self, agent_name: str) -> Optional[dict]:
        """Fetch binding by agent name. AuthToken stays encrypted."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, account_sid, auth_token_encrypted,
                       from_number, inbound_number, webhook_secret, webhook_url,
                       daily_call_cap, display_name, enabled,
                       created_by, created_at, updated_at
                FROM voip_bindings WHERE agent_name = ?
            """, (agent_name,))
            row = cursor.fetchone()
        return self._row_to_binding(row) if row else None

    def get_binding_by_webhook_secret(self, webhook_secret: str) -> Optional[dict]:
        """Resolve webhook_secret → binding (Phase 2 inbound routing)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, account_sid, auth_token_encrypted,
                       from_number, inbound_number, webhook_secret, webhook_url,
                       daily_call_cap, display_name, enabled,
                       created_by, created_at, updated_at
                FROM voip_bindings WHERE webhook_secret = ?
            """, (webhook_secret,))
            row = cursor.fetchone()
        return self._row_to_binding(row) if row else None

    def get_decrypted_auth_token(self, agent_name: str) -> Optional[str]:
        binding = self.get_binding_by_agent(agent_name)
        if not binding:
            return None
        return self._decrypt_auth_token(binding["auth_token_encrypted"])

    def get_all_bindings(self) -> List[dict]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, account_sid, auth_token_encrypted,
                       from_number, inbound_number, webhook_secret, webhook_url,
                       daily_call_cap, display_name, enabled,
                       created_by, created_at, updated_at
                FROM voip_bindings
            """)
            rows = cursor.fetchall()
        return [self._row_to_binding(row) for row in rows]

    def update_webhook_url(self, agent_name: str, webhook_url: str) -> None:
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE voip_bindings
                SET webhook_url = ?, updated_at = ?
                WHERE agent_name = ?
            """, (webhook_url, now, agent_name))
            conn.commit()

    def delete_binding(self, agent_name: str) -> bool:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM voip_bindings WHERE agent_name = ?",
                (agent_name,)
            )
            deleted = cursor.rowcount > 0
            conn.commit()
        return deleted

    # =========================================================================
    # Call Log Operations
    # =========================================================================

    def create_call_log(
        self,
        call_id: str,
        agent_name: str,
        to_number: str,
        chat_session_id: Optional[str] = None,
        initiated_by_user_id: Optional[int] = None,
        initiated_by_email: Optional[str] = None,
        direction: str = "outbound",
    ) -> None:
        """Record an initiated call (status='initiated')."""
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO voip_call_logs
                (call_id, agent_name, chat_session_id, to_number, direction,
                 status, initiated_by_user_id, initiated_by_email, started_at)
                VALUES (?, ?, ?, ?, ?, 'initiated', ?, ?, ?)
            """, (call_id, agent_name, chat_session_id, to_number, direction,
                  initiated_by_user_id, initiated_by_email, now))
            conn.commit()

    def update_call_status(
        self,
        call_id: str,
        status: str,
        twilio_call_sid: Optional[str] = None,
        error: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        """Advance a call's lifecycle status. Stamps connected_at/ended_at."""
        now = utc_now_iso()
        sets = ["status = ?"]
        params: list = [status]
        if twilio_call_sid is not None:
            sets.append("twilio_call_sid = ?")
            params.append(twilio_call_sid)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if duration_ms is not None:
            sets.append("duration_ms = ?")
            params.append(duration_ms)
        if status == "connected":
            sets.append("connected_at = ?")
            params.append(now)
        if status in ("completed", "failed"):
            sets.append("ended_at = ?")
            params.append(now)
        params.append(call_id)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE voip_call_logs SET {', '.join(sets)} WHERE call_id = ?",
                params,
            )
            conn.commit()

    def count_calls_since(self, agent_name: str, hours: int = 24) -> int:
        """Durable count of calls started in the trailing window (daily cap).

        Uses iso_cutoff() per Invariant #16 — started_at is an ISO-Z string,
        so a datetime('now', ...) comparison would be format-mismatched.
        """
        cutoff = iso_cutoff(hours)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM voip_call_logs
                WHERE agent_name = ? AND started_at >= ?
            """, (agent_name, cutoff))
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    # =========================================================================
    # Row converters
    # =========================================================================

    @staticmethod
    def _row_to_binding(row) -> dict:
        return {
            "id": row[0],
            "agent_name": row[1],
            "account_sid": row[2],
            "auth_token_encrypted": row[3],
            "from_number": row[4],
            "inbound_number": row[5],
            "webhook_secret": row[6],
            "webhook_url": row[7],
            "daily_call_cap": row[8],
            "display_name": row[9],
            "enabled": bool(row[10]),
            "created_by": row[11],
            "created_at": row[12],
            "updated_at": row[13],
        }
