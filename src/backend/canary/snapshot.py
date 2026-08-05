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

Note on the `inspect()` column/table guards below: they reflect the *actual*
live DB (SQLite PRAGMA or PG information_schema, transparently) so a minimal or
older test/agent DDL missing an optional column degrades to a fail-open skip
instead of a hard SELECT error. They are a minimal-DDL / older-image
test-compatibility shim, NOT load-bearing for PG correctness (the full schema is
always present at Alembic head), and are retirable post-SQLite-EOS
(2026-09-01, #1278).

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

Backend seam (#300 / #1450 / #1540): all SQL-tier collector reads route through
the `get_engine()`/DATABASE_URL seam (`db/engine.py` + `db/tables.py`), so the
harness reads the live configured backend — SQLite OR PostgreSQL — not a stale
`/data/trinity.db`. B-01's queued Side B was the first read migrated (#1450);
#1540 finished the job for the remaining collectors (`_collect_known_agents`,
`_collect_executions`, `_collect_terminal_executions`, `_collect_terminal_rows`,
`_collect_enabled_schedules`, `_collect_orphan_refs`). Before #1540 those raw
`db.connection.get_db_connection()` reads deliberately ignored a non-SQLite
DATABASE_URL, so on PostgreSQL `_collect_known_agents` returned zero rows → the
whole per-agent loop never ran → S-01/E-05/B-02/E-04/G-04/B-01 (and the
fleet-wide E-02/E-03/G-03/E-06/L-03 reads) went vacuously green. The collector
no longer imports `db.connection`. The `sources_unavailable` labels below keep
their historical `sqlite.*` prefixes on purpose: several invariants skip on that
exact string prefix (see the note at `collect_snapshot`), so the labels are an
internal skip contract, not a live backend claim.
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


# E-03 / G-03 (#1077) terminal subset — success/failed/cancelled ONLY, and
# deliberately NOT reusing `TERMINAL_EXECUTION_STATUSES` / `_TERMINAL_SQL_LIST`
# above, which include `skipped`. A `skipped` execution (an empty-stdout
# pre-check, #454) legitimately has no `completed_at`/`duration_ms`, so pulling
# it into the terminal-row set would make E-03 (completed_at populated) fire on
# a perfectly healthy row.
_E03_TERMINAL_STATUSES = (
    TaskExecutionStatus.SUCCESS.value,
    TaskExecutionStatus.FAILED.value,
    TaskExecutionStatus.CANCELLED.value,
)

# Head-room past the per-agent execution timeout when sizing the terminal-row
# window (see `_collect_terminal_rows`). Matches `SLOT_TTL_BUFFER` in
# services/slot_service.py and the E-01 invariant's buffer — hard-coded so the
# canary stays insulated from runtime config drift.
TERMINAL_WINDOW_BUFFER_SECONDS = 300

# Cap on terminal rows pulled per cycle. `schedule_executions` has no
# (status, started_at) index and retains terminal rows 90 days, so an unbounded
# window scan is a full scan every 5 min. E-03/G-03 are a leading-edge
# regression tripwire — a live malformed-row bug fires continuously on fresh
# rows — not a backfill audit, so bounding the scan is an accepted coverage
# tradeoff (logged via a `sampled` flag when hit). Mirrors the analytics
# 5000-row cap.
_TERMINAL_ROWS_CAP = 5000


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
# #1644: mirrors services.retention_guard.ALARM_AGENT_NAME. Duplicated as a
# literal rather than imported: `canary/` is a deterministic read-only library
# that must not pull a service (which imports `database`) into its import graph.
# `tests/unit/test_1644_retention_guard.py` asserts the two stay equal.
_RETENTION_GUARD_AGENT = "_retention-guard"

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
    # #1644: exclude the retention guard's reserved sentinel. L-03 hunts GHOST
    # AGENTS — rows referencing an agent_name that a delete should have cascaded
    # away. `_retention-guard` is not an agent and never was: it is a platform
    # alarm host, deliberately un-createable (leading '_' is stripped by
    # sanitize_agent_name), so it can never appear in agent_ownership and would
    # otherwise report a permanent, un-fixable orphan. This narrows the predicate
    # to what the invariant actually means; it does not weaken it.
    (
        "operator_queue",
        "agent_name",
        f"status = 'pending' AND agent_name != '{_RETENTION_GUARD_AGENT}'",
    ),
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
    # `lease_expires_at` per running id (ISO str, or None). A non-NULL value
    # marks a #1081-Phase-3 pull-CLAIMED row: it is `status='running'` but is
    # owned EXCLUSIVELY by the lease-reaper and NEVER enters the slot ZSET (a
    # claim is a pure SQL UPDATE with no ZADD). S-01 uses this to exclude leased
    # rows from the SQL side of its slot–row bijection, so a legitimately-
    # unslotted pull row is not flagged `in_sql_only`. Read by S-01 and, since
    # #1766, by E-05 — a leased row is `running` with a NULL claude_session_id
    # by design, and `mark_no_session_executions_failed` (the sweep E-05 watches)
    # already excludes leased rows for that reason. E-01/E-02 keep seeing the
    # full `running_exec_ids` unchanged.
    running_lease_expires_at: Dict[str, Optional[str]] = field(default_factory=dict)
    # `claude_session_id` per running id (str or None); used by E-05 to detect
    # dispatched rows that never acquired a backing session.
    running_claude_session_ids: Dict[str, Optional[str]] = field(default_factory=dict)
    # Queued id-set from `_collect_executions` (engine seam since #1540). Consumed
    # by B-02 and E-02. **No longer B-01's Side B** (#1450): B-01 uses the
    # dedicated `queued_ids_via_engine` below (an independent `SELECT id` code
    # path) so its two sides compare like-for-like against the accessor. Before
    # #1540 this read raw-sqlite and diverged from the engine backend on
    # Postgres; both now honor DATABASE_URL.
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
    # E-04 / G-04 input (#1077): per-queued-execution metadata
    # `{eid: {"queued_at": ..., "backlog_metadata": ...}}`, scoped STRICTLY to
    # `status='queued'` rows in `_collect_executions` — never terminal rows, so
    # #1449 (deferred; NULLs `backlog_metadata` on terminal rows) cannot make
    # E-04 false-fire. Populated only when the `queued_at` + `backlog_metadata`
    # columns both exist (BACKLOG-001); on an older/minimal DDL the map is left
    # empty for that agent so E-04/G-04 skip its queued eids (older-image
    # fail-open). An eid in `queued_exec_ids` but ABSENT from this map means the
    # metadata columns weren't observable — E-04/G-04 skip it; a PRESENT entry
    # with a NULL value is a real E-04 violation.
    queued_meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)
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
    # E-03 / G-03 input (#1077): recent terminal execution rows
    # ({id, agent_name, status, started_at, completed_at, duration_ms}),
    # windowed on `started_at` (NOT `completed_at` — E-03 must see rows whose
    # `completed_at` is NULL). Scoped to success/failed/cancelled (never
    # `skipped`). Empty when the collection was skipped (older/minimal DDL
    # missing `completed_at`/`duration_ms`, recorded in `sources_unavailable`).
    terminal_rows: List[Dict[str, Any]] = field(default_factory=list)
    # Diagnostics — empty on a clean cycle.
    sources_unavailable: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------


def _collect_known_agents() -> List[Dict[str, Any]]:
    """Read agent_ownership rows. One source of truth for valid agent names.

    Reads through the `get_engine()`/DATABASE_URL seam (#1540) so the harness
    sees the live configured backend (SQLite OR PostgreSQL). This is the
    load-bearing un-blinding read: on PostgreSQL the old raw-sqlite path returned
    zero rows → `snap.agents` empty → the entire per-agent loop
    (S-01/E-05/B-02/E-04/G-04 and B-01) went vacuously green.
    """
    from sqlalchemy import func, select
    from db.engine import get_engine
    from db.tables import agent_ownership

    # func.coalesce keeps the raw SQL's COALESCE(...) defaults AT the query so a
    # NULL column never flows through as `int(None)` downstream (948/955).
    # Intentional default note: execution_timeout_seconds coalesces to 900 (the
    # collector's historical fallback, snapshot.py) — deliberately NOT the newer
    # schema default 3600 (#665). This value sizes the E-01 window and the
    # terminal-row `max_timeout`; preserving 900 keeps a faithful read-repoint
    # (unifying to 3600 would change when E-01 fires — a separate behavior
    # change, out of scope for #1540). Post-#665 the column is effectively always
    # populated, so the fallback is near-dead — it only matters for a NULL-timeout
    # row a migrated DB shouldn't have.
    stmt = select(
        agent_ownership.c.agent_name,
        func.coalesce(agent_ownership.c.is_system, 0).label("is_system"),
        func.coalesce(agent_ownership.c.max_parallel_tasks, 3).label(
            "max_parallel_tasks"
        ),
        func.coalesce(agent_ownership.c.execution_timeout_seconds, 900).label(
            "execution_timeout_seconds"
        ),
    )
    # Intentionally NOT filtering `deleted_at IS NULL` (#834). The canary's
    # `known_agents` set drives L-03 (orphan-row detection) — soft-deleted-
    # pending-purge agents legitimately have child rows in the live tables until
    # the retention sweep runs. Treating them as "unknown" would surface those
    # preserved rows as false-positive orphans.
    with get_engine().connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def _collect_executions(agent_name: str) -> Dict[str, Any]:
    """Per-agent running + queued execution_ids.

    Adds `claude_session_id` for running rows (E-05) — fetched in the same
    query so the canary cycle stays O(N agents) and never grows per-row.
    The column has been on `schedule_executions` since #106; rows predating
    that migration return NULL and are tolerated by the E-05 grace window.

    Also captures `queued_at` + `backlog_metadata` for **queued** rows (E-04 /
    G-04, #1077) in the same query, keyed by execution_id in `queued_meta`.
    Both columns are PRAGMA-guarded (added by BACKLOG-001): when either is
    absent (older/minimal DDL) `queued_meta` is left empty so E-04/G-04 skip
    those eids (older-image fail-open) rather than firing on a column that never
    existed. The metadata is read STRICTLY off queued rows — never terminal ones
    — so #1449 (deferred; NULLs `backlog_metadata` on terminal rows) can't make
    E-04 false-fire.
    """
    from sqlalchemy import inspect, select
    from db.engine import get_engine
    from db.tables import schedule_executions

    out: Dict[str, Any] = {
        "running": set(),
        "queued": set(),
        "started_at": {},
        "claude_session_ids": {},
        "lease_expires_at": {},
        "queued_meta": {},
    }
    with get_engine().connect() as conn:
        # `claude_session_id` may not exist in the minimal test DDLs (or a
        # pre-#106 DB); reflect the LIVE columns and select only what exists so
        # the unit tests don't have to mirror every production column. `inspect()`
        # reflects the actual backend (SQLite PRAGMA / PG information_schema), so
        # the fail-open guard behaves identically to the old raw PRAGMA path.
        c = schedule_executions.c
        cols = {col["name"] for col in inspect(conn).get_columns("schedule_executions")}
        has_session_col = "claude_session_id" in cols
        # #1081 Phase 3: `lease_expires_at` marks a pull-claimed row. Guarded
        # like `claude_session_id` so minimal test DDLs (or a pre-migration DB)
        # that lack the column still collect — absent ⇒ every row is non-leased.
        has_lease_col = "lease_expires_at" in cols
        # E-04/G-04 (#1077): both queued-metadata columns must exist to observe
        # queued metadata at all. Guard on BOTH (they land together in
        # BACKLOG-001) so a partial DDL degrades to older-image fail-open.
        has_queued_meta = "queued_at" in cols and "backlog_metadata" in cols
        select_cols = [c.id, c.status, c.started_at]
        if has_session_col:
            select_cols.append(c.claude_session_id)
        if has_lease_col:
            select_cols.append(c.lease_expires_at)
        if has_queued_meta:
            select_cols += [c.queued_at, c.backlog_metadata]
        stmt = select(*select_cols).where(
            c.agent_name == agent_name,
            c.status.in_(("running", "queued")),
        )
        # A `.mappings()` row carries ONLY the selected columns, so the optional
        # reads below MUST stay gated on the `has_*` flags (else KeyError).
        for row in conn.execute(stmt).mappings():
            if row["status"] == "running":
                out["running"].add(row["id"])
                if row["started_at"]:
                    out["started_at"][row["id"]] = row["started_at"]
                out["claude_session_ids"][row["id"]] = (
                    row["claude_session_id"] if has_session_col else None
                )
                out["lease_expires_at"][row["id"]] = (
                    row["lease_expires_at"] if has_lease_col else None
                )
            elif row["status"] == "queued":
                out["queued"].add(row["id"])
                if has_queued_meta:
                    # A NULL value here IS a violation (real E-04 case); leaving
                    # the eid out of the map only happens when the columns are
                    # absent (older image → skipped above).
                    out["queued_meta"][row["id"]] = {
                        "queued_at": row["queued_at"],
                        "backlog_metadata": row["backlog_metadata"],
                    }
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
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import schedule_executions

    # Invariant #16: Python-computed ISO-Z cutoff bound as a per-dialect param
    # (never `datetime('now', ...)`). Core binds `cutoff` for us.
    cutoff = iso_cutoff(minutes=int(window_minutes))
    stmt = select(schedule_executions.c.id, schedule_executions.c.status).where(
        schedule_executions.c.status.in_(TERMINAL_EXECUTION_STATUSES),
        schedule_executions.c.completed_at > cutoff,
    )
    with get_engine().connect() as conn:
        return {row["id"]: row["status"] for row in conn.execute(stmt).mappings()}


def _collect_terminal_rows(window_seconds: int) -> Dict[str, Any]:
    """Recent terminal rows for E-03 (`completed_at` populated) + G-03
    (`started_at <= completed_at`), windowed on `started_at`.

    Window on `started_at`, NOT `completed_at`: E-03 must be able to see a
    terminal row whose `completed_at` is NULL — the very bug it guards — and a
    `completed_at` window would filter those out. Scoped to
    success/failed/cancelled via the LOCAL `_E03_TERMINAL_STATUSES` list; the
    module-level `_TERMINAL_SQL_LIST` also includes `skipped`, which
    legitimately has no `completed_at`/`duration_ms` and would false-fire E-03.

    Column-absent vs value-NULL: if `completed_at` or `duration_ms` is missing
    from a minimal/older DDL, the whole collection is skipped (reported via the
    returned `unavailable`) so E-03/G-03 skip the cycle — defaulting an *absent*
    column to None would make E-03 fire on every row. A NULL *value* is a real
    violation; an absent *column* is a source gap.

    Bounded `ORDER BY started_at DESC LIMIT _TERMINAL_ROWS_CAP`: see the
    constant's rationale (no index, 90-day retention, tripwire-not-audit).

    Returns ``{"rows": List[Dict], "unavailable": Optional[str],
    "sampled": bool}``.
    """
    from sqlalchemy import inspect, select
    from db.engine import get_engine
    from db.tables import schedule_executions

    # `iso_cutoff` is minute-granular (keyword-only `minutes`); round the
    # second-granular window UP so it never shrinks below the requested span
    # (a slightly larger window can never miss a just-completed terminal row).
    # Invariant #16: Python-computed ISO-Z cutoff, bound as a param by Core.
    cutoff_minutes = (int(window_seconds) + 59) // 60
    cutoff = iso_cutoff(minutes=cutoff_minutes)
    c = schedule_executions.c
    with get_engine().connect() as conn:
        # Reflect LIVE columns (SQLite PRAGMA / PG information_schema): a
        # minimal/older DDL missing completed_at/duration_ms skips the whole
        # collection (source gap) rather than defaulting an absent column to
        # None and false-firing E-03 on every row.
        cols = {col["name"] for col in inspect(conn).get_columns("schedule_executions")}
        missing = [name for name in ("completed_at", "duration_ms") if name not in cols]
        if missing:
            return {
                "rows": [],
                "unavailable": (
                    "schedule_executions missing column(s): " + ", ".join(missing)
                ),
                "sampled": False,
            }
        # Fetch one past the cap so we can flag when the window was truncated
        # without a second COUNT query.
        stmt = (
            select(
                c.id, c.agent_name, c.status, c.started_at, c.completed_at, c.duration_ms
            )
            .where(c.status.in_(_E03_TERMINAL_STATUSES), c.started_at > cutoff)
            .order_by(c.started_at.desc())
            .limit(_TERMINAL_ROWS_CAP + 1)
        )
        fetched = list(conn.execute(stmt).mappings())
        sampled = len(fetched) > _TERMINAL_ROWS_CAP
        rows = [
            {
                "id": r["id"],
                "agent_name": r["agent_name"],
                "status": r["status"],
                "started_at": r["started_at"],
                "completed_at": r["completed_at"],
                "duration_ms": r["duration_ms"],
            }
            for r in fetched[:_TERMINAL_ROWS_CAP]
        ]
        if sampled:
            logger.warning(
                "canary terminal-row collector hit the %d-row cap "
                "(window=%ds); E-03/G-03 coverage is leading-edge only "
                "this cycle",
                _TERMINAL_ROWS_CAP,
                window_seconds,
            )
        return {"rows": rows, "unavailable": None, "sampled": sampled}


def _collect_enabled_schedules() -> List[Dict[str, Any]]:
    """Enabled, non-deleted schedules → {schedule_id, agent_name, next_run_at}
    for E-06 (stale next_run_at detection, #1472). Mirrors the scheduler's own
    read predicate (`enabled = 1 AND deleted_at IS NULL`)."""
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import agent_schedules

    # `enabled` is INTEGER on both backends (Trinity keeps SQLite-style integer
    # booleans; the Alembic baseline reuses the same DDL), so `== 1` is correct
    # on PostgreSQL too — no boolean-type surprise.
    stmt = select(
        agent_schedules.c.id, agent_schedules.c.agent_name, agent_schedules.c.next_run_at
    ).where(agent_schedules.c.enabled == 1, agent_schedules.c.deleted_at.is_(None))
    with get_engine().connect() as conn:
        return [
            {
                "schedule_id": row["id"],
                "agent_name": row["agent_name"],
                "next_run_at": row["next_run_at"],
            }
            for row in conn.execute(stmt).mappings()
        ]


def _collect_orphan_refs(known_agents: Set[str]) -> List[OrphanRef]:
    """Scan cross-table agent_name refs for any not in known_agents.

    Driven by ORPHAN_SCAN_TABLES. Each tuple is (table, column, optional
    SQL filter clause that further narrows what counts as 'live').
    """
    # LAZY imports (Invariant: canary stays a low-dependency leaf; a module-level
    # `db.tables` import would force `db/__init__` at canary import time and
    # change the reimport-fixture failure surface — eng-review #3).
    from sqlalchemy import inspect, literal, select, text
    from db.engine import get_engine
    from db.tables import (
        access_requests,
        agent_public_links,
        agent_reports,
        agent_schedules,
        agent_shared_files,
        agent_sharing,
        agent_skills,
        agent_tags,
        chat_sessions,
        mcp_api_keys,
        operator_queue,
        schedule_executions,
    )

    refs: List[OrphanRef] = []
    if not known_agents:
        return refs  # nothing to compare against; scan would mark every row

    # Static name→Table map for the ORPHAN_SCAN_TABLES entries. The PK is derived
    # from the STATIC Table (`primary_key.columns`), NEVER from reflection:
    # `agent_tags` is composite-PK `(agent_name, tag)` in production but is
    # declared single-`id` in some minimal test DDLs, so a reflected `["id"]`
    # would `KeyError` on the static Table that has no `id` → crash/blind L-03
    # (#1540 HIGH). `inspect().has_table` is used ONLY for the existence gate.
    _TABLE_BY_NAME = {
        t.name: t
        for t in (
            agent_sharing,
            agent_schedules,
            schedule_executions,
            chat_sessions,
            agent_skills,
            agent_tags,
            agent_shared_files,
            agent_public_links,
            operator_queue,
            access_requests,
            agent_reports,
        )
    }
    # Fail loud if a future ORPHAN_SCAN_TABLES addition lacks a db.tables entry,
    # at first call rather than silently skipping the table.
    _unmapped = [t for t, _, _ in ORPHAN_SCAN_TABLES if t not in _TABLE_BY_NAME]
    assert not _unmapped, (
        f"ORPHAN_SCAN_TABLES has no db.tables entry for: {_unmapped}"
    )

    with get_engine().connect() as conn:
        insp = inspect(conn)
        for table, column, extra_filter in ORPHAN_SCAN_TABLES:
            if not insp.has_table(table):
                # Table not present (test DB or partial install). Skip.
                continue
            tbl = _TABLE_BY_NAME[table]
            agent_col = tbl.c[column]
            # PK from the STATIC Table (Column objects). Single-column → index
            # the Column directly; composite (agent_tags) → synthetic row_id.
            pk_cols = list(tbl.primary_key.columns)
            row_id = pk_cols[0] if len(pk_cols) == 1 else literal(f"{table}-row")
            stmt = select(
                row_id.label("row_id"), agent_col.label("agent_name")
            ).where(agent_col.notin_(known_agents))
            if extra_filter:
                # SECURITY: every `extra_filter` fragment is a module-level
                # constant literal from ORPHAN_SCAN_TABLES (never user input) and
                # plain standard SQL valid on both SQLite and PG — not an
                # injection vector. Table/column names likewise come only from
                # the hard-coded ORPHAN_SCAN_TABLES list. These MUST stay
                # compile-time constants.
                stmt = stmt.where(text(extra_filter))
            for row in conn.execute(stmt).mappings():
                refs.append(
                    OrphanRef(
                        table=table,
                        column=column,
                        referenced_agent_name=row["agent_name"],
                        row_id=str(row["row_id"]),
                    )
                )

        # Agent-scoped MCP keys: same logic, separate filter on `scope`.
        if insp.has_table("mcp_api_keys"):
            stmt = select(mcp_api_keys.c.id, mcp_api_keys.c.agent_name).where(
                mcp_api_keys.c.scope == "agent",
                mcp_api_keys.c.agent_name.isnot(None),
                mcp_api_keys.c.agent_name.notin_(known_agents),
            )
            for row in conn.execute(stmt).mappings():
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

    ⚠ Label contract (#1540): the `sqlite.*` prefixes on the SQL-source
    `sources_unavailable` entries below are a LOAD-BEARING internal skip contract,
    NOT a live backend claim. Several invariants fail-open on the exact string
    PREFIX — l03_delete_cascades matches `"sqlite.orphan_refs"`,
    e02_no_phantom_reversal matches `"sqlite.terminal_executions"`. The reads now
    route through the engine seam (may be PostgreSQL), but these labels MUST stay
    verbatim; renaming them to `engine.*`/`sql.*` would silently disable those
    fail-open skips (a check would run against empty data and could false-fire).
    A future rename MUST update every consuming invariant's `startswith(...)` in
    the SAME PR. Guarded by the engine-down skip test in test_canary_invariants.
    """
    snap = Snapshot(snapshot_time=utc_now_iso())

    # agent_ownership is the source of truth for "known agents" (engine seam).
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
                "lease_expires_at": {},
                "queued_meta": {},
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
                running_lease_expires_at=execs.get("lease_expires_at", {}),
                queued_exec_ids=execs["queued"],
                queued_meta=execs.get("queued_meta", {}),
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

    # SQLite: recent terminal rows for E-03 (completed_at populated) + G-03
    # (started_at <= completed_at). Window off the MAX per-agent execution
    # timeout so a just-completed max-timeout task (per-agent cap 60–7200s,
    # #922) whose started_at is up to its timeout ago is still in-window;
    # default 900s when no agents exist. Principled, not a hardcoded 120min.
    max_timeout = max(
        (int(r["execution_timeout_seconds"]) for r in agent_rows), default=900
    )
    try:
        terminal = _collect_terminal_rows(max_timeout + TERMINAL_WINDOW_BUFFER_SECONDS)
        snap.terminal_rows = terminal["rows"]
        if terminal["unavailable"]:
            snap.sources_unavailable.append(
                f"sqlite.terminal_rows: {terminal['unavailable']}"
            )
    except Exception as exc:
        logger.exception("canary snapshot: terminal-row read failed")
        snap.sources_unavailable.append(f"sqlite.terminal_rows: {exc}")

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
