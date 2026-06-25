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

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL.
"""

import logging
import secrets
from typing import List, Optional

from sqlalchemy import select, insert, update, delete, func

from .engine import get_engine, make_insert
from .tables import voip_bindings, voip_call_logs
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

        stmt = make_insert(voip_bindings).values(
            agent_name=agent_name,
            account_sid=account_sid,
            auth_token_encrypted=encrypted_token,
            from_number=from_number,
            inbound_number=inbound_number,
            webhook_secret=webhook_secret,
            daily_call_cap=cap,
            display_name=display_name,
            enabled=1,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        ).on_conflict_do_update(
            index_elements=[voip_bindings.c.agent_name],
            set_={
                "account_sid": account_sid,
                "auth_token_encrypted": encrypted_token,
                "from_number": from_number,
                "inbound_number": inbound_number,
                "webhook_secret": webhook_secret,
                "daily_call_cap": cap,
                "display_name": display_name,
                # NOTE: `enabled` is deliberately NOT in the conflict set_ (#28).
                # A fresh binding inserts enabled=1; re-saving credentials on an
                # existing binding must PRESERVE the current enable state so an
                # owner who disabled VoIP doesn't get it silently re-enabled by
                # editing the from-number (reviewer H3). Toggle via set_enabled().
                "updated_at": now,
            },
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return self.get_binding_by_agent(agent_name)

    def get_binding_by_agent(self, agent_name: str) -> Optional[dict]:
        """Fetch binding by agent name. AuthToken stays encrypted."""
        stmt = select(
            voip_bindings.c.id,
            voip_bindings.c.agent_name,
            voip_bindings.c.account_sid,
            voip_bindings.c.auth_token_encrypted,
            voip_bindings.c.from_number,
            voip_bindings.c.inbound_number,
            voip_bindings.c.webhook_secret,
            voip_bindings.c.webhook_url,
            voip_bindings.c.daily_call_cap,
            voip_bindings.c.display_name,
            voip_bindings.c.enabled,
            voip_bindings.c.created_by,
            voip_bindings.c.created_at,
            voip_bindings.c.updated_at,
        ).where(voip_bindings.c.agent_name == agent_name)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_binding(row) if row else None

    def get_binding_by_webhook_secret(self, webhook_secret: str) -> Optional[dict]:
        """Resolve webhook_secret → binding (Phase 2 inbound routing)."""
        stmt = select(
            voip_bindings.c.id,
            voip_bindings.c.agent_name,
            voip_bindings.c.account_sid,
            voip_bindings.c.auth_token_encrypted,
            voip_bindings.c.from_number,
            voip_bindings.c.inbound_number,
            voip_bindings.c.webhook_secret,
            voip_bindings.c.webhook_url,
            voip_bindings.c.daily_call_cap,
            voip_bindings.c.display_name,
            voip_bindings.c.enabled,
            voip_bindings.c.created_by,
            voip_bindings.c.created_at,
            voip_bindings.c.updated_at,
        ).where(voip_bindings.c.webhook_secret == webhook_secret)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_binding(row) if row else None

    def get_decrypted_auth_token(self, agent_name: str) -> Optional[str]:
        binding = self.get_binding_by_agent(agent_name)
        if not binding:
            return None
        return self._decrypt_auth_token(binding["auth_token_encrypted"])

    def get_all_bindings(self) -> List[dict]:
        stmt = select(
            voip_bindings.c.id,
            voip_bindings.c.agent_name,
            voip_bindings.c.account_sid,
            voip_bindings.c.auth_token_encrypted,
            voip_bindings.c.from_number,
            voip_bindings.c.inbound_number,
            voip_bindings.c.webhook_secret,
            voip_bindings.c.webhook_url,
            voip_bindings.c.daily_call_cap,
            voip_bindings.c.display_name,
            voip_bindings.c.enabled,
            voip_bindings.c.created_by,
            voip_bindings.c.created_at,
            voip_bindings.c.updated_at,
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_binding(row) for row in rows]

    def update_webhook_url(self, agent_name: str, webhook_url: str) -> None:
        now = utc_now_iso()
        stmt = (
            update(voip_bindings)
            .where(voip_bindings.c.agent_name == agent_name)
            .values(webhook_url=webhook_url, updated_at=now)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def set_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Flip a binding's enabled flag without touching credentials (#28).

        Returns False when no binding row exists for the agent (caller 404s) so
        the UI never shows a disabled-but-nonexistent state.
        """
        stmt = (
            update(voip_bindings)
            .where(voip_bindings.c.agent_name == agent_name)
            .values(enabled=1 if enabled else 0, updated_at=utc_now_iso())
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def delete_binding(self, agent_name: str) -> bool:
        stmt = delete(voip_bindings).where(voip_bindings.c.agent_name == agent_name)
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            deleted = result.rowcount > 0
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
        stmt = insert(voip_call_logs).values(
            call_id=call_id,
            agent_name=agent_name,
            chat_session_id=chat_session_id,
            to_number=to_number,
            direction=direction,
            status="initiated",
            initiated_by_user_id=initiated_by_user_id,
            initiated_by_email=initiated_by_email,
            started_at=now,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

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
        values: dict = {"status": status}
        if twilio_call_sid is not None:
            values["twilio_call_sid"] = twilio_call_sid
        if error is not None:
            values["error"] = error
        if duration_ms is not None:
            values["duration_ms"] = duration_ms
        if status == "connected":
            values["connected_at"] = now
        if status in ("completed", "failed"):
            values["ended_at"] = now
        stmt = (
            update(voip_call_logs)
            .where(voip_call_logs.c.call_id == call_id)
            .values(**values)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def count_calls_since(self, agent_name: str, hours: int = 24) -> int:
        """Durable count of calls started in the trailing window (daily cap).

        Uses iso_cutoff() per Invariant #16 — started_at is an ISO-Z string,
        so a datetime('now', ...) comparison would be format-mismatched.
        """
        cutoff = iso_cutoff(hours)
        stmt = select(func.count()).where(
            voip_call_logs.c.agent_name == agent_name,
            voip_call_logs.c.started_at >= cutoff,
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return int(row[0]) if row else 0

    # =========================================================================
    # Row converters
    # =========================================================================

    @staticmethod
    def _row_to_binding(row) -> dict:
        return {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "account_sid": row["account_sid"],
            "auth_token_encrypted": row["auth_token_encrypted"],
            "from_number": row["from_number"],
            "inbound_number": row["inbound_number"],
            "webhook_secret": row["webhook_secret"],
            "webhook_url": row["webhook_url"],
            "daily_call_cap": row["daily_call_cap"],
            "display_name": row["display_name"],
            "enabled": bool(row["enabled"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
