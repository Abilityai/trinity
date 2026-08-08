"""Data-access for the per-agent MCP connector (ent#46; OSS-core since #118).

Owns the ``enterprise_connectors`` config row (one per agent: enabled + the
exposed-playbook allow-list), and mints/reads/revokes the scoped connector key as
a row in the OSS ``mcp_api_keys`` table with ``scope='connector'``. Key generation
+ hashing reuse ``McpKeyOperations`` so ``validate_mcp_api_key`` recognizes the key
unchanged.

Relocated verbatim from the private ``enterprise/backend/mcp_connector/db.py``
when the connector became OSS-core (#118). The table keeps its original
``enterprise_connectors`` name so existing enterprise installs adopt their data
with zero migration (see #118). SQLAlchemy Core so it runs on SQLite + PostgreSQL.
"""
from __future__ import annotations

import json
from typing import List, Optional

from sqlalchemy import select, insert, update, delete

from .engine import get_engine
from .tables import mcp_api_keys, enterprise_connectors
from .mcp_keys import McpKeyOperations
from utils.helpers import utc_now_iso

_SCOPE = "connector"


def _decode_playbooks(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else None
    except (ValueError, TypeError):
        return None


class ConnectorOperations:
    """Config CRUD (enterprise_connectors) + scoped-key mint/revoke (mcp_api_keys)."""

    # ---- Connector config (enterprise_connectors) -------------------------

    def get_config(self, agent_name: str) -> Optional[dict]:
        """Return ``{enabled, exposed_playbooks, created_at, updated_at}`` or None."""
        stmt = select(
            enterprise_connectors.c.enabled,
            enterprise_connectors.c.exposed_playbooks,
            enterprise_connectors.c.created_at,
            enterprise_connectors.c.updated_at,
        ).where(enterprise_connectors.c.agent_name == agent_name)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        return {
            "enabled": bool(row["enabled"]),
            "exposed_playbooks": _decode_playbooks(row["exposed_playbooks"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_enabled_agents(self) -> List[str]:
        """Agent names whose connector config is enabled (#848).

        The candidate pool for MCP inline email auth: only a connector-enabled
        agent is reachable from an MCP client at all, so resolving what a
        verified email may reach starts here and is then filtered per-agent by
        ``email_has_agent_access``. Enumerating this (small) set rather than the
        whole fleet keeps the resolve bounded regardless of fleet size.
        """
        stmt = select(enterprise_connectors.c.agent_name).where(
            enterprise_connectors.c.enabled == 1
        ).order_by(enterprise_connectors.c.agent_name)
        with get_engine().connect() as conn:
            return [row[0] for row in conn.execute(stmt)]

    def upsert_config(
        self,
        agent_name: str,
        enabled: Optional[bool] = None,
        exposed_playbooks: Optional[List[str]] = None,
        *,
        clear_playbooks: bool = False,
    ) -> dict:
        """Create or update the connector config; only provided fields change."""
        now = utc_now_iso()
        encoded = (
            None
            if clear_playbooks or exposed_playbooks is None
            else json.dumps(list(exposed_playbooks))
        )
        with get_engine().begin() as conn:
            existing = conn.execute(
                select(
                    enterprise_connectors.c.enabled,
                    enterprise_connectors.c.exposed_playbooks,
                    enterprise_connectors.c.created_at,
                ).where(enterprise_connectors.c.agent_name == agent_name)
            ).mappings().first()

            if existing:
                new_enabled = enabled if enabled is not None else bool(existing["enabled"])
                if clear_playbooks:
                    new_playbooks = None
                elif exposed_playbooks is not None:
                    new_playbooks = encoded
                else:
                    new_playbooks = existing["exposed_playbooks"]
                conn.execute(
                    update(enterprise_connectors)
                    .where(enterprise_connectors.c.agent_name == agent_name)
                    .values(
                        enabled=1 if new_enabled else 0,
                        exposed_playbooks=new_playbooks,
                        updated_at=now,
                    )
                )
                created_at = existing["created_at"]
            else:
                new_enabled = bool(enabled)
                new_playbooks = None if clear_playbooks else encoded
                conn.execute(
                    insert(enterprise_connectors).values(
                        agent_name=agent_name,
                        enabled=1 if new_enabled else 0,
                        exposed_playbooks=new_playbooks,
                        created_at=now,
                        updated_at=now,
                    )
                )
                created_at = now

        return {
            "enabled": new_enabled,
            "exposed_playbooks": _decode_playbooks(new_playbooks),
            "created_at": created_at,
            "updated_at": now,
        }

    def delete_config(self, agent_name: str) -> bool:
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(enterprise_connectors).where(
                    enterprise_connectors.c.agent_name == agent_name
                )
            )
            return result.rowcount > 0

    # ---- Connector key (mcp_api_keys, scope='connector') ------------------

    def mint_key(self, agent_name: str, user_id: int) -> dict:
        """Insert a fresh connector key bound to ``agent_name``; secret returned once."""
        api_key = McpKeyOperations._generate_mcp_api_key()
        key_hash = McpKeyOperations._hash_api_key(api_key)
        key_id = McpKeyOperations._generate_id()
        now = utc_now_iso()
        with get_engine().begin() as conn:
            conn.execute(
                insert(mcp_api_keys).values(
                    id=key_id,
                    name=f"connector-{agent_name}-key",
                    description=f"Connector key for agent {agent_name}",
                    key_prefix=api_key[:20],
                    key_hash=key_hash,
                    created_at=now,
                    user_id=user_id,
                    agent_name=agent_name,
                    scope=_SCOPE,
                    is_active=1,
                )
            )
        return {"api_key": api_key, "key_prefix": api_key[:20]}

    def get_key_prefix(self, agent_name: str) -> Optional[str]:
        """Active connector key's masked prefix, or None (never the secret)."""
        stmt = (
            select(mcp_api_keys.c.key_prefix)
            .where(
                mcp_api_keys.c.agent_name == agent_name,
                mcp_api_keys.c.scope == _SCOPE,
                mcp_api_keys.c.is_active == 1,
            )
            .order_by(mcp_api_keys.c.created_at.desc())
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
            return row[0] if row else None

    def revoke_key(self, agent_name: str) -> bool:
        """Delete all connector-scoped keys for the agent (idempotent)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(mcp_api_keys).where(
                    mcp_api_keys.c.agent_name == agent_name,
                    mcp_api_keys.c.scope == _SCOPE,
                )
            )
            return result.rowcount > 0

    def regenerate_key(self, agent_name: str, user_id: int) -> dict:
        """Revoke the old key(s) + mint a new one in ONE transaction.

        Atomic so two concurrent regenerates can't interleave into two active
        connector keys for one agent — a single key is the invariant.
        """
        api_key = McpKeyOperations._generate_mcp_api_key()
        key_hash = McpKeyOperations._hash_api_key(api_key)
        key_id = McpKeyOperations._generate_id()
        now = utc_now_iso()
        with get_engine().begin() as conn:
            conn.execute(
                delete(mcp_api_keys).where(
                    mcp_api_keys.c.agent_name == agent_name,
                    mcp_api_keys.c.scope == _SCOPE,
                )
            )
            conn.execute(
                insert(mcp_api_keys).values(
                    id=key_id,
                    name=f"connector-{agent_name}-key",
                    description=f"Connector key for agent {agent_name}",
                    key_prefix=api_key[:20],
                    key_hash=key_hash,
                    created_at=now,
                    user_id=user_id,
                    agent_name=agent_name,
                    scope=_SCOPE,
                    is_active=1,
                )
            )
        return {"api_key": api_key, "key_prefix": api_key[:20]}
