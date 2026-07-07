"""
E-06 — No overdue next_run_at (CANARY-001 / Issue #411; #1472).

An enabled, non-deleted schedule's ``next_run_at`` is a projection of the next
FUTURE cron window. A value more than ``misfire_grace_time`` in the PAST means
the scheduler never advanced the projection — the "Next: Nd ago" bug (#1472):
the schedule was never registered (a silent ``_add_job`` failure), or a fire
path returned without advancing. The #1472 fixes close every known such path;
E-06 is the detection net for any residual / future regression.

Tier B because it is a time-window check (a value is only overdue once it is
past ``now − grace``), aligned with the 5-min canary cadence. Severity **major**,
not critical: a frozen projection is user-visible (a schedule that looks
perpetually overdue and isn't firing) but is not a data-corruption event.
"""

from datetime import datetime, timedelta, timezone
from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "E-06"
TIER = "B"
SEVERITY = "major"

# Matches src/scheduler/config.py MISFIRE_GRACE_TIME default (3600s). Hard-coded
# so the canary stays decoupled from runtime config drift (cf. E-01's buffer):
# generous enough that a healthy schedule (next_run_at always future) can never
# fire it, but a truly frozen projection (>1h in the past) does.
MISFIRE_GRACE_SECONDS = 3600


def _to_utc(ts: str) -> datetime:
    """ISO-8601 → aware UTC datetime. Handles a trailing 'Z' and an offset
    (a non-UTC schedule stores an aware next_run_at) — comparing naive vs aware
    would raise, so normalize both sides to aware UTC."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """One violation per enabled schedule whose next_run_at is more than the
    misfire grace behind the snapshot time."""
    violations: List[ViolationReport] = []
    try:
        now = _to_utc(snapshot.snapshot_time)
    except ValueError:
        return violations
    threshold = now - timedelta(seconds=MISFIRE_GRACE_SECONDS)

    for sched in snapshot.enabled_schedules:
        nxt = sched.get("next_run_at")
        if not nxt:
            # No projection yet (e.g. a brand-new schedule the scheduler hasn't
            # registered this cycle). Not overdue — E-06 only flags a value that
            # exists and is stale, so a transient null can't false-fire.
            continue
        try:
            nxt_dt = _to_utc(nxt)
        except ValueError:
            continue
        if nxt_dt >= threshold:
            continue

        overdue_seconds = int((now - nxt_dt).total_seconds())
        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "agent_name": sched.get("agent_name"),
                    "schedule_id": sched.get("schedule_id"),
                    "next_run_at": nxt,
                    "snapshot_time": snapshot.snapshot_time,
                    "overdue_seconds": overdue_seconds,
                    "misfire_grace_seconds": MISFIRE_GRACE_SECONDS,
                },
                signal_query=(
                    f"agent_schedules row {sched.get('schedule_id')} "
                    f"(agent={sched.get('agent_name')}) enabled with "
                    f"next_run_at={nxt} overdue {overdue_seconds}s "
                    f"> grace {MISFIRE_GRACE_SECONDS}s"
                ),
            )
        )
    return violations
