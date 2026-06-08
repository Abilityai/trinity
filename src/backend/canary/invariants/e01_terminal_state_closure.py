"""
E-01 — Terminal-state closure (CANARY-001 / Issue #411 — Phase 2).

Every `schedule_executions` row reaches a terminal status within its
agent's `execution_timeout_seconds + 300s` (SLOT_TTL_BUFFER). A row that
is still `status='running'` past that window means either the cleanup
watchdog never fired (CLEANUP-001 regression), the execution wedged
without raising, or the timeout enforcement was bypassed entirely.

## Per-agent timeout, not per-execution

The original catalog (`docs/testing/orchestration-invariant-catalog.md`)
specifies `timeout_seconds` on `schedule_executions`. Trinity stores the
timeout on `agent_ownership.execution_timeout_seconds` instead — agents
have a uniform per-agent cap. The check uses that value, which is
already in `AgentSnapshot.execution_timeout_seconds`.

## Buffer

300s of head-room past the timeout matches `SLOT_TTL_BUFFER` in
`services/slot_service.py` — the same buffer the cleanup service uses
before declaring a slot stale. Aligning with that constant means E-01
fires *after* the cleanup service has had its window to act, so a
violation is unambiguously "cleanup failed to act on a timed-out row"
rather than "cleanup hasn't run yet". Hard-coded rather than imported
because the canary is intentionally insulated from runtime config drift
— if SLOT_TTL_BUFFER changes upstream, this constant should be reviewed
deliberately rather than shifted silently.

## Tier B because the SLA is "eventually ≤ timeout + 5min"

Unlike S-01 / S-02 which are point-in-time bijection checks, E-01 has a
time component: a row is only a violation once it's *past* its window.
The 5-min canary cadence is well-aligned with the 300s buffer.

Tier B, severity critical. A stuck-forever execution that the watchdog
missed is a direct user-visible failure (the schedule never reports,
the slot is never released, the agent is one parallel task lighter
forever).
"""

from datetime import datetime
from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "E-01"
TIER = "B"
SEVERITY = "critical"

# Matches services/slot_service.py SLOT_TTL_BUFFER. Hard-coded so the
# canary check stays decoupled from upstream config drift — a change to
# the runtime buffer should be a deliberate review, not silent shift.
SLOT_TTL_BUFFER_SECONDS = 300


def _parse_iso(ts: str) -> datetime:
    """Tolerant ISO-8601 parser — strips trailing 'Z' that fromisoformat
    rejects on <3.11. The canary persists `started_at` in `Z` form (see
    `utils.helpers.utc_now_iso`)."""
    if ts.endswith("Z"):
        ts = ts[:-1]
    return datetime.fromisoformat(ts)


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Emit one violation per running row past its timeout + buffer."""
    violations: List[ViolationReport] = []

    snap_dt = _parse_iso(snapshot.snapshot_time)

    for agent in snapshot.agents:
        # No per-execution timeout column; agent-level cap governs all rows.
        threshold = agent.execution_timeout_seconds + SLOT_TTL_BUFFER_SECONDS

        for eid in sorted(agent.running_exec_ids):
            started_at = agent.running_started_at.get(eid)
            if not started_at:
                # No start timestamp — cannot age the row. Skip; either the
                # row is brand new (no started_at written yet, rare) or the
                # snapshot dropped the field. Other invariants flag the
                # surrounding bug class; E-01 should not double-fire.
                continue
            try:
                started_dt = _parse_iso(started_at)
            except ValueError:
                continue
            age_seconds = (snap_dt - started_dt).total_seconds()
            if age_seconds <= threshold:
                continue

            violations.append(
                ViolationReport(
                    invariant_id=INVARIANT_ID,
                    tier=TIER,
                    severity=SEVERITY,
                    observed_state={
                        "agent_name": agent.name,
                        "execution_id": eid,
                        "started_at": started_at,
                        "snapshot_time": snapshot.snapshot_time,
                        "age_seconds": int(age_seconds),
                        "execution_timeout_seconds": agent.execution_timeout_seconds,
                        "slot_ttl_buffer_seconds": SLOT_TTL_BUFFER_SECONDS,
                    },
                    signal_query=(
                        f"schedule_executions row {eid} "
                        f"(agent={agent.name}) status='running' "
                        f"age={int(age_seconds)}s > "
                        f"timeout+buffer={threshold}s"
                    ),
                )
            )

    return violations
