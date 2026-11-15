"""
Agent security settings database operations.

Handles full capabilities (container security), read-only mode, and
guardrails configuration (GUARD-001).

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL.
"""

import json
from typing import Optional

from sqlalchemy import select, update, and_, func

from ..engine import get_engine
from ..tables import agent_ownership


class SecurityMixin:
    """Mixin for agent security settings (full capabilities, read-only mode)."""

    # =========================================================================
    # Full Capabilities (Container Security)
    # =========================================================================

    def get_full_capabilities(self, agent_name: str) -> bool:
        """
        Get full_capabilities setting for an agent.
        When True, container runs with Docker default capabilities (apt-get works).
        When False (default), container runs with restricted capabilities (secure).

        Args:
            agent_name: Name of the agent

        Returns:
            True if full capabilities enabled, False otherwise
        """
        stmt = select(agent_ownership.c.full_capabilities).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
            if row and row[0] is not None:
                return bool(row[0])
            return False

    def set_full_capabilities(self, agent_name: str, enabled: bool) -> bool:
        """
        Set full_capabilities setting for an agent.
        Requires container restart to take effect.

        Args:
            agent_name: Name of the agent
            enabled: True for Docker default capabilities, False for restricted

        Returns:
            True if update succeeded, False otherwise
        """
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(full_capabilities=1 if enabled else 0)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    # =========================================================================
    # Read-Only Mode
    # =========================================================================

    def get_read_only_mode(self, agent_name: str) -> dict:
        """
        Get read-only mode status and configuration for an agent.

        Returns:
            dict with 'enabled' (bool) and 'config' (dict or None)
        """
        stmt = select(
            func.coalesce(agent_ownership.c.read_only_mode, 0).label("read_only_mode"),
            agent_ownership.c.read_only_config,
        ).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            if row:
                config = None
                if row["read_only_config"]:
                    try:
                        config = json.loads(row["read_only_config"])
                    except json.JSONDecodeError:
                        config = None
                return {
                    "enabled": bool(row["read_only_mode"]),
                    "config": config
                }
            return {"enabled": False, "config": None}

    def set_read_only_mode(self, agent_name: str, enabled: bool, config: Optional[dict] = None) -> bool:
        """
        Set read-only mode status and configuration for an agent.

        Args:
            agent_name: Name of the agent
            enabled: True to enable read-only mode
            config: Optional dict with 'blocked_patterns' and 'allowed_patterns' lists

        Returns:
            True if update succeeded
        """
        config_json = json.dumps(config) if config else None
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(read_only_mode=1 if enabled else 0, read_only_config=config_json)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    # =========================================================================
    # Guardrails (GUARD-001)
    # =========================================================================

    def get_guardrails_config(self, agent_name: str) -> dict:
        """Return per-agent guardrails overrides, or {} if none set."""
        stmt = select(agent_ownership.c.guardrails_config).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            if row and row["guardrails_config"]:
                try:
                    return json.loads(row["guardrails_config"])
                except json.JSONDecodeError:
                    return {}
            return {}

    # =========================================================================
    # Tool access (GUARD-008, trinity-enterprise#341) — the guardrails blob
    # and the read-only columns as ONE row, read and written together
    # =========================================================================

    def get_tool_access_row(self, agent_name: str) -> Optional[dict]:
        """The fields a tool-access write reads and compares, or None.

        ``guardrails_raw`` is the stored TEXT exactly as it is, so the write
        below can compare-and-set on it; ``guardrails`` is its parsed form ({}
        when unset or malformed).
        """
        stmt = select(
            agent_ownership.c.guardrails_config,
            func.coalesce(agent_ownership.c.read_only_mode, 0).label("read_only_mode"),
            agent_ownership.c.read_only_config,
        ).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        raw = row["guardrails_config"]
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        try:
            ro_config = json.loads(row["read_only_config"]) if row["read_only_config"] else None
        except json.JSONDecodeError:
            ro_config = None
        return {
            "guardrails_raw": raw,
            "guardrails": parsed if isinstance(parsed, dict) else {},
            "read_only": bool(row["read_only_mode"]),
            "read_only_config": ro_config,
        }

    _KEEP = object()

    def write_tool_access_row(
        self,
        agent_name: str,
        *,
        expected_guardrails_raw: Optional[str],
        expected_read_only: bool,
        guardrails: Optional[dict],
        read_only: bool,
        read_only_config=_KEEP,
    ) -> bool:
        """Write the guardrails blob and the read-only layer in ONE UPDATE,
        only if neither moved since ``get_tool_access_row`` read them.

        False means another writer got there first (or the agent is gone): the
        caller answers 409 rather than leave a profile and a read-only layer
        that disagree. ``read_only_config`` is left as stored unless given, so
        turning read-only off keeps the owner's patterns for the next time.
        """
        values = {
            "guardrails_config": json.dumps(guardrails) if guardrails else None,
            "read_only_mode": 1 if read_only else 0,
        }
        if read_only_config is not SecurityMixin._KEEP:
            values["read_only_config"] = json.dumps(read_only_config) if read_only_config else None
        stmt = (
            update(agent_ownership)
            .where(
                and_(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                    agent_ownership.c.guardrails_config.is_not_distinct_from(expected_guardrails_raw),
                    func.coalesce(agent_ownership.c.read_only_mode, 0) == (1 if expected_read_only else 0),
                )
            )
            .values(**values)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

    def set_guardrails_config(self, agent_name: str, config: Optional[dict]) -> bool:
        """Store per-agent guardrails overrides as a JSON blob. None clears."""
        payload = json.dumps(config) if config else None
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(guardrails_config=payload)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0
