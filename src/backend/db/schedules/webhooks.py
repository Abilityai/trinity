"""Schedule webhook token + HMAC signature-secret management (WEBHOOK-001, ent#77)."""

import logging
import secrets
from typing import Optional, Dict

from sqlalchemy import select, update, and_

from ..engine import get_engine
from ..tables import (
    agent_schedules,
    agent_ownership,
)
from db_models import Schedule
from utils.helpers import utc_now_iso

logger = logging.getLogger("db.schedules")

class ScheduleWebhooksMixin:
    """Webhook token / signature-secret operations."""

    # =========================================================================
    # Webhook Management (WEBHOOK-001, #291)
    # =========================================================================

    def generate_webhook_token(self, schedule_id: str) -> Optional[str]:
        """Generate (or regenerate) a webhook token for a schedule.

        Creates a 32-byte URL-safe random token stored in the DB. Calling
        again replaces the old token, immediately invalidating the old URL.
        """
        token = secrets.token_urlsafe(32)
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.id == schedule_id)
                # ent#77: rotating the URL is a credential-rotation event — reset
                # any prior signing secret so a leaked old secret can't sign for
                # the new token. The caller re-enables signing explicitly.
                .values(
                    webhook_token=token,
                    webhook_enabled=1,
                    webhook_secret_encrypted=None,
                    webhook_auth_enabled=0,
                    updated_at=now,
                )
            )
            if result.rowcount == 0:
                return None
        return token

    def get_schedule_by_webhook_token(self, token: str) -> Optional[Schedule]:
        """Look up a schedule by its webhook token (O(1) via index).

        Applies the same two soft-delete filters as `list_all_enabled_schedules`
        (#834, #1423): skip a soft-deleted *schedule* (`agent_schedules.deleted_at`)
        AND a schedule whose *agent* is soft-deleted (`agent_ownership.deleted_at`).
        Without the agent join, a webhook could still fire a schedule of a
        soft-deleted (recoverable) agent during the retention window — the exact
        hole the cron path guards against.
        """
        stmt = (
            select(agent_schedules)
            .select_from(
                agent_schedules.join(
                    agent_ownership,
                    agent_ownership.c.agent_name == agent_schedules.c.agent_name,
                )
            )
            .where(
                and_(
                    agent_schedules.c.webhook_token == token,
                    agent_schedules.c.deleted_at.is_(None),
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        return self._row_to_schedule(row)

    def set_webhook_enabled(self, schedule_id: str, enabled: bool) -> bool:
        """Enable or disable webhook triggering for a schedule."""
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.id == schedule_id)
                .values(webhook_enabled=1 if enabled else 0, updated_at=now)
            )
            return result.rowcount > 0

    def revoke_webhook_token(self, schedule_id: str) -> bool:
        """Revoke a webhook token, immediately invalidating the URL.

        ent#77: also clears the signing secret + auth flag — a revoked webhook
        must not leave a live secret behind, and a re-minted token should start
        auth-off (the caller re-enables signing explicitly).
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.id == schedule_id)
                .values(
                    webhook_token=None,
                    webhook_enabled=0,
                    webhook_secret_encrypted=None,
                    webhook_auth_enabled=0,
                    updated_at=now,
                )
            )
            return result.rowcount > 0

    # ---- ent#77: webhook signature-auth secret --------------------------------

    @staticmethod
    def _encrypt_webhook_secret(secret: str) -> str:
        from services.credential_encryption import get_credential_encryption_service
        from services.webhook_signature import SECRET_ENVELOPE_KEY
        return get_credential_encryption_service().encrypt({SECRET_ENVELOPE_KEY: secret})

    @staticmethod
    def _decrypt_webhook_secret(encrypted: Optional[str]) -> Optional[str]:
        if not encrypted:
            return None
        try:
            from services.credential_encryption import get_credential_encryption_service
            from services.webhook_signature import SECRET_ENVELOPE_KEY
            return get_credential_encryption_service().decrypt(encrypted).get(SECRET_ENVELOPE_KEY)
        except Exception as e:
            logger.error(f"Failed to decrypt webhook secret: {e}")
            return None

    def set_webhook_secret(self, schedule_id: str) -> Optional[str]:
        """Mint (or rotate) the HMAC signing secret and enable signature auth.

        Returns the PLAINTEXT secret exactly once (the caller surfaces it to the
        user and never persists it in the clear); only the AES-256-GCM envelope
        is stored. Returns None if the schedule row is gone. Requires an existing
        webhook token — signature auth on a schedule with no webhook is a no-op,
        so the router gates on `has_token` first.
        """
        secret = "whsec_" + secrets.token_urlsafe(32)
        encrypted = self._encrypt_webhook_secret(secret)
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(
                    and_(
                        agent_schedules.c.id == schedule_id,
                        agent_schedules.c.webhook_token.isnot(None),
                    )
                )
                .values(
                    webhook_secret_encrypted=encrypted,
                    webhook_auth_enabled=1,
                    updated_at=now,
                )
            )
            if result.rowcount == 0:
                return None
        return secret

    def clear_webhook_secret(self, schedule_id: str) -> bool:
        """Disable signature auth and drop the stored secret (webhook stays live)."""
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.id == schedule_id)
                .values(
                    webhook_secret_encrypted=None,
                    webhook_auth_enabled=0,
                    updated_at=now,
                )
            )
            return result.rowcount > 0

    def get_webhook_status(self, schedule_id: str) -> Optional[Dict]:
        """Return webhook configuration for a schedule (never the secret)."""
        stmt = select(
            agent_schedules.c.webhook_token,
            agent_schedules.c.webhook_enabled,
            agent_schedules.c.webhook_auth_enabled,
            agent_schedules.c.webhook_secret_encrypted,
        ).where(
            and_(
                agent_schedules.c.id == schedule_id,
                agent_schedules.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        return {
            "webhook_token": row["webhook_token"],
            "webhook_enabled": bool(row["webhook_enabled"]),
            "has_token": row["webhook_token"] is not None,
            # ent#77 — surface the auth STATE only, never the secret material
            "auth_enabled": bool(row["webhook_auth_enabled"]),
            "has_secret": row["webhook_secret_encrypted"] is not None,
        }
