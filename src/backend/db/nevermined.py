"""
Nevermined payment configuration database operations (NVM-001).

Manages per-agent Nevermined x402 payment configuration and payment audit logs.
NVM_API_KEY is encrypted using the same AES-256-GCM system as subscription tokens.
"""

import uuid
from typing import Optional, List

from sqlalchemy import select, insert, update, delete, func

from .engine import get_engine
from .tables import nevermined_agent_config, nevermined_payment_log
from db_models import NeverminedConfig, NeverminedPaymentLog
from utils.helpers import utc_now_iso


class NeverminedOperations:
    """Database operations for Nevermined payment configuration and logging."""

    def __init__(self, encryption_service=None):
        self._encryption_service = encryption_service

    def _get_encryption_service(self):
        """Get or create the encryption service (lazy loading)."""
        if self._encryption_service is None:
            from services.credential_encryption import get_credential_encryption_service
            self._encryption_service = get_credential_encryption_service()
        return self._encryption_service

    @staticmethod
    def _row_to_config(row) -> NeverminedConfig:
        """Convert a database row to a NeverminedConfig model."""
        row_dict = dict(row) if row else {}
        return NeverminedConfig(
            id=row_dict["id"],
            agent_name=row_dict["agent_name"],
            nvm_environment=row_dict["nvm_environment"],
            nvm_agent_id=row_dict["nvm_agent_id"],
            nvm_plan_id=row_dict["nvm_plan_id"],
            credits_per_request=row_dict["credits_per_request"],
            enabled=bool(row_dict["enabled"]),
            created_at=row_dict["created_at"],
            updated_at=row_dict["updated_at"],
        )

    @staticmethod
    def _row_to_payment_log(row) -> NeverminedPaymentLog:
        """Convert a database row to a NeverminedPaymentLog model."""
        row_dict = dict(row) if row else {}
        return NeverminedPaymentLog(
            id=row_dict["id"],
            agent_name=row_dict["agent_name"],
            execution_id=row_dict.get("execution_id"),
            action=row_dict["action"],
            subscriber_address=row_dict.get("subscriber_address"),
            credits_amount=row_dict.get("credits_amount"),
            tx_hash=row_dict.get("tx_hash"),
            remaining_balance=row_dict.get("remaining_balance"),
            success=bool(row_dict["success"]),
            error=row_dict.get("error"),
            created_at=row_dict["created_at"],
        )

    # =========================================================================
    # Config CRUD
    # =========================================================================

    def create_or_update_config(
        self,
        agent_name: str,
        nvm_api_key: str,
        nvm_environment: str,
        nvm_agent_id: str,
        nvm_plan_id: str,
        credits_per_request: int = 1,
    ) -> NeverminedConfig:
        """Create or update Nevermined config for an agent. Encrypts the API key."""
        encryption_service = self._get_encryption_service()
        encrypted = encryption_service.encrypt({"nvm_api_key": nvm_api_key})
        now = utc_now_iso()

        with get_engine().begin() as conn:
            existing = conn.execute(
                select(nevermined_agent_config.c.id).where(
                    nevermined_agent_config.c.agent_name == agent_name
                )
            ).mappings().first()

            if existing:
                config_id = existing["id"]
                conn.execute(
                    update(nevermined_agent_config)
                    .where(nevermined_agent_config.c.id == config_id)
                    .values(
                        encrypted_credentials=encrypted,
                        nvm_environment=nvm_environment,
                        nvm_agent_id=nvm_agent_id,
                        nvm_plan_id=nvm_plan_id,
                        credits_per_request=credits_per_request,
                        updated_at=now,
                    )
                )
            else:
                config_id = str(uuid.uuid4())
                conn.execute(
                    insert(nevermined_agent_config).values(
                        id=config_id,
                        agent_name=agent_name,
                        encrypted_credentials=encrypted,
                        nvm_environment=nvm_environment,
                        nvm_agent_id=nvm_agent_id,
                        nvm_plan_id=nvm_plan_id,
                        credits_per_request=credits_per_request,
                        enabled=0,
                        created_at=now,
                        updated_at=now,
                    )
                )

            row = conn.execute(
                select(nevermined_agent_config).where(
                    nevermined_agent_config.c.id == config_id
                )
            ).mappings().first()
            return self._row_to_config(row)

    def get_config(self, agent_name: str) -> Optional[NeverminedConfig]:
        """Get Nevermined config for an agent (without decrypted key)."""
        with get_engine().connect() as conn:
            row = conn.execute(
                select(nevermined_agent_config).where(
                    nevermined_agent_config.c.agent_name == agent_name
                )
            ).mappings().first()
            return self._row_to_config(row) if row else None

    def get_config_with_key(self, agent_name: str) -> Optional[dict]:
        """Get config + decrypted NVM_API_KEY. Internal use only."""
        with get_engine().connect() as conn:
            row = conn.execute(
                select(nevermined_agent_config).where(
                    nevermined_agent_config.c.agent_name == agent_name
                )
            ).mappings().first()
            if not row:
                return None

            config = self._row_to_config(row)
            encryption_service = self._get_encryption_service()
            decrypted = encryption_service.decrypt(row["encrypted_credentials"])
            nvm_api_key = decrypted.get("nvm_api_key", "")

            return {
                "config": config,
                "nvm_api_key": nvm_api_key,
            }

    def delete_config(self, agent_name: str) -> bool:
        """Delete Nevermined config for an agent."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(nevermined_agent_config).where(
                    nevermined_agent_config.c.agent_name == agent_name
                )
            )
            return result.rowcount > 0

    def set_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Enable or disable Nevermined payments for an agent."""
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(nevermined_agent_config)
                .where(nevermined_agent_config.c.agent_name == agent_name)
                .values(enabled=1 if enabled else 0, updated_at=now)
            )
            return result.rowcount > 0

    def is_nevermined_enabled(self, agent_name: str) -> bool:
        """Fast check if Nevermined is enabled for an agent."""
        with get_engine().connect() as conn:
            row = conn.execute(
                select(nevermined_agent_config.c.enabled).where(
                    nevermined_agent_config.c.agent_name == agent_name
                )
            ).mappings().first()
            return bool(row["enabled"]) if row else False

    # =========================================================================
    # Payment Log
    # =========================================================================

    def log_payment(
        self,
        agent_name: str,
        action: str,
        success: bool,
        execution_id: Optional[str] = None,
        subscriber_address: Optional[str] = None,
        credits_amount: Optional[int] = None,
        tx_hash: Optional[str] = None,
        remaining_balance: Optional[int] = None,
        error: Optional[str] = None,
    ) -> NeverminedPaymentLog:
        """Log a payment action (verify, settle, settle_failed, reject)."""
        log_id = str(uuid.uuid4())
        now = utc_now_iso()

        with get_engine().begin() as conn:
            conn.execute(
                insert(nevermined_payment_log).values(
                    id=log_id,
                    agent_name=agent_name,
                    execution_id=execution_id,
                    action=action,
                    subscriber_address=subscriber_address,
                    credits_amount=credits_amount,
                    tx_hash=tx_hash,
                    remaining_balance=remaining_balance,
                    success=1 if success else 0,
                    error=error,
                    created_at=now,
                )
            )

            row = conn.execute(
                select(nevermined_payment_log).where(
                    nevermined_payment_log.c.id == log_id
                )
            ).mappings().first()
            return self._row_to_payment_log(row)

    def get_payment_log(
        self,
        agent_name: str,
        limit: int = 50,
    ) -> List[NeverminedPaymentLog]:
        """Get payment log entries for an agent, newest first."""
        with get_engine().connect() as conn:
            rows = conn.execute(
                select(nevermined_payment_log)
                .where(nevermined_payment_log.c.agent_name == agent_name)
                .order_by(nevermined_payment_log.c.created_at.desc())
                .limit(limit)
            ).mappings().all()
            return [self._row_to_payment_log(row) for row in rows]

    def payer_owns_execution(
        self, agent_name: str, execution_id: str, subscriber_address: str
    ) -> bool:
        """Did this payer wallet pay for this execution? (ent#679 T5)

        The no-schema payer→task binding the A2A payment path uses to decide
        whether an anonymous x402 caller may `tasks/get` / `tasks/cancel` a
        task: the settle / settle_failed rows already carry both
        ``execution_id`` and ``subscriber_address``, so the join exists without
        a new column.

        A targeted query rather than a scan of ``get_payment_log``'s newest 50:
        on a busy agent a payer's own row rolls out of that window within
        minutes, and the payer would then lose access to the task it paid for
        — a correctness bug that only appears under load.

        Matching is case-insensitive because an EVM address is hex and the
        facilitator's checksum casing is not guaranteed stable across a verify
        and a settle.
        """
        if not (agent_name and execution_id and subscriber_address):
            return False
        with get_engine().connect() as conn:
            row = conn.execute(
                select(nevermined_payment_log.c.id)
                .where(nevermined_payment_log.c.agent_name == agent_name)
                .where(nevermined_payment_log.c.execution_id == execution_id)
                .where(
                    func.lower(nevermined_payment_log.c.subscriber_address)
                    == subscriber_address.lower()
                )
                .limit(1)
            ).first()
        return row is not None

    def get_settlement_failures(self, limit: int = 50) -> List[NeverminedPaymentLog]:
        """Get all failed settlements across all agents (admin view)."""
        with get_engine().connect() as conn:
            rows = conn.execute(
                select(nevermined_payment_log)
                .where(nevermined_payment_log.c.action == "settle_failed")
                .order_by(nevermined_payment_log.c.created_at.desc())
                .limit(limit)
            ).mappings().all()
            return [self._row_to_payment_log(row) for row in rows]

    def get_payment_log_entry(self, log_id: str) -> Optional[NeverminedPaymentLog]:
        """Get a single payment log entry by ID."""
        with get_engine().connect() as conn:
            row = conn.execute(
                select(nevermined_payment_log).where(
                    nevermined_payment_log.c.id == log_id
                )
            ).mappings().first()
            return self._row_to_payment_log(row) if row else None
