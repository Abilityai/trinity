"""
S-01 — Slot–row bijection (CANARY-001 / Issue #411).

Per agent A: the set of execution_ids in `agent:slots:A` (Redis ZSET) must
equal the set of execution_ids in `schedule_executions` with status='running'
and agent_name=A.

Drain sentinels (members starting with `drain-`) are filtered out — see
services/backlog_service.py for why they exist.

Tier A, severity critical. A bijection violation always indicates either
leaked Redis slots (capacity is wrong) or phantom SQL running rows (cleanup
service is failing) — both of which directly cause user-visible breakage.
"""

import time
from datetime import datetime
from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "S-01"
TIER = "A"
SEVERITY = "critical"

DRAIN_PREFIX = "drain-"
# Suppress race-window false positives: SQL row commits before the Redis ZADD
# on start (~30ms typ), and SQL terminal flip precedes ZREM on stop (~5ms).
# Real leaks (PR #378/#403 class) survive multiple cycles, so 3s is generous.
GRACE_SECONDS = 3.0


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Compare Redis slot ZSET membership to SQL running rows per agent."""
    violations: List[ViolationReport] = []

    # If Redis was unreachable this cycle, skip — better silence than a
    # false positive that trains operators to mute the alert.
    if any(s.startswith("redis") for s in snapshot.sources_unavailable):
        return violations

    for agent in snapshot.agents:
        # Filter drain sentinels: they hold a slot for a few seconds during
        # backlog drain and are intentionally not present in SQL.
        slot_ids = {sid for sid in agent.slot_ids if not sid.startswith(DRAIN_PREFIX)}
        running_ids = agent.running_exec_ids

        if slot_ids == running_ids:
            continue

        cutoff = time.time() - GRACE_SECONDS
        in_redis_only = sorted(
            sid for sid in slot_ids - running_ids
            if agent.slot_scores.get(sid, 0) < cutoff
        )
        in_sql_only = sorted(
            eid for eid in running_ids - slot_ids
            if (ts := agent.running_started_at.get(eid)) is None
            or datetime.fromisoformat(ts).timestamp() < cutoff
        )
        if not in_redis_only and not in_sql_only:
            continue

        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "agent_name": agent.name,
                    "redis_slot_count": len(slot_ids),
                    "sql_running_count": len(running_ids),
                    "in_redis_only": in_redis_only,
                    "in_sql_only": in_sql_only,
                    "snapshot_time": snapshot.snapshot_time,
                },
                signal_query=(
                    "set(ZRANGE agent:slots:{name}) - drain sentinels "
                    "vs set(SELECT id FROM schedule_executions "
                    "WHERE agent_name = '{name}' AND status = 'running')"
                ).format(name=agent.name),
            )
        )

    return violations
