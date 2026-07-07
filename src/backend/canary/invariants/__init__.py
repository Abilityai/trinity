"""
Canary invariant library (CANARY-001 / Issue #411).

Each invariant is a pure function `check(snapshot) → list[ViolationReport]`.
The library is registry-driven so the run-cycle endpoint can enable/disable
invariants per request.

Phase 1 (#653) shipped:

- S-01: slot–row bijection (Redis ZSET vs SQL running rows)
- E-02: no phantom state reversal (terminal executions stay terminal)
- L-03: delete cascades (no orphan rows referencing removed agents)

Phase 2 (#882) adds four single-source SQL/Redis checks — same shape as
Phase 1, no new source types:

- S-02: no overbooking (ZCARD ≤ max_parallel_tasks)
- E-01: terminal-state closure (no row past timeout + buffer in `running`)
- E-05: dispatched rows have session (no >60s running row with NULL session)
- B-01: queue-status coherence (`db.get_queued_count` vs id-list count)

Phase 3 (#882, same PR) adds three moderate-complexity checks — each
brings one new piece of plumbing:

- S-03: slot TTL ≥ execution timeout (per-slot Redis TTL lookup)
- B-02: no queued without slots-full (`canary:drain_tick_at` heartbeat)
- R-01: no zombie claude processes (docker exec into agent containers)

Subsequent phases register additional invariants here without changes to
the snapshot collector or the run-cycle endpoint.
"""

from typing import Callable, Dict, Iterable, List

from ..snapshot import Snapshot, ViolationReport
from .s01_slot_row_bijection import check as s01_check
from .s02_no_overbooking import check as s02_check
from .s03_slot_ttl_floor import check as s03_check
from .e01_terminal_state_closure import check as e01_check
from .e02_no_phantom_reversal import check as e02_check
from .e05_dispatched_rows_have_session import check as e05_check
from .e06_no_overdue_next_run import check as e06_check
from .l03_delete_cascades import check as l03_check
from .b01_queue_status_coherence import check as b01_check
from .b02_no_queued_without_slots_full import check as b02_check
from .r01_no_zombie_claude import check as r01_check


# Public registry. Keys are the invariant ids the run-cycle endpoint
# accepts in its `invariants` filter.
INVARIANTS: Dict[str, Callable[[Snapshot], List[ViolationReport]]] = {
    "S-01": s01_check,
    "S-02": s02_check,
    "S-03": s03_check,
    "E-01": e01_check,
    "E-02": e02_check,
    "E-05": e05_check,
    "E-06": e06_check,
    "L-03": l03_check,
    "B-01": b01_check,
    "B-02": b02_check,
    "R-01": r01_check,
}


def run_invariants(
    snapshot: Snapshot,
    ids: Iterable[str] | None = None,
) -> Dict[str, List[ViolationReport]]:
    """Apply the named invariants to the snapshot.

    Returns dict {invariant_id: [violations]}. Empty list = invariant held.
    A check raising is logged and surfaces as `{}` for that id (caller can
    distinguish skipped via the absence of the key, but Phase 1 treats both
    as "no violation written").
    """
    selected = list(ids) if ids is not None else list(INVARIANTS.keys())
    out: Dict[str, List[ViolationReport]] = {}
    for inv_id in selected:
        check_fn = INVARIANTS.get(inv_id)
        if check_fn is None:
            continue
        try:
            out[inv_id] = check_fn(snapshot)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "canary invariant %s raised; skipping cycle for this id", inv_id
            )
            # Do not write a violation for a check error — that would be
            # noise. Surface via logs and let operators investigate.
            out[inv_id] = []
    return out
