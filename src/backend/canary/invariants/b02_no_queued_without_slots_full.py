"""
B-02 — Queued work is being picked up (CANARY-001 / Issue #411 — Phase 3;
pull arm #2840).

An agent's queued executions must be on their way to running. Who picks them
up depends on the agent's dispatch mode, so the check has one arm per mode.
The module keeps its historical filename and the id B-02 so alert routing and
`canary_violations` history stay continuous through the push→pull migration.

## Push arm (every agent not in `PULL_MODE_PILOT_AGENTS`)

The backend drains the queue into free Redis slots. A queued row has exactly
two legitimate reasons to stay queued:

1. **Slots are at the cap.** `len(slot_ids excluding sentinels) ==
   effective max_parallel_tasks` — every drain attempt would correctly fail at
   `acquire_slot`. Working as intended.
2. **A drain tick fired recently.** `CapacityManager.run_maintenance()` runs
   every 60s and writes a heartbeat to `canary:drain_tick_at`. If we just ran a
   drain pass and the queue is still there, the next release callback will pick
   it up; nothing is wedged.

If *neither* holds — queued rows, free slots, and a drain tick older than 60s
(or never written) — the drain pipeline has stalled. The heartbeat is written
at the END of the sweep, so a crash mid-sweep leaves it stale and this arm
catches it. 60s matches the maintenance loop's cadence.

## Pull arm (pilot agents, #2840)

The backend never drains a pilot (`pull_owns_dispatch`); the agent's own
worker pool claims rows from the durable queue. The drain tick and the slot
ZSET say nothing about that, which is why the push rule fired `critical` on a
healthy pilot that was caught between enqueue and claim (#2840, eu2
2026-09-09). The pull arm asks the pull question instead: *is there an idle
worker, and is it taking the work?*

- **Capacity is the container's pool, never the database cap.** The pool size
  is baked into the container env (`TRINITY_MAX_PARALLEL_TASKS`) at create and
  recreate only, so after a cap change without a recreate the database value
  and the running pool disagree (#3039). Reading the container makes the check
  judge what is actually running. A container without `TRINITY_PULL_MODE=true`
  runs no workers at all, so its pool is 0.
- **Busy = a running row whose lease has not expired.** The #2846 turn clamp
  ends every healthy turn inside its lease, so an expired lease on a `running`
  row means its worker is gone until the reaper requeues it. Counting it as
  busy would make a dead pool read as full. Rows the backend pushed to a pilot
  (interactive triggers) hold Redis slots, not pool workers, and are ignored
  here on purpose — never compute pool occupancy from `slot_ids`.
- **Every queued row counts, whatever its trigger.** `claim_next_queued`
  claims the oldest queued row for the agent with no trigger filter, so an
  idle worker takes any of them.

Finding: `queued_not_claimed` (critical) — a worker is idle and the oldest
queued row is older than `PULL_CLAIM_GRACE_SECONDS`.

B-02 reports only this live incident, which clears once the pool claims again.
States that last until an operator acts belong to B-08: a stopped pilot with
work waiting, a container not in pull mode, and a silent pool. An alert fires
only on an invariant's green→red transition, so any of those under B-02 would
hold it red for days and silence every new push stall behind it. When Docker
could not be listed, or the container's env could not be read, the pull arm
skips: it cannot tell what is running.

Known ceiling: a worker that has just POSTed a terminal re-claims tens of
milliseconds later. A snapshot landing exactly in that gap, with a queued row
already older than the grace, reads one worker as idle. With a 5-minute cycle
the odds are negligible; the next cycle clears it.
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..snapshot import AgentSnapshot, Snapshot, ViolationReport


INVARIANT_ID = "B-02"
TIER = "B"
SEVERITY = "critical"

DRAIN_PREFIX = "drain-"

# Grace window for the drain-tick heartbeat. Matches main.py's 60s
# maintenance loop; anything older means a tick was skipped.
DRAIN_TICK_GRACE_SECONDS = 60

# Pull arm. An idle worker polls at least every 15s (`pull_worker`
# `_IDLE_BACKOFF_CAP`), so a row an idle pool has not taken in 120s is stuck.
PULL_CLAIM_GRACE_SECONDS = 120


def parse_ts(value) -> Optional[float]:
    """Unix seconds from an ISO timestamp (`Z`, offset, or naive-as-UTC), or
    None when absent or unparseable — the caller then skips rather than fires."""
    if not value:
        return None
    try:
        from utils.helpers import parse_iso_timestamp

        return parse_iso_timestamp(str(value)).timestamp()
    except Exception:  # noqa: BLE001 — an unreadable value is a non-signal
        return None


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """One report per agent whose queued work is not being picked up."""
    violations: List[ViolationReport] = []
    # Redis failures already get logged via sources_unavailable. The push arm
    # reads slot ZSETs and the drain tick, so it skips on a Redis failure; the
    # pull arm's decisive inputs are SQL and Docker, so it still runs.
    redis_blind = any(s.startswith("redis") for s in snapshot.sources_unavailable)
    now_ts = time.time()

    for agent in snapshot.agents:
        if agent.is_pull_pilot:
            report = _check_pull(agent, snapshot)
            if report is not None:
                violations.append(report)
        elif not redis_blind:
            report = _check_push(agent, snapshot, now_ts)
            if report is not None:
                violations.append(report)
    return violations


def _check_push(
    agent: AgentSnapshot, snapshot: Snapshot, now_ts: float
) -> Optional[ViolationReport]:
    queued = len(agent.queued_exec_ids)
    if queued == 0:
        return None

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
        return None

    if snapshot.drain_tick_at is None:
        # Heartbeat never written (cold cluster, Redis read failed, or a bug
        # in the maintenance loop) — "drain has never run".
        tick_age = float("inf")
    else:
        # Floor at 0 so clock skew can't pass stale data off as fresh.
        tick_age = max(0.0, now_ts - snapshot.drain_tick_at)
    if tick_age <= DRAIN_TICK_GRACE_SECONDS:
        # Drain ran within the window; the next release callback will pick
        # this up. Not yet a violation.
        return None

    return ViolationReport(
        invariant_id=INVARIANT_ID,
        tier=TIER,
        severity=SEVERITY,
        observed_state={
            "mode": "push",
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


def pull_pool_state(
    agent: AgentSnapshot, snapshot: Snapshot, now: float
) -> Optional[Dict[str, Any]]:
    """What a pilot's worker pool looks like this cycle, or None when it
    cannot be known (Docker unlisted, or the container env unreadable).
    Shared with B-08.

    `{"running": False}` for a container Docker does not list as running;
    otherwise `running`, `pull_mode`, `pool_size`, `busy_workers`,
    `idle_workers`. A container without `TRINITY_PULL_MODE=true` has pool 0.
    """
    if snapshot.pull_container_env is None:
        # Docker could not be listed: a stopped container and an unseen one
        # look the same, so say nothing rather than guess.
        return None
    if agent.name not in snapshot.docker_agent_names:
        return {"running": False}
    container = snapshot.pull_container_env.get(agent.name)
    if container is None:
        return None  # running, but its env could not be read this cycle
    pull_mode = bool(container.get("pull_mode"))
    pool = int(container.get("pool_size") or 0) if pull_mode else 0
    busy = sum(
        1
        for lease in agent.running_lease_expires_at.values()
        if (exp := parse_ts(lease)) is not None and exp > now
    )
    return {
        "running": True,
        "pull_mode": pull_mode,
        "pool_size": pool,
        "busy_workers": busy,
        "idle_workers": max(0, pool - busy),
    }


def oldest_queued_age(agent: AgentSnapshot, now: float) -> Optional[float]:
    """Seconds the agent's oldest queued row has waited, or None. Shared with
    B-08. NULL / unparseable `queued_at` is E-04's finding, and an eid absent
    from `queued_meta` means the columns do not exist; both are skipped. A
    future-dated value floors at age 0."""
    times = [
        t
        for eid in agent.queued_exec_ids
        if (t := parse_ts((agent.queued_meta.get(eid) or {}).get("queued_at")))
        is not None
    ]
    return max(0.0, now - min(times)) if times else None


def _check_pull(agent: AgentSnapshot, snapshot: Snapshot) -> Optional[ViolationReport]:
    now = parse_ts(snapshot.snapshot_time)
    if now is None:
        return None
    state = pull_pool_state(agent, snapshot, now)
    # Stopped, unseen, or no pull workers at all: states that last until an
    # operator acts, so they are B-08's. B-02 reports only the live incident.
    if state is None or not state["running"] or state["idle_workers"] <= 0:
        return None
    oldest_age = oldest_queued_age(agent, now)
    if oldest_age is None or oldest_age <= PULL_CLAIM_GRACE_SECONDS:
        return None
    queued = len(agent.queued_exec_ids)
    return _pull_report(
        "critical", "queued_not_claimed",
        {
            "mode": "pull",
            "agent_name": agent.name,
            "queued_count": queued,
            "oldest_queued_age_seconds": int(oldest_age),
            "claim_grace_seconds": PULL_CLAIM_GRACE_SECONDS,
            "container_pull_mode": state["pull_mode"],
            "pool_size": state["pool_size"],
            "busy_workers": state["busy_workers"],
            "idle_workers": state["idle_workers"],
            "snapshot_time": snapshot.snapshot_time,
        },
        f"agent {agent.name}: queued={queued}, oldest {int(oldest_age)}s "
        f"> {PULL_CLAIM_GRACE_SECONDS}s with {state['idle_workers']}/"
        f"{state['pool_size']} worker(s) idle",
    )


def _pull_report(severity: str, kind: str, observed: dict, signal: str) -> ViolationReport:
    return ViolationReport(
        invariant_id=INVARIANT_ID,
        tier=TIER,
        severity=severity,
        observed_state={**observed, "kind": kind},
        signal_query=signal,
    )
