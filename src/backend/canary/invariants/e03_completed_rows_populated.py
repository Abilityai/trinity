"""
E-03 — Completed rows fully populated (CANARY-001 / Issue #411; #1077).

Every terminal `schedule_executions` row (`status IN
(success, failed, cancelled)`) must carry a non-NULL `completed_at`. A
terminal row with a NULL `completed_at` is a *half-written* row: the status
CAS landed but the paired timestamp write did not — an observability gap that
breaks every duration/rollup query keyed on `completed_at` and hints at a
producer path that finalized `status` without going through the shared terminal
applier.

## Predicate: `completed_at`-only, NOT `+ duration_ms`

The orchestration catalog phrases E-03 as `completed_at IS NOT NULL AND
duration_ms IS NOT NULL`. That second clause **false-fires in bulk on healthy
rows** and is deliberately dropped here (verified against `db/schedules.py`):

- The bulk queue-terminators — `cancel_queued_for_agent`,
  `fail_queued_for_agent`, `expire_stale_queued` — set `status`/`completed_at`
  but **never** `duration_ms`. Only a ran-to-completion `update_execution_status`
  computes `duration_ms`.
- So every agent-delete-with-backlog, dispatch-breaker trip, and 24h
  stale-queue expiry legitimately leaves a `cancelled`/`failed` row with
  `duration_ms = NULL`.

E-03 therefore asserts only what *every* terminal path honors:
`completed_at IS NOT NULL`. (A future `duration_ms`-coverage check would have to
scope to dispatched rows — `claude_session_id IS NOT NULL` — never
queue-terminated ones.)

## Leading-edge tripwire, not a backfill auditor

The terminal-row collector windows on `started_at` (`max(agent timeout) + 300s`,
capped at 5000 rows). A row malformed longer ago than that window is
out-of-scope **by design**: a live regression in a producer path fires E-03
continuously on fresh rows, which is what a canary is for. E-03 does not
retro-audit the 90-day terminal history.

## Why a canary and not a writer unit test

A writer unit test only covers the producer path it exercises. E-03 catches
**all** producers that write a terminal row — including the standalone scheduler
(`src/scheduler/`), whose raw-SQL non-CAS status writers (#1082 follow-up) a
backend writer test never touches. That cross-path coverage is the load-bearing
reason this is an invariant, not a unit test.

Tier A, severity **major** — a half-written terminal row is a real data-integrity
defect (observability queries silently drop it), but not a live orchestration
hazard (the slot is released, the agent keeps running).
"""

from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "E-03"
TIER = "A"
SEVERITY = "major"


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """One violation per terminal row with a NULL `completed_at`."""
    violations: List[ViolationReport] = []

    for row in snapshot.terminal_rows:
        if row.get("completed_at") is not None:
            continue

        eid = row.get("id")
        agent_name = row.get("agent_name")
        status = row.get("status")
        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "agent_name": agent_name,
                    "execution_id": eid,
                    "status": status,
                    "started_at": row.get("started_at"),
                    "completed_at": None,
                    "snapshot_time": snapshot.snapshot_time,
                },
                signal_query=(
                    f"schedule_executions row {eid} (agent={agent_name}) "
                    f"status='{status}' has completed_at IS NULL"
                ),
            )
        )

    return violations
