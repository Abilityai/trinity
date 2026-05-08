"""
E-02 — No phantom state reversal (CANARY-001 / Issue #411).

A `schedule_executions` row that has been observed in a terminal status
must never appear in a non-terminal status in a later snapshot. Catches
the bug class behind PR #378 / #403 (phantom stale-slot failures), where
a success/failed/cancelled/skipped execution silently flips back to running.

## Phase 1 implementation note

The design doc proposed "Vector log diff (`update_execution_status` lines)"
as the snapshot input. That requires log-file plumbing into the canary's
container — non-trivial and orthogonal to the check itself.

Phase 1 instead uses a **state-comparison** detector: each cycle, the set
of recently-terminal execution_ids (last 30 min, per the snapshot
collector) is compared against the previous cycle's set, persisted in a
Redis sorted set `canary:e02:terminal_seen` with `score = unix ts last
seen terminal`. Any execution_id that was in the previous set, is *not*
terminal in this snapshot's running/queued tables, **and** still exists
in the DB → reversal violation.

This is strictly more sensitive than log diffing for this bug class:
even a reversal that happens silently (no log line, e.g. via direct DB
write) is caught. The trade-off is a small Redis side-table; fine in
exchange for a cleaner Phase 1.

The side-table is bounded by **age**, not by a hard count. Each cycle
calls `ZREMRANGEBYSCORE` to drop entries older than
`PREV_TERMINAL_RETENTION_SECONDS`. Earlier versions of this code
DEL'd the entire key once it crossed a 5000-row hard cap and re-added
only the current cycle's terminals, which left a one-cycle E-02 blind
spot every time the cap was crossed on busy installs. Score-based
trimming bounds memory by `activity_rate × retention_window` and never
loses an in-window terminal id to a hard reset.

When Phase 2 wires up Vector log access, we may add a complementary
log-based detector — but the state-comparison check stands on its own.

Tier A, severity critical. Backsliding past terminal is exactly the
class of bug this harness exists to catch.
"""

import logging
import time
from typing import Dict, List, Set

from ..snapshot import Snapshot, ViolationReport, TERMINAL_EXECUTION_STATUSES


logger = logging.getLogger(__name__)


INVARIANT_ID = "E-02"
TIER = "A"
SEVERITY = "critical"

# Redis sorted set storing the previous-cycle terminal ids.
# Members are execution_ids; scores are the unix ts the id was last
# observed in a snapshot's terminal set.
REDIS_KEY_PREV_TERMINAL = "canary:e02:terminal_seen"

# Parallel hash carrying the actual terminal status (success / failed /
# cancelled / skipped) for each id in the ZSET. Read on reversal so the
# violation report (and the Slack forensic line) can render the real
# prior status — earlier code stored the placeholder string "terminal"
# which made on-call alerts read "terminal → running" instead of e.g.
# "success → running".
REDIS_KEY_PREV_TERMINAL_STATUS = "canary:e02:terminal_status"

# Trim ids older than this many seconds. Comfortably larger than the
# snapshot collector's 30-min terminal window so an id does not age out
# between the cycle that records it and the cycle that detects a
# reversal of it. Age-based trimming replaced an earlier hard count cap
# (5000) that DEL'd the entire key on overflow, creating one-cycle blind
# spots on busy installs.
PREV_TERMINAL_RETENTION_SECONDS = 60 * 60  # 1 hour


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

    now_ts = time.time()
    cutoff_ts = now_ts - PREV_TERMINAL_RETENTION_SECONDS

    try:
        redis_client = _redis()
        # Snapshot the about-to-expire ids before trimming so we can
        # garbage-collect their entries from the parallel status hash in
        # the same cycle. Done via ZRANGEBYSCORE before ZREMRANGEBYSCORE
        # because the latter does not return the dropped members.
        expired_ids: List[str] = list(
            redis_client.zrangebyscore(REDIS_KEY_PREV_TERMINAL, "-inf", cutoff_ts) or []
        )
        redis_client.zremrangebyscore(REDIS_KEY_PREV_TERMINAL, "-inf", cutoff_ts)
        if expired_ids:
            redis_client.hdel(REDIS_KEY_PREV_TERMINAL_STATUS, *expired_ids)
        previous: Set[str] = set(
            redis_client.zrange(REDIS_KEY_PREV_TERMINAL, 0, -1) or []
        )
    except Exception:
        # Redis unreachable — record once via the snapshot mechanism on
        # subsequent cycles, but for this cycle there's nothing to compare.
        logger.exception("E-02: previous terminal set unreadable; skipping")
        return violations

    current_terminal: Dict[str, str] = snapshot.terminal_exec_statuses

    # Reversal candidates: ids that were terminal previously but are now
    # in the running/queued sets. Cross-reference against per-agent
    # snapshots (the only place running/queued sets live).
    running_now: Set[str] = set()
    queued_now: Set[str] = set()
    for agent in snapshot.agents:
        running_now |= agent.running_exec_ids
        queued_now |= agent.queued_exec_ids

    reversed_ids = previous & (running_now | queued_now)
    if reversed_ids:
        try:
            prev_status_values = redis_client.hmget(
                REDIS_KEY_PREV_TERMINAL_STATUS, *sorted(reversed_ids)
            )
        except Exception:
            logger.exception("E-02: status lookup failed; reporting status as unknown")
            prev_status_values = [None] * len(reversed_ids)
        prev_status_by_eid = dict(zip(sorted(reversed_ids), prev_status_values))
    else:
        prev_status_by_eid = {}

    for eid in sorted(reversed_ids):
        current_status = "running" if eid in running_now else "queued"
        previous_status = prev_status_by_eid.get(eid) or "unknown"
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
                    f"execution_id {eid} was {previous_status} in previous "
                    f"cycle; now {current_status}"
                ),
            )
        )

    # Update the side-table with this cycle's terminal set so the next
    # cycle has something to compare against. ZADD updates the score on
    # existing members, so a still-terminal id has its retention clock
    # refreshed and will not age out while it's still being observed.
    # The parallel hash carries the row's actual terminal status; HSET
    # overwrites on transitions between terminal statuses (rare in
    # practice but cheap to honour).
    try:
        if current_terminal:
            redis_client.zadd(
                REDIS_KEY_PREV_TERMINAL,
                {eid: now_ts for eid in current_terminal},
            )
            redis_client.hset(
                REDIS_KEY_PREV_TERMINAL_STATUS,
                mapping=current_terminal,
            )
    except Exception:
        logger.exception("E-02: failed to persist terminal set; next cycle will skip")

    return violations
