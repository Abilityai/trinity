"""
L-03 — Delete cascades (CANARY-001 / Issue #411).

No live row in any cross-cutting table (agent_sharing, agent_schedules,
non-terminal schedule_executions, agent_skills, agent_tags, agent_shared_files,
agent_public_links, pending operator_queue, pending access_requests,
agent-scoped mcp_api_keys, active chat_sessions) may reference an
agent_name that is not present in `agent_ownership`.

Additionally, no Redis `agent:slots:{name}` key may exist for a name not
in `agent_ownership`.

This invariant catches the bug class where deletion of an agent leaves
dangling references — the symptom of the original Issue #129 family. The
list of scanned tables is in `canary.snapshot.ORPHAN_SCAN_TABLES`.

Tier A. Severity scales with the table:
- `schedule_executions` (running/queued), Redis slot keys → critical
  (active orchestration state pointing at a ghost agent)
- All other orphan refs → major
  (dangling permissions/sharing/tags; user-visible but not active orchestration)
"""

from collections import defaultdict
from typing import Dict, List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "L-03"
TIER = "A"

# Tables whose orphan rows constitute *active* orchestration state and
# warrant the `critical` severity tier. Rest are `major`.
CRITICAL_TABLES = frozenset({"schedule_executions"})


def _severity_for(table: str) -> str:
    return "critical" if table in CRITICAL_TABLES else "major"


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Emit one violation per orphaned agent_name (grouped across tables)."""
    violations: List[ViolationReport] = []

    # If the SQL orphan scan failed, skip — partial result is misleading.
    if any(s.startswith("sqlite.orphan_refs") for s in snapshot.sources_unavailable):
        return violations

    # Group orphan refs by referenced_agent_name so one ghost agent shows
    # up as one violation report (not one per dangling row).
    by_agent: Dict[str, List] = defaultdict(list)
    for ref in snapshot.orphan_refs:
        by_agent[ref.referenced_agent_name].append(ref)

    # Redis orphan slots also belong to the same agent grouping.
    redis_orphans = dict(snapshot.orphan_redis_slots)
    for name in redis_orphans:
        by_agent.setdefault(name, [])

    for agent_name, refs in sorted(by_agent.items()):
        tables_hit = {ref.table for ref in refs}
        # If this ghost agent has Redis slot membership, that's an
        # active-orchestration signal regardless of which SQL tables fired.
        has_redis_slots = redis_orphans.get(agent_name, 0) > 0
        if has_redis_slots:
            tables_hit.add("redis:agent:slots")

        severity = (
            "critical"
            if any(t in CRITICAL_TABLES for t in tables_hit) or has_redis_slots
            else "major"
        )

        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=severity,
                observed_state={
                    "ghost_agent_name": agent_name,
                    "snapshot_time": snapshot.snapshot_time,
                    "orphan_count": len(refs),
                    "redis_slot_count": redis_orphans.get(agent_name, 0),
                    "tables_hit": sorted(tables_hit),
                    "sample_refs": [
                        {"table": r.table, "column": r.column, "row_id": r.row_id}
                        for r in refs[:10]
                    ],
                },
                signal_query=(
                    f"agent_name '{agent_name}' referenced by "
                    f"{len(refs)} SQL row(s) and "
                    f"{redis_orphans.get(agent_name, 0)} Redis slot(s) "
                    "but absent from agent_ownership"
                ),
            )
        )

    return violations
