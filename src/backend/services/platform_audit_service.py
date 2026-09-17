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
    OPERATOR_QUEUE = "operator_queue"  # Issue #1017: bulk clear actions
    NOTIFICATION = "notification"  # Issue #1017: bulk dismiss
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

    # `system_settings` key backing the hash-chain toggle (#2015). Persisted
    # rather than held in the instance: the flag used to be in-memory only, so
    # every backend restart silently switched the integrity control back off
    # and nothing told the operator. Restarts are routine — CLAUDE.md documents
    # that users re-login after one — so an audit log could sit unhashed
    # indefinitely while the UI still showed the feature as available.
    HASH_CHAIN_SETTING = "audit_hash_chain_enabled"

    def __init__(self) -> None:
        # No `_last_hash`: the chain head is read from the DB at write time
        # (`db.create_audit_entry_chained`). Holding it here made the chain a
        # property of one process — see that method for what that cost.
        pass

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
        # Explicit actor email, for principals the resolver cannot derive one
        # for. #848 inline-auth callers hold no key, no user row at call time
        # and no agent name — the verified email is their only identity, and
        # without this the row is unattributable. Never overrides an email the
        # resolver established from a real user.
        actor_email: Optional[str] = None,
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

            # #2323 — derive WHICH credential from the authenticated principal.
            #
            # Every one of the ~70 `actor_user=`-only call sites gets this for
            # free, with zero diff at any of them: the principal already knows,
            # it was simply never carried. Before this, an admin action taken
            # with a machine key was byte-identical in `audit_log` to the owner
            # clicking in a browser, so a leaked key could not be scoped after
            # the fact.
            #
            # `getattr` with a `None` default is MANDATORY, not stylistic: some
            # callers pass a `SimpleNamespace` actor (see `routers/voice.py`),
            # and a plain attribute access would raise into `log()`'s bare
            # `except`, which returns None — silently DROPPING the audit row.
            # `None` is also the unprivileged answer, per the rule that a
            # getattr default across principal types must never be the
            # privileged one.
            #
            # An explicitly-passed value still wins ONLY where the principal
            # carries none. The two dispatch-admission sites that used to pass a
            # client-supplied `X-MCP-Key-Id` no longer do — that header is
            # validated nowhere and is persisted into the backlog replay blob,
            # so honouring it would have let any authenticated caller forge the
            # attribution in the very rows this exists to make trustworthy.
            # `routers/internal.py` still passes explicitly and is unaffected:
            # it sends no `actor_user`, so derivation cannot compete, and it is
            # internal-secret-gated.
            if actor_user is not None:
                if mcp_key_id is None:
                    mcp_key_id = getattr(actor_user, "mcp_key_id", None)
                if mcp_key_name is None:
                    mcp_key_name = getattr(actor_user, "mcp_key_name", None)
                if mcp_scope is None:
                    mcp_scope = getattr(actor_user, "mcp_scope", None)

            actor_type, actor_id, resolved_email = self._resolve_actor(
                actor_user=actor_user,
                actor_agent_name=actor_agent_name,
                mcp_scope=mcp_scope,
                mcp_key_id=mcp_key_id,
            )
            # Resolver wins when it found a real identity; the explicit value is
            # a fallback, not an override.
            actor_email = resolved_email or actor_email

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

            if self.hash_chain_enabled:
                # The chain head comes from the DB, inside the insert's own
                # transaction — not from a process attribute (#2015).
                db.create_audit_entry_chained(entry, self._compute_hash)
            else:
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

    @property
    def hash_chain_enabled(self) -> bool:
        """Is hash chaining on for THIS INSTALL (not this process)? (#2015)

        Read live from `system_settings` on every call, deliberately uncached —
        the same reasoning `settings_service._resolve_bool_flag` records for the
        other platform flags: a cache lets a worker keep hashing (or keep not
        hashing) after an admin has flipped the toggle, and here the two halves
        of a divergent fleet write rows that cannot be verified against each
        other.

        Fail-CLOSED, unlike those flags. They fail open because an exception
        would zero every feature flag in the UI; this one decides whether an
        integrity record is written, and a settings-read failure is not a reason
        to claim one exists. Off is the honest answer, and `verify_chain`'s
        `unverifiable` state describes the result accurately.
        """
        try:
            stored = db.get_setting_value(self.HASH_CHAIN_SETTING)
        except Exception:  # noqa: BLE001 — fail closed, see above
            logger.warning(
                "[PlatformAuditService] could not read %s; treating hash chain "
                "as disabled", self.HASH_CHAIN_SETTING, exc_info=True,
            )
            return False
        return str(stored).lower() in ("true", "1", "yes")

    def enable_hash_chain(self, enabled: bool = True) -> None:
        """Toggle hash chain computation for new entries — durably (#2015).

        This used to set an instance attribute and write nothing, so the
        control turned itself off at the next restart. It now persists, and
        `hash_chain_enabled` reads it back, so the answer survives a restart
        and is the same in every worker.

        No seeding step any more: the chain head is read from the DB inside the
        insert transaction, so there is nothing in this process to seed.
        """
        db.set_setting(self.HASH_CHAIN_SETTING, "true" if enabled else "false")
        logger.info(
            "[PlatformAuditService] hash chain %s (persisted)",
            "enabled" if enabled else "disabled",
        )

    async def verify_chain(self, start_id: int, end_id: int) -> Dict[str, Any]:
        """Verify hash chain integrity between two row IDs (inclusive).

        `valid` is deliberately TRI-STATE (#1984):

          * ``True``  — entries were hashed and the chain checks out
          * ``False`` — a hash mismatch: tampering or corruption
          * ``None``  — **unverifiable**: there was nothing to check

        The third state is the fix. Skipping an unhashed entry is right on its
        own (a chain enabled midway legitimately has an unhashed prefix), but
        when EVERY entry was skipped the old code still answered ``valid: True``
        — so an install that never enabled hashing, which is the default,
        reported its audit log verified-intact across thousands of rows. One
        answer for three different states: verified, empty, and "no integrity
        data exists". An operator asking "was this tampered with?" during an
        incident got a green tick meaning "unanswerable".

        ``None`` rather than ``False``: ``False`` claims tampering, which is an
        equally wrong and considerably louder lie. A caller doing a plain
        truthiness test degrades to "not verified" — the safe direction.

        Returns:
            {"valid": bool | None, "status": str, "checked": int,
             "skipped_unhashed": int, "total_in_range": int,
             "hash_chain_enabled": bool, "first_invalid_id": int | None}
        """
        entries = db.get_audit_entries_range(start_id, end_id)
        base: Dict[str, Any] = {
            "checked": 0,
            "skipped_unhashed": 0,
            "total_in_range": len(entries),
            "hash_chain_enabled": self.hash_chain_enabled,
            "first_invalid_id": None,
        }

        if not entries:
            # Distinct from "rows exist but none are hashed": the caller asked
            # about a range holding nothing, which is not a statement about
            # integrity in either direction.
            return {**base, "valid": None, "status": "empty_range"}

        checked = 0
        skipped = 0
        for i, entry in enumerate(entries):
            if not entry.get("entry_hash"):
                # Written before hash chain was enabled — skipped, and now
                # COUNTED, so the verdict below can tell "some" from "none".
                skipped += 1
                continue
            expected = self._compute_hash(entry)
            if entry["entry_hash"] != expected:
                return {
                    **base, "valid": False, "status": "tampered",
                    "checked": checked + 1, "skipped_unhashed": skipped,
                    "first_invalid_id": entry["id"],
                }
            if i > 0 and entry.get("previous_hash"):
                prev = entries[i - 1]
                if prev.get("entry_hash") and entry["previous_hash"] != prev["entry_hash"]:
                    return {
                        **base, "valid": False, "status": "tampered",
                        "checked": checked + 1, "skipped_unhashed": skipped,
                        "first_invalid_id": entry["id"],
                    }
            checked += 1

        if checked == 0:
            # Rows exist; none carry a hash. THE reported bug (#1984).
            return {
                **base, "valid": None, "status": "unverifiable",
                "skipped_unhashed": skipped,
            }

        return {
            **base,
            "valid": True,
            # Named apart so a partially-hashed range cannot pass for a fully
            # verified one — the unhashed prefix is permanent on any install
            # that enabled hashing later.
            "status": "verified_partial" if skipped else "verified",
            "checked": checked,
            "skipped_unhashed": skipped,
        }

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
