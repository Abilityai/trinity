"""
B-01 — Queue-status coherence (CANARY-001 / Issue #411 — Phase 2).

The production accessor `db.get_queued_count(agent_name)` — the one
`BacklogService` calls on every enqueue and drain — must agree with a
direct id-list count of `schedule_executions WHERE status='queued'` for
the same agent. Both numbers come from the same table, so they should
match. When they don't, something has been wedged between the
service-layer accessor and the raw rows: a cache, a status-filter
regression, an enum-value drift, a partial migration.

## Why this isn't a tautology

After the #428 CapacityManager consolidation the backlog has no
secondary representation — the queue is `status='queued'` rows. So
yes, calling `SELECT COUNT(*) WHERE status='queued'` twice will agree.

The point of B-01 is to *fail loudly* the day someone adds an in-memory
cache to `db.get_queued_count` (or a Redis read-through, or a stale
materialized view), and gets the cache invalidation wrong. The two
query paths live in different files (`db/schedules.py` vs
`canary/snapshot.py`); they share a database but not a code path.

Today this check is trivially-green. That's fine — it's a regression
guard. The cost is one extra `SELECT COUNT(*)` per agent per cycle,
which is unmeasurably cheap.

## Skip behavior

If the snapshot collector could not reach the production accessor (the
unit-test environment stubs `db.connection` but not the full `database`
facade), the per-agent `queued_count_via_service` is `None`. The check
skips those agents rather than firing a false positive.

Tier A, severity critical. Backlog/orchestration agreement is a
load-bearing invariant — if it ever fires, it means a user's queued
task is either invisible or double-counted, and the drain path will
either skip it or stall waiting for a free slot that already exists.
"""

from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "B-01"
TIER = "A"
SEVERITY = "critical"


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Compare `db.get_queued_count` against `len(queued_exec_ids)`."""
    violations: List[ViolationReport] = []

    for agent in snapshot.agents:
        service_count = agent.queued_count_via_service
        # Accessor unavailable this cycle (unit-test mode) — skip silently.
        if service_count is None:
            continue
        snapshot_count = len(agent.queued_exec_ids)
        if service_count == snapshot_count:
            continue

        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "agent_name": agent.name,
                    "service_count": service_count,
                    "snapshot_count": snapshot_count,
                    "snapshot_queued_ids": sorted(agent.queued_exec_ids),
                    "snapshot_time": snapshot.snapshot_time,
                },
                signal_query=(
                    f"db.get_queued_count('{agent.name}') = {service_count} "
                    f"!= |queued ids in snapshot| = {snapshot_count}"
                ),
            )
        )

    return violations
