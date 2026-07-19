"""Persistent backlog (BACKLOG-001) + #1081 dark pull/lease/CAS seams."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

from sqlalchemy import select, update, and_, func

from ..engine import get_engine
from ..tables import (
    schedule_executions,
)
from models import TaskExecutionStatus
from utils.helpers import utc_now_iso, to_utc_iso

class ScheduleQueueMixin:
    """Queued/backlog accessors + pull-lease reaper seams."""

    # =========================================================================
    # Persistent Backlog (BACKLOG-001)
    # =========================================================================

    def update_execution_to_queued(
        self, execution_id: str, backlog_metadata: str, queued_at: str
    ) -> bool:
        """Transition an execution row to QUEUED state and attach its backlog metadata.

        Called by BacklogService.enqueue(). The row is already created by
        create_task_execution in RUNNING state, so we flip it back to queued and
        stamp queued_at for FIFO ordering.

        Args:
            execution_id: Execution row to transition.
            backlog_metadata: JSON string capturing the full request context.
            queued_at: ISO timestamp (used as the FIFO ordering key).

        Returns:
            True if the row was moved to QUEUED, False if it is missing or no
            longer RUNNING. The ``status == RUNNING`` precondition makes this a
            CAS-guarded projection write (#1082): a stale or duplicate re-queue
            against an already-terminal row is rejected, so a terminal row can
            never be resurrected into QUEUED (the E-02 phantom-reversal class).
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        # Only a currently-running row (the state set by
                        # create_task_execution before the slot acquire failed)
                        # may spill into the backlog — mirrors the sibling
                        # release_claim_to_queued guard (#1082).
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                    )
                )
                .values(
                    status=TaskExecutionStatus.QUEUED,
                    queued_at=queued_at,
                    backlog_metadata=backlog_metadata,
                    # reset started_at so drain records a clean run window
                    started_at=queued_at,
                )
            )
            return result.rowcount > 0

    def claim_next_queued(
        self,
        agent_name: str,
        worker_id: Optional[str] = None,
        lease_seconds: Optional[int] = None,
    ) -> Optional[Dict]:
        """Atomically claim the oldest QUEUED execution for an agent.

        Uses a single SQL UPDATE with a subquery that selects the oldest row
        by queued_at, filtered WHERE status='queued'. RETURNING gives us the
        full row so the caller can reconstruct the request. This is race-safe
        under concurrent drain callbacks — only one caller wins the update.

        Pull / work-stealing extension (#1081 Phase 1 — **DARK**): when
        ``worker_id`` is provided, the SAME atomic UPDATE additionally stamps
        the dark pull-coordination columns — a fresh ``claim_token``, the
        ``lease_expires_at`` deadline (now + ``lease_seconds``, ISO-Z), and
        ``claimed_by_worker`` — and RETURNING carries them back. When
        ``worker_id`` is None (the existing push/backlog-drain caller) the
        behavior is byte-for-byte what it was: those columns stay NULL and no
        production caller passes a worker yet. One claim path, no fork.

        Args:
            agent_name: Agent whose backlog head to claim.
            worker_id: Opaque pull-worker identity. None ⇒ legacy push claim.
            lease_seconds: Lease TTL in seconds (worker path only). The caller
                supplies ``execution_timeout_seconds + SLOT_TTL_BUFFER`` to match
                the slot-TTL convention.

        Returns:
            Dict of the claimed row (id, agent_name, message, backlog_metadata,
            source_*, and — on the worker path — claim_token / lease_expires_at /
            claimed_by_worker) or None if the backlog is empty for this agent.
        """
        now_dt = datetime.now(timezone.utc)
        now = to_utc_iso(now_dt)
        # C1 fix (#1081): under Postgres READ COMMITTED the uncorrelated scalar
        # subquery compiled to a once-evaluated InitPlan whose id every blocked
        # updater re-applied (its EvalPlanQual re-check passed because the outer
        # WHERE had no status predicate), so a single queued row was claimed by
        # up to N workers and double-RAN. FOR UPDATE SKIP LOCKED makes concurrent
        # claimers lock DISTINCT head rows; SQLite serialises writers and does
        # not support the clause, so it is applied on Postgres only — the outer
        # status re-check below is the cross-dialect backstop.
        oldest_queued_select = (
            select(schedule_executions.c.id)
            .where(
                and_(
                    schedule_executions.c.status == TaskExecutionStatus.QUEUED,
                    schedule_executions.c.agent_name == agent_name,
                )
            )
            .order_by(schedule_executions.c.queued_at.asc())
            .limit(1)
        )
        if get_engine().dialect.name == "postgresql":
            oldest_queued_select = oldest_queued_select.with_for_update(
                skip_locked=True
            )
        oldest_queued = oldest_queued_select.scalar_subquery()
        values = {
            "status": TaskExecutionStatus.RUNNING,
            "started_at": now,
            "queued_at": None,
        }
        if worker_id is not None:
            # Pull claim (#1081): stamp the lease columns in the SAME atomic
            # UPDATE so claim + lease are indivisible (no read-then-write race).
            values["claim_token"] = secrets.token_urlsafe(32)
            values["lease_expires_at"] = to_utc_iso(
                now_dt + timedelta(seconds=int(lease_seconds or 0))
            )
            values["claimed_by_worker"] = worker_id
        stmt = (
            update(schedule_executions)
            .where(
                and_(
                    schedule_executions.c.id == oldest_queued,
                    # Re-check status in the OUTER update so a losing updater is
                    # a no-op even if two ever resolve the same id (C1 backstop,
                    # cross-dialect — also covers SQLite, which has no SKIP LOCKED).
                    schedule_executions.c.status == TaskExecutionStatus.QUEUED,
                )
            )
            .values(**values)
            .returning(
                schedule_executions.c.id,
                schedule_executions.c.agent_name,
                schedule_executions.c.message,
                schedule_executions.c.backlog_metadata,
                schedule_executions.c.source_user_id,
                schedule_executions.c.source_user_email,
                schedule_executions.c.source_agent_name,
                schedule_executions.c.source_mcp_key_id,
                schedule_executions.c.source_mcp_key_name,
                schedule_executions.c.subscription_id,
                schedule_executions.c.triggered_by,
                schedule_executions.c.claude_session_id,
                schedule_executions.c.model_used,
                schedule_executions.c.started_at,
                schedule_executions.c.claim_token,
                schedule_executions.c.lease_expires_at,
                schedule_executions.c.claimed_by_worker,
                schedule_executions.c.redelivery_count,
            )
        )
        with get_engine().begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return dict(row) if row else None

    def release_claim_to_queued(self, execution_id: str) -> bool:
        """Release a claimed row back to QUEUED state.

        Used when drain_next() acquired a slot, claimed a row, but then something
        downstream failed (e.g. slot released concurrently, spawn failed) and we
        need to put the row back in the backlog.

        Returns:
            True if the row transitioned back to queued, False otherwise.
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                    )
                )
                .values(
                    status=TaskExecutionStatus.QUEUED,
                    queued_at=schedule_executions.c.started_at,
                )
            )
            return result.rowcount > 0

    # ------------------------------------------------------------------
    # Lease reaper (#1081 Phase 3 — #429 / #1402). The pull-coordination
    # recovery path: a worker that dies/hangs leaves a `running` row with a
    # past `lease_expires_at`. These three methods are the DB half of the
    # single lease-reaper — find the expired leases, then either re-queue the
    # SAME row (under cap) or poison-park it (at cap). Each transition carries
    # a status + lease CAS precondition (#1082) so two reaper passes (or a late
    # worker result) can never double-act. Additive: they key off
    # `lease_expires_at IS NOT NULL`, so they touch ONLY pull-claimed rows and
    # never the #1083 async-dispatch / push rows (which leave that column NULL).
    # ------------------------------------------------------------------

    def find_expired_leases(
        self, now_iso: Optional[str] = None, limit: int = 500
    ) -> List[Dict]:
        """Return pull-claimed executions whose lease has expired.

        A row is a lease-reaper candidate when it is still ``running`` AND
        carries a non-NULL ``lease_expires_at`` that is now in the past. The
        ``lease_expires_at IS NOT NULL`` filter is what keeps this disjoint from
        every non-pull row (push / #1083 fire-and-forget leave it NULL), so the
        reaper never double-acts with the existing stale-slot sweeps.

        Read-only. The re-queue / park decision (against ``MAX_REDELIVERY``) is
        made by the reaper service; the CAS lives in the transition methods
        below, so a candidate that a concurrent pass already actioned simply
        fails its CAS there (rowcount 0) rather than being filtered here.

        Returns:
            List of ``{id, agent_name, redelivery_count}`` dicts, oldest lease
            first, capped at ``limit``.
        """
        now = now_iso or utc_now_iso()
        stmt = (
            select(
                schedule_executions.c.id,
                schedule_executions.c.agent_name,
                schedule_executions.c.redelivery_count,
            )
            .where(
                and_(
                    schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                    schedule_executions.c.lease_expires_at.isnot(None),
                    schedule_executions.c.lease_expires_at < now,
                )
            )
            .order_by(schedule_executions.c.lease_expires_at.asc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [
                {
                    "id": row["id"],
                    "agent_name": row["agent_name"],
                    "redelivery_count": row["redelivery_count"] or 0,
                }
                for row in conn.execute(stmt).mappings()
            ]

    def requeue_expired_lease(
        self, execution_id: str, now_iso: Optional[str] = None
    ) -> bool:
        """Re-queue an expired-lease pull row (under the re-delivery cap).

        **Preserves ``execution_id`` (HARD invariant #1084/#525):** this
        re-queues the SAME row — status → ``queued``, ``redelivery_count``
        incremented **in the same atomic UPDATE** (``+ 1`` column expression, no
        read-then-write), and the lease/worker columns cleared (``claim_token``
        KEPT — #1081 B2/B5 — so a genuinely-late result from the original worker
        can still CAS-match; a fresh claim re-stamps a new token). It NEVER mints
        a new row/id — a new id would defeat the
        ``execution_id``-scoped effect_guard (#1084) and idempotency (#525) dedup
        (the agent would re-send a message / re-charge on the retry).

        CAS guard (#1082): the WHERE requires the row is still ``running`` with a
        past lease, so a second reaper pass (or a worker result that already
        finalized the row) finds nothing to write. Indivisible with the
        increment — no double-count.

        Returns:
            True if this call performed the re-queue, False if the CAS lost.
        """
        now = now_iso or utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                        schedule_executions.c.lease_expires_at.isnot(None),
                        schedule_executions.c.lease_expires_at < now,
                    )
                )
                .values(
                    status=TaskExecutionStatus.QUEUED,
                    queued_at=now,
                    # reset the run window so the next drain/claim records cleanly
                    started_at=now,
                    redelivery_count=schedule_executions.c.redelivery_count + 1,
                    # #1081 B2/B5: clear the lease/worker but DELIBERATELY KEEP
                    # claim_token — a genuinely-late SUCCESS from the original
                    # worker (which still holds this token) must still CAS-match
                    # and win while the row sits queued-not-yet-reclaimed (the
                    # documented "late SUCCESS overwrites a reaper LEASE_EXPIRED"
                    # guarantee). A fresh claim overwrites the token anyway, so a
                    # re-claiming worker owns it and the stale token can't match.
                    lease_expires_at=None,
                    claimed_by_worker=None,
                )
            )
            return result.rowcount > 0

    def park_expired_lease(
        self,
        execution_id: str,
        error: str,
        now_iso: Optional[str] = None,
    ) -> bool:
        """Poison-park an expired-lease pull row (at the re-delivery cap).

        Terminal: status → ``failed`` with the poison ``error`` text and the
        lease/worker columns cleared (``claim_token`` KEPT — #1081 B2 — so a
        genuinely-late SUCCESS from the original worker can still CAS-overwrite
        this FAILED row; the token-gated CAS treats a NULL token as unmatchable).
        ``redelivery_count`` is deliberately left intact so the audit trail shows
        the row hit the cap. Operator-queue park + activity-close are the reaper
        service's job (this is the DB transition only).

        CAS guard (#1082): same ``running`` + past-lease precondition as
        ``requeue_expired_lease``, so a second pass / late worker result can
        never double-park.

        Returns:
            True if this call performed the park, False if the CAS lost.
        """
        now = now_iso or utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                        schedule_executions.c.lease_expires_at.isnot(None),
                        schedule_executions.c.lease_expires_at < now,
                    )
                )
                .values(
                    status=TaskExecutionStatus.FAILED,
                    completed_at=now,
                    error=error,
                    # #1081 B2: clear the lease/worker but KEEP claim_token so a
                    # genuinely-late SUCCESS from the original worker can still
                    # CAS-overwrite this poison-parked FAILED row (the token-gated
                    # CAS treats a NULL token as unmatchable — nulling it here was
                    # exactly what swallowed the late SUCCESS as `replayed`).
                    lease_expires_at=None,
                    claimed_by_worker=None,
                )
            )
            return result.rowcount > 0

    # ------------------------------------------------------------------
    # Physical-occupancy meter (#1081 Phase 3 — "capacity becomes physical").
    # Read-only counters of pull-claimed ("leased") `running` rows. A pull
    # pilot's capacity claim is a pure SQL UPDATE (no Redis ZSET ZADD), so the
    # ZSET-derived slot meter reads 0 for it. The CapacityManager facade adds
    # this physical term into its TWO meter methods so occupancy reflects real
    # rows. Disjoint-by-construction from the ZSET term: push/#1083 rows leave
    # `lease_expires_at` NULL, pull-leased rows set it — the same
    # `lease_expires_at IS NOT NULL` idiom `find_expired_leases` keys off. This
    # is metering only — admission (acquire/acquire_slot/release) never reads it.
    # ------------------------------------------------------------------

    def count_active_leased_by_agent(
        self, agent_names: List[str]
    ) -> Dict[str, int]:
        """Count active pull-leased `running` rows per agent (one grouped query).

        A row counts when it is ``status='running'`` AND carries a non-NULL
        ``lease_expires_at`` (a live pull claim). Rows with an already-past lease
        are still counted — the lease-reaper converges those, and excluding them
        would make the meter briefly under-report. Read-only.

        Returns:
            ``{agent_name: count}`` for agents in ``agent_names`` that have at
            least one leased row; agents with none are absent (caller treats
            absent as 0). Empty input ⇒ ``{}``.
        """
        if not agent_names:
            return {}
        stmt = (
            select(
                schedule_executions.c.agent_name,
                func.count().label("c"),
            )
            .where(
                and_(
                    schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                    schedule_executions.c.lease_expires_at.isnot(None),
                    schedule_executions.c.agent_name.in_(agent_names),
                )
            )
            .group_by(schedule_executions.c.agent_name)
        )
        with get_engine().connect() as conn:
            return {
                row["agent_name"]: int(row["c"])
                for row in conn.execute(stmt).mappings()
            }

    def count_active_leased(self, agent_name: str) -> int:
        """Scalar single-agent variant of ``count_active_leased_by_agent``.

        Used by the per-agent meter path (``CapacityManager.get_slot_state``).
        Returns 0 when the agent has no leased rows. Read-only.
        """
        stmt = select(func.count().label("c")).where(
            and_(
                schedule_executions.c.agent_name == agent_name,
                schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                schedule_executions.c.lease_expires_at.isnot(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return int(row["c"]) if row else 0

    def get_queued_count(self, agent_name: str) -> int:
        """Count queued backlog items for an agent."""
        stmt = select(func.count().label("c")).where(
            and_(
                schedule_executions.c.agent_name == agent_name,
                schedule_executions.c.status == TaskExecutionStatus.QUEUED,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return int(row["c"]) if row else 0

    def cancel_queued_execution(self, execution_id: str, reason: str = "cancelled") -> bool:
        """Cancel a single queued execution. No container interaction.

        Returns:
            True if the row was still queued and is now cancelled, False otherwise.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        schedule_executions.c.status == TaskExecutionStatus.QUEUED,
                    )
                )
                .values(
                    status=TaskExecutionStatus.CANCELLED,
                    completed_at=now,
                    error=reason,
                )
            )
            return result.rowcount > 0

    def cancel_queued_for_agent(self, agent_name: str, reason: str = "agent_deleted") -> int:
        """Bulk-cancel all queued executions for an agent.

        Used on agent deletion so orphan queued rows don't linger.

        Returns:
            Count of rows moved from QUEUED to CANCELLED.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.agent_name == agent_name,
                        schedule_executions.c.status == TaskExecutionStatus.QUEUED,
                    )
                )
                .values(
                    status=TaskExecutionStatus.CANCELLED,
                    completed_at=now,
                    error=reason,
                )
            )
            return result.rowcount

    def fail_queued_for_agent(self, agent_name: str, reason: str = "circuit_open") -> int:
        """Bulk-FAIL all queued executions for an agent (#526, RELIABILITY-007).

        Called when the per-agent dispatch circuit breaker trips: the queued
        backlog is doomed (the agent is auth-dead), so fail it out immediately
        instead of letting each row drain into its own failure after the detect
        window.

        Mirrors ``expire_stale_queued`` (status → FAILED) — intentionally NOT
        ``cancel_queued_for_agent`` (which sets CANCELLED). The #526 acceptance
        criteria require these rows close FAILED so they read as failures, not
        as user cancellations.

        Returns:
            Count of rows moved from QUEUED to FAILED.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.agent_name == agent_name,
                        schedule_executions.c.status == TaskExecutionStatus.QUEUED,
                    )
                )
                .values(
                    status=TaskExecutionStatus.FAILED,
                    completed_at=now,
                    error=reason,
                )
            )
            return result.rowcount

    def fail_all_nonterminal_for_agent(
        self, agent_name: str, reason: str = "ghost_discarded"
    ) -> int:
        """Bulk-FAIL every non-terminal execution for an agent
        (trinity-enterprise#69).

        Discard step 1 for ephemeral agents: queued, running, AND
        pending_retry rows are all doomed (the container is about to be
        force-removed and the DB rows purged), so terminal-ize them first.
        This keeps canary L-03/E-01 green through the purge — the KEEP-policy
        ``schedule_executions`` rows that survive must never be non-terminal
        rows referencing an agent absent from ``agent_ownership``. It also
        means a late in-flight ``apply_result`` for one of these rows loses
        its CAS and records no side effects (no breaker-key resurrection).

        The status filter doubles as the CAS guard: a row that reached a real
        terminal (e.g. SUCCESS landing between our read and write) is not
        overwritten.

        Returns:
            Count of rows moved to FAILED.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.agent_name == agent_name,
                        schedule_executions.c.status.in_(
                            [
                                TaskExecutionStatus.QUEUED,
                                TaskExecutionStatus.RUNNING,
                                TaskExecutionStatus.PENDING_RETRY,
                            ]
                        ),
                    )
                )
                .values(
                    status=TaskExecutionStatus.FAILED,
                    completed_at=now,
                    error=reason,
                )
            )
            return result.rowcount

    def expire_stale_queued(self, max_age_hours: float = 24) -> int:
        """Mark queued executions older than max_age_hours as FAILED.

        Runs from the 60s maintenance task. Uses ISO-8601 string comparison on
        queued_at, matching how stale running executions are handled elsewhere.

        Returns:
            Count of queued rows expired.
        """
        now = utc_now_iso()
        threshold = (
            datetime.now(timezone.utc) - timedelta(hours=float(max_age_hours))
        ).strftime('%Y-%m-%dT%H:%M:%S')
        error_msg = f"Backlog expired: queued longer than {max_age_hours} hours"
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.status == TaskExecutionStatus.QUEUED,
                        schedule_executions.c.queued_at.isnot(None),
                        schedule_executions.c.queued_at < threshold,
                    )
                )
                .values(
                    status=TaskExecutionStatus.FAILED,
                    completed_at=now,
                    error=error_msg,
                )
            )
            return result.rowcount

    def list_agents_with_queued(self) -> List[str]:
        """Return the list of agent names that currently have queued backlog items.

        Used by the 60s maintenance task to drain orphans after a restart
        (backend crashed between enqueue and drain, or drain callback was lost).
        """
        stmt = (
            select(schedule_executions.c.agent_name)
            .where(schedule_executions.c.status == TaskExecutionStatus.QUEUED)
            .distinct()
        )
        with get_engine().connect() as conn:
            return [row["agent_name"] for row in conn.execute(stmt).mappings()]
