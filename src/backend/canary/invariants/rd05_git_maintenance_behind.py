"""
RD-05 — Git maintenance keeps up (CANARY-001 / #4000).

A measured repo never sits at more than 3x the agent's own maintenance
trigger: `loose_objects <= 3 * GIT_MAINTENANCE_LOOSE_THRESHOLD` and
`pack_count <= 3 * GIT_MAINTENANCE_PACK_THRESHOLD`. The agent repacks once
either crosses 1x, so 3x means maintenance has been failing, skipped or
out-run for many ticks — the state that ends in a full disk. Level-triggered
on the stored stats; the canary ledger pages once per green→red.

Separate id from RD-04 on purpose: both are persistent states, and folding
them into one id would stop a bloat episode from paging while a measurement
episode is already red (ledger is green→red only).

Only running agents (`snapshot.docker_agent_names`): a stopped agent's row is
frozen, and reporting its last numbers as current would hold the id red for
nothing.

Tier B, severity **major**. NULL stats are RD-04's to report, not this one's.
"""

from typing import List

from ..snapshot import COLLECTOR_GIT_STATS, Snapshot, ViolationReport


INVARIANT_ID = "RD-05"
TIER = "B"
SEVERITY = "major"

# The agent's DEFAULTS (`docker/base-image/agent_server/routers/git.py`,
# `GIT_MAINTENANCE_{LOOSE,PACK}_THRESHOLD`). Per-agent env overrides are not
# visible to the backend, so these are constants, pinned to the agent defaults
# by tests/unit/test_4000_git_health_canary.py.
AGENT_LOOSE_THRESHOLD = 6700
AGENT_PACK_THRESHOLD = 20
BACKLOG_MULTIPLIER = 3


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """One violation per git-bound agent whose stored backlog is past 3x."""
    if not snapshot.collector_succeeded(COLLECTOR_GIT_STATS):
        return []
    loose_limit = BACKLOG_MULTIPLIER * AGENT_LOOSE_THRESHOLD
    pack_limit = BACKLOG_MULTIPLIER * AGENT_PACK_THRESHOLD

    violations: List[ViolationReport] = []
    for name, row in sorted(snapshot.git_stats.items()):
        if name not in snapshot.docker_agent_names:
            continue
        loose = row.get("loose_objects")
        packs = row.get("pack_count")
        if (loose or 0) <= loose_limit and (packs or 0) <= pack_limit:
            continue
        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "agent_name": name,
                    "loose_objects": loose,
                    "pack_count": packs,
                    "git_dir_bytes": row.get("git_dir_bytes"),
                    "maintenance_failures": row.get("maintenance_failures"),
                    "git_stats_at": row.get("git_stats_at"),
                    "loose_limit": loose_limit,
                    "pack_limit": pack_limit,
                    "snapshot_time": snapshot.snapshot_time,
                },
                signal_query=(
                    f"agent_sync_state row for {name}: loose_objects={loose} "
                    f"(limit {loose_limit}), pack_count={packs} (limit {pack_limit})"
                ),
            )
        )
    return violations
