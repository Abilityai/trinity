"""
Database operations for WhatsApp (Twilio) bindings and chat tracking (WHATSAPP-001).

Handles:
- Bot bindings (one Twilio sender per agent; AuthToken encrypted at rest)
- Chat link tracking (WhatsApp phone → session mapping)
- Verified-email storage for unified access control (#311 — Phase 2 uses these columns)

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the ``whatsapp_bindings`` /
``whatsapp_chat_links`` tables in ``db/tables.py``; the engine is resolved via
``db/engine.py``. Public API is unchanged.
"""

import logging
import secrets
from typing import List, Optional

from sqlalchemy import select, update, delete, and_, func

from .engine import get_engine, make_insert
from .tables import whatsapp_bindings, whatsapp_chat_links
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# Twilio Sandbox shared sender number — auto-detect sandbox mode.
_TWILIO_SANDBOX_FROM_NUMBER = "whatsapp:+14155238886"


def _is_sandbox_number(from_number: str) -> bool:
    """True if the from_number is the shared Twilio WhatsApp Sandbox sender."""
    return from_number.strip() == _TWILIO_SANDBOX_FROM_NUMBER


class WhatsAppChannelOperations:
    """Operations for WhatsApp/Twilio bindings and chat links."""

    # =========================================================================
    # Encryption helpers (same pattern as Slack/Telegram)
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
            logger.error(f"Failed to decrypt Twilio AuthToken: {e}")
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
        messaging_service_sid: Optional[str] = None,
        display_name: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> dict:
        """Create or replace a WhatsApp (Twilio) binding for an agent."""
        webhook_secret = secrets.token_urlsafe(32)
        now = utc_now_iso()
        encrypted_token = self._encrypt_auth_token(auth_token)
        is_sandbox = 1 if _is_sandbox_number(from_number) else 0

        stmt = make_insert(whatsapp_bindings).values(
            agent_name=agent_name,
            account_sid=account_sid,
            auth_token_encrypted=encrypted_token,
            from_number=from_number,
            messaging_service_sid=messaging_service_sid,
            display_name=display_name,
            is_sandbox=is_sandbox,
            webhook_secret=webhook_secret,
            enabled=1,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        ).on_conflict_do_update(
            index_elements=[whatsapp_bindings.c.agent_name],
            set_={
                "account_sid": account_sid,
                "auth_token_encrypted": encrypted_token,
                "from_number": from_number,
                "messaging_service_sid": messaging_service_sid,
                "display_name": display_name,
                "is_sandbox": is_sandbox,
                "webhook_secret": webhook_secret,
                "enabled": 1,
                "updated_at": now,
            },
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return self.get_binding_by_agent(agent_name)

    def get_binding_by_agent(self, agent_name: str) -> Optional[dict]:
        """Fetch binding by agent name. AuthToken stays encrypted."""
        stmt = select(
            whatsapp_bindings.c.id,
            whatsapp_bindings.c.agent_name,
            whatsapp_bindings.c.account_sid,
            whatsapp_bindings.c.auth_token_encrypted,
            whatsapp_bindings.c.from_number,
            whatsapp_bindings.c.messaging_service_sid,
            whatsapp_bindings.c.display_name,
            whatsapp_bindings.c.is_sandbox,
            whatsapp_bindings.c.webhook_secret,
            whatsapp_bindings.c.webhook_url,
            whatsapp_bindings.c.enabled,
            whatsapp_bindings.c.created_by,
            whatsapp_bindings.c.created_at,
            whatsapp_bindings.c.updated_at,
        ).where(whatsapp_bindings.c.agent_name == agent_name)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_binding(row) if row else None

    def get_binding_by_webhook_secret(self, webhook_secret: str) -> Optional[dict]:
        """Resolve webhook_secret → binding for incoming webhook routing."""
        stmt = select(
            whatsapp_bindings.c.id,
            whatsapp_bindings.c.agent_name,
            whatsapp_bindings.c.account_sid,
            whatsapp_bindings.c.auth_token_encrypted,
            whatsapp_bindings.c.from_number,
            whatsapp_bindings.c.messaging_service_sid,
            whatsapp_bindings.c.display_name,
            whatsapp_bindings.c.is_sandbox,
            whatsapp_bindings.c.webhook_secret,
            whatsapp_bindings.c.webhook_url,
            whatsapp_bindings.c.enabled,
            whatsapp_bindings.c.created_by,
            whatsapp_bindings.c.created_at,
            whatsapp_bindings.c.updated_at,
        ).where(whatsapp_bindings.c.webhook_secret == webhook_secret)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_binding(row) if row else None

    def get_decrypted_auth_token(self, agent_name: str) -> Optional[str]:
        binding = self.get_binding_by_agent(agent_name)
        if not binding:
            return None
        return self._decrypt_auth_token(binding["auth_token_encrypted"])

    def get_all_bindings(self) -> List[dict]:
        """All bindings (for startup reconciliation + webhook URL backfill)."""
        stmt = select(
            whatsapp_bindings.c.id,
            whatsapp_bindings.c.agent_name,
            whatsapp_bindings.c.account_sid,
            whatsapp_bindings.c.auth_token_encrypted,
            whatsapp_bindings.c.from_number,
            whatsapp_bindings.c.messaging_service_sid,
            whatsapp_bindings.c.display_name,
            whatsapp_bindings.c.is_sandbox,
            whatsapp_bindings.c.webhook_secret,
            whatsapp_bindings.c.webhook_url,
            whatsapp_bindings.c.enabled,
            whatsapp_bindings.c.created_by,
            whatsapp_bindings.c.created_at,
            whatsapp_bindings.c.updated_at,
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_binding(row) for row in rows]

    def update_webhook_url(self, agent_name: str, webhook_url: str) -> None:
        """Persist the canonical webhook URL for this binding (UI display)."""
        now = utc_now_iso()
        stmt = (
            update(whatsapp_bindings)
            .where(whatsapp_bindings.c.agent_name == agent_name)
            .values(webhook_url=webhook_url, updated_at=now)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def delete_binding(self, agent_name: str) -> bool:
        """Delete a binding and cascade to chat_links."""
        binding_ids = select(whatsapp_bindings.c.id).where(
            whatsapp_bindings.c.agent_name == agent_name
        )
        with get_engine().begin() as conn:
            conn.execute(
                delete(whatsapp_chat_links).where(
                    whatsapp_chat_links.c.binding_id.in_(binding_ids)
                )
            )
            result = conn.execute(
                delete(whatsapp_bindings).where(
                    whatsapp_bindings.c.agent_name == agent_name
                )
            )
            deleted = result.rowcount > 0
        return deleted

    # =========================================================================
    # Chat Link Operations
    # =========================================================================

    def get_or_create_chat_link(
        self,
        binding_id: int,
        wa_user_phone: str,
        wa_user_name: Optional[str] = None,
    ) -> dict:
        """Get or create a chat link for a WhatsApp user (by phone number)."""
        select_stmt = select(
            whatsapp_chat_links.c.id,
            whatsapp_chat_links.c.binding_id,
            whatsapp_chat_links.c.wa_user_phone,
            whatsapp_chat_links.c.wa_user_name,
            whatsapp_chat_links.c.session_id,
            whatsapp_chat_links.c.verified_email,
            whatsapp_chat_links.c.verified_at,
            whatsapp_chat_links.c.message_count,
            whatsapp_chat_links.c.last_active,
            whatsapp_chat_links.c.created_at,
        ).where(
            and_(
                whatsapp_chat_links.c.binding_id == binding_id,
                whatsapp_chat_links.c.wa_user_phone == wa_user_phone,
            )
        )
        with get_engine().begin() as conn:
            row = conn.execute(select_stmt).mappings().first()

            if row:
                return self._row_to_chat_link(row)

            now = utc_now_iso()
            conn.execute(
                make_insert(whatsapp_chat_links).values(
                    binding_id=binding_id,
                    wa_user_phone=wa_user_phone,
                    wa_user_name=wa_user_name,
                    message_count=0,
                    created_at=now,
                    last_active=now,
                )
            )

            row = conn.execute(select_stmt).mappings().first()
            return self._row_to_chat_link(row)

    def get_verified_email(self, binding_id: int, wa_user_phone: str) -> Optional[str]:
        """Return verified email for this phone, or None (#311 Phase 2)."""
        stmt = select(whatsapp_chat_links.c.verified_email).where(
            and_(
                whatsapp_chat_links.c.binding_id == binding_id,
                whatsapp_chat_links.c.wa_user_phone == wa_user_phone,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return row["verified_email"] if row and row["verified_email"] else None

    def list_clients_for_agent(self, agent_name: str) -> List[dict]:
        """External WhatsApp clients who have messaged this agent (#20 roster).

        Joins the agent's binding to its chat links. ``identity`` is the
        E.164-style ``whatsapp:+...`` phone the user messaged from.
        """
        stmt = (
            select(
                whatsapp_chat_links.c.wa_user_phone,
                whatsapp_chat_links.c.wa_user_name,
                whatsapp_chat_links.c.verified_email,
                whatsapp_chat_links.c.message_count,
                whatsapp_chat_links.c.last_active,
            )
            .select_from(
                whatsapp_chat_links.join(
                    whatsapp_bindings,
                    whatsapp_chat_links.c.binding_id == whatsapp_bindings.c.id,
                )
            )
            .where(whatsapp_bindings.c.agent_name == agent_name)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        clients = []
        for row in rows:
            clients.append({
                "channel": "whatsapp",
                "identity": row["wa_user_phone"],
                "display_name": row["wa_user_name"] or None,
                "verified_email": row["verified_email"] or None,
                "message_count": row["message_count"] or 0,
                "last_active": row["last_active"],
            })
        return clients

    def increment_message_count(self, chat_link_id: int) -> None:
        now = utc_now_iso()
        stmt = (
            update(whatsapp_chat_links)
            .where(whatsapp_chat_links.c.id == chat_link_id)
            .values(
                message_count=whatsapp_chat_links.c.message_count + 1,
                last_active=now,
            )
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def set_verified_email(
        self, binding_id: int, wa_user_phone: str, email: str
    ) -> None:
        """Mark this WhatsApp user as verified for the given email (#311 Phase 2).

        Creates the chat_link row if missing — /login can land before any prior
        inbound message has touched the row.
        """
        now = utc_now_iso()
        normalized_email = email.strip().lower()
        stmt = make_insert(whatsapp_chat_links).values(
            binding_id=binding_id,
            wa_user_phone=wa_user_phone,
            verified_email=normalized_email,
            verified_at=now,
            message_count=0,
            created_at=now,
            last_active=now,
        ).on_conflict_do_update(
            index_elements=[
                whatsapp_chat_links.c.binding_id,
                whatsapp_chat_links.c.wa_user_phone,
            ],
            set_={
                "verified_email": normalized_email,
                "verified_at": now,
                "last_active": now,
            },
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def clear_verified_email(self, binding_id: int, wa_user_phone: str) -> None:
        """Clear the verified email binding for this WhatsApp user."""
        stmt = (
            update(whatsapp_chat_links)
            .where(
                and_(
                    whatsapp_chat_links.c.binding_id == binding_id,
                    whatsapp_chat_links.c.wa_user_phone == wa_user_phone,
                )
            )
            .values(verified_email=None, verified_at=None)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def get_chat_link_by_verified_email(
        self, binding_id: int, email: str
    ) -> Optional[dict]:
        """Resolve a verified email → chat_link row (for proactive delivery)."""
        normalized_email = email.strip().lower()
        stmt = (
            select(
                whatsapp_chat_links.c.id,
                whatsapp_chat_links.c.binding_id,
                whatsapp_chat_links.c.wa_user_phone,
                whatsapp_chat_links.c.wa_user_name,
                whatsapp_chat_links.c.session_id,
                whatsapp_chat_links.c.verified_email,
                whatsapp_chat_links.c.verified_at,
                whatsapp_chat_links.c.message_count,
                whatsapp_chat_links.c.last_active,
                whatsapp_chat_links.c.created_at,
            )
            .where(
                and_(
                    whatsapp_chat_links.c.binding_id == binding_id,
                    func.lower(whatsapp_chat_links.c.verified_email)
                    == normalized_email,
                )
            )
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_chat_link(row) if row else None

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
            "messaging_service_sid": row["messaging_service_sid"],
            "display_name": row["display_name"],
            "is_sandbox": bool(row["is_sandbox"]),
            "webhook_secret": row["webhook_secret"],
            "webhook_url": row["webhook_url"],
            "enabled": bool(row["enabled"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _row_to_chat_link(row) -> dict:
        return {
            "id": row["id"],
            "binding_id": row["binding_id"],
            "wa_user_phone": row["wa_user_phone"],
            "wa_user_name": row["wa_user_name"],
            "session_id": row["session_id"],
            "verified_email": row["verified_email"],
            "verified_at": row["verified_at"],
            "message_count": row["message_count"],
            "last_active": row["last_active"],
            "created_at": row["created_at"],
        }
