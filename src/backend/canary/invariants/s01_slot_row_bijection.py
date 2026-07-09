"""
S-01 — Slot–row bijection (CANARY-001 / Issue #411).

Per agent A: the set of execution_ids in `agent:slots:A` (Redis ZSET) must
equal the set of execution_ids in `schedule_executions` with status='running'
and agent_name=A.

Drain sentinels (members starting with `drain-`) are filtered out — see
services/backlog_service.py for why they exist.

Pull-CLAIMED rows (`lease_expires_at IS NOT NULL`, #1081 Phase 3) are excluded
from the SQL side of the bijection: they are `running` but never enter the slot
ZSET (a claim is a pure SQL UPDATE, no ZADD), so counting them would be a
guaranteed false `in_sql_only`. Same `lease_expires_at IS NULL` exclusion the
cleanup sweeps in db/schedules.py apply. NON-leased (push) rows are unaffected.

Tier A, severity major. A bijection violation indicates either leaked Redis
slots (capacity is wrong) or phantom SQL running rows (cleanup service is
failing) — real slot-ZSET/SQL drift worth catching while the push-model ZSET
exists.

## Why major, not critical (#1082)

S-01 becomes **redundant** once single-owner status (#1082, status-as-
projection) holds: with `schedule_executions.status` a CAS-guarded projection
of the execution's terminal event, the slot ZSET is no longer a *competing*
authority for "is running" — it is only an ephemeral coordination hint, so a
ZSET/SQL disagreement no longer implies a corrupted source of truth. The check
stays registered and Tier A (it still catches genuine drift under the push
model), but is downgraded to `major` and is slated for removal *with the slot
ZSET itself* in #1081 Phase 5. Matches the E-05 Tier-A/Tier-B downgrade
precedent.
"""

import time
from datetime import datetime
from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "S-01"
TIER = "A"
# Downgraded critical → major (#1082): redundant under single-owner status,
# retires with the slot ZSET in #1081 Phase 5. See module docstring.
SEVERITY = "major"

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
        # #1081 Phase 3: a pull-CLAIMED row (lease_expires_at IS NOT NULL) is
        # `status='running'` but is owned exclusively by the lease-reaper and
        # NEVER enters the slot ZSET (a claim is a pure SQL UPDATE, no ZADD).
        # Exclude leased rows from the SQL side of the bijection so a
        # legitimately-unslotted pull row is not flagged `in_sql_only`. Mirrors
        # the `lease_expires_at IS NULL` exclusion the cleanup sweeps in
        # db/schedules.py already apply. NON-leased (push) rows are unaffected.
        running_ids = {
            eid
            for eid in agent.running_exec_ids
            if agent.running_lease_expires_at.get(eid) is None
        }

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
