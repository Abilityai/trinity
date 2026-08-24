"""
MCP API key management database operations.

Handles creation, validation, and revocation of MCP API keys.
Supports both user-scoped and agent-scoped keys for agent-to-agent collaboration.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the ``mcp_api_keys`` and
``users`` tables in ``db/tables.py`` (dialect-agnostic, no placeholders), and
the engine is resolved via ``db/engine.py``. The public API of
``McpKeyOperations`` is unchanged.
"""

import secrets
import hashlib
from datetime import datetime
from typing import Optional, List, Dict

from sqlalchemy import select, insert, update, delete

from .engine import get_engine
from .tables import mcp_api_keys, users, agent_ownership
from db_models import McpApiKey, McpApiKeyCreate, McpApiKeyWithSecret
from utils.helpers import utc_now_iso


# Columns selected for the JOIN read paths: every mcp_api_keys column (the old
# ``k.*``) plus username/email from the joined users row.
_KEY_JOIN_COLUMNS = (
    mcp_api_keys.c.id,
    mcp_api_keys.c.name,
    mcp_api_keys.c.description,
    mcp_api_keys.c.key_prefix,
    mcp_api_keys.c.key_hash,
    mcp_api_keys.c.created_at,
    mcp_api_keys.c.last_used_at,
    mcp_api_keys.c.usage_count,
    mcp_api_keys.c.is_active,
    mcp_api_keys.c.user_id,
    mcp_api_keys.c.agent_name,
    mcp_api_keys.c.scope,
    users.c.username,
    users.c.email,
)


def hash_mcp_api_key(api_key: str) -> str:
    """Public alias for the key-hashing convention (#1854).

    The container config-truth probe and the start-time drift predicate both
    compare a digest against ``mcp_api_keys.key_hash``. Routing them through
    this one helper (rather than re-spelling ``hashlib.sha256``) means a future
    change to the hashing scheme cannot silently make both of them report
    "no matching row" for every healthy agent in the fleet.
    """
    return McpKeyOperations._hash_api_key(api_key)


class McpKeyOperations:
    """MCP API key database operations."""

    def __init__(self, user_ops):
        """Initialize with reference to user operations for lookups."""
        self._user_ops = user_ops

    @staticmethod
    def _generate_id() -> str:
        """Generate a unique ID."""
        return secrets.token_urlsafe(16)

    @staticmethod
    def _generate_mcp_api_key() -> str:
        """Generate a new MCP API key with prefix."""
        return f"trinity_mcp_{secrets.token_urlsafe(32)}"

    @staticmethod
    def _hash_api_key(api_key: str) -> str:
        """Hash an API key for secure storage."""
        return hashlib.sha256(api_key.encode()).hexdigest()

    @staticmethod
    def _row_to_mcp_api_key(row) -> McpApiKey:
        """Convert an mcp_api_keys row to a McpApiKey model."""
        # Handle new columns with backwards compatibility
        row_keys = row.keys()
        agent_name = row["agent_name"] if "agent_name" in row_keys else None
        scope = row["scope"] if "scope" in row_keys and row["scope"] else "user"

        return McpApiKey(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            key_prefix=row["key_prefix"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_used_at=datetime.fromisoformat(row["last_used_at"]) if row["last_used_at"] else None,
            usage_count=row["usage_count"],
            is_active=bool(row["is_active"]),
            user_id=row["user_id"],
            username=row["username"],
            user_email=row["email"],
            agent_name=agent_name,
            scope=scope
        )

    # ent#163: the scopes this endpoint may mint. `agent`, `connector` and
    # `system` are deliberately absent — they are bound to an agent and are
    # minted by their own code paths; accepting them here would let a caller
    # forge an agent principal with no agent behind it.
    # #2323: `ops` is a bounded read-only machine credential. Admin-gated AND
    # human-only at the router; refused here too so a bad value can never reach
    # the column by another path.
    _USER_CREATABLE_SCOPES = ("user", "portal_delegate", "ops")

    # #2323: scopes that must never carry an `agent_name`. Three separate sweeps
    # filter on `scope IN ('agent','connector')` to find their work — the canary
    # L-03 orphan scan, the key orphan sweep, and the agent rename/purge cascade
    # — so a non-agent scope carrying an agent name would be invisible to all
    # three and outlive its agent forever. Enforced, not left to convention.
    _AGENTLESS_SCOPES = ("user", "portal_delegate", "ops")

    def create_mcp_api_key(self, username: str, key_data: McpApiKeyCreate) -> Optional[McpApiKeyWithSecret]:
        """Create a new MCP API key for a user.

        Scope defaults to `user`. `portal_delegate` (ent#163) is admin-gated at
        the router; this layer only refuses anything outside the creatable set
        so a bad value can never reach the column.
        """
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return None

        scope = getattr(key_data, "scope", None) or "user"
        if scope not in self._USER_CREATABLE_SCOPES:
            return None
        # #2323 — see `_AGENTLESS_SCOPES`. This endpoint has never set an agent
        # name, so the guard is belt-and-braces today; it exists so a future
        # caller cannot quietly bind one.
        if scope in self._AGENTLESS_SCOPES and getattr(key_data, "agent_name", None):
            return None

        key_id = self._generate_id()
        api_key = self._generate_mcp_api_key()
        key_hash = self._hash_api_key(api_key)
        now = utc_now_iso()

        with get_engine().begin() as conn:
            conn.execute(
                insert(mcp_api_keys).values(
                    id=key_id,
                    name=key_data.name,
                    description=key_data.description,
                    key_prefix=api_key[:20],
                    key_hash=key_hash,
                    created_at=now,
                    user_id=user["id"],
                    agent_name=None,
                    scope=scope,
                    # Set explicitly rather than leaning on the column default:
                    # `schema.py` declares `is_active INTEGER DEFAULT 1` but
                    # `db/tables.py` (the Core/Alembic source) declares a bare
                    # `Column("is_active", Integer)` with no default, so a table
                    # built from the metadata yields NULL here — and
                    # `validate_mcp_api_key` treats a falsy is_active as revoked,
                    # i.e. every minted key would be born invalid. Harmless today
                    # (both live schema paths carry the DDL default) but a real
                    # trap for anything built off the metadata.
                    is_active=1,
                )
            )

        return McpApiKeyWithSecret(
            id=key_id,
            name=key_data.name,
            description=key_data.description,
            key_prefix=api_key[:20],
            created_at=datetime.fromisoformat(now),
            last_used_at=None,
            usage_count=0,
            is_active=True,
            user_id=user["id"],
            username=username,
            user_email=user.get("email"),
            agent_name=None,
            scope=scope,
            api_key=api_key
        )

    def create_agent_mcp_api_key(self, agent_name: str, owner_username: str, description: Optional[str] = None) -> Optional[McpApiKeyWithSecret]:
        """
        Create an agent-scoped MCP API key for agent-to-agent collaboration.

        Args:
            agent_name: Name of the agent that will use this key
            owner_username: Username of the agent owner
            description: Optional description

        Returns:
            McpApiKeyWithSecret with the full API key (only returned once)
        """
        user = self._user_ops.get_user_by_username(owner_username)
        if not user:
            return None

        key_id = self._generate_id()
        api_key = self._generate_mcp_api_key()
        key_hash = self._hash_api_key(api_key)
        now = utc_now_iso()
        key_name = f"agent-{agent_name}-key"

        with get_engine().begin() as conn:
            conn.execute(
                insert(mcp_api_keys).values(
                    id=key_id,
                    name=key_name,
                    description=description or f"Auto-generated key for agent {agent_name}",
                    key_prefix=api_key[:20],
                    key_hash=key_hash,
                    created_at=now,
                    user_id=user["id"],
                    agent_name=agent_name,
                    scope="agent",
                    # #1854: set explicitly, exactly as `create_mcp_api_key`
                    # does and for the same reason — `schema.py` declares
                    # `is_active INTEGER DEFAULT 1` but `db/tables.py` (the
                    # Core/Alembic source) declares a bare
                    # `Column("is_active", Integer)` with no default, so a table
                    # built from the metadata yields NULL, and
                    # `validate_mcp_api_key` treats a falsy is_active as
                    # revoked. At creation that is a visibly-broken new agent;
                    # on the ROTATION path (#1854) it is a brick — the new key
                    # is born revoked while the old one has just been deleted.
                    # `usage_count` is the same defect one column up: NULL there
                    # makes `_row_to_mcp_api_key` raise a Pydantic
                    # ValidationError, so the row cannot even be READ back.
                    is_active=1,
                    usage_count=0,
                )
            )

        return McpApiKeyWithSecret(
            id=key_id,
            name=key_name,
            description=description or f"Auto-generated key for agent {agent_name}",
            key_prefix=api_key[:20],
            created_at=datetime.fromisoformat(now),
            last_used_at=None,
            usage_count=0,
            is_active=True,
            user_id=user["id"],
            username=owner_username,
            user_email=user.get("email"),
            agent_name=agent_name,
            scope="agent",
            api_key=api_key
        )

    def get_agent_mcp_api_key(self, agent_name: str) -> Optional[McpApiKey]:
        """Get the MCP API key for an agent (does not return the secret)."""
        stmt = (
            select(*_KEY_JOIN_COLUMNS)
            .select_from(mcp_api_keys.join(users, mcp_api_keys.c.user_id == users.c.id))
            .where(
                mcp_api_keys.c.agent_name == agent_name,
                mcp_api_keys.c.scope == "agent",
                mcp_api_keys.c.is_active == 1,
            )
            .order_by(mcp_api_keys.c.created_at.desc())
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_mcp_api_key(row) if row else None

    def list_active_agent_key_ids(self, agent_name: str) -> List[str]:
        """Ids of the agent's ACTIVE ``scope='agent'`` keys (#1854).

        Captured BEFORE a rotation mint so the delete step can name exactly the
        rows it superseded. Deliberately ``scope='agent'``-only: the sibling
        helpers (`set_agent_keys_active`, `_deactivate_agent_keys_in_txn`) span
        ``('agent','connector')`` and reusing either here would sweep the
        owner's MCP **connector** key into a rotation it has nothing to do with.
        """
        stmt = (
            select(mcp_api_keys.c.id)
            .where(
                mcp_api_keys.c.agent_name == agent_name,
                mcp_api_keys.c.scope == "agent",
                mcp_api_keys.c.is_active == 1,
            )
            .order_by(mcp_api_keys.c.created_at.desc())
        )
        with get_engine().connect() as conn:
            return [row[0] for row in conn.execute(stmt)]

    def delete_superseded_agent_keys(
        self, agent_name: str, keep_id: str, key_ids
    ) -> int:
        """DELETE the named superseded ``scope='agent'`` rows for `agent_name` (#1854).

        Three deliberate narrowings, each load-bearing:

        * **DELETE, not deactivate.** ``recover_agent_ownership`` reactivates
          every inactive per-agent row with no notion of "suspended by
          soft-delete" vs "superseded by rotation", so a deactivated rotated-out
          key comes back ALIVE after a soft-delete/recover cycle — rotation
          would not be durable. Deleting also drops the ``key_hash`` of a
          credential just declared compromised. Matches the connector precedent
          (delete+insert in one transaction).
        * **Only the CAPTURED id set**, never ``id != keep_id``. There is no
          per-agent start lock, so a concurrent ``recreate_missing_container``
          can mint K3 mid-rotation; an ``id != new_id`` form would delete K3 —
          the key actually baked into the live container.
        * **``scope='agent'`` only**, and ``keep_id`` is excluded belt-and-braces
          so a caller that mis-captured the new id still cannot brick the agent.

        Returns the number of rows removed.
        """
        ids = [k for k in set(key_ids or ()) if k and k != keep_id]
        if not ids:
            return 0
        stmt = delete(mcp_api_keys).where(
            mcp_api_keys.c.agent_name == agent_name,
            mcp_api_keys.c.scope == "agent",
            mcp_api_keys.c.id.in_(ids),
            mcp_api_keys.c.id != keep_id,
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount or 0

    def find_mcp_key_by_hash(self, key_hash: str) -> Optional[Dict]:
        """Look a key row up by its SHA-256 hash — metadata only, no secret (#1854).

        Backs the container config-truth probe and the start-time drift
        predicate: the digest is computed inside the container (probe) or from
        the container env (predicate) and matched against ``key_hash``, which is
        a plain unsalted ``sha256(api_key)``. Returns inactive rows too — the
        caller decides what an inactive match means (the probe wants to say
        "this is a revoked key", the predicate wants to call it drift).
        """
        if not key_hash:
            return None
        stmt = select(
            mcp_api_keys.c.id,
            mcp_api_keys.c.name,
            mcp_api_keys.c.key_prefix,
            mcp_api_keys.c.agent_name,
            mcp_api_keys.c.scope,
            mcp_api_keys.c.is_active,
            mcp_api_keys.c.user_id,
        ).where(mcp_api_keys.c.key_hash == key_hash)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        out = dict(row)
        out["scope"] = out.get("scope") or "user"
        out["is_active"] = bool(out.get("is_active"))
        return out

    def delete_agent_mcp_api_key(self, agent_name: str) -> bool:
        """Delete all MCP API keys for an agent (called when agent is deleted)."""
        stmt = delete(mcp_api_keys).where(
            mcp_api_keys.c.agent_name == agent_name,
            mcp_api_keys.c.scope == "agent",
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def set_agent_keys_active(self, agent_name: str, active: bool) -> int:
        """Activate/deactivate every per-agent key for `agent_name` (#1745).

        Soft-deleting an agent used to leave its keys `is_active = 1`, so the
        credential kept authenticating for the whole soft-delete window (default
        180 days) — enumerating the fleet, reading other agents, and able to mint
        a fresh key of its own, which made revoking it afterwards pointless.
        `mcp_api_keys` is registered CASCADE in `AGENT_REFS` for exactly this
        reason ("an orphaned key must not survive its agent"), but that cascade
        only runs at the hard purge at the END of that window.

        Deactivation rather than deletion because soft-delete is *recoverable*:
        recovering the agent flips the same keys back on, so a recovered agent
        keeps working without re-issuing credentials to a running container.
        `validate_mcp_api_key` already requires `is_active = 1`, so this is
        sufficient on both the REST and MCP paths.

        Covers scope='agent' AND scope='connector' — both are per-agent
        credentials and both are CASCADE entries.

        Returns: number of key rows changed.
        """
        stmt = (
            update(mcp_api_keys)
            .where(
                mcp_api_keys.c.agent_name == agent_name,
                mcp_api_keys.c.scope.in_(("agent", "connector")),
                mcp_api_keys.c.is_active == (0 if active else 1),
            )
            .values(is_active=1 if active else 0)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount or 0

    def deactivate_orphaned_agent_keys(self) -> int:
        """Deactivate per-agent keys whose agent is not live (#1745 backfill).

        "Not live" = no `agent_ownership` row at all, or one carrying
        `deleted_at`. Idempotent — only flips rows that are still active, so it
        finds nothing once an instance is clean.

        This is not hypothetical: on the instance where the bug was found, 12 of
        17 active agent-scoped keys were already orphaned, 10 of them from
        ephemeral test agents.
        """
        live = select(agent_ownership.c.agent_name).where(
            agent_ownership.c.deleted_at.is_(None)
        )
        stmt = (
            update(mcp_api_keys)
            .where(
                mcp_api_keys.c.agent_name.isnot(None),
                mcp_api_keys.c.scope.in_(("agent", "connector")),
                mcp_api_keys.c.is_active == 1,
                mcp_api_keys.c.agent_name.notin_(live),
            )
            .values(is_active=0)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount or 0

    def validate_mcp_api_key(
        self, api_key: str, *, track_usage: bool = True
    ) -> Optional[Dict]:
        """Validate an MCP API key and return user/agent info if valid.

        Args:
            api_key: The raw MCP API key to validate.
            track_usage: When True (default) the call bumps ``last_used_at`` /
                ``usage_count`` as before. High-frequency, low-value callers
                (the agent heartbeat — #307) pass ``False`` to validate without
                amplifying the usage counter or writing to SQLite on every beat.

        Returns:
            Dict with key info including:
            - key_id, key_name: Key identifiers
            - user_id, user_email: Owner info (username for backward compat)
            - agent_name: Agent name if scope is 'agent', else None
            - scope: 'user' or 'agent'
        """
        key_hash = self._hash_api_key(api_key)

        select_stmt = (
            select(
                mcp_api_keys.c.id,
                mcp_api_keys.c.name,
                mcp_api_keys.c.user_id,
                mcp_api_keys.c.is_active,
                mcp_api_keys.c.agent_name,
                mcp_api_keys.c.scope,
                users.c.username,
                users.c.email,
            )
            .select_from(mcp_api_keys.join(users, mcp_api_keys.c.user_id == users.c.id))
            .where(mcp_api_keys.c.key_hash == key_hash)
        )

        with get_engine().begin() as conn:
            row = conn.execute(select_stmt).mappings().first()

            if not row:
                return None

            if not row["is_active"]:
                return None

            # Update usage statistics. Skipped for high-frequency, low-value
            # callers (heartbeat — #307) so a 5s beat doesn't amplify the
            # counter or write to the DB ~12x/min/agent.
            if track_usage:
                now = utc_now_iso()
                conn.execute(
                    update(mcp_api_keys)
                    .where(mcp_api_keys.c.id == row["id"])
                    .values(
                        last_used_at=now,
                        usage_count=mcp_api_keys.c.usage_count + 1,
                    )
                )

            # Include agent collaboration fields
            return {
                "key_id": row["id"],
                "key_name": row["name"],
                "user_id": row["username"],  # Return username for backward compat
                "user_email": row["email"],
                "agent_name": row["agent_name"],  # Agent name if scope is 'agent'
                "scope": row["scope"] or "user"  # 'user' or 'agent'
            }

    def get_mcp_api_key(self, key_id: str, username: str) -> Optional[McpApiKey]:
        """Get MCP API key metadata."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return None

        stmt = (
            select(*_KEY_JOIN_COLUMNS)
            .select_from(mcp_api_keys.join(users, mcp_api_keys.c.user_id == users.c.id))
            .where(
                mcp_api_keys.c.id == key_id,
                mcp_api_keys.c.user_id == user["id"],
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_mcp_api_key(row) if row else None

    def list_mcp_api_keys(self, username: str) -> List[McpApiKey]:
        """List all MCP API keys for a user."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return []

        stmt = (
            select(*_KEY_JOIN_COLUMNS)
            .select_from(mcp_api_keys.join(users, mcp_api_keys.c.user_id == users.c.id))
            .where(mcp_api_keys.c.user_id == user["id"])
            .order_by(mcp_api_keys.c.created_at.desc())
        )
        with get_engine().connect() as conn:
            return [self._row_to_mcp_api_key(row) for row in conn.execute(stmt).mappings()]

    def list_all_mcp_api_keys(self) -> List[McpApiKey]:
        """List all MCP API keys (admin only)."""
        stmt = (
            select(*_KEY_JOIN_COLUMNS)
            .select_from(mcp_api_keys.join(users, mcp_api_keys.c.user_id == users.c.id))
            .order_by(mcp_api_keys.c.created_at.desc())
        )
        with get_engine().connect() as conn:
            return [self._row_to_mcp_api_key(row) for row in conn.execute(stmt).mappings()]

    def revoke_mcp_api_key(self, key_id: str, username: str) -> bool:
        """Revoke (deactivate) an MCP API key."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        with get_engine().begin() as conn:
            # Check ownership (unless admin)
            if user["role"] != "admin":
                row = conn.execute(
                    select(mcp_api_keys.c.user_id).where(mcp_api_keys.c.id == key_id)
                ).mappings().first()
                if not row or row["user_id"] != user["id"]:
                    return False

            result = conn.execute(
                update(mcp_api_keys)
                .where(mcp_api_keys.c.id == key_id)
                .values(is_active=0)
            )
            return result.rowcount > 0

    def delete_mcp_api_key(self, key_id: str, username: str) -> bool:
        """Permanently delete an MCP API key."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        with get_engine().begin() as conn:
            # Check ownership (unless admin)
            if user["role"] != "admin":
                row = conn.execute(
                    select(mcp_api_keys.c.user_id).where(mcp_api_keys.c.id == key_id)
                ).mappings().first()
                if not row or row["user_id"] != user["id"]:
                    return False

            result = conn.execute(
                delete(mcp_api_keys).where(mcp_api_keys.c.id == key_id)
            )
            return result.rowcount > 0
