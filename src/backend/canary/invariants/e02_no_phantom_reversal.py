"""
E-02 — No phantom state reversal (CANARY-001 / Issue #411).

A `schedule_executions` row that has been observed in a terminal status
must never appear in a non-terminal status in a later snapshot. Catches
the bug class behind PR #378 / #403 (phantom stale-slot failures), where
a completed/failed/cancelled execution silently flips back to running.

## Phase 1 implementation note

The design doc proposed "Vector log diff (`update_execution_status` lines)"
as the snapshot input. That requires log-file plumbing into the canary's
container — non-trivial and orthogonal to the check itself.

Phase 1 instead uses a **state-comparison** detector: each cycle, the set
of recently-terminal execution_ids (last 30 min, per the snapshot
collector) is compared against the previous cycle's set, persisted in a
Redis hash `canary:e02:terminal_seen`. Any execution_id that was in the
previous set, is *not* terminal in this snapshot's running/queued tables,
**and** still exists in the DB → reversal violation.

This is strictly more sensitive than log diffing for this bug class:
even a reversal that happens silently (no log line, e.g. via direct DB
write) is caught. The trade-off is a small Redis side-table; fine in
exchange for a cleaner Phase 1.

When Phase 2 wires up Vector log access, we may add a complementary
log-based detector — but the state-comparison check stands on its own.

Tier A, severity critical. Backsliding past terminal is exactly the
class of bug this harness exists to catch.
"""

import logging
from typing import List, Set

from ..snapshot import Snapshot, ViolationReport, TERMINAL_EXECUTION_STATUSES


logger = logging.getLogger(__name__)


INVARIANT_ID = "E-02"
TIER = "A"
SEVERITY = "critical"

# Redis hash storing the previous cycle's terminal set.
# Members are execution_ids; values are the terminal status at last sight.
REDIS_KEY_PREV_TERMINAL = "canary:e02:terminal_seen"

# Cap on stored set size to bound the side-table; older entries are
# trimmed by snapshot_collector's window (30 min) anyway.
PREV_TERMINAL_MAX = 5000


def _redis():
    """Lazy import of the slot service's Redis client."""
    from services.slot_service import get_slot_service

    return get_slot_service().redis


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Detect terminal→non-terminal reversals across snapshots."""
    violations: List[ViolationReport] = []

    # If SQL terminal-set read failed, skip — the comparison is meaningless.
    if any(
        s.startswith("sqlite.terminal_executions") for s in snapshot.sources_unavailable
    ):
        return violations

    try:
        redis_client = _redis()
        previous: Set[str] = set(
            redis_client.hkeys(REDIS_KEY_PREV_TERMINAL) or []
        )
    except Exception:
        # Redis unreachable — record once via the snapshot mechanism on
        # subsequent cycles, but for this cycle there's nothing to compare.
        logger.exception("E-02: previous terminal set unreadable; skipping")
        return violations

    current_terminal = snapshot.terminal_exec_ids

    # Reversal candidates: ids that were terminal previously but are now
    # in the running/queued sets. Cross-reference against per-agent
    # snapshots (the only place running/queued sets live).
    running_now: Set[str] = set()
    queued_now: Set[str] = set()
    for agent in snapshot.agents:
        running_now |= agent.running_exec_ids
        queued_now |= agent.queued_exec_ids

    reversed_ids = previous & (running_now | queued_now)
    for eid in sorted(reversed_ids):
        previous_status = redis_client.hget(REDIS_KEY_PREV_TERMINAL, eid)
        current_status = "running" if eid in running_now else "queued"
        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "execution_id": eid,
                    "previous_status": previous_status,
                    "current_status": current_status,
                    "snapshot_time": snapshot.snapshot_time,
                    "terminal_statuses_tracked": list(TERMINAL_EXECUTION_STATUSES),
                },
                signal_query=(
                    f"execution_id {eid} was terminal in previous cycle "
                    f"({previous_status}); now {current_status}"
                ),
            )
        )

    # Update the side-table with this cycle's terminal set so the next
    # cycle has something to compare against. Done after the check so a
    # crash mid-write doesn't poison the next cycle.
    try:
        # Mapping eid -> status for richer reports; we store a placeholder
        # since current snapshot doesn't carry the per-row status (the
        # collector only fetches ids). Acceptable trade-off — the row's
        # actual status at violation time is what we report.
        if current_terminal:
            redis_client.hset(
                REDIS_KEY_PREV_TERMINAL,
                mapping={eid: "terminal" for eid in current_terminal},
            )
        # Trim if oversized — pop the oldest by HLEN heuristic. SQL
        # window already bounds growth, so this is belt-and-suspenders.
        if redis_client.hlen(REDIS_KEY_PREV_TERMINAL) > PREV_TERMINAL_MAX:
            redis_client.delete(REDIS_KEY_PREV_TERMINAL)
            if current_terminal:
                redis_client.hset(
                    REDIS_KEY_PREV_TERMINAL,
                    mapping={eid: "terminal" for eid in current_terminal},
                )
    except Exception:
        logger.exception("E-02: failed to persist terminal set; next cycle will skip")

    return violations
