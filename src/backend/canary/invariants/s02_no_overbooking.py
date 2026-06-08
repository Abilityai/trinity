"""
S-02 — No overbooking (CANARY-001 / Issue #411 — Phase 2).

Per agent A: `ZCARD(agent:slots:A)` (excluding drain sentinels) must not
exceed `agent_ownership.max_parallel_tasks`. A violation means the
`acquire_slot` concurrency guard was bypassed and the agent is currently
running more tasks than its declared parallelism cap allows.

This is structurally cheaper than S-01 — S-01 needs a SQL cross-check,
S-02 only needs the Redis ZCARD and one column from `agent_ownership`,
both already in the snapshot. We still run it as its own invariant
because the *meaning* of a violation is different: S-01 says "Redis and
SQL disagree", S-02 says "even Redis on its own already shows we're
violating the cap". A site can be S-01-clean but S-02-red if both Redis
and SQL agree on N+1 running tasks against a max of N (capacity bypass);
conversely a site can be S-02-clean but S-01-red (leaked phantom slot).
They catch overlapping but distinct bug classes.

Drain sentinels (members starting with `drain-`) are filtered before the
check — see services/backlog_service.py for why they exist. They hold a
slot for a few ms during backlog drain and don't represent real work.

Tier A, severity critical. A capacity-cap bypass is a direct user-
visible breakage (the agent is doing N+1 things at once when the user
told it to do at most N).
"""

from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "S-02"
TIER = "A"
SEVERITY = "critical"

DRAIN_PREFIX = "drain-"


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Flag any agent whose real slot count exceeds its configured cap."""
    violations: List[ViolationReport] = []

    # If Redis was unreachable this cycle, skip — slot counts are unreadable.
    if any(s.startswith("redis") for s in snapshot.sources_unavailable):
        return violations

    for agent in snapshot.agents:
        real_slots = {sid for sid in agent.slot_ids if not sid.startswith(DRAIN_PREFIX)}
        slot_count = len(real_slots)
        cap = agent.max_parallel
        if slot_count <= cap:
            continue

        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "agent_name": agent.name,
                    "slot_count": slot_count,
                    "max_parallel_tasks": cap,
                    "overbooked_by": slot_count - cap,
                    "slot_ids": sorted(real_slots),
                    "snapshot_time": snapshot.snapshot_time,
                },
                signal_query=(
                    f"ZCARD(agent:slots:{agent.name}) - drain sentinels = "
                    f"{slot_count} > max_parallel_tasks = {cap}"
                ),
            )
        )

    return violations
