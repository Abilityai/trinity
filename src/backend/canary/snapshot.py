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

Backend seam (#300 / #1450): most collector reads still go through the raw
sqlite3 `db.connection.get_db_connection()` at DB_PATH — a #300 follow-up
migrates them to `get_engine()`/DATABASE_URL. B-01's queued Side B is the
first read migrated: `_collect_queued_ids_via_engine` reads through
`get_engine()` so both B-01 sides honor DATABASE_URL and stay
backend-consistent on Postgres (the accessor already did). The collector is
therefore *half-migrated* — B-01's queued id-set is engine-backed while the
running-side / known-agents / orphan reads remain raw-sqlite. That split is
invisible on SQLite (one file) and tracked for the running side in the
collector-wide migration follow-up.
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
    # #918 — CASCADE table holding agent-published report payloads (can be
    # sensitive); watch for orphans referencing a deleted agent.
    ("agent_reports", "agent_name", None),
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
    # `started_at` per running id (ISO); used by S-01 grace + E-01 / E-05 age.
    running_started_at: Dict[str, str] = field(default_factory=dict)
    # `claude_session_id` per running id (str or None); used by E-05 to detect
    # dispatched rows that never acquired a backing session.
    running_claude_session_ids: Dict[str, Optional[str]] = field(default_factory=dict)
    # Raw-sqlite queued id-set from `_collect_executions` (reads
    # `db.connection.get_db_connection()` at DB_PATH). Consumed by B-02 and
    # E-02. **No longer B-01's Side B** (#1450): on Postgres this raw-sqlite
    # file diverges from the `get_engine()`/DATABASE_URL backend the accessor
    # uses, so B-01 moved to `queued_ids_via_engine` below to keep both of its
    # sides on the same backend.
    queued_exec_ids: Set[str] = field(default_factory=set)
    # `db.get_queued_count(name)` — the production accessor BacklogService
    # calls on every enqueue/drain (goes through `get_engine()`, honoring
    # DATABASE_URL). B-01 Side A. Compared against `len(queued_ids_via_engine)`
    # (Side B, below) so a divergence between the two query paths (e.g. a
    # future cache layer on the accessor, or a status-filter regression)
    # surfaces as a violation rather than going silent. `None` means the
    # accessor was unavailable this cycle (import error in test mode, or the
    # collector's confirm-re-read could not be completed) and B-01 must skip.
    queued_count_via_service: Optional[int] = None
    # B-01 Side B (#1450): queued execution ids read via the SAME `get_engine()`
    # seam as `db.get_queued_count`, so both B-01 sides honor DATABASE_URL and
    # compare like-for-like on Postgres (not raw-sqlite vs engine). Independent
    # code path from the accessor (`SELECT id`/literal 'queued' here vs
    # `COUNT(*)`/the TaskExecutionStatus.QUEUED enum in db/schedules.py) — shares
    # a database, not a code path, so a cache/status-filter regression on the
    # accessor still surfaces (non-tautology). `None` means the engine read
    # failed this cycle (or the collector's confirm-re-read could not be
    # completed) → B-01 skips this agent rather than comparing against a
    # different backend's row set.
    queued_ids_via_engine: Optional[Set[str]] = None
    # Per-slot Redis TTL on the companion `agent:slot:{name}:{eid}` HASH.
    # Value semantics from `redis.ttl()`: positive int = seconds until
    # expiry; -2 = key does not exist; -1 = key exists with no TTL. S-03
    # uses this to detect slots whose metadata expired prematurely
    # (#226 bug class). Empty dict means the per-slot read was skipped
    # this cycle (Redis unavailable); the check skips silently.
    slot_ttls: Dict[str, int] = field(default_factory=dict)
    # #506: stored `max_parallel` clamped to the fleet ceiling. S-01/S-02/S-03
    # keep using the raw `max_parallel` (clamping only lowers ZCARD vs stored,
    # so the no-overbooking bound stays valid), but B-02 must compare slot
    # count to the EFFECTIVE cap — under a lower ceiling an effective-full +
    # queued agent would otherwise read as "free slots → drain stalled" and
    # false-fire. None ⇒ fall back to `max_parallel` (test constructors that
    # predate the ceiling).
    effective_max_parallel: Optional[int] = None


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
    # B-02 input: unix timestamp of the most recent successful
    # `CapacityManager.run_maintenance()` sweep, written to
    # `canary:drain_tick_at` at the END of the sweep so a mid-sweep crash
    # leaves the value stale. `None` means the key has never been written
    # (cold cluster or Redis unavailable) — B-02 treats that as "no
    # drain has ever run" and skips its time-window arm.
    drain_tick_at: Optional[float] = None
    # R-01 input: per-agent zombie process count (`ps -eo stat,comm | grep
    # ' Z.*claude' | wc -l`). Populated by docker_exec'ing into every
    # running `trinity.platform=agent` container. Missing agent name in
    # this map means the exec failed for that container — recorded in
    # `sources_unavailable` and the R-01 check skips that agent rather
    # than firing.
    zombie_counts: Dict[str, int] = field(default_factory=dict)
    # E-06 input: enabled, non-deleted schedules → {schedule_id, agent_name,
    # next_run_at}. The check flags any whose next_run_at is more than the
    # misfire grace behind the snapshot time (a stale projection the scheduler
    # never advanced — the "Next: Nd ago" bug, #1472). Empty means the read was
    # skipped (recorded in sources_unavailable).
    enabled_schedules: List[Dict[str, Any]] = field(default_factory=list)
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
        # Intentionally NOT filtering `deleted_at IS NULL` (#834). The
        # canary's `known_agents` set drives L-03 (orphan-row detection)
        # — soft-deleted-pending-purge agents legitimately have child
        # rows in the live tables until the retention sweep runs.
        # Treating them as "unknown" would surface those preserved rows
        # as false-positive orphans.
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


def _collect_executions(agent_name: str) -> Dict[str, Any]:
    """Per-agent running + queued execution_ids.

    Adds `claude_session_id` for running rows (E-05) — fetched in the same
    query so the canary cycle stays O(N agents) and never grows per-row.
    The column has been on `schedule_executions` since #106; rows predating
    that migration return NULL and are tolerated by the E-05 grace window.
    """
    from db.connection import get_db_connection

    with get_db_connection() as conn:
        cursor = conn.cursor()
        # `claude_session_id` may not exist in the minimal test DDLs; guard
        # with a PRAGMA introspection and select * if absent so the unit
        # tests don't have to mirror every production column.
        cursor.execute("PRAGMA table_info(schedule_executions)")
        cols = {c["name"] for c in cursor.fetchall()}
        has_session_col = "claude_session_id" in cols
        select_cols = "id, status, started_at"
        if has_session_col:
            select_cols += ", claude_session_id"
        cursor.execute(
            f"SELECT {select_cols} FROM schedule_executions "
            "WHERE agent_name = ? AND status IN ('running', 'queued')",
            (agent_name,),
        )
        out: Dict[str, Any] = {
            "running": set(),
            "queued": set(),
            "started_at": {},
            "claude_session_ids": {},
        }
        for row in cursor.fetchall():
            if row["status"] == "running":
                out["running"].add(row["id"])
                if row["started_at"]:
                    out["started_at"][row["id"]] = row["started_at"]
                out["claude_session_ids"][row["id"]] = (
                    row["claude_session_id"] if has_session_col else None
                )
            elif row["status"] == "queued":
                out["queued"].add(row["id"])
        return out


def _collect_zombie_counts() -> Dict[str, Any]:
    """Per-running-agent zombie-process count via Docker exec.

    For every running container labeled `trinity.platform=agent`, runs
    `sh -c "ps -eo stat,comm | grep '^Z.*claude' | wc -l"` and parses
    the integer result. Used by R-01 to detect unreaped Claude child
    processes (#407 bug class).

    The shell command pattern matters: `STAT` is the first column from
    `ps -eo stat,comm`, and a zombie's STAT field is `Z` (sometimes
    with suffixes like `Z+`). `^Z` anchors at the start of the line —
    procps-ng on the agent base image emits STAT left-aligned with no
    leading space for single-letter codes, so the catalog's space-Z
    pattern misses. Verified live against a real zombie spawned via
    `os.fork()` + `prctl(PR_SET_NAME, "claude")`.

    Returns a dict with two keys:
      "counts":     {agent_name: int}   for containers we successfully exec'd.
      "unavailable": [str, ...]         per-agent failure messages for the
                                        caller to append to sources_unavailable.

    All-or-nothing failure (e.g. docker_client None) returns
    {"counts": {}, "unavailable": ["docker: <reason>"]}.
    """
    out: Dict[str, Any] = {"counts": {}, "unavailable": []}
    try:
        from services.docker_service import docker_client
    except Exception as exc:
        out["unavailable"].append(f"docker.import: {exc}")
        return out
    if docker_client is None:
        out["unavailable"].append("docker: client unavailable")
        return out

    try:
        containers = docker_client.containers.list(
            filters={"label": "trinity.platform=agent", "status": "running"},
        )
    except Exception as exc:
        out["unavailable"].append(f"docker.list: {exc}")
        return out

    # The catalog spec uses ` Z.*claude` (leading-space), but procps-ng on
    # the agent base image emits STAT left-aligned with NO leading space
    # for single-letter codes. Anchor at start-of-line instead — same
    # intent (STAT field begins with Z), works across both formatters.
    cmd = ["sh", "-c", "ps -eo stat,comm | grep '^Z.*claude' | wc -l"]
    for container in containers:
        # Container name is the canonical agent identifier (handles renames
        # correctly per docker_service.list_all_agents_fast). Strip the
        # historical `agent-` prefix to align with agent_ownership.agent_name.
        agent_name = container.name.removeprefix("agent-")
        try:
            result = container.exec_run(cmd)
            raw = result.output
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            count = int((raw or "0").strip().splitlines()[-1])
            out["counts"][agent_name] = count
        except Exception as exc:
            # Per-container failure should not poison the cycle — just
            # record and skip; R-01 will skip this agent.
            out["unavailable"].append(f"docker.exec[{agent_name}]: {exc}")

    return out


def _collect_queued_count_via_service(agent_name: str) -> Optional[int]:
    """Call the production `db.get_queued_count` accessor.

    Used by B-01: we compare what this returns against the snapshot's
    independently-collected `len(queued_exec_ids)` so that any drift
    between the service-layer accessor and a direct SELECT — a cache
    layer, a status-filter regression, anything — surfaces as a
    violation. Returns `None` on import or attribute error so unit
    tests (which stub `db.connection` but not the full `database`
    facade) can still build snapshots; the B-01 check then skips that
    agent rather than firing a false positive.
    """
    try:
        from database import db
        return int(db.get_queued_count(agent_name))
    except Exception:  # pragma: no cover - exercised in unit tests via stubbing
        logger.debug(
            "canary snapshot: db.get_queued_count unavailable for %s; "
            "B-01 will skip this agent",
            agent_name,
        )
        return None


def _collect_queued_ids_via_engine(agent_name: str) -> Set[str]:
    """B-01 Side B: queued execution ids via the SAME `get_engine()` seam as
    `db.get_queued_count` (#1450), so both sides honor DATABASE_URL.

    Independent code path from the production accessor — `SELECT id` here vs
    `COUNT(*)` in `db/schedules.py`, and the literal ``'queued'`` here vs the
    ``TaskExecutionStatus.QUEUED`` enum there — so a cache or status-filter
    regression on the accessor still surfaces (non-tautology). Shares a
    *database* with Side A, not a *code path*.

    Raises on failure; the caller forces a B-01 skip rather than falling back
    to the raw-sqlite id-set, which on Postgres would re-arm the very
    backend-divergence gap this closes (comparing an engine count against a
    stale/absent SQLite file).
    """
    from sqlalchemy import and_, select
    from db.engine import get_engine
    from db.tables import schedule_executions

    stmt = select(schedule_executions.c.id).where(
        and_(
            schedule_executions.c.agent_name == agent_name,
            schedule_executions.c.status == "queued",
        )
    )
    with get_engine().connect() as conn:
        return {row[0] for row in conn.execute(stmt)}


def _clamp_to_ceiling(stored_max_parallel: int) -> int:
    """Clamp a stored per-agent cap to the fleet ceiling for B-02 (#506).

    Defensive: if the settings read fails (settings_service / database
    unavailable under unit-test stubs), fall back to the stored value so the
    snapshot never crashes — B-02 then behaves exactly as before the ceiling.
    """
    try:
        from services.settings_service import clamp_to_ceiling
        return clamp_to_ceiling(stored_max_parallel)
    except Exception:  # pragma: no cover - defensive, exercised via stubbing
        return stored_max_parallel


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


def _collect_enabled_schedules() -> List[Dict[str, Any]]:
    """Enabled, non-deleted schedules → {schedule_id, agent_name, next_run_at}
    for E-06 (stale next_run_at detection, #1472). Mirrors the scheduler's own
    read predicate (`enabled = 1 AND deleted_at IS NULL`)."""
    from db.connection import get_db_connection

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, agent_name, next_run_at
            FROM agent_schedules
            WHERE enabled = 1 AND deleted_at IS NULL
            """
        )
        return [
            {
                "schedule_id": row["id"],
                "agent_name": row["agent_name"],
                "next_run_at": row["next_run_at"],
            }
            for row in cursor.fetchall()
        ]


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

    Returns dict with these keys:
      "by_agent": {agent_name: set(execution_ids)} for known agents
      "scores":   {agent_name: {execution_id: zset_score}}
      "slot_ttls": {agent_name: {execution_id: ttl_seconds}} — per-slot
                   metadata HASH TTLs read for S-03 (one TTL call per slot;
                   bounded by ZCARD which is ≤ max_parallel_tasks).
      "orphan_slots": {agent_name_in_key: count} for keys matching agents
                      NOT in agent_ownership
    """
    from services.slot_service import get_slot_service

    slot_service = get_slot_service()
    redis_client = slot_service.redis
    prefix = slot_service.slots_prefix
    metadata_prefix = slot_service.metadata_prefix

    by_agent: Dict[str, Set[str]] = {}
    scores: Dict[str, Dict[str, float]] = {}
    slot_ttls: Dict[str, Dict[str, int]] = {}
    orphan_slots: Dict[str, int] = {}

    # Per-agent ZRANGE for known agents (with scores for S-01 grace).
    # Per-slot TTL lookup for S-03 — `redis.ttl()` semantics: positive int
    # is seconds until expiry; -2 means the key doesn't exist; -1 means
    # the key exists without a TTL. All three are surfaced verbatim and
    # interpreted in the S-03 invariant check.
    for name in known_agents:
        with_scores = redis_client.zrange(f"{prefix}{name}", 0, -1, withscores=True)
        by_agent[name] = {m for m, _ in with_scores}
        scores[name] = {m: float(s) for m, s in with_scores}
        ttl_map: Dict[str, int] = {}
        for eid, _ in with_scores:
            # Drain sentinels are intentionally short-lived; skip the TTL
            # check for them (S-03 only cares about real execution slots).
            if eid.startswith("drain-"):
                continue
            try:
                ttl_map[eid] = int(redis_client.ttl(f"{metadata_prefix}{name}:{eid}"))
            except Exception:
                # Per-slot TTL failure should not poison the whole map; the
                # missing entry simply means S-03 skips that slot.
                continue
        slot_ttls[name] = ttl_map

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

    return {
        "by_agent": by_agent,
        "scores": scores,
        "slot_ttls": slot_ttls,
        "orphan_slots": orphan_slots,
    }


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
    redis_state: Dict[str, Any] = {
        "by_agent": {},
        "scores": {},
        "slot_ttls": {},
        "orphan_slots": {},
    }
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
            execs = {
                "running": set(),
                "queued": set(),
                "started_at": {},
                "claude_session_ids": {},
            }

        # B-01 inputs (#1450): both sides go through the `get_engine()` seam so
        # they honor DATABASE_URL (backend-consistent on Postgres). Side A is
        # the production accessor `db.get_queued_count`; Side B is an
        # independent `SELECT id` over the same engine. On an engine-read
        # failure, force a B-01 skip (`service_count=None`) — never fall back to
        # the raw-sqlite `execs["queued"]` set for the comparison, which would
        # re-arm the backend-divergence gap on Postgres.
        try:
            engine_qids: Optional[Set[str]] = _collect_queued_ids_via_engine(name)
        except Exception as exc:
            logger.exception(
                "canary snapshot: engine queued-id read failed for %s", name
            )
            snap.sources_unavailable.append(f"engine.queued_ids[{name}]: {exc}")
            engine_qids = None

        queued_via_service = _collect_queued_count_via_service(name)

        # Temporal-race tolerance (#1450 gap a): the two reads happen at
        # different instants; a concurrent enqueue/backlog-drain landing
        # between them yields a transient mismatch. Confirm once — a real drift
        # persists across the re-read, a race self-resolves. An unconfirmable
        # confirm degrades to a skip (don't fire critical on a single
        # unconfirmed mismatch); a confirm pair that STILL disagrees is stored
        # verbatim so B-01 can still catch a persistent drift (a rare
        # double-straddle is an accepted, self-healing residual).
        if (
            engine_qids is not None
            and queued_via_service is not None
            and queued_via_service != len(engine_qids)
        ):
            try:
                confirm_ids = _collect_queued_ids_via_engine(name)
                confirm_count = _collect_queued_count_via_service(name)
            except Exception as exc:
                logger.warning(
                    "canary B-01 confirm re-read failed for %s: %s", name, exc
                )
                queued_via_service = None  # unconfirmable → B-01 skips this cycle
            else:
                if confirm_count is None:
                    queued_via_service = None  # accessor gone → skip
                else:
                    engine_qids, queued_via_service = confirm_ids, confirm_count

        stored_max_parallel = int(row["max_parallel_tasks"])
        snap.agents.append(
            AgentSnapshot(
                name=name,
                is_system=bool(row["is_system"]),
                max_parallel=stored_max_parallel,
                effective_max_parallel=_clamp_to_ceiling(stored_max_parallel),
                execution_timeout_seconds=int(row["execution_timeout_seconds"]),
                slot_ids=redis_state["by_agent"].get(name, set()),
                slot_scores=redis_state["scores"].get(name, {}),
                slot_ttls=redis_state["slot_ttls"].get(name, {}),
                running_exec_ids=execs["running"],
                running_started_at=execs.get("started_at", {}),
                running_claude_session_ids=execs.get("claude_session_ids", {}),
                queued_exec_ids=execs["queued"],
                queued_count_via_service=queued_via_service,
                queued_ids_via_engine=engine_qids,
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

    # SQLite: enabled schedules → next_run_at for the E-06 stale-projection check.
    try:
        snap.enabled_schedules = _collect_enabled_schedules()
    except Exception as exc:
        logger.exception("canary snapshot: enabled schedules read failed")
        snap.sources_unavailable.append(f"sqlite.enabled_schedules: {exc}")

    # Redis: drain-tick heartbeat for B-02. Reuses the slot_service Redis
    # client (same one used by `_collect_redis_slot_state` above). On
    # failure we leave `drain_tick_at` as None — the B-02 check then
    # cannot prove a drain ran in-window and falls back to its
    # slots-full arm, which is the correct conservative behavior.
    try:
        from services.slot_service import get_slot_service
        raw = get_slot_service().redis.get("canary:drain_tick_at")
        if raw is not None:
            snap.drain_tick_at = float(raw)
    except Exception as exc:
        logger.exception("canary snapshot: drain-tick read failed")
        snap.sources_unavailable.append(f"redis.drain_tick: {exc}")

    # Docker exec: per-agent zombie process count for R-01. New source
    # type for the canary; treat individual container failures as
    # per-agent skips (caller appends to sources_unavailable so the
    # operator can see which agents got skipped this cycle).
    try:
        z = _collect_zombie_counts()
        snap.zombie_counts = z["counts"]
        snap.sources_unavailable.extend(z["unavailable"])
    except Exception as exc:
        logger.exception("canary snapshot: zombie collector raised")
        snap.sources_unavailable.append(f"docker: {exc}")

    return snap
