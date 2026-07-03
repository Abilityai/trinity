"""
Database operations for Telegram bot bindings and chat tracking.

Handles:
- Bot bindings (one bot token per agent, encrypted)
- Chat link tracking (Telegram user → session mapping)

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the Core table handles in
``db/tables.py`` (dialect-agnostic, no ``?`` placeholders); the engine is
resolved via ``db/engine.py``. Public API is unchanged.
"""

import logging
import secrets
from typing import Optional, List

from sqlalchemy import select, insert, update, delete, and_

from .engine import get_engine, make_insert
from .tables import (
    telegram_bindings,
    telegram_chat_links,
    telegram_group_configs,
)
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


class TelegramChannelOperations:
    """Operations for Telegram bot bindings and chat links."""

    # =========================================================================
    # Encryption helpers (same pattern as SlackChannelOperations)
    # =========================================================================

    def _get_encryption_service(self):
        """Lazy-load encryption service."""
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
            logger.error(f"Failed to decrypt Telegram bot token: {e}")
            return None

    # =========================================================================
    # Binding Operations
    # =========================================================================

    def create_binding(
        self,
        agent_name: str,
        bot_token: str,
        bot_username: Optional[str] = None,
        bot_id: Optional[str] = None,
    ) -> dict:
        """Create or update a Telegram bot binding for an agent."""
        webhook_secret = secrets.token_urlsafe(32)
        telegram_secret_token = secrets.token_urlsafe(32)
        now = utc_now_iso()
        encrypted_token = self._encrypt_token(bot_token)

        stmt = make_insert(telegram_bindings).values(
            agent_name=agent_name,
            bot_token_encrypted=encrypted_token,
            bot_username=bot_username,
            bot_id=bot_id,
            webhook_secret=webhook_secret,
            telegram_secret_token=telegram_secret_token,
            created_at=now,
            updated_at=now,
        ).on_conflict_do_update(
            index_elements=[telegram_bindings.c.agent_name],
            set_={
                "bot_token_encrypted": encrypted_token,
                "bot_username": bot_username,
                "bot_id": bot_id,
                "webhook_secret": webhook_secret,
                "telegram_secret_token": telegram_secret_token,
                "updated_at": now,
            },
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return self.get_binding_by_agent(agent_name)

    _BINDING_COLUMNS = (
        telegram_bindings.c.id,
        telegram_bindings.c.agent_name,
        telegram_bindings.c.bot_token_encrypted,
        telegram_bindings.c.bot_username,
        telegram_bindings.c.bot_id,
        telegram_bindings.c.webhook_secret,
        telegram_bindings.c.webhook_url,
        telegram_bindings.c.telegram_secret_token,
        telegram_bindings.c.last_update_id,
        telegram_bindings.c.created_at,
        telegram_bindings.c.updated_at,
    )

    def get_binding_by_agent(self, agent_name: str) -> Optional[dict]:
        """Get Telegram binding for an agent. Bot token is NOT decrypted."""
        stmt = select(*self._BINDING_COLUMNS).where(
            telegram_bindings.c.agent_name == agent_name
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None
        return self._row_to_binding(row)

    def get_binding_by_bot_id(self, bot_id: str) -> Optional[dict]:
        """Resolve bot_id → agent binding (for incoming updates)."""
        stmt = select(*self._BINDING_COLUMNS).where(
            telegram_bindings.c.bot_id == bot_id
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None
        return self._row_to_binding(row)

    def get_binding_by_webhook_secret(self, webhook_secret: str) -> Optional[dict]:
        """Resolve webhook_secret → agent binding (for routing incoming webhooks)."""
        stmt = select(*self._BINDING_COLUMNS).where(
            telegram_bindings.c.webhook_secret == webhook_secret
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None
        return self._row_to_binding(row)

    def get_decrypted_bot_token(self, agent_name: str) -> Optional[str]:
        """Get decrypted bot token for an agent."""
        binding = self.get_binding_by_agent(agent_name)
        if not binding:
            return None
        return self._decrypt_token(binding["bot_token_encrypted"])

    def get_all_bindings(self) -> List[dict]:
        """Get all Telegram bindings (for webhook reconciliation on startup)."""
        stmt = select(*self._BINDING_COLUMNS)
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        return [self._row_to_binding(row) for row in rows]

    def update_webhook_url(self, agent_name: str, webhook_url: str) -> None:
        """Update the registered webhook URL for a binding."""
        now = utc_now_iso()
        stmt = (
            update(telegram_bindings)
            .where(telegram_bindings.c.agent_name == agent_name)
            .values(webhook_url=webhook_url, updated_at=now)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def update_last_update_id(self, agent_name: str, update_id: int) -> None:
        """Update the last processed update_id for dedup."""
        stmt = (
            update(telegram_bindings)
            .where(telegram_bindings.c.agent_name == agent_name)
            .values(last_update_id=update_id)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def delete_binding(self, agent_name: str) -> bool:
        """Delete a Telegram binding and associated chat links and group configs."""
        binding_id_subq = (
            select(telegram_bindings.c.id)
            .where(telegram_bindings.c.agent_name == agent_name)
            .scalar_subquery()
        )
        with get_engine().begin() as conn:
            # Delete group configs first
            conn.execute(
                delete(telegram_group_configs).where(
                    telegram_group_configs.c.binding_id.in_(binding_id_subq)
                )
            )
            # Delete chat links
            conn.execute(
                delete(telegram_chat_links).where(
                    telegram_chat_links.c.binding_id.in_(binding_id_subq)
                )
            )
            result = conn.execute(
                delete(telegram_bindings).where(
                    telegram_bindings.c.agent_name == agent_name
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
        telegram_user_id: str,
        telegram_username: Optional[str] = None,
    ) -> dict:
        """Get or create a chat link for a Telegram user."""
        select_stmt = select(
            telegram_chat_links.c.id,
            telegram_chat_links.c.binding_id,
            telegram_chat_links.c.telegram_user_id,
            telegram_chat_links.c.telegram_username,
            telegram_chat_links.c.session_id,
            telegram_chat_links.c.message_count,
            telegram_chat_links.c.created_at,
            telegram_chat_links.c.last_active,
        ).where(
            and_(
                telegram_chat_links.c.binding_id == binding_id,
                telegram_chat_links.c.telegram_user_id == telegram_user_id,
            )
        )
        with get_engine().begin() as conn:
            row = conn.execute(select_stmt).mappings().first()

            if row:
                return self._row_to_chat_link(row)

            now = utc_now_iso()
            conn.execute(
                insert(telegram_chat_links).values(
                    binding_id=binding_id,
                    telegram_user_id=telegram_user_id,
                    telegram_username=telegram_username,
                    message_count=0,
                    created_at=now,
                    last_active=now,
                )
            )

            row = conn.execute(select_stmt).mappings().first()
            return self._row_to_chat_link(row)

    def get_chat_link(self, binding_id: int, telegram_user_id: str) -> Optional[dict]:
        """Look up a chat link without creating it."""
        stmt = select(
            telegram_chat_links.c.id,
            telegram_chat_links.c.binding_id,
            telegram_chat_links.c.telegram_user_id,
            telegram_chat_links.c.telegram_username,
            telegram_chat_links.c.session_id,
            telegram_chat_links.c.message_count,
            telegram_chat_links.c.created_at,
            telegram_chat_links.c.last_active,
            telegram_chat_links.c.verified_email,
            telegram_chat_links.c.verified_at,
        ).where(
            and_(
                telegram_chat_links.c.binding_id == binding_id,
                telegram_chat_links.c.telegram_user_id == telegram_user_id,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        return {
            "id": row["id"],
            "binding_id": row["binding_id"],
            "telegram_user_id": row["telegram_user_id"],
            "telegram_username": row["telegram_username"],
            "session_id": row["session_id"],
            "message_count": row["message_count"],
            "created_at": row["created_at"],
            "last_active": row["last_active"],
            "verified_email": row["verified_email"],
            "verified_at": row["verified_at"],
        }

    def get_verified_email(self, binding_id: int, telegram_user_id: str) -> Optional[str]:
        """Return the verified email for this Telegram user, or None."""
        link = self.get_chat_link(binding_id, telegram_user_id)
        return link["verified_email"] if link else None

    def list_clients_for_agent(self, agent_name: str) -> List[dict]:
        """External Telegram clients who have messaged this agent (#20 roster).

        Joins the agent's binding to its chat links. Returns normalized roster
        rows; ``identity`` is the @username when known, else the numeric user id.
        """
        stmt = (
            select(
                telegram_chat_links.c.telegram_user_id,
                telegram_chat_links.c.telegram_username,
                telegram_chat_links.c.verified_email,
                telegram_chat_links.c.message_count,
                telegram_chat_links.c.last_active,
            )
            .select_from(
                telegram_chat_links.join(
                    telegram_bindings,
                    telegram_chat_links.c.binding_id == telegram_bindings.c.id,
                )
            )
            .where(telegram_bindings.c.agent_name == agent_name)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        clients = []
        for row in rows:
            username = row["telegram_username"]
            identity = f"@{username}" if username else str(row["telegram_user_id"])
            clients.append({
                "channel": "telegram",
                "identity": identity,
                "display_name": username or None,
                "verified_email": row["verified_email"] or None,
                "message_count": row["message_count"] or 0,
                "last_active": row["last_active"],
            })
        return clients

    def set_verified_email(
        self,
        binding_id: int,
        telegram_user_id: str,
        email: str,
    ) -> bool:
        """Persist a verified email onto the chat link (auto-creates the row)."""
        now = utc_now_iso()
        email = email.lower()
        with get_engine().begin() as conn:
            # Ensure row exists
            conn.execute(
                make_insert(telegram_chat_links).values(
                    binding_id=binding_id,
                    telegram_user_id=telegram_user_id,
                    message_count=0,
                    created_at=now,
                    last_active=now,
                ).on_conflict_do_nothing(
                    index_elements=[
                        telegram_chat_links.c.binding_id,
                        telegram_chat_links.c.telegram_user_id,
                    ]
                )
            )
            result = conn.execute(
                update(telegram_chat_links)
                .where(
                    and_(
                        telegram_chat_links.c.binding_id == binding_id,
                        telegram_chat_links.c.telegram_user_id == telegram_user_id,
                    )
                )
                .values(verified_email=email, verified_at=now)
            )
            return result.rowcount > 0

    def clear_verified_email(self, binding_id: int, telegram_user_id: str) -> bool:
        """Unbind a verified email (logout)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(telegram_chat_links)
                .where(
                    and_(
                        telegram_chat_links.c.binding_id == binding_id,
                        telegram_chat_links.c.telegram_user_id == telegram_user_id,
                    )
                )
                .values(verified_email=None, verified_at=None)
            )
            return result.rowcount > 0

    def get_chat_link_by_verified_email(
        self, binding_id: int, email: str
    ) -> Optional[dict]:
        """Reverse lookup: find a chat link by verified email for proactive messaging (#321).

        Returns the chat link for a user who has verified with this email,
        or None if no such user exists for this binding.
        """
        stmt = (
            select(
                telegram_chat_links.c.id,
                telegram_chat_links.c.binding_id,
                telegram_chat_links.c.telegram_user_id,
                telegram_chat_links.c.telegram_username,
                telegram_chat_links.c.session_id,
                telegram_chat_links.c.message_count,
                telegram_chat_links.c.created_at,
                telegram_chat_links.c.last_active,
                telegram_chat_links.c.verified_email,
                telegram_chat_links.c.verified_at,
            )
            .where(
                and_(
                    telegram_chat_links.c.binding_id == binding_id,
                    telegram_chat_links.c.verified_email == email.lower(),
                )
            )
            .order_by(telegram_chat_links.c.last_active.desc())
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        return {
            "id": row["id"],
            "binding_id": row["binding_id"],
            "telegram_user_id": row["telegram_user_id"],
            "telegram_username": row["telegram_username"],
            "session_id": row["session_id"],
            "message_count": row["message_count"],
            "created_at": row["created_at"],
            "last_active": row["last_active"],
            "verified_email": row["verified_email"],
            "verified_at": row["verified_at"],
        }

    def increment_message_count(self, chat_link_id: int) -> None:
        """Increment message count and update last_active."""
        now = utc_now_iso()
        stmt = (
            update(telegram_chat_links)
            .where(telegram_chat_links.c.id == chat_link_id)
            .values(
                message_count=telegram_chat_links.c.message_count + 1,
                last_active=now,
            )
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    # =========================================================================
    # Group Config Operations (TGRAM-GROUP)
    # =========================================================================

    _GROUP_CONFIG_COLUMNS = (
        telegram_group_configs.c.id,
        telegram_group_configs.c.binding_id,
        telegram_group_configs.c.chat_id,
        telegram_group_configs.c.chat_title,
        telegram_group_configs.c.chat_type,
        telegram_group_configs.c.trigger_mode,
        telegram_group_configs.c.welcome_enabled,
        telegram_group_configs.c.welcome_text,
        telegram_group_configs.c.is_active,
        telegram_group_configs.c.created_at,
        telegram_group_configs.c.updated_at,
        telegram_group_configs.c.verified_by_email,
        telegram_group_configs.c.verified_at,
    )

    def get_or_create_group_config(
        self,
        binding_id: int,
        chat_id: str,
        chat_title: Optional[str] = None,
        chat_type: str = "group",
    ) -> dict:
        """Get or create a group config. Auto-creates on first interaction."""
        now = utc_now_iso()
        select_stmt = select(*self._GROUP_CONFIG_COLUMNS).where(
            and_(
                telegram_group_configs.c.binding_id == binding_id,
                telegram_group_configs.c.chat_id == chat_id,
            )
        )
        with get_engine().begin() as conn:
            row = conn.execute(select_stmt).mappings().first()

            if row:
                # Re-activate if inactive (bot removed then re-added) and update title
                needs_update = False
                if chat_title and chat_title != row["chat_title"]:
                    needs_update = True
                if not row["is_active"]:  # is_active == 0
                    needs_update = True

                if needs_update:
                    conn.execute(
                        update(telegram_group_configs)
                        .where(telegram_group_configs.c.id == row["id"])
                        .values(
                            chat_title=chat_title or row["chat_title"],
                            is_active=1,
                            updated_at=now,
                        )
                    )
                    return {**self._row_to_group_config(row),
                            "chat_title": chat_title or row["chat_title"],
                            "is_active": True}
                return self._row_to_group_config(row)

            conn.execute(
                insert(telegram_group_configs).values(
                    binding_id=binding_id,
                    chat_id=chat_id,
                    chat_title=chat_title,
                    chat_type=chat_type,
                    trigger_mode="mention",
                    welcome_enabled=0,
                    is_active=1,
                    created_at=now,
                    updated_at=now,
                )
            )

            row = conn.execute(select_stmt).mappings().first()
            return self._row_to_group_config(row)

    def get_group_config(self, binding_id: int, chat_id: str) -> Optional[dict]:
        """Get group config for a specific chat."""
        stmt = select(*self._GROUP_CONFIG_COLUMNS).where(
            and_(
                telegram_group_configs.c.binding_id == binding_id,
                telegram_group_configs.c.chat_id == chat_id,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_group_config(row) if row else None

    def get_groups_for_binding(self, binding_id: int) -> List[dict]:
        """Get all group configs for a bot binding."""
        stmt = (
            select(*self._GROUP_CONFIG_COLUMNS)
            .where(
                and_(
                    telegram_group_configs.c.binding_id == binding_id,
                    telegram_group_configs.c.is_active == 1,
                )
            )
            .order_by(telegram_group_configs.c.chat_title)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_group_config(row) for row in rows]

    def get_groups_for_agent(self, agent_name: str) -> List[dict]:
        """Get all group configs for an agent (via binding lookup)."""
        binding = self.get_binding_by_agent(agent_name)
        if not binding:
            return []
        return self.get_groups_for_binding(binding["id"])

    def update_group_config(
        self,
        group_config_id: int,
        trigger_mode: Optional[str] = None,
        welcome_enabled: Optional[bool] = None,
        welcome_text: Optional[str] = None,
    ) -> Optional[dict]:
        """Update group config settings."""
        now = utc_now_iso()
        values = {"updated_at": now}

        if trigger_mode is not None:
            values["trigger_mode"] = trigger_mode
        if welcome_enabled is not None:
            values["welcome_enabled"] = 1 if welcome_enabled else 0
        if welcome_text is not None:
            values["welcome_text"] = welcome_text

        select_stmt = select(*self._GROUP_CONFIG_COLUMNS).where(
            telegram_group_configs.c.id == group_config_id
        )
        with get_engine().begin() as conn:
            conn.execute(
                update(telegram_group_configs)
                .where(telegram_group_configs.c.id == group_config_id)
                .values(**values)
            )

            row = conn.execute(select_stmt).mappings().first()
        return self._row_to_group_config(row) if row else None

    def deactivate_group_config(self, binding_id: int, chat_id: str) -> bool:
        """Mark a group config as inactive (bot removed from group)."""
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(telegram_group_configs)
                .where(
                    and_(
                        telegram_group_configs.c.binding_id == binding_id,
                        telegram_group_configs.c.chat_id == chat_id,
                    )
                )
                .values(is_active=0, updated_at=now)
            )
            deleted = result.rowcount > 0
        return deleted

    def delete_groups_for_binding(self, binding_id: int) -> int:
        """Delete all group configs for a binding (when bot is disconnected)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(telegram_group_configs).where(
                    telegram_group_configs.c.binding_id == binding_id
                )
            )
            count = result.rowcount
        return count

    # =========================================================================
    # Group Verification (group_auth_mode support)
    # =========================================================================

    def is_group_verified(self, binding_id: int, chat_id: str) -> bool:
        """Check if a group has at least one verified member."""
        stmt = select(telegram_group_configs.c.verified_by_email).where(
            and_(
                telegram_group_configs.c.binding_id == binding_id,
                telegram_group_configs.c.chat_id == chat_id,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return bool(row and row["verified_by_email"])

    def get_group_verified_email(self, binding_id: int, chat_id: str) -> Optional[str]:
        """Get the email that verified this group, if any."""
        stmt = select(telegram_group_configs.c.verified_by_email).where(
            and_(
                telegram_group_configs.c.binding_id == binding_id,
                telegram_group_configs.c.chat_id == chat_id,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return row["verified_by_email"] if row else None

    def set_group_verified(self, binding_id: int, chat_id: str, email: str) -> bool:
        """Mark a group as verified by the given email."""
        now = utc_now_iso()
        stmt = (
            update(telegram_group_configs)
            .where(
                and_(
                    telegram_group_configs.c.binding_id == binding_id,
                    telegram_group_configs.c.chat_id == chat_id,
                )
            )
            .values(verified_by_email=email.lower(), verified_at=now, updated_at=now)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def clear_group_verification(self, binding_id: int, chat_id: str) -> bool:
        """Clear verification status for a group (e.g., when verified user leaves)."""
        now = utc_now_iso()
        stmt = (
            update(telegram_group_configs)
            .where(
                and_(
                    telegram_group_configs.c.binding_id == binding_id,
                    telegram_group_configs.c.chat_id == chat_id,
                )
            )
            .values(verified_by_email=None, verified_at=None, updated_at=now)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    # =========================================================================
    # Row converters
    # =========================================================================

    def _row_to_group_config(self, row) -> dict:
        return {
            "id": row["id"],
            "binding_id": row["binding_id"],
            "chat_id": row["chat_id"],
            "chat_title": row["chat_title"],
            "chat_type": row["chat_type"],
            "trigger_mode": row["trigger_mode"],
            "welcome_enabled": bool(row["welcome_enabled"]),
            "welcome_text": row["welcome_text"],
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "verified_by_email": row["verified_by_email"],
            "verified_at": row["verified_at"],
        }

    def _row_to_binding(self, row) -> dict:
        return {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "bot_token_encrypted": row["bot_token_encrypted"],
            "bot_username": row["bot_username"],
            "bot_id": row["bot_id"],
            "webhook_secret": row["webhook_secret"],
            "webhook_url": row["webhook_url"],
            "telegram_secret_token": row["telegram_secret_token"],
            "last_update_id": row["last_update_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_chat_link(self, row) -> dict:
        return {
            "id": row["id"],
            "binding_id": row["binding_id"],
            "telegram_user_id": row["telegram_user_id"],
            "telegram_username": row["telegram_username"],
            "session_id": row["session_id"],
            "message_count": row["message_count"],
            "created_at": row["created_at"],
            "last_active": row["last_active"],
        }
