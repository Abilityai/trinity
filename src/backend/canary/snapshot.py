"""
Canary snapshot collector (CANARY-001 / Issue #411 — Phase 1).

Gathers a roughly-simultaneous read of orchestration state across:

- SQLite — agent ownership, execution rows (running + queued), plus per-table
  agent_name references for the L-03 orphan scan.
- Redis — agent slot ZSETs (`agent:slots:{name}`).
- Vector logs — deferred to Phase 2; E-02 uses a state-comparison detector
  in this phase (see invariants/e02_no_phantom_reversal.py for rationale).
- Agent registries / container exec — deferred to Phase 2 invariants.

The collector is pure read. It writes nothing. Invariant library functions
take the resulting `Snapshot` and return zero-or-more `ViolationReport`s.

Phase 1 scope is S-01, E-02, L-03 — the rest of the design doc's snapshot
fields are placeholders until their invariants land.

## Why a separate module from the invariants

The three Phase 1 invariants (S-01, E-02, L-03) all read overlapping
state. Splitting state collection out gives three things:

1. **One consistent view per cycle.** All invariants see the same
   `Snapshot` instance, so per-check timing drift cannot introduce
   spurious mismatches — e.g. L-03 reading the SQL `agent_ownership`
   set after S-01 has already started ZRANGEing on agents that were
   live a moment earlier.
2. **No duplicated query code.** New invariants are pure functions
   `(snapshot) → list[ViolationReport]`; they never re-implement
   SELECTs or ZRANGEs against live state. This keeps the registry in
   `invariants/__init__.py` the only file the catalog grows in.
3. **Test-friendly.** Tests pass synthetic `Snapshot` dataclasses
   straight in (see `tests/test_canary_invariants.py`) and never
   need a live Redis or SQLite to exercise the checking logic.

Note: the snapshot is *not* atomic across Redis and SQLite — those
don't share transactions, and our reads are sequential. The harness
deliberately accepts sub-second inconsistencies (a real bug persists
across a 5-minute cycle by definition; transient races self-resolve
and are not what we're trying to catch).
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from models import TaskExecutionStatus
from utils.helpers import iso_cutoff, utc_now_iso


logger = logging.getLogger(__name__)


# Statuses considered "terminal" for execution rows. Derived directly
# from `TaskExecutionStatus` (models.py) — the same set PR #524's CAS
# state machine treats as write-once. Used by E-02 (phantom reversal
# detection) and the L-03 orphan scan filter. Sourcing from the enum
# means a new terminal status added there flows here automatically;
# the previous hand-maintained tuple silently drifted (see /review I3).
TERMINAL_EXECUTION_STATUSES = (
    TaskExecutionStatus.SUCCESS.value,
    TaskExecutionStatus.FAILED.value,
    TaskExecutionStatus.CANCELLED.value,
    TaskExecutionStatus.SKIPPED.value,
)
_TERMINAL_SQL_LIST = ", ".join(f"'{s}'" for s in TERMINAL_EXECUTION_STATUSES)


# Tables whose `agent_name` column references `agent_ownership.agent_name`.
# Used by L-03 (delete cascades) to scan for orphan rows.
#
# Exclusions:
# - `chat_messages` — denormalized via `chat_sessions`; covered transitively.
# - `agent_health_checks`, `agent_dashboard_values` — observational tables
#   that legitimately retain history of deleted agents (rolled up by retention).
# - `nevermined_payment_log` — append-only audit; deletes do not cascade by design.
# - `monitoring_alert_cooldowns` — cooldown TTL handles cleanup.
#
# The list intentionally errs on the side of catching more orphans rather
# than fewer; false positives surface as L-03 violations operators triage.
ORPHAN_SCAN_TABLES = [
    ("agent_sharing", "agent_name", None),
    ("agent_schedules", "agent_name", None),
    # Only non-terminal executions; terminal rows are immutable history per
    # PR #524's CAS-guarded state machine and may legitimately reference a
    # later-deleted agent.
    (
        "schedule_executions",
        "agent_name",
        f"status NOT IN ({_TERMINAL_SQL_LIST})",
    ),
    ("chat_sessions", "agent_name", "status = 'active'"),
    ("agent_skills", "agent_name", None),
    ("agent_tags", "agent_name", None),
    ("agent_shared_files", "agent_name", None),
    ("agent_public_links", "agent_name", None),
    ("operator_queue", "agent_name", "status = 'pending'"),
    ("access_requests", "agent_name", "status = 'pending'"),
]


@dataclass
class OrphanRef:
    """One orphan row found during the L-03 scan."""

    table: str
    column: str
    referenced_agent_name: str
    row_id: str  # Stringified primary key (TEXT or INTEGER)


@dataclass
class ViolationReport:
    """Output of an invariant check that fired.

    Mirrors the canary_violations table schema so the run-cycle endpoint
    can persist these directly.
    """

    invariant_id: str
    tier: str  # 'A' or 'B'
    severity: str  # 'critical' | 'major' | 'minor'
    observed_state: Dict[str, Any]
    signal_query: Optional[str] = None


@dataclass
class AgentSnapshot:
    """Per-agent slice of the snapshot."""

    name: str
    is_system: bool
    max_parallel: int
    execution_timeout_seconds: int
    # Redis ZSET membership for `agent:slots:{name}`. Drain sentinels
    # (members starting with 'drain-') are filtered out by S-01 before the
    # bijection check; we keep the raw set here so other invariants can see
    # them if needed.
    slot_ids: Set[str] = field(default_factory=set)
    # ZSET score per slot (Unix epoch seconds at acquire); used by S-01 grace.
    slot_scores: Dict[str, float] = field(default_factory=dict)
    # SQLite execution_id sets, partitioned by status.
    running_exec_ids: Set[str] = field(default_factory=set)
    # `started_at` per running id (ISO); used by S-01 grace.
    running_started_at: Dict[str, str] = field(default_factory=dict)
    queued_exec_ids: Set[str] = field(default_factory=set)


@dataclass
class Snapshot:
    """Full snapshot at one moment in time."""

    snapshot_time: str  # ISO 8601 UTC
    agents: List[AgentSnapshot] = field(default_factory=list)
    # All known agent names (from agent_ownership). Source of truth for L-03.
    known_agents: Set[str] = field(default_factory=set)
    # L-03 inputs: orphan rows found via cross-table scan.
    orphan_refs: List[OrphanRef] = field(default_factory=list)
    # Redis slot keys observed for agents NOT in known_agents (also L-03).
    orphan_redis_slots: Dict[str, int] = field(default_factory=dict)
    # E-02 inputs: terminal-state map per execution_id in the most recent
    # snapshot. The check compares this against a stored "previously
    # terminal" set fetched from Redis to detect reversals. The status
    # value (success/failed/cancelled/skipped) is preserved so reversal
    # alerts can render the real prior status, not a placeholder.
    terminal_exec_statuses: Dict[str, str] = field(default_factory=dict)
    # Diagnostics — empty on a clean cycle.
    sources_unavailable: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------


def _collect_known_agents() -> List[Dict[str, Any]]:
    """Read agent_ownership rows. One source of truth for valid agent names."""
    from db.connection import get_db_connection

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT agent_name,
                   COALESCE(is_system, 0) AS is_system,
                   COALESCE(max_parallel_tasks, 3) AS max_parallel_tasks,
                   COALESCE(execution_timeout_seconds, 900) AS execution_timeout_seconds
            FROM agent_ownership
            """
        )
        return [dict(row) for row in cursor.fetchall()]


def _collect_executions(agent_name: str) -> Dict[str, Set[str]]:
    """Per-agent running + queued execution_ids."""
    from db.connection import get_db_connection

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, status, started_at FROM schedule_executions "
            "WHERE agent_name = ? AND status IN ('running', 'queued')",
            (agent_name,),
        )
        out: Dict[str, Any] = {"running": set(), "queued": set(), "started_at": {}}
        for row in cursor.fetchall():
            if row["status"] == "running":
                out["running"].add(row["id"])
                if row["started_at"]:
                    out["started_at"][row["id"]] = row["started_at"]
            elif row["status"] == "queued":
                out["queued"].add(row["id"])
        return out


def _collect_terminal_executions(window_minutes: int = 30) -> Dict[str, str]:
    """Recent terminal execution_ids → status (for E-02 reversal detection).

    Bounding the window keeps the comparison set small. Reversals are
    expected within minutes of the original transition; older terminal
    rows reverting would also indicate corruption but at vanishingly low
    base rate, and would be caught by E-01 (terminal-state closure) too.

    Returns a dict so E-02 can persist the *real* prior status (success
    / failed / cancelled / skipped) into its Redis side-table — the
    reversal alert prints that back to the operator, and a placeholder
    string ("terminal") would erase the forensic value of the alert.
    """
    from db.connection import get_db_connection

    placeholders = ",".join("?" * len(TERMINAL_EXECUTION_STATUSES))
    cutoff = iso_cutoff(minutes=int(window_minutes))
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT id, status FROM schedule_executions
            WHERE status IN ({placeholders})
              AND completed_at > ?
            """,
            (*TERMINAL_EXECUTION_STATUSES, cutoff),
        )
        return {row["id"]: row["status"] for row in cursor.fetchall()}


def _collect_orphan_refs(known_agents: Set[str]) -> List[OrphanRef]:
    """Scan cross-table agent_name refs for any not in known_agents.

    Driven by ORPHAN_SCAN_TABLES. Each tuple is (table, column, optional
    SQL filter clause that further narrows what counts as 'live').
    """
    from db.connection import get_db_connection

    refs: List[OrphanRef] = []
    if not known_agents:
        return refs  # nothing to compare against; scan would mark every row

    placeholder_list = ",".join("?" * len(known_agents))
    known_params = list(known_agents)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        for table, column, extra_filter in ORPHAN_SCAN_TABLES:
            # Discover the primary-key column name so we can return a
            # stable row_id without hardcoding per-table schemas.
            cursor.execute(f"PRAGMA table_info({table})")
            cols = cursor.fetchall()
            if not cols:
                # Table not present (test DB or partial install). Skip.
                continue
            pk_col = next((c["name"] for c in cols if c["pk"]), None)
            if pk_col is None:
                # Composite-PK or no-PK tables get a synthetic row_id.
                pk_expr = f"'{table}-row'"
            else:
                pk_expr = pk_col

            where = f"{column} NOT IN ({placeholder_list})"
            if extra_filter:
                where += f" AND ({extra_filter})"

            cursor.execute(
                f"SELECT {pk_expr} AS row_id, {column} AS agent_name "
                f"FROM {table} WHERE {where}",
                known_params,
            )
            for row in cursor.fetchall():
                refs.append(
                    OrphanRef(
                        table=table,
                        column=column,
                        referenced_agent_name=row["agent_name"],
                        row_id=str(row["row_id"]),
                    )
                )

        # Agent-scoped MCP keys: same logic, separate filter on `scope`.
        cursor.execute("PRAGMA table_info(mcp_api_keys)")
        cols = cursor.fetchall()
        if cols:
            cursor.execute(
                f"""
                SELECT id, agent_name FROM mcp_api_keys
                WHERE scope = 'agent'
                  AND agent_name IS NOT NULL
                  AND agent_name NOT IN ({placeholder_list})
                """,
                known_params,
            )
            for row in cursor.fetchall():
                refs.append(
                    OrphanRef(
                        table="mcp_api_keys",
                        column="agent_name",
                        referenced_agent_name=row["agent_name"],
                        row_id=str(row["id"]),
                    )
                )

    return refs


def _collect_redis_slot_state(known_agents: Set[str]) -> Dict[str, Dict[str, Any]]:
    """Per-agent Redis slot ZSET membership + scan for orphan slot keys.

    Returns dict with two keys:
      "by_agent": {agent_name: set(execution_ids)} for known agents
      "orphan_slots": {agent_name_in_key: count} for keys matching agents
                      NOT in agent_ownership
    """
    from services.slot_service import get_slot_service

    slot_service = get_slot_service()
    redis_client = slot_service.redis
    prefix = slot_service.slots_prefix

    by_agent: Dict[str, Set[str]] = {}
    scores: Dict[str, Dict[str, float]] = {}
    orphan_slots: Dict[str, int] = {}

    # Per-agent ZRANGE for known agents (with scores for S-01 grace).
    for name in known_agents:
        with_scores = redis_client.zrange(f"{prefix}{name}", 0, -1, withscores=True)
        by_agent[name] = {m for m, _ in with_scores}
        scores[name] = {m: float(s) for m, s in with_scores}

    # SCAN for orphan keys (agent name in the key but not in known set).
    cursor = 0
    while True:
        cursor, keys = redis_client.scan(
            cursor=cursor, match=f"{prefix}*", count=200
        )
        for key in keys:
            # `decode_responses=True` on the slot_service client; key is str.
            name = key[len(prefix):]
            if name not in known_agents:
                orphan_slots[name] = redis_client.zcard(key)
        if cursor == 0:
            break

    return {"by_agent": by_agent, "scores": scores, "orphan_slots": orphan_slots}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_snapshot() -> Snapshot:
    """Collect one complete snapshot.

    Sources that fail (e.g. Redis unreachable) are recorded in
    `sources_unavailable` and the snapshot is still returned with whatever
    succeeded. Invariant checks are responsible for skipping cycles when
    their required sources are absent — see each invariant for the policy.
    """
    snap = Snapshot(snapshot_time=utc_now_iso())

    # SQLite: agent_ownership is the source of truth for "known agents".
    try:
        agent_rows = _collect_known_agents()
    except Exception as exc:
        logger.exception("canary snapshot: agent_ownership read failed")
        snap.sources_unavailable.append(f"sqlite.agent_ownership: {exc}")
        return snap

    snap.known_agents = {row["agent_name"] for row in agent_rows}

    # Redis slot state (scan once for both per-agent and orphan keys).
    redis_state: Dict[str, Any] = {"by_agent": {}, "scores": {}, "orphan_slots": {}}
    try:
        redis_state = _collect_redis_slot_state(snap.known_agents)
        snap.orphan_redis_slots = redis_state["orphan_slots"]
    except Exception as exc:
        logger.exception("canary snapshot: redis read failed")
        snap.sources_unavailable.append(f"redis: {exc}")

    # SQLite: per-agent running/queued executions.
    for row in agent_rows:
        name = row["agent_name"]
        try:
            execs = _collect_executions(name)
        except Exception as exc:
            logger.exception("canary snapshot: executions read failed for %s", name)
            snap.sources_unavailable.append(f"sqlite.executions[{name}]: {exc}")
            execs = {"running": set(), "queued": set()}

        snap.agents.append(
            AgentSnapshot(
                name=name,
                is_system=bool(row["is_system"]),
                max_parallel=int(row["max_parallel_tasks"]),
                execution_timeout_seconds=int(row["execution_timeout_seconds"]),
                slot_ids=redis_state["by_agent"].get(name, set()),
                slot_scores=redis_state["scores"].get(name, {}),
                running_exec_ids=execs["running"],
                running_started_at=execs.get("started_at", {}),
                queued_exec_ids=execs["queued"],
            )
        )

    # SQLite: orphan refs across cross-cutting tables (L-03).
    try:
        snap.orphan_refs = _collect_orphan_refs(snap.known_agents)
    except Exception as exc:
        logger.exception("canary snapshot: orphan ref scan failed")
        snap.sources_unavailable.append(f"sqlite.orphan_refs: {exc}")

    # SQLite: terminal execution ids → status for E-02 detector.
    try:
        snap.terminal_exec_statuses = _collect_terminal_executions()
    except Exception as exc:
        logger.exception("canary snapshot: terminal executions read failed")
        snap.sources_unavailable.append(f"sqlite.terminal_executions: {exc}")

    return snap
