"""Ephemeral "ghost" agent lifecycle accessors (trinity-enterprise#69).

Ghost agents carry a hard budget (``ephemeral_max_executions`` and/or a TTL —
``ephemeral_expires_at`` is ALWAYS stamped so no ghost is immortal) and are
hard-discarded when it is exhausted: no soft-delete, no 180-day retention, no
name reservation. ``ephemeral_expires_at`` doubles as the durable discard-intent
marker — the discard primitive sets it to *now* as its first step, so a crash at
any later step re-qualifies the row for the GC sweep's DB pass (crash
convergence).

Spawn provenance (``spawned_by_agent`` / ``spawned_by_key_id``) is written for
ANY agent-spawned creation — durable or ephemeral — and backs the Part 2
parent-control gate (name AND key-id must match; name alone is forgeable via
name reuse).

Budget counting: terminal = success/failed/cancelled (SKIPPED is excluded —
Claude was never invoked, no work was consumed). The admission predicate also
counts non-terminal rows (queued/running/pending_retry) so check-then-act
overshoot is bounded.
"""

from typing import Dict, List, Optional

from sqlalchemy import and_, case, delete, func, or_, select, update

from models import TaskExecutionStatus
from utils.helpers import utc_now_iso

from ..engine import get_engine
from ..tables import agent_ownership, schedule_executions

# Statuses that consume ghost budget (work actually ran or was killed mid-run).
_BUDGET_TERMINAL_STATUSES = (
    TaskExecutionStatus.SUCCESS,
    TaskExecutionStatus.FAILED,
    TaskExecutionStatus.CANCELLED,
)
# Non-terminal rows counted by the admission predicate (overshoot bound).
_ACTIVE_STATUSES = (
    TaskExecutionStatus.QUEUED,
    TaskExecutionStatus.RUNNING,
    TaskExecutionStatus.PENDING_RETRY,
)


class EphemeralMixin:
    """Mixin for ephemeral ("ghost") agent lifecycle (trinity-enterprise#69)."""

    def get_agent_ephemeral_info(self, agent_name: str) -> Optional[Dict]:
        """Ephemeral metadata + spawn provenance for a live agent.

        Returns None when the agent has no live ownership row. For durable
        agents ``is_ephemeral`` is False but the spawn provenance is still
        returned (Part 2 gates apply to durable spawned children too).
        """
        stmt = select(
            func.coalesce(agent_ownership.c.is_ephemeral, 0).label("is_ephemeral"),
            agent_ownership.c.ephemeral_max_executions,
            agent_ownership.c.ephemeral_expires_at,
            agent_ownership.c.spawned_by_agent,
            agent_ownership.c.spawned_by_key_id,
            agent_ownership.c.owner_id,
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
        result = dict(row)
        result["is_ephemeral"] = bool(result["is_ephemeral"])
        return result

    def mark_ephemeral_discard_intent(self, agent_name: str) -> bool:
        """Step 0 of the discard primitive: durably expire the ghost NOW.

        Sets ``ephemeral_expires_at = now`` (only on ``is_ephemeral`` rows), so
        a crash anywhere later in the discard sequence leaves a row the GC
        sweep's DB pass re-qualifies next cycle. Idempotent — re-marking an
        already-expired ghost is harmless.
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_ownership)
                .where(
                    and_(
                        agent_ownership.c.agent_name == agent_name,
                        func.coalesce(agent_ownership.c.is_ephemeral, 0) == 1,
                    )
                )
                .values(ephemeral_expires_at=utc_now_iso())
            )
            return result.rowcount > 0

    def count_ephemeral_budget_usage(self, agent_name: str) -> Dict[str, int]:
        """Budget usage counters for the admission gate + terminal hook.

        Single-pass conditional aggregation: ``terminal`` = consumed budget
        (success/failed/cancelled), ``active`` = queued/running/pending_retry
        (counted at admission so concurrency can't overshoot the budget).
        """
        terminal_statuses = [s.value for s in _BUDGET_TERMINAL_STATUSES]
        active_statuses = [s.value for s in _ACTIVE_STATUSES]
        stmt = select(
            func.coalesce(
                func.sum(
                    case(
                        (schedule_executions.c.status.in_(terminal_statuses), 1),
                        else_=0,
                    )
                ),
                0,
            ).label("terminal"),
            func.coalesce(
                func.sum(
                    case(
                        (schedule_executions.c.status.in_(active_statuses), 1),
                        else_=0,
                    )
                ),
                0,
            ).label("active"),
        ).where(schedule_executions.c.agent_name == agent_name)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return {
            "terminal": int(row["terminal"] or 0),
            "active": int(row["active"] or 0),
        }

    def find_discardable_ephemeral_agents(self, limit: int = 50) -> List[str]:
        """Ghosts ready for discard: expired TTL or exhausted exec budget.

        Bounded by ``limit`` (the GC caps discards per cycle so a burst of
        expired ghosts can't stall the cleanup loop's other sweeps). The
        expiry comparison is lexicographic on ISO-Z strings — both sides are
        written by ``utc_now_iso`` (Invariant #16).
        """
        if limit <= 0:
            return []
        now = utc_now_iso()
        terminal_statuses = [s.value for s in _BUDGET_TERMINAL_STATUSES]
        exec_count = (
            select(func.count())
            .select_from(schedule_executions)
            .where(
                and_(
                    schedule_executions.c.agent_name
                    == agent_ownership.c.agent_name,
                    schedule_executions.c.status.in_(terminal_statuses),
                )
            )
            .scalar_subquery()
        )
        stmt = (
            select(agent_ownership.c.agent_name)
            .where(
                and_(
                    func.coalesce(agent_ownership.c.is_ephemeral, 0) == 1,
                    or_(
                        and_(
                            agent_ownership.c.ephemeral_expires_at.is_not(None),
                            agent_ownership.c.ephemeral_expires_at < now,
                        ),
                        and_(
                            agent_ownership.c.ephemeral_max_executions.is_not(None),
                            exec_count
                            >= agent_ownership.c.ephemeral_max_executions,
                        ),
                    ),
                )
            )
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [row["agent_name"] for row in conn.execute(stmt).mappings()]

    def count_live_ephemeral_agents_for_owner(self, owner_id: int) -> int:
        """Live ghost count per owner — the quota fallback/reconcile read.

        The primary quota gate is an atomic Redis INCR-with-cap in the
        creation path; this DB count is the fail-open fallback when Redis is
        unavailable and the drift-correction source.
        """
        stmt = (
            select(func.count())
            .select_from(agent_ownership)
            .where(
                and_(
                    agent_ownership.c.owner_id == owner_id,
                    func.coalesce(agent_ownership.c.is_ephemeral, 0) == 1,
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
        )
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)

    def purge_ephemeral_agent_ownership(self, agent_name: str) -> bool:
        """Hard-purge an EPHEMERAL agent's rows — live rows allowed.

        The ghost counterpart of ``purge_agent_ownership`` (#834): ghosts skip
        soft-delete entirely, so this variant purges a live row — but ONLY when
        ``is_ephemeral = 1``. A durable agent (live or soft-deleted) is refused;
        durable purges must keep going through the retention sweep.
        """
        from db.agent_cleanup import cascade_delete

        with get_engine().begin() as conn:
            row = (
                conn.execute(
                    select(
                        func.coalesce(agent_ownership.c.is_ephemeral, 0).label(
                            "is_ephemeral"
                        )
                    ).where(agent_ownership.c.agent_name == agent_name)
                )
                .mappings()
                .first()
            )
            if not row:
                return False
            if not row["is_ephemeral"]:
                # Refuse: this primitive must be unable to hard-delete a
                # durable agent.
                return False

            cascade_delete(conn, agent_name)
            result = conn.execute(
                delete(agent_ownership).where(
                    agent_ownership.c.agent_name == agent_name
                )
            )
            return result.rowcount > 0
