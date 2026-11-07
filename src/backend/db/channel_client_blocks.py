"""
Database operations for per-agent channel client blocks (trinity-enterprise#21).

An agent's owner can block one external channel client — a Telegram user, a
WhatsApp number, a Slack user — from reaching that agent. The channel access
gate (``adapters/message_router.py``) reads ``is_blocked`` on every message.
Keyed on the channel-native sender id the gate sees, not on an email: a client
who never verified one is still blockable.

SQLAlchemy Core over ``db/tables.py``, so it runs on SQLite and PostgreSQL.
"""

import secrets
from typing import List, Optional

from sqlalchemy import delete, select

from .engine import get_engine, make_insert
from .tables import channel_client_blocks
from utils.helpers import utc_now_iso


class ChannelClientBlockOperations:
    """Operations for the channel_client_blocks table."""

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "agent_name": row["agent_name"],
            "channel": row["channel"],
            "client_id": row["client_id"],
            "blocked_at": row["blocked_at"],
            "blocked_by_id": row["blocked_by_id"],
            "blocked_by_email": row["blocked_by_email"],
            "reason": row["reason"],
        }

    def block(
        self,
        agent_name: str,
        channel: str,
        client_id: str,
        blocked_by_id: Optional[int],
        blocked_by_email: Optional[str],
        reason: Optional[str] = None,
    ) -> dict:
        """Block the client. Idempotent: an existing block is kept as it was
        (its first ``blocked_at`` and actor), and that row is returned."""
        stmt = make_insert(channel_client_blocks).values(
            id=secrets.token_urlsafe(16),
            agent_name=agent_name,
            channel=channel,
            client_id=client_id,
            blocked_at=utc_now_iso(),
            blocked_by_id=blocked_by_id,
            blocked_by_email=blocked_by_email,
            reason=reason,
        ).on_conflict_do_nothing(index_elements=["agent_name", "channel", "client_id"])
        with get_engine().begin() as conn:
            conn.execute(stmt)
        return self.get(agent_name, channel, client_id)

    def unblock(self, agent_name: str, channel: str, client_id: str) -> bool:
        """Lift the block. True when one was lifted."""
        stmt = delete(channel_client_blocks).where(
            channel_client_blocks.c.agent_name == agent_name,
            channel_client_blocks.c.channel == channel,
            channel_client_blocks.c.client_id == client_id,
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

    def get(self, agent_name: str, channel: str, client_id: str) -> Optional[dict]:
        stmt = select(channel_client_blocks).where(
            channel_client_blocks.c.agent_name == agent_name,
            channel_client_blocks.c.channel == channel,
            channel_client_blocks.c.client_id == client_id,
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_dict(row) if row else None

    def is_blocked(self, agent_name: str, channel: str, client_id: str) -> bool:
        return self.get(agent_name, channel, client_id) is not None

    def list_for_agent(self, agent_name: str) -> List[dict]:
        stmt = select(channel_client_blocks).where(channel_client_blocks.c.agent_name == agent_name)
        with get_engine().connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(stmt).mappings().all()]
