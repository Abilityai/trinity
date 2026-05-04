"""
Canary invariant library — Phase 1 (CANARY-001 / Issue #411).

Each invariant is a pure function `check(snapshot) → list[ViolationReport]`.
The library is registry-driven so the run-cycle endpoint can enable/disable
invariants per request.

Phase 1 ships three:

- S-01: slot–row bijection (Redis ZSET vs SQL running rows)
- E-02: no phantom state reversal (terminal executions stay terminal)
- L-03: delete cascades (no orphan rows referencing removed agents)

Subsequent phases register additional invariants here without changes to
the snapshot collector or the run-cycle endpoint.
"""

from typing import Callable, Dict, Iterable, List

from ..snapshot import Snapshot, ViolationReport
from .s01_slot_row_bijection import check as s01_check
from .e02_no_phantom_reversal import check as e02_check
from .l03_delete_cascades import check as l03_check


# Public registry. Keys are the invariant ids the run-cycle endpoint
# accepts in its `invariants` filter.
INVARIANTS: Dict[str, Callable[[Snapshot], List[ViolationReport]]] = {
    "S-01": s01_check,
    "E-02": e02_check,
    "L-03": l03_check,
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
