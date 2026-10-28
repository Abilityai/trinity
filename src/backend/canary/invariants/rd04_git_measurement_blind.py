"""
RD-04 — Git health is measured (CANARY-001 / #4000).

Every running git-bound agent, once past its startup grace, has measured its
repo (`agent_sync_state.git_dir_bytes / pack_count / loose_objects`) within
the last 24 h. Before #4000 a self-pushing agent (`auto_sync_enabled = 0`)
never reached the measuring code, so the three columns stayed NULL and the
`git_bloat` alert built on them could never fire: the repo grew unbounded with
nothing anywhere saying so. RD-04 is the net under that blindness.

Measurement time is `git_stats_at`. An agent built before #4000 never writes
it but still measures on its sync cycle, so while it is NULL and the stats are
present `last_sync_at` stands in — the backend deploys before agents are
recreated, and that window must not page the fleet.

Tier B (the predicate is a 24 h window), severity **major**: a blind health
signal is not itself corruption, but it is what lets a disk fill silently.
"""

from datetime import timedelta
from typing import List

from ..snapshot import COLLECTOR_GIT_STATS, Snapshot, ViolationReport
from .e06_no_overdue_next_run import _to_utc


INVARIANT_ID = "RD-04"
TIER = "B"
SEVERITY = "major"

# The agent's first maintenance tick lands up to interval (900 s) + first-tick
# jitter (up to 900 s) + repack budget (up to 1800 s) after the container
# starts — 3600 s worst case, then up to one 60 s sync-health poll before the
# row carries it. Doubled for margin; inside it a NULL is "not measured YET".
STARTUP_GRACE_SECONDS = 7200
STALE_AFTER_SECONDS = 24 * 3600

_STAT_KEYS = ("git_dir_bytes", "pack_count", "loose_objects")


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """One violation per running, past-grace git-bound agent whose git stats
    are NULL or whose measurement is older than 24 h."""
    if not snapshot.collector_succeeded(COLLECTOR_GIT_STATS):
        return []
    try:
        now = _to_utc(snapshot.snapshot_time)
    except ValueError:
        return []

    violations: List[ViolationReport] = []
    for name, row in sorted(snapshot.git_stats.items()):
        if name not in snapshot.docker_agent_names:
            continue
        started = snapshot.zombie_container_started_at.get(name)
        if not started:
            continue  # unknown start: cannot tell blind from booting
        try:
            up_seconds = (now - _to_utc(started)).total_seconds()
        except ValueError:
            continue
        if up_seconds < STARTUP_GRACE_SECONDS:
            continue

        measured_age = None
        if any(row.get(k) is not None for k in _STAT_KEYS):
            measured_at = row.get("git_stats_at") or row.get("last_sync_at")
            if not measured_at:
                continue  # stats with no time at all: cannot age them
            try:
                measured_age = int((now - _to_utc(measured_at)).total_seconds())
            except ValueError:
                continue
            if measured_age <= STALE_AFTER_SECONDS:
                continue
            reason = "stale"
        else:
            reason = "never_measured"

        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "agent_name": name,
                    "reason": reason,
                    "git_stats_at": row.get("git_stats_at"),
                    "last_sync_at": row.get("last_sync_at"),
                    "measured_age_seconds": measured_age,
                    "container_up_seconds": int(up_seconds),
                    "snapshot_time": snapshot.snapshot_time,
                },
                signal_query=(
                    f"agent_sync_state row for {name}: git stats {reason} "
                    f"(age={measured_age}s, threshold {STALE_AFTER_SECONDS}s) "
                    f"while the container has been up {int(up_seconds)}s"
                ),
            )
        )
    return violations
