"""
Platform Audit Service (SEC-001 / Issue #20 — Phase 1).

Single entry point for cross-cutting audit logging across the Trinity
platform: agent lifecycle, authentication, authorization, configuration,
credentials, MCP operations, git operations, and system events.

Phase 1 ships the service surface and the global instance. Phase 2
will sprinkle `await platform_audit_service.log(...)` calls through the
existing routers and services.
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from database import db

logger = logging.getLogger(__name__)


class AuditEventType(str, Enum):
    """High-level categories for audit events.

    Action strings (e.g. "create", "login_success") are free-form within a
    category — keep them lowercase, snake_case, and stable so historical
    queries remain meaningful.
    """

    AGENT_LIFECYCLE = "agent_lifecycle"
    EXECUTION = "execution"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    CONFIGURATION = "configuration"
    CREDENTIALS = "credentials"
    MCP_OPERATION = "mcp_operation"
    GIT_OPERATION = "git_operation"
    PROACTIVE_MESSAGE = "proactive_message"  # Issue #321
    SITE_ACCESS = "site_access"  # SITE-001: agent website proxy visits
    SYSTEM = "system"


class AuditActorType(str, Enum):
    """Who performed the action."""

    USER = "user"          # human via UI / API token
    AGENT = "agent"        # agent container acting on its own
    MCP_CLIENT = "mcp_client"  # external client via MCP API key
    SYSTEM = "system"      # platform itself (scheduler, system agent)


class PlatformAuditService:
    """Centralized audit logging with immutability guarantees.

    - Actor attribution from JWT user, agent name, or MCP context
    - Append-only writes through `db.create_audit_entry`
    - Optional hash chain (Phase 4) for tamper evidence — disabled by default
    - Errors are logged but never raised; audit failures must not break the
      caller's primary operation
    """

    def __init__(self) -> None:
        self._last_hash: Optional[str] = None
        self._hash_chain_enabled = False  # Phase 4 toggle

    async def log(
        self,
        event_type: AuditEventType,
        event_action: str,
        source: str,
        *,
        # Actor — supply at least one
        actor_user: Optional[Any] = None,        # Pydantic User model
        actor_agent_name: Optional[str] = None,
        actor_ip: Optional[str] = None,
        # MCP context (when via MCP API key)
        mcp_key_id: Optional[str] = None,
        mcp_key_name: Optional[str] = None,
        mcp_scope: Optional[str] = None,
        # Target
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        # Request context
        request_id: Optional[str] = None,
        endpoint: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Log a single audit event.

        Returns the generated `event_id` (UUID) on success, or `None` on
        failure. Callers should not branch on the return value — audit
        logging is best-effort and must not affect business logic.
        """
        try:
            event_id = str(uuid.uuid4())
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            actor_type, actor_id, actor_email = self._resolve_actor(
                actor_user=actor_user,
                actor_agent_name=actor_agent_name,
                mcp_scope=mcp_scope,
                mcp_key_id=mcp_key_id,
            )

            entry: Dict[str, Any] = {
                "event_id": event_id,
                "event_type": event_type.value
                if isinstance(event_type, AuditEventType)
                else str(event_type),
                "event_action": str(event_action),
                "actor_type": actor_type,
                "actor_id": actor_id,
                "actor_email": actor_email,
                "actor_ip": actor_ip,
                "mcp_key_id": mcp_key_id,
                "mcp_key_name": mcp_key_name,
                "mcp_scope": mcp_scope,
                "target_type": target_type,
                "target_id": str(target_id) if target_id is not None else None,
                "timestamp": timestamp,
                "details": json.dumps(details) if details else None,
                "request_id": request_id,
                "source": source,
                "endpoint": endpoint,
                "previous_hash": None,
                "entry_hash": None,
            }

            if self._hash_chain_enabled:
                entry["previous_hash"] = self._last_hash
                entry["entry_hash"] = self._compute_hash(entry)
                self._last_hash = entry["entry_hash"]

            db.create_audit_entry(entry)
            return event_id

        except Exception as e:
            # Audit failures are non-fatal: log loudly but never raise.
            logger.error(
                "[PlatformAuditService] failed to write audit entry "
                f"({event_type}/{event_action}): {e}",
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_actor(
        actor_user: Optional[Any],
        actor_agent_name: Optional[str],
        mcp_scope: Optional[str],
        mcp_key_id: Optional[str],
    ) -> tuple:
        """Determine (actor_type, actor_id, actor_email) from inputs.

        Precedence: user > agent > mcp_scope=='system' > mcp_client.
        Returns ('system', 'trinity-system', None) for system events with no
        identifiable actor — never returns all-None so the NOT NULL
        actor_type column is satisfied.
        """
        if actor_user is not None:
            return (
                AuditActorType.USER.value,
                str(getattr(actor_user, "id", None) or ""),
                getattr(actor_user, "email", None),
            )
        if actor_agent_name:
            return (AuditActorType.AGENT.value, actor_agent_name, None)
        if mcp_scope == "system":
            return (AuditActorType.SYSTEM.value, "trinity-system", None)
        if mcp_key_id:
            return (AuditActorType.MCP_CLIENT.value, mcp_key_id, None)
        return (AuditActorType.SYSTEM.value, "trinity-system", None)

    def enable_hash_chain(self, enabled: bool = True) -> None:
        """Toggle hash chain computation for new entries (Phase 4)."""
        self._hash_chain_enabled = enabled
        if enabled:
            # Seed from the last entry in DB so chain continues
            try:
                entries = db.get_audit_entries(limit=1, offset=0)
                if entries and isinstance(entries, list) and len(entries) > 0:
                    last = entries[0]
                    if isinstance(last, dict) and last.get("entry_hash"):
                        self._last_hash = last["entry_hash"]
            except Exception as e:
                # Non-fatal: chain will start from None and verify_chain skips
                # entries written before seeding succeeded.
                logger.warning(
                    "[PlatformAuditService] failed to seed hash chain from last entry: %s",
                    e,
                )

    async def verify_chain(self, start_id: int, end_id: int) -> Dict[str, Any]:
        """Verify hash chain integrity between two row IDs (inclusive).

        Returns:
            {"valid": bool, "checked": int, "first_invalid_id": int | None}
        """
        entries = db.get_audit_entries_range(start_id, end_id)
        if not entries:
            return {"valid": True, "checked": 0, "first_invalid_id": None}

        checked = 0
        for i, entry in enumerate(entries):
            if not entry.get("entry_hash"):
                # Entry was written before hash chain was enabled — skip
                continue
            expected = self._compute_hash(entry)
            if entry["entry_hash"] != expected:
                return {
                    "valid": False,
                    "checked": checked + 1,
                    "first_invalid_id": entry["id"],
                }
            if i > 0 and entry.get("previous_hash"):
                prev = entries[i - 1]
                if prev.get("entry_hash") and entry["previous_hash"] != prev["entry_hash"]:
                    return {
                        "valid": False,
                        "checked": checked + 1,
                        "first_invalid_id": entry["id"],
                    }
            checked += 1

        return {"valid": True, "checked": checked, "first_invalid_id": None}

    @staticmethod
    def _compute_hash(entry: Dict[str, Any]) -> str:
        """SHA-256 over a stable subset of the entry. Used only when hash chain is enabled."""
        # Details round-trips through DB as JSON text: string at write-time (see log()),
        # dict at read-time (see db/audit.py::_row_to_dict). Normalize to dict so the
        # hash is stable across both paths.
        details = entry.get("details")
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except (TypeError, ValueError):
                pass
        content = json.dumps(
            {
                "event_id": entry["event_id"],
                "event_type": entry["event_type"],
                "event_action": entry["event_action"],
                "actor_id": entry.get("actor_id"),
                "target_id": entry.get("target_id"),
                "timestamp": entry["timestamp"],
                "details": details,
                "previous_hash": entry.get("previous_hash"),
            },
            sort_keys=True,
        )
        return hashlib.sha256(content.encode()).hexdigest()


# Global instance — import as `from services.platform_audit_service import platform_audit_service`
platform_audit_service = PlatformAuditService()
