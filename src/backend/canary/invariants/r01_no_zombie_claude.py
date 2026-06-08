"""
R-01 — No zombie Claude processes (CANARY-001 / Issue #411 — Phase 3).

For every running Trinity agent container:
    `ps -eo stat,comm | grep ' Z.*claude' | wc -l == 0`

A nonzero count means a `claude` child process exited but was not
reaped by its parent — a zombie. Zombies don't consume CPU, but they
hold a PID, eventually exhausting the container's process table on
busy agents. PR #407 fixed the case where the agent-server stopped
calling `wait()` on its Claude subprocesses; R-01 is the regression
guard for that bug class.

## Source caveats

This is the first canary invariant that needs Docker access. The
snapshot collector handles the new failure modes:

- Docker client not initialized (test/embedded mode): the snapshot
  records `docker: client unavailable` in `sources_unavailable` and
  produces no `zombie_counts` entries. The check below treats every
  agent as "no data, skip" — i.e. silently green, no false fires.
- Per-container exec failure (container died, network glitch,
  permission error): recorded as `docker.exec[name]: <reason>` in
  `sources_unavailable`. That single agent is skipped; the rest of
  the cycle continues normally.

The R-01 check itself only fires when we have a real number from a
real container and that number is > 0.

## Severity

Tier A, critical. A zombie leak in an agent container is a slow-fuse
breakage — the agent keeps running fine for minutes, then suddenly
can't fork anything. The canary catches it before the symptom does.
"""

from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "R-01"
TIER = "A"
SEVERITY = "critical"


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Emit one violation per agent with zombie claude processes."""
    violations: List[ViolationReport] = []

    # If docker_client / docker.list failed wholesale, `zombie_counts` is
    # empty and `sources_unavailable` carries a `docker:` entry. The
    # all-agents check below short-circuits in that case (no entries to
    # iterate); no special handling needed.
    for agent_name, count in sorted(snapshot.zombie_counts.items()):
        if count <= 0:
            continue
        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "agent_name": agent_name,
                    "zombie_count": count,
                    "snapshot_time": snapshot.snapshot_time,
                },
                signal_query=(
                    f"docker exec agent-{agent_name} sh -c "
                    "\"ps -eo stat,comm | grep '^Z.*claude' | wc -l\" "
                    f"= {count}"
                ),
            )
        )

    return violations
