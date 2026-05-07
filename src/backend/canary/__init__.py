"""
Canary invariant harness (CANARY-001 / Issue #411).

Continuous orchestration-invariant testing harness running against
staging/dev. Ships in phases per docs/planning/CANARY_HARNESS_PHASE_1.md;
Phase 1 covers S-01, E-02, L-03 with the snapshot collector and
deterministic invariant library colocated here.

Public API:
- `collect_snapshot()` — gather a roughly-simultaneous read of Redis × SQL
  × agent registry state, returning a typed `Snapshot`.
- `run_invariants(snapshot, ids)` — apply the named invariants to the
  snapshot and return a list of `ViolationReport`.
- `INVARIANTS` — registry of invariant id → check function.

`services/canary_service.py` drives these on a 5-minute background loop
in the backend process; `POST /api/canary/run-cycle` exposes the same
entrypoint for on-demand smoke tests.
"""

from .snapshot import (
    Snapshot,
    AgentSnapshot,
    OrphanRef,
    ViolationReport,
    collect_snapshot,
)
from .invariants import INVARIANTS, run_invariants

__all__ = [
    "Snapshot",
    "AgentSnapshot",
    "OrphanRef",
    "ViolationReport",
    "collect_snapshot",
    "run_invariants",
    "INVARIANTS",
]
