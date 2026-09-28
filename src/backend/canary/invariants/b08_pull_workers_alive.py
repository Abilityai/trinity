"""
B-08 — Pull workers alive (CANARY-001 / Issue #2840).

A pull pilot's worker pool must be able to claim work, whether or not any is
waiting. B-02 notices a dead pool only once a row has aged in the queue; this
check notices it while the queue is still empty, so the first job after a
quiet night is not the one that finds out.

Evaluated for running pilot containers only (a stopped pilot with waiting work
is B-02's `pilot_stopped`). Pool state comes from B-02's `pull_pool_state`:
capacity is the pool size in the container's own env, busy = a `running` row
whose lease has not expired.

| kind | condition |
|---|---|
| `workers_missing` | the backend treats the agent as a pilot, but its container has no `TRINITY_PULL_MODE=true` — it predates the flag and needs a recreate, and no worker exists to claim anything |
| `workers_silent` | a worker is idle, but no claim attempt has arrived for `PULL_POLL_SILENCE_SECONDS` |

`workers_silent` reads the per-agent last-poll timestamp that
`pull_coordination_service.claim_next_task` stamps on every claim attempt. An
idle worker polls at least every 15s (30s after a transport error), so five
minutes of silence means the pool is dead or cannot reach the backend. Silence
is measured from the later of the last poll and the container start, so a
freshly started container, a recycled agent name, or an expired key never
fires on its own. When the poll could not be read (Redis down, or this
pilot's read failed) the silence arm skips: an unknown value is not "never
polled". Busy workers do not poll, so a full pool is never silent.

Kept separate from B-02 on purpose: alerts fire only on an invariant's
green→red transition, and these states can last until someone recreates or
restarts the agent. Under B-02's id they would hold it red and silence every
new drain stall behind them.

Severity major: no work is waiting yet, so nothing has been lost.
"""

from typing import List, Optional

from ..snapshot import (
    PULL_POLL_UNAVAILABLE_PREFIX,
    AgentSnapshot,
    Snapshot,
    ViolationReport,
)
from .b02_no_queued_without_slots_full import parse_ts, pull_pool_state


INVARIANT_ID = "B-08"
TIER = "B"
SEVERITY = "major"

# Idle-poll cap 15s, transport-error backoff cap 30s — 300s is ten missed polls.
PULL_POLL_SILENCE_SECONDS = 300


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """One report per running pilot whose worker pool cannot claim."""
    now = parse_ts(snapshot.snapshot_time)
    if now is None:
        return []
    violations: List[ViolationReport] = []
    for agent in snapshot.agents:
        if agent.is_pull_pilot:
            report = _check_agent(agent, snapshot, now)
            if report is not None:
                violations.append(report)
    return violations


def _check_agent(
    agent: AgentSnapshot, snapshot: Snapshot, now: float
) -> Optional[ViolationReport]:
    state = pull_pool_state(agent, snapshot, now)
    if state is None or not state["running"]:
        return None
    observed = {
        "agent_name": agent.name,
        "container_pull_mode": state["pull_mode"],
        "pool_size": state["pool_size"],
        "busy_workers": state["busy_workers"],
        "idle_workers": state["idle_workers"],
        "snapshot_time": snapshot.snapshot_time,
    }
    if not state["pull_mode"]:
        return _report(
            "workers_missing", observed,
            f"agent {agent.name}: backend treats it as a pull pilot but its "
            f"container has no TRINITY_PULL_MODE=true — recreate the agent",
        )
    if state["idle_workers"] <= 0:
        return None

    poll_unread = any(
        s.startswith("redis") or s.startswith(f"{PULL_POLL_UNAVAILABLE_PREFIX}{agent.name}]")
        for s in snapshot.sources_unavailable
    )
    if poll_unread:
        return None
    started = parse_ts(snapshot.zombie_container_started_at.get(agent.name))
    last_signs = [t for t in (agent.last_worker_poll_at, started) if t is not None]
    if not last_signs:
        return None
    silence = now - max(last_signs)
    if silence <= PULL_POLL_SILENCE_SECONDS:
        return None
    observed.update(
        last_worker_poll_at=agent.last_worker_poll_at,
        poll_silence_seconds=int(silence),
        poll_silence_grace_seconds=PULL_POLL_SILENCE_SECONDS,
    )
    return _report(
        "workers_silent", observed,
        f"agent {agent.name}: {state['idle_workers']}/{state['pool_size']} "
        f"worker(s) idle but no claim attempt for {int(silence)}s "
        f"> {PULL_POLL_SILENCE_SECONDS}s",
    )


def _report(kind: str, observed: dict, signal: str) -> ViolationReport:
    return ViolationReport(
        invariant_id=INVARIANT_ID,
        tier=TIER,
        severity=SEVERITY,
        observed_state={**observed, "kind": kind},
        signal_query=signal,
    )
