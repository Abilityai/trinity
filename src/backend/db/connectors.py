"""
Per-agent MCP connector config database operations (ent#46).

Holds the `agent_connectors` row: whether the connector is enabled and the
exposed-playbook allow-list. The scoped connector *key* lives in
`mcp_api_keys` (scope='connector') and is managed by `McpKeyOperations`.

SQLAlchemy Core so it runs unchanged on SQLite and PostgreSQL.
"""

import json
from datetime import datetime
from typing import Optional, List

from sqlalchemy import select, insert, update, delete

from .engine import get_engine
from .tables import agent_connectors
from db_models import ConnectorConfig
from utils.helpers import utc_now_iso


class ConnectorOperations:
    """Per-agent MCP connector config operations."""

    @staticmethod
    def _decode_playbooks(raw: Optional[str]) -> Optional[List[str]]:
        """JSON TEXT -> list, or None (= all user_invocable playbooks)."""
        if not raw:
            return None
        try:
            value = json.loads(raw)
            return value if isinstance(value, list) else None
        except (ValueError, TypeError):
            return None

    @classmethod
    def _row_to_config(cls, row) -> ConnectorConfig:
        return ConnectorConfig(
            agent_name=row["agent_name"],
            enabled=bool(row["enabled"]),
            exposed_playbooks=cls._decode_playbooks(row["exposed_playbooks"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_connector_config(self, agent_name: str) -> Optional[ConnectorConfig]:
        """Return the connector config, or None if never configured."""
        stmt = select(
            agent_connectors.c.agent_name,
            agent_connectors.c.enabled,
            agent_connectors.c.exposed_playbooks,
            agent_connectors.c.created_at,
            agent_connectors.c.updated_at,
        ).where(agent_connectors.c.agent_name == agent_name)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_config(row) if row else None

    def upsert_connector_config(
        self,
        agent_name: str,
        enabled: Optional[bool] = None,
        exposed_playbooks: Optional[List[str]] = None,
        *,
        clear_playbooks: bool = False,
    ) -> ConnectorConfig:
        """Create or update the connector config.

        Only fields that are provided change. ``exposed_playbooks=None`` leaves
        the allow-list untouched on an update; pass ``clear_playbooks=True`` to
        explicitly reset it to "all user_invocable playbooks" (stores NULL).
        """
        now = utc_now_iso()
        encoded = (
            None
            if clear_playbooks or exposed_playbooks is None
            else json.dumps(list(exposed_playbooks))
        )

        with get_engine().begin() as conn:
            existing = conn.execute(
                select(
                    agent_connectors.c.agent_name,
                    agent_connectors.c.enabled,
                    agent_connectors.c.exposed_playbooks,
                    agent_connectors.c.created_at,
                ).where(agent_connectors.c.agent_name == agent_name)
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
                    update(agent_connectors)
                    .where(agent_connectors.c.agent_name == agent_name)
                    .values(enabled=new_enabled, exposed_playbooks=new_playbooks, updated_at=now)
                )
                created_at = existing["created_at"]
            else:
                new_enabled = enabled if enabled is not None else False
                new_playbooks = encoded
                conn.execute(
                    insert(agent_connectors).values(
                        agent_name=agent_name,
                        enabled=new_enabled,
                        exposed_playbooks=new_playbooks,
                        created_at=now,
                        updated_at=now,
                    )
                )
                created_at = now

        return ConnectorConfig(
            agent_name=agent_name,
            enabled=new_enabled,
            exposed_playbooks=self._decode_playbooks(new_playbooks),
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(now),
        )

    def delete_connector_config(self, agent_name: str) -> bool:
        """Remove the connector config row (cascade on agent delete)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(agent_connectors).where(agent_connectors.c.agent_name == agent_name)
            )
            return result.rowcount > 0
