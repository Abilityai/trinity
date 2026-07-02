"""
Database operations for Slack multi-agent channel routing.

Handles:
- Workspace connections (one bot token per workspace, encrypted)
- Channel-agent bindings (which agent responds in which channel)
- Active thread tracking (reply-without-mention)

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL.
"""

import logging
import secrets
from typing import Optional, List

from sqlalchemy import select, delete, update, and_

from .engine import get_engine, make_insert
from .tables import slack_workspaces, slack_channel_agents, slack_active_threads
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


class SlackChannelOperations:
    """Operations for Slack workspace connections and channel-agent bindings."""

    # =========================================================================
    # Encryption helpers (same pattern as subscription_credentials)
    # =========================================================================

    def _get_encryption_service(self):
        """Lazy-load encryption service."""
        from services.credential_encryption import CredentialEncryptionService
        return CredentialEncryptionService()

    def _encrypt_token(self, token: str) -> str:
        """Encrypt a bot token for storage."""
        svc = self._get_encryption_service()
        return svc.encrypt({"bot_token": token})

    def _decrypt_token(self, encrypted: str) -> Optional[str]:
        """Decrypt a bot token from storage. Returns None if decryption fails."""
        try:
            svc = self._get_encryption_service()
            decrypted = svc.decrypt(encrypted)
            return decrypted.get("bot_token")
        except Exception as e:
            logger.error(f"Failed to decrypt bot token: {e}")
            # Fallback: might be plaintext from before encryption was added
            if encrypted.startswith("xoxb-"):
                logger.warning("Bot token stored in plaintext — will re-encrypt on next update")
                return encrypted
            return None

    # =========================================================================
    # Workspace Operations
    # =========================================================================

    def create_workspace(
        self,
        team_id: str,
        team_name: Optional[str],
        bot_token: str,
        connected_by: Optional[str] = None
    ) -> dict:
        """Create or update a workspace connection. Bot token is encrypted at rest."""
        workspace_id = secrets.token_urlsafe(16)
        now = utc_now_iso()
        encrypted_token = self._encrypt_token(bot_token)

        stmt = make_insert(slack_workspaces).values(
            id=workspace_id,
            team_id=team_id,
            team_name=team_name,
            bot_token=encrypted_token,
            connected_by=connected_by,
            connected_at=now,
            enabled=1,
        ).on_conflict_do_update(
            index_elements=["team_id"],
            set_={
                "bot_token": encrypted_token,
                "team_name": team_name,
                "connected_at": now,
            },
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return self.get_workspace_by_team(team_id)

    def get_workspace_by_team(self, team_id: str) -> Optional[dict]:
        """Get workspace connection by Slack team ID. Bot token is decrypted."""
        stmt = select(
            slack_workspaces.c.id,
            slack_workspaces.c.team_id,
            slack_workspaces.c.team_name,
            slack_workspaces.c.bot_token,
            slack_workspaces.c.connected_by,
            slack_workspaces.c.connected_at,
            slack_workspaces.c.enabled,
        ).where(
            and_(
                slack_workspaces.c.team_id == team_id,
                slack_workspaces.c.enabled == 1,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()

        if not row:
            return None

        result = self._row_to_workspace(row)
        # Decrypt the bot token
        result["bot_token"] = self._decrypt_token(result["bot_token"]) or ""
        return result

    def get_workspace_bot_token(self, team_id: str) -> Optional[str]:
        """Get decrypted bot token for a workspace."""
        ws = self.get_workspace_by_team(team_id)
        return ws["bot_token"] if ws else None

    def get_all_workspaces(self) -> List[dict]:
        """Get all enabled workspaces with decrypted bot tokens."""
        stmt = select(
            slack_workspaces.c.id,
            slack_workspaces.c.team_id,
            slack_workspaces.c.team_name,
            slack_workspaces.c.bot_token,
            slack_workspaces.c.connected_by,
            slack_workspaces.c.connected_at,
            slack_workspaces.c.enabled,
        ).where(slack_workspaces.c.enabled == 1)
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()

        results = []
        for row in rows:
            ws = self._row_to_workspace(row)
            ws["bot_token"] = self._decrypt_token(ws["bot_token"]) or ""
            results.append(ws)
        return results

    def delete_workspace(self, team_id: str) -> bool:
        """Delete a workspace and all its channel bindings."""
        with get_engine().begin() as conn:
            conn.execute(
                delete(slack_channel_agents).where(
                    slack_channel_agents.c.team_id == team_id
                )
            )
            result = conn.execute(
                delete(slack_workspaces).where(
                    slack_workspaces.c.team_id == team_id
                )
            )
            deleted = result.rowcount > 0
        return deleted

    # =========================================================================
    # Channel-Agent Binding Operations
    # =========================================================================

    def bind_channel_to_agent(
        self,
        team_id: str,
        slack_channel_id: str,
        slack_channel_name: Optional[str],
        agent_name: str,
        created_by: Optional[str] = None,
        is_dm_default: bool = False,
    ) -> dict:
        """Bind a Slack channel to an agent."""
        binding_id = secrets.token_urlsafe(16)
        now = utc_now_iso()
        dm_default_int = 1 if is_dm_default else 0

        stmt = make_insert(slack_channel_agents).values(
            id=binding_id,
            team_id=team_id,
            slack_channel_id=slack_channel_id,
            slack_channel_name=slack_channel_name,
            agent_name=agent_name,
            is_dm_default=dm_default_int,
            created_by=created_by,
            created_at=now,
        ).on_conflict_do_update(
            index_elements=["team_id", "slack_channel_id"],
            set_={
                "agent_name": agent_name,
                "slack_channel_name": slack_channel_name,
                "is_dm_default": dm_default_int,
            },
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return self.get_channel_agent(team_id, slack_channel_id)

    def get_channel_agent(self, team_id: str, slack_channel_id: str) -> Optional[dict]:
        """Get which agent is bound to a channel."""
        stmt = select(
            slack_channel_agents.c.id,
            slack_channel_agents.c.team_id,
            slack_channel_agents.c.slack_channel_id,
            slack_channel_agents.c.slack_channel_name,
            slack_channel_agents.c.agent_name,
            slack_channel_agents.c.is_dm_default,
            slack_channel_agents.c.created_by,
            slack_channel_agents.c.created_at,
        ).where(
            and_(
                slack_channel_agents.c.team_id == team_id,
                slack_channel_agents.c.slack_channel_id == slack_channel_id,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()

        if not row:
            return None
        return self._row_to_channel_agent(row)

    def get_agent_name_for_channel(self, team_id: str, slack_channel_id: str) -> Optional[str]:
        """Get agent name for a channel (fast lookup)."""
        binding = self.get_channel_agent(team_id, slack_channel_id)
        return binding["agent_name"] if binding else None

    def get_dm_default_agent(self, team_id: str) -> Optional[str]:
        """Get the default agent for DMs in a workspace."""
        stmt = (
            select(slack_channel_agents.c.agent_name)
            .where(
                and_(
                    slack_channel_agents.c.team_id == team_id,
                    slack_channel_agents.c.is_dm_default == 1,
                )
            )
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return row[0] if row else None

    def set_dm_default(self, team_id: str, agent_name: str) -> bool:
        """Make ``agent_name`` the DM-default for the workspace.

        Single transaction: clear all existing flags, then set on the target.
        Avoids any window where two agents would both look like the default
        (the schema has no exclusivity constraint, so the read-side falls
        back to ``LIMIT 1`` and would pick non-deterministically).

        Returns True if the target row was updated, False if the agent is
        not bound in this workspace (caller should 404).
        """
        with get_engine().begin() as conn:
            conn.execute(
                update(slack_channel_agents)
                .where(slack_channel_agents.c.team_id == team_id)
                .values(is_dm_default=0)
            )
            result = conn.execute(
                update(slack_channel_agents)
                .where(
                    and_(
                        slack_channel_agents.c.team_id == team_id,
                        slack_channel_agents.c.agent_name == agent_name,
                    )
                )
                .values(is_dm_default=1)
            )
            changed = result.rowcount > 0
        return changed

    def get_agents_for_workspace(self, team_id: str) -> List[dict]:
        """Get all agent-channel bindings for a workspace."""
        stmt = (
            select(
                slack_channel_agents.c.id,
                slack_channel_agents.c.team_id,
                slack_channel_agents.c.slack_channel_id,
                slack_channel_agents.c.slack_channel_name,
                slack_channel_agents.c.agent_name,
                slack_channel_agents.c.is_dm_default,
                slack_channel_agents.c.created_by,
                slack_channel_agents.c.created_at,
            )
            .where(slack_channel_agents.c.team_id == team_id)
            .order_by(slack_channel_agents.c.created_at.asc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()

        return [self._row_to_channel_agent(row) for row in rows]

    def get_channel_for_agent(self, team_id: str, agent_name: str) -> Optional[dict]:
        """Get channel binding for a specific agent in a workspace."""
        stmt = select(
            slack_channel_agents.c.id,
            slack_channel_agents.c.team_id,
            slack_channel_agents.c.slack_channel_id,
            slack_channel_agents.c.slack_channel_name,
            slack_channel_agents.c.agent_name,
            slack_channel_agents.c.is_dm_default,
            slack_channel_agents.c.created_by,
            slack_channel_agents.c.created_at,
        ).where(
            and_(
                slack_channel_agents.c.team_id == team_id,
                slack_channel_agents.c.agent_name == agent_name,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()

        if not row:
            return None
        return self._row_to_channel_agent(row)

    def get_channels_for_agent(self, agent_name: str) -> List[dict]:
        """All channel bindings for an agent across every workspace (#350).

        Powers the MCP ``list_channel_groups`` discovery + proactive-send target
        validation. Cross-workspace (no ``team_id`` filter), so an agent bound in
        multiple Slack workspaces sees them all.
        """
        stmt = (
            select(
                slack_channel_agents.c.id,
                slack_channel_agents.c.team_id,
                slack_channel_agents.c.slack_channel_id,
                slack_channel_agents.c.slack_channel_name,
                slack_channel_agents.c.agent_name,
                slack_channel_agents.c.is_dm_default,
                slack_channel_agents.c.created_by,
                slack_channel_agents.c.created_at,
            )
            .where(slack_channel_agents.c.agent_name == agent_name)
            .order_by(slack_channel_agents.c.created_at.asc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()
        return [self._row_to_channel_agent(row) for row in rows]

    def unbind_agent(self, team_id: str, agent_name: str) -> bool:
        """Remove an agent's channel binding.

        Pure delete — does not auto-promote a new DM default. The router
        layer is responsible for refusing to unbind the current DM default
        while other agents are still bound (#584). When the unbind target
        is the only agent in the workspace, the binding is removed cleanly
        and the workspace ends up with no Slack agents at all.
        """
        stmt = delete(slack_channel_agents).where(
            and_(
                slack_channel_agents.c.team_id == team_id,
                slack_channel_agents.c.agent_name == agent_name,
            )
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            deleted = result.rowcount > 0
        return deleted

    def unbind_channel(self, team_id: str, slack_channel_id: str) -> bool:
        """Remove a channel's agent binding."""
        stmt = delete(slack_channel_agents).where(
            and_(
                slack_channel_agents.c.team_id == team_id,
                slack_channel_agents.c.slack_channel_id == slack_channel_id,
            )
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            deleted = result.rowcount > 0
        return deleted

    # =========================================================================
    # Row converters
    # =========================================================================

    def _row_to_workspace(self, row) -> dict:
        return {
            "id": row[0],
            "team_id": row[1],
            "team_name": row[2],
            "bot_token": row[3],
            "connected_by": row[4],
            "connected_at": row[5],
            "enabled": bool(row[6]),
        }

    # =========================================================================
    # Active Thread Operations (for reply-without-mention)
    # =========================================================================

    def register_active_thread(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        agent_name: str,
    ) -> None:
        """Record that the bot responded in a thread (enables reply-without-mention)."""
        now = utc_now_iso()
        stmt = make_insert(slack_active_threads).values(
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            agent_name=agent_name,
            created_at=now,
        ).on_conflict_do_nothing(
            index_elements=["team_id", "channel_id", "thread_ts"],
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def is_active_thread(self, team_id: str, channel_id: str, thread_ts: str) -> Optional[str]:
        """Check if a thread is active (bot participated). Returns agent_name or None."""
        stmt = select(slack_active_threads.c.agent_name).where(
            and_(
                slack_active_threads.c.team_id == team_id,
                slack_active_threads.c.channel_id == channel_id,
                slack_active_threads.c.thread_ts == thread_ts,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return row[0] if row else None

    # =========================================================================
    # Row converters
    # =========================================================================

    def _row_to_channel_agent(self, row) -> dict:
        return {
            "id": row[0],
            "team_id": row[1],
            "slack_channel_id": row[2],
            "slack_channel_name": row[3],
            "agent_name": row[4],
            "is_dm_default": bool(row[5]),
            "created_by": row[6],
            "created_at": row[7],
        }
