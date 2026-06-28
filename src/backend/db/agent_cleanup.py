"""
Single source of truth for agent-referencing tables (Issue #816).

Both `routers/agents.py:delete_agent_endpoint` and
`db/agent_settings/metadata.py:rename_agent` consume the same `AGENT_REFS`
registry so the set of tables that must follow an agent's lifecycle is
declared in exactly one place. Adding a new table that references an agent
without an `AgentRef` entry fails the parity check in
`tests/unit/test_agent_cleanup_parity.py`.

Policy
------
- CASCADE: rows are deleted when the agent is deleted. Default for
  per-agent config, state, history that has no consumer once the agent is
  gone.
- KEEP: rows survive the agent's deletion. Used for tables that drive
  rolling-window or forever financial rollups keyed on something other
  than `agent_name` (e.g. `subscription_id`). The table must have its own
  retention discipline — otherwise it's a slow leak.

The current KEEP set is intentionally small:

| Table                  | Why KEEP                                       |
|------------------------|------------------------------------------------|
| schedule_executions    | Billing rollup by subscription_id; retention   |
|                        | sweep prunes terminal rows past 90d (#772).    |
| nevermined_payment_log | Forever financial record.                      |

Rename always touches every entry (CASCADE *and* KEEP) — the row's
`agent_name` is a foreign-key value; renaming the agent must update
every reference, regardless of delete policy.

Order
-----
`AGENT_REFS` is ordered children → parent. `agent_ownership` is NOT in
the list — it's the parent row and is deleted by the caller after
`cascade_delete()` returns. This ordering is meaningful only for the
future PostgreSQL migration where FK enforcement is on by default; on
SQLite with `PRAGMA foreign_keys=OFF` (G11) the order is irrelevant.

Portability
-----------
Data access is SQLAlchemy Core (#300): the cascade/rename/orphan
functions receive a SQLAlchemy ``Connection`` and build dialect-agnostic
``delete()`` / ``update()`` / ``select()`` statements against the shared
``db/tables.py`` MetaData (tables resolved by name from the registry).
No ``?`` placeholders, no SQLite-only constructs (`datetime('now', ...)`,
`PRAGMA`, `rowid`, `sqlite_master`) — the same code runs on SQLite and
PostgreSQL. The only free-form fragment, ``extra_filter``
(``scope = 'agent'``), is ANSI SQL wrapped in ``text()`` and AND-ed into
the Core WHERE clause.

Import discipline
-----------------
Module-level imports are stdlib-only so the parity check in
``tests/unit/test_agent_cleanup_parity.py`` can ``exec_module`` this file
without the backend venv (SQLAlchemy absent). The SQLAlchemy imports are
therefore deferred into the data-access functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class Policy(str, Enum):
    """Cascade behavior on agent delete."""

    CASCADE = "cascade"  # DELETE rows when agent is deleted
    KEEP = "keep"        # Rows survive agent delete (rolling-window / forever record)


@dataclass(frozen=True)
class AgentRef:
    """One column that references an agent identifier."""

    table: str
    column: str               # "agent_name" or non-canonical (e.g. "source_agent")
    policy: Policy
    extra_filter: Optional[str] = None  # Additional WHERE clause (e.g. mcp_api_keys scope)


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------
#
# Order: children → parent. `agent_ownership` is the parent row and is
# managed by the caller; do not list it here.
#
# Multiple AgentRef entries per table are allowed for tables that have
# more than one agent-identifier column (agent_permissions,
# agent_event_subscriptions).
#
AGENT_REFS: List[AgentRef] = [
    # --- Sharing, scheduling, execution history ----------------------------
    AgentRef("agent_sharing",                "agent_name",        Policy.CASCADE),
    AgentRef("agent_schedules",              "agent_name",        Policy.CASCADE),
    AgentRef("schedule_executions",          "agent_name",        Policy.KEEP),

    # --- Agent loops (#740 Phase 1) ----------------------------------------
    # Loop ownership lives on the TARGET agent. `agent_loops` carries a
    # second column `source_agent_name` for the initiator (audit-trail
    # only — analogous to `chat_messages.user_id`); it's intentionally
    # NOT registered here. The parity test's regex doesn't match
    # `source_agent_name`, which matches that semantic distinction.
    # `agent_loop_runs` chains via `loop_id` and is cleaned up below in
    # LINK_CHAINED_DELETES (no agent column of its own).
    AgentRef("agent_loops",                  "agent_name",        Policy.CASCADE),

    # --- Chat / session history --------------------------------------------
    # Children before parents for future FK-enforced Postgres migration:
    # chat_messages → chat_sessions; agent_session_messages → agent_sessions.
    AgentRef("chat_messages",                "agent_name",        Policy.CASCADE),
    AgentRef("chat_sessions",                "agent_name",        Policy.CASCADE),
    AgentRef("agent_session_messages",       "agent_name",        Policy.CASCADE),
    AgentRef("agent_sessions",               "agent_name",        Policy.CASCADE),

    # --- Activity / notifications ------------------------------------------
    AgentRef("agent_activities",             "agent_name",        Policy.CASCADE),
    AgentRef("agent_notifications",          "agent_name",        Policy.CASCADE),

    # --- Agent-to-agent wiring ---------------------------------------------
    AgentRef("agent_permissions",            "source_agent",      Policy.CASCADE),
    AgentRef("agent_permissions",            "target_agent",      Policy.CASCADE),
    AgentRef("agent_event_subscriptions",    "subscriber_agent",  Policy.CASCADE),
    AgentRef("agent_event_subscriptions",    "source_agent",      Policy.CASCADE),
    AgentRef("agent_events",                 "source_agent",      Policy.CASCADE),

    # --- Files / shared folders --------------------------------------------
    AgentRef("agent_shared_folder_config",   "agent_name",        Policy.CASCADE),
    AgentRef("agent_shared_files",           "agent_name",        Policy.CASCADE),

    # --- Public links and chained tables -----------------------------------
    # Order: chained tables before agent_public_links so the link rows
    # still exist for the chained DELETEs to find. The chained tables
    # (public_link_*, public_chat_*, slack_link_connections) reference
    # agent_public_links.id, not agent_name — we delete them via JOIN.
    AgentRef("agent_public_links",           "agent_name",        Policy.CASCADE),
    AgentRef("public_user_memory",           "agent_name",        Policy.CASCADE),

    # --- Per-agent config ---------------------------------------------------
    AgentRef("agent_git_config",             "agent_name",        Policy.CASCADE),
    AgentRef("agent_sync_state",             "agent_name",        Policy.CASCADE),
    AgentRef("agent_skills",                 "agent_name",        Policy.CASCADE),
    AgentRef("agent_tags",                   "agent_name",        Policy.CASCADE),
    # #668 — latest compatibility snapshot (transient/recomputable): wipe on
    # delete, re-key on rename via this registry.
    AgentRef("agent_compatibility_results",  "agent_name",        Policy.CASCADE),

    # --- Monitoring / dashboards -------------------------------------------
    AgentRef("agent_health_checks",          "agent_name",        Policy.CASCADE),
    AgentRef("monitoring_alert_cooldowns",   "agent_name",        Policy.CASCADE),
    AgentRef("agent_dashboard_values",       "agent_name",        Policy.CASCADE),
    AgentRef("agent_dashboard_cache",        "agent_name",        Policy.CASCADE),
    # #918 — agent-published reports may hold sensitive domain payloads; wipe on
    # delete and re-key on rename so a reused agent name can't inherit another
    # tenant's reports (cross-tenant disclosure).
    AgentRef("agent_reports",                "agent_name",        Policy.CASCADE),

    # --- Channel adapters (encrypted bot tokens — security-relevant) -------
    AgentRef("slack_channel_agents",         "agent_name",        Policy.CASCADE),
    AgentRef("slack_active_threads",         "agent_name",        Policy.CASCADE),
    AgentRef("telegram_bindings",            "agent_name",        Policy.CASCADE),
    AgentRef("whatsapp_bindings",            "agent_name",        Policy.CASCADE),
    # VoIP (VOIP-001, #1056) — bindings carry encrypted Twilio creds; both
    # must rename WITH the agent (else calls break after rename) and be wiped
    # on purge.
    AgentRef("voip_bindings",                "agent_name",        Policy.CASCADE),
    AgentRef("voip_call_logs",               "agent_name",        Policy.CASCADE),

    # --- Monetization -------------------------------------------------------
    AgentRef("nevermined_agent_config",      "agent_name",        Policy.CASCADE),
    AgentRef("nevermined_payment_log",       "agent_name",        Policy.KEEP),
    AgentRef("subscription_rate_limit_events", "agent_name",      Policy.CASCADE),

    # --- Operations ---------------------------------------------------------
    AgentRef("operator_queue",               "agent_name",        Policy.CASCADE),
    AgentRef("access_requests",              "agent_name",        Policy.CASCADE),

    # --- MCP keys (scope='agent' only — user/system keys are not per-agent)
    AgentRef("mcp_api_keys",                 "agent_name",        Policy.CASCADE,
             extra_filter="scope = 'agent'"),
]


# Chained-via-link tables: these don't have an `agent_name` column but
# their rows are owned by an agent via `agent_public_links.id` or a
# channel-binding `id`. They are not in AGENT_REFS (parity check is
# scoped to agent-name-like columns); they are cleaned up by
# `cascade_delete()` using the parent-id JOIN below.
LINK_CHAINED_DELETES: List[tuple] = [
    # (table, link-id column, parent table, parent agent column)
    ("public_link_usage",         "link_id",    "agent_public_links", "agent_name"),
    ("public_link_verifications", "link_id",    "agent_public_links", "agent_name"),
    ("public_chat_sessions",      "link_id",    "agent_public_links", "agent_name"),
    # public_chat_messages chains via public_chat_sessions.id — handled below
    ("slack_link_connections",    "link_id",    "agent_public_links", "agent_name"),
    ("telegram_chat_links",       "binding_id", "telegram_bindings",   "agent_name"),
    ("telegram_group_configs",    "binding_id", "telegram_bindings",   "agent_name"),
    ("whatsapp_chat_links",       "binding_id", "whatsapp_bindings",   "agent_name"),
    # agent_loop_runs (#740) chains via loop_id → agent_loops.agent_name.
    # Must run BEFORE the AGENT_REFS delete of agent_loops itself (the
    # main cascade loop runs LINK_CHAINED_DELETES first, then
    # AGENT_REFS — so the JOIN still resolves).
    ("agent_loop_runs",           "loop_id",    "agent_loops",         "agent_name"),
]


def _table(table: str):
    """Resolve a registry table name to its Core ``Table`` handle from the
    shared ``db/tables.py`` MetaData."""
    from .tables import metadata

    return metadata.tables[table]


def _table_exists(conn, table: str) -> bool:
    """Lightweight existence check against the live database. Uses the
    SQLAlchemy inspector so it works identically on SQLite and PostgreSQL
    (replacing the former ``sqlite_master`` query)."""
    from sqlalchemy import inspect as sa_inspect

    return sa_inspect(conn).has_table(table)


def cascade_delete(conn, agent_name: str) -> Dict[str, int]:
    """
    Delete all CASCADE-policy rows referencing this agent.

    Caller is responsible for deleting the `agent_ownership` row itself
    after this returns (parent row last). `conn` is a SQLAlchemy
    ``Connection``; the transaction is managed by the caller
    (`get_engine().begin()` commits on context exit).

    Tables absent from the connected database are silently skipped —
    test DBs with a subset of the schema, and partial-install
    instances, both work without modification.

    Returns
    -------
    Dict mapping table name (with `:column` suffix for multi-column
    tables) to rows deleted. Useful for logging / audit / backfill
    reporting.
    """
    from sqlalchemy import delete as sa_delete, select as sa_select, text as sa_text

    deleted: Dict[str, int] = {}

    # Two-hop chained delete FIRST: public_chat_messages → session →
    # link. Must run BEFORE the single-hop LINK_CHAINED_DELETES loop
    # below — that loop deletes public_chat_sessions, and then there's
    # nothing left to join through.
    if (
        _table_exists(conn, "public_chat_messages")
        and _table_exists(conn, "public_chat_sessions")
        and _table_exists(conn, "agent_public_links")
    ):
        pcm = _table("public_chat_messages")
        pcs = _table("public_chat_sessions")
        apl = _table("agent_public_links")
        inner_links = sa_select(apl.c.id).where(apl.c.agent_name == agent_name)
        inner_sessions = sa_select(pcs.c.id).where(pcs.c.link_id.in_(inner_links))
        result = conn.execute(
            sa_delete(pcm).where(pcm.c.session_id.in_(inner_sessions))
        )
        if result.rowcount > 0:
            deleted["public_chat_messages"] = result.rowcount

    # Single-hop chained link tables — they reference rows we're about
    # to delete from agent_public_links / telegram_bindings / etc.
    for table, link_col, parent_table, parent_col in LINK_CHAINED_DELETES:
        if not (_table_exists(conn, table) and _table_exists(conn, parent_table)):
            continue
        child = _table(table)
        parent = _table(parent_table)
        inner = sa_select(parent.c.id).where(parent.c[parent_col] == agent_name)
        result = conn.execute(
            sa_delete(child).where(child.c[link_col].in_(inner))
        )
        if result.rowcount > 0:
            deleted[table] = result.rowcount

    # Main CASCADE loop
    for ref in AGENT_REFS:
        if ref.policy != Policy.CASCADE:
            continue
        if not _table_exists(conn, ref.table):
            continue
        tbl = _table(ref.table)
        stmt = sa_delete(tbl).where(tbl.c[ref.column] == agent_name)
        if ref.extra_filter:
            stmt = stmt.where(sa_text(ref.extra_filter))
        result = conn.execute(stmt)
        if result.rowcount > 0:
            key = f"{ref.table}:{ref.column}" if _multi_column(ref.table) else ref.table
            deleted[key] = deleted.get(key, 0) + result.rowcount

    return deleted


def cascade_rename(conn, old_name: str, new_name: str) -> Dict[str, int]:
    """
    Update every agent reference from `old_name` to `new_name`.

    Touches every entry in `AGENT_REFS` regardless of policy — rename is
    a column-value update; the delete policy doesn't apply. Chained-link
    tables don't carry `agent_name` so no rename pass needed for them.

    Caller is responsible for renaming `agent_ownership` itself and
    handling the uniqueness check on the destination name.
    """
    from sqlalchemy import text as sa_text, update as sa_update

    updated: Dict[str, int] = {}

    for ref in AGENT_REFS:
        if not _table_exists(conn, ref.table):
            continue
        tbl = _table(ref.table)
        stmt = (
            sa_update(tbl)
            .where(tbl.c[ref.column] == old_name)
            .values({ref.column: new_name})
        )
        if ref.extra_filter:
            stmt = stmt.where(sa_text(ref.extra_filter))
        result = conn.execute(stmt)
        if result.rowcount > 0:
            key = f"{ref.table}:{ref.column}" if _multi_column(ref.table) else ref.table
            updated[key] = updated.get(key, 0) + result.rowcount

    return updated


def find_orphan_agent_names(conn) -> Dict[str, int]:
    """
    Return mapping of {orphan_agent_name: row_count} aggregated across
    every CASCADE-policy table. Used by the backfill script and as the
    truth source for what `cascade_delete()` would clean up.

    An "orphan" is an agent_name value present in a registered table
    but absent from `agent_ownership`.
    """
    from sqlalchemy import func, select as sa_select, text as sa_text

    ao = _table("agent_ownership")
    known = {
        row[0]
        for row in conn.execute(sa_select(ao.c.agent_name)).all()
    }

    if not known:
        # No agents exist; every row everywhere is technically orphan.
        # Refuse to operate to avoid wiping a fresh-install DB.
        return {}

    known_params = list(known)
    counts: Dict[str, int] = {}

    for ref in AGENT_REFS:
        if ref.policy != Policy.CASCADE:
            continue
        if not _table_exists(conn, ref.table):
            continue
        tbl = _table(ref.table)
        name_col = tbl.c[ref.column]
        stmt = (
            sa_select(name_col.label("name"), func.count().label("n"))
            .where(name_col.notin_(known_params))
        )
        if ref.extra_filter:
            stmt = stmt.where(sa_text(ref.extra_filter))
        stmt = stmt.group_by(name_col)
        for row in conn.execute(stmt).all():
            name = row[0]
            if name is None:
                continue
            counts[name] = counts.get(name, 0) + int(row[1])

    return counts


def _multi_column(table: str) -> bool:
    """True if `table` appears in AGENT_REFS with more than one column."""
    seen: set = set()
    for ref in AGENT_REFS:
        if ref.table == table:
            seen.add(ref.column)
    return len(seen) > 1


# Convenience accessors for parity test --------------------------------------

def registered_columns() -> List[tuple]:
    """Return (table, column) pairs in registration order."""
    return [(r.table, r.column) for r in AGENT_REFS]
