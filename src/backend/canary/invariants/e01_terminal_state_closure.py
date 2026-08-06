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

## Pull-claimed rows are excluded (#1990)

A `#1081` Phase 3 pull-CLAIMED row (`lease_expires_at IS NOT NULL`) is
`status='running'` but is owned **exclusively by the lease-reaper**, which
re-queues it (`redelivery_count` ++, `started_at` reset) or poison-parks it
terminal at `MAX_REDELIVERY`. Its age is therefore not evidence of the
stuck-execution class E-01 detects: the deadline that governs a leased row is
its *lease*, not this window, and a different component owns the recovery.

The overlap is not merely awkward, it is exact and total. `claim_next_queued`
stamps `lease_expires_at = started_at + (execution_timeout_seconds +
SLOT_TTL_BUFFER)` (`services/pull_coordination_service.py`) — the **same**
threshold this check builds. So `age > threshold` becomes true at the precise
instant the lease expires, which is the instant the reaper's recovery window
*opens*. The 300s buffer above exists so E-01 fires only *after* the owning
component has had its window to act; against a leased row that head-room is
zero, and E-01 would page **critical** for the whole gap between lease expiry
and the reaper's next sweep — on every re-delivery, per execution, while the
machinery works exactly as designed.

The db layer already encodes this ownership split, including on the very sweep
whose failure E-01 exists to detect: `mark_stale_executions_failed` carries
`lease_expires_at IS NULL`, as do `get_running_executions`,
`get_running_executions_with_agent_info`, `fail_stale_slot_execution` and
`mark_no_session_executions_failed` (the six sweep exclusions recorded in
`docs/testing/PULL_MIGRATION_TESTING.md` §Appendix). The canary was the layer
that had not caught up. Mirrors S-01's exclusion and E-05's (#1982).

The exclusion is keyed on the lease, **not** a blanket silencing: a NULL-lease
(push) row of identical age still fires, unchanged. Closes T3.7 in
`docs/testing/PULL_MIGRATION_TESTING.md` §3.

### Scope: E-02 was assessed and deliberately does NOT get this exclusion

E-01, E-05, S-01 and E-02 are the four invariants that read
`running_exec_ids`; the first three now exclude leased rows and E-02 must not.
E-02 asks a different question — "was this id terminal in a previous cycle and
is it non-terminal now?" — and the answer does not depend on who owns the row.
The reaper cannot produce that transition (`requeue_expired_lease` /
`park_expired_lease` both CAS on `status='running'` with a past lease, so a
terminal row is unreachable to them), and re-delivery *preserves* the
`execution_id` by construction (#1084/#525 are execution_id-scoped), so a
terminal id reappearing as running/queued is exactly the corruption E-02
exists to catch. Pull is more exposed to it than push, not less — a late
worker result and a reaper pass race for the same row — so excluding leased
rows there would blind E-02 precisely on the path #1081 introduces.
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
            # #1990: exclude pull-CLAIMED rows (mirrors S-01 and E-05, and the
            # `lease_expires_at IS NULL` clause `mark_stale_executions_failed`
            # — the sweep whose failure this invariant detects — already
            # carries). A leased row is owned EXCLUSIVELY by the lease-reaper,
            # and its lease is stamped at `started_at + execution_timeout +
            # SLOT_TTL_BUFFER`: the identical window, so this check would fire
            # at the exact instant the reaper first becomes eligible to act,
            # with zero head-room, on every re-delivery. See module docstring.
            if agent.running_lease_expires_at.get(eid) is not None:
                continue
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
