"""
Nevermined card-delegation database operations (trinity-enterprise#966).

Two tables, beside the provider-side rail in ``db/nevermined.py``:

* ``nevermined_organization`` — the instance's one Nevermined organization
  (singleton row ``id = 'default'``). The widget secret and the agent API key
  live in ONE AES-256-GCM envelope (Invariant #12) and are only ever returned
  by ``get_organization_secrets`` — never by a route.
* ``agent_delegations`` — the delegation id a person bound to an agent.
  Nevermined holds the card and the mandate; this row records only the id.

"Active" means ``revoked_at IS NULL AND agent_deleted_at IS NULL`` — the same
predicate as the partial unique index — and every count also joins the live
``agent_ownership`` row, so a deleted agent's kept row never blocks anything.
"""

from typing import Dict, List, Optional

from sqlalchemy import delete, func, insert, select, update

from .engine import get_engine
from .tables import agent_delegations, agent_ownership, nevermined_organization
from utils.helpers import utc_now_iso

ORGANIZATION_ROW_ID = "default"


class NeverminedDelegationOperations:
    """Database operations for the Nevermined organization and delegation bindings."""

    def __init__(self, encryption_service=None):
        self._encryption_service = encryption_service

    def _get_encryption_service(self):
        if self._encryption_service is None:
            from services.credential_encryption import get_credential_encryption_service
            self._encryption_service = get_credential_encryption_service()
        return self._encryption_service

    # -- organization -------------------------------------------------------

    @staticmethod
    def _public_organization(row) -> Dict:
        return {
            "org_id": row["org_id"],
            "environment": row["environment"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    def get_organization(self) -> Optional[Dict]:
        """The organization row WITHOUT its secrets, or None when not configured."""
        with get_engine().connect() as conn:
            row = conn.execute(
                select(nevermined_organization).where(
                    nevermined_organization.c.id == ORGANIZATION_ROW_ID
                )
            ).mappings().first()
        return self._public_organization(row) if row else None

    def get_organization_secrets(self) -> Optional[Dict[str, str]]:
        """The decrypted envelope ``{"widget_secret", "agent_api_key"}``.

        For the service's upstream calls only; never serialise the result.
        """
        with get_engine().connect() as conn:
            encrypted = conn.execute(
                select(nevermined_organization.c.encrypted_credentials).where(
                    nevermined_organization.c.id == ORGANIZATION_ROW_ID
                )
            ).scalar()
        if not encrypted:
            return None
        return self._get_encryption_service().decrypt(encrypted)

    def save_organization(
        self,
        org_id: str,
        environment: str,
        secrets: Dict[str, str],
        updated_by: Optional[str],
    ) -> Dict:
        """Create or replace the singleton row; ``secrets`` is the FULL envelope."""
        encrypted = self._get_encryption_service().encrypt(dict(secrets))
        now = utc_now_iso()
        with get_engine().begin() as conn:
            exists = conn.execute(
                select(nevermined_organization.c.id).where(
                    nevermined_organization.c.id == ORGANIZATION_ROW_ID
                )
            ).scalar()
            values = dict(
                org_id=org_id,
                environment=environment,
                encrypted_credentials=encrypted,
                updated_at=now,
                updated_by=updated_by,
            )
            if exists:
                conn.execute(
                    update(nevermined_organization)
                    .where(nevermined_organization.c.id == ORGANIZATION_ROW_ID)
                    .values(**values)
                )
            else:
                conn.execute(
                    insert(nevermined_organization).values(
                        id=ORGANIZATION_ROW_ID, created_at=now, **values
                    )
                )
        return self.get_organization()

    def delete_organization(self) -> bool:
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(nevermined_organization).where(
                    nevermined_organization.c.id == ORGANIZATION_ROW_ID
                )
            )
        return result.rowcount > 0

    # -- delegations --------------------------------------------------------

    def count_active_delegations(self) -> int:
        """Active bindings held by agents that still exist."""
        with get_engine().connect() as conn:
            return int(
                conn.execute(
                    select(func.count())
                    .select_from(
                        agent_delegations.join(
                            agent_ownership,
                            agent_ownership.c.agent_name == agent_delegations.c.agent_name,
                        )
                    )
                    .where(
                        agent_delegations.c.revoked_at.is_(None),
                        agent_delegations.c.agent_deleted_at.is_(None),
                    )
                ).scalar()
                or 0
            )

    #: The "active" predicate — the partial unique index's, verbatim.
    @staticmethod
    def _active_clause():
        return (
            agent_delegations.c.revoked_at.is_(None),
            agent_delegations.c.agent_deleted_at.is_(None),
        )

    @staticmethod
    def _row(mapping) -> Dict:
        return {
            "id": mapping["id"],
            "agent_name": mapping["agent_name"],
            "delegation_id": mapping["delegation_id"],
            "payment_method_id": mapping["payment_method_id"],
            "provider": mapping["provider"],
            "environment": mapping["environment"],
            "api_key_injected": bool(mapping["api_key_injected"]),
            "created_by": mapping["created_by"],
            "created_at": mapping["created_at"],
            "revoked_at": mapping["revoked_at"],
            "revoked_by": mapping["revoked_by"],
            "agent_deleted_at": mapping["agent_deleted_at"],
        }

    def get_active_delegation(self, agent_name: str) -> Optional[Dict]:
        """The agent's one active binding (same predicate as the index, joined
        to the live ownership row), or None."""
        with get_engine().connect() as conn:
            row = conn.execute(
                select(agent_delegations)
                .select_from(
                    agent_delegations.join(
                        agent_ownership,
                        agent_ownership.c.agent_name == agent_delegations.c.agent_name,
                    )
                )
                .where(agent_delegations.c.agent_name == agent_name, *self._active_clause())
                .order_by(agent_delegations.c.created_at.desc())
                .limit(1)
            ).mappings().first()
        return self._row(row) if row else None

    def list_delegation_history(self, agent_name: str, limit: int = 10) -> List[Dict]:
        """Most recent bindings first — the GET's `history` block."""
        with get_engine().connect() as conn:
            rows = conn.execute(
                select(
                    agent_delegations.c.delegation_id,
                    agent_delegations.c.created_at,
                    agent_delegations.c.revoked_at,
                )
                .where(agent_delegations.c.agent_name == agent_name)
                .order_by(agent_delegations.c.created_at.desc(), agent_delegations.c.id.desc())
                .limit(limit)
            ).mappings().all()
        return [
            {"delegation_id": r["delegation_id"], "created_at": r["created_at"], "revoked_at": r["revoked_at"]}
            for r in rows
        ]
