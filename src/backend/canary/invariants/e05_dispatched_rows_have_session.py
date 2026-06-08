"""
E-05 — Dispatched rows have session (CANARY-001 / Issue #411 — Phase 2).

A `schedule_executions` row that is `status='running'` and has been
dispatched for more than 60 seconds must have `claude_session_id IS NOT
NULL`. The 60-second window is the SLA `mark_no_session_executions_failed`
operates on — anything older than that should already have been failed
out by the watchdog.

A violation says: the row was dispatched, the agent should have written
back a session id by now (Claude Code's first turn always returns one),
the watchdog should have noticed and failed the row out if it hadn't —
and none of those things happened. That's issue #106 reopening.

## Why 60 seconds

The first `claude --print` turn writes a session id within milliseconds
of starting. Anything past 60s without one means either:
- agent-server crashed before writing back (watchdog should catch)
- session-id update path is broken (the bug class to detect)
- DB write failed silently

The catalog calls 60s out explicitly as both the SLA and the grace
window — see `docs/testing/orchestration-invariant-catalog.md` E-05.

## Why major, not critical

A missing session id doesn't directly break the agent's work — the
execution still runs, the slot still releases, the user still gets a
response. What breaks is session resumption, conversation continuity,
and the observability link to the JSONL file in the container. Real
operational problem, but not the lights-out kind S-01/S-02/E-01 flag.

Tier B, severity major.
"""

from datetime import datetime
from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "E-05"
TIER = "B"
SEVERITY = "major"

# Per the catalog: dispatched rows have 60s to acquire a session id.
SESSION_GRACE_SECONDS = 60


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1]
    return datetime.fromisoformat(ts)


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Emit one violation per running row past 60s with no session id."""
    violations: List[ViolationReport] = []

    snap_dt = _parse_iso(snapshot.snapshot_time)

    for agent in snapshot.agents:
        for eid in sorted(agent.running_exec_ids):
            session_id = agent.running_claude_session_ids.get(eid)
            if session_id:
                continue
            started_at = agent.running_started_at.get(eid)
            if not started_at:
                # No start timestamp — cannot age the row. Skip.
                continue
            try:
                started_dt = _parse_iso(started_at)
            except ValueError:
                continue
            age_seconds = (snap_dt - started_dt).total_seconds()
            if age_seconds <= SESSION_GRACE_SECONDS:
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
                        "grace_seconds": SESSION_GRACE_SECONDS,
                    },
                    signal_query=(
                        f"schedule_executions row {eid} "
                        f"(agent={agent.name}) status='running' "
                        f"age={int(age_seconds)}s > {SESSION_GRACE_SECONDS}s "
                        f"and claude_session_id IS NULL"
                    ),
                )
            )

    return violations
