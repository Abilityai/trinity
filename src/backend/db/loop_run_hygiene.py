"""Which `running` loop-run rows can never be advanced (#4058).

Before #4023 the reconcile-versus-advance race (#3317) could insert two
`agent_loop_runs` rows with one `(loop_id, run_number)`. #4023 stops new ones,
but rows already in a database stay. A duplicate whose terminal arrives after
the loop moved past its run number is refused by the #3316 repair path and
stays `running` for ever, and `rearm_lost_dispatch` used to take the first
`running` row as the loop's open run, so reconcile kept retrying that row and
could miss a newer open run whose terminal was lost.

One decision, shared by the runtime (`LoopOperations.rearm_lost_dispatch`) and
the one-time cleanup (SQLite `loop_runs_stale_duplicates` + Alembic
`0105_loop_runs_stale_duplicates`). Pure: no database access and no imports,
so both migration tracks can load it.
"""
from typing import Iterable, Mapping, Set

# The run status the cleanup closes a stale row with. Not `failed`:
# `_DerivedState` counts `failed` rows into `failed_runs` and the failure
# policy, and a duplicate row is not a failed iteration.
ABANDONED = "abandoned"
ABANDONED_ERROR = (
    "Closed by the #4058 cleanup: a duplicate or orphaned run row that no "
    "terminal could ever advance."
)


def stale_running_run_ids(
    *, loop_is_terminal: bool, runs_completed: int, runs: Iterable[Mapping],
) -> Set[str]:
    """The ids of the loop's `running` run rows that no terminal can advance.

    * Every `running` row of a loop that has ended: `claim_loop_advance`
      excludes a terminal loop, so nothing advances it.
    * A `running` row numbered below `runs_completed`: the loop has claimed a
      later run, and the #3316 repair acts only at `runs_completed`.
    * A `running` row whose run number also has a closed row: that number was
      already advanced from its twin.
    * Of several `running` rows sharing a number with no closed twin, all but
      the earliest (by `started_at`, then id): the earliest stays the open run.

    A lone `running` row at `runs_completed` (a #3316 claimed-but-unclosed
    run) or at `runs_completed + 1` (the run in flight) is never stale.
    """
    runs = list(runs)
    running = [r for r in runs if r.get("status") == "running"]
    if loop_is_terminal:
        return {r["id"] for r in running}
    closed_numbers = {r.get("run_number") for r in runs if r.get("status") != "running"}
    stale: Set[str] = set()
    kept: Set = set()
    for r in sorted(running, key=lambda r: (r.get("started_at") or "", r["id"])):
        number = r.get("run_number")
        if (
            (isinstance(number, int) and number < (runs_completed or 0))
            or number in closed_numbers
            or number in kept
        ):
            stale.add(r["id"])
        else:
            kept.add(number)
    return stale
