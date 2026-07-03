"""
B-02 — No queued without slots-full (CANARY-001 / Issue #411 — Phase 3).

If an agent has any queued executions, the system must have a legitimate
reason to keep them queued. There are exactly two legitimate reasons:

1. **Slots are at the cap.** `len(slot_ids excluding sentinels) ==
   max_parallel_tasks` — every drain attempt would correctly fail at
   `acquire_slot`. Working as intended.
2. **A drain tick fired recently.** `CapacityManager.run_maintenance()`
   runs every 60s and writes a heartbeat to `canary:drain_tick_at`. If
   we just ran a drain pass and the queue is still there, the next
   release callback will pick it up; nothing is wedged.

If *neither* holds — there are queued rows, free slots exist, and the
drain tick is stale (>60s old or never written) — the drain pipeline
has stalled. The user's queued task is invisible from their perspective
and the agent looks idle.

## Heartbeat plumbing

The drain-tick timestamp is written by `services/capacity_manager.py:
run_maintenance` at the END of the sweep (not the start), so a crash
mid-sweep leaves the cursor stale and lets this check catch the
breakage. The snapshot reads it via the same Redis client the slot
service uses.

## Grace window

60s matches the call cadence of `main.py`'s maintenance loop. We want
the canary cycle to allow at least one full maintenance interval before
calling drain stalled — otherwise we'd false-fire any time the canary
ran in the small window between an enqueue and the next maintenance
tick. The 5-min canary cadence is well within `60s + maintenance time`.

Tier B, severity critical. A stalled drain is invisible to the user
(no error, just silence) until they notice their queued task never
ran. Catching it on the canary lets the operator intervene.
"""

import time
from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "B-02"
TIER = "B"
SEVERITY = "critical"

DRAIN_PREFIX = "drain-"

# Grace window for the drain-tick heartbeat. Matches main.py's 60s
# maintenance loop; anything older means a tick was skipped.
DRAIN_TICK_GRACE_SECONDS = 60


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Emit one violation per agent with queue + free slots + stale drain."""
    violations: List[ViolationReport] = []

    # Redis failures already get logged via sources_unavailable. If the
    # slot ZSET reads failed (S-01's gate), the slot counts are unreliable
    # and we shouldn't fire B-02 violations based on stale data.
    if any(s.startswith("redis") for s in snapshot.sources_unavailable):
        return violations

    now_ts = time.time()
    tick_age: float
    if snapshot.drain_tick_at is None:
        # Heartbeat never written (cold cluster, Redis read failed, or
        # bug in the maintenance loop). Treat as "drain has never run"
        # — sentinel value much larger than the grace window.
        tick_age = float("inf")
    else:
        tick_age = now_ts - snapshot.drain_tick_at
        # Defensive: clock skew or a future-dated heartbeat shouldn't
        # let stale data pass as fresh. Floor at 0.
        if tick_age < 0:
            tick_age = 0.0

    for agent in snapshot.agents:
        queued = len(agent.queued_exec_ids)
        if queued == 0:
            continue

        # #506: compare against the EFFECTIVE cap (stored clamped to the fleet
        # ceiling), not the stored cap. Under a lower ceiling an agent can be
        # effective-full while stored > slots, and queued is then the correct
        # state — using the stored cap would false-fire "drain stalled".
        effective_cap = (
            agent.effective_max_parallel
            if agent.effective_max_parallel is not None
            else agent.max_parallel
        )

        # Filter drain sentinels — they're held briefly while the next
        # backlog item is being claimed and are not real running work.
        real_slots = {s for s in agent.slot_ids if not s.startswith(DRAIN_PREFIX)}
        if len(real_slots) >= effective_cap:
            # Slots are at the (effective) cap; queued is the correct state.
            continue

        if tick_age <= DRAIN_TICK_GRACE_SECONDS:
            # Drain ran within the window; the next release callback
            # will pick this up. Not yet a violation.
            continue

        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "agent_name": agent.name,
                    "queued_count": queued,
                    "slot_count": len(real_slots),
                    "max_parallel_tasks": agent.max_parallel,
                    "effective_max_parallel_tasks": effective_cap,
                    "free_slots": effective_cap - len(real_slots),
                    "drain_tick_at": snapshot.drain_tick_at,
                    "drain_tick_age_seconds": (
                        None if tick_age == float("inf") else int(tick_age)
                    ),
                    "drain_tick_grace_seconds": DRAIN_TICK_GRACE_SECONDS,
                    "snapshot_time": snapshot.snapshot_time,
                },
                signal_query=(
                    f"agent {agent.name}: queued={queued}, "
                    f"slots={len(real_slots)}/{effective_cap} "
                    f"(free={effective_cap - len(real_slots)}), "
                    f"drain_tick_age="
                    f"{'never' if tick_age == float('inf') else f'{int(tick_age)}s'} "
                    f"> {DRAIN_TICK_GRACE_SECONDS}s"
                ),
            )
        )

    return violations
