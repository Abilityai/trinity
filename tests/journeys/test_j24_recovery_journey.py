"""Journey J24: my agents' work survives the platform failing under it
(trinity-enterprise#905; ent#847 matrix row B9).

**Promise:** a cancel really ends the work, a backend restart mid-turn neither
loses the result nor mistimes it, and a rebuilt container keeps doing its job.

**The real harness is not in this tier.** It is
``tests/system_chains/test_j24_recovery_chain.py`` — six legs from the rc3
recheck log, pinning the live wiring of t#3588 / t#3589 / t#3590. It restarts
the backend, force-removes containers, waits out the 180 s watchdog claim grace
and needs a real model credential, so it cannot ride the per-PR journey lane:
its lane is ``.github/workflows/recovery-journey.yml`` (workflow_dispatch +
``secrets.JOURNEY_ANTHROPIC_API_KEY``), driven by
``scripts/system/run_recovery.sh`` in two phases.

This file is the tier-level stand-in the Journey Impact gate (#2350) asks a
``new:`` declaration to carry — a named, failing-on-success claim, the J11
pattern. ``strict=True`` is the load-bearing half: if this ever passes by
accident the suite goes RED. It goes away when the catalog flips J24 to
``built: true`` after the first green dispatch run.

The lane's WIRING is pinned per-PR by
``tests/unit/test_ent905_recovery_lane_parity.py`` — the pilot agent name must
agree across the harness, the runner and the workflow, and the workflow must
stay dispatch-only (credentials are forbidden on ``pull_request``, #1896).
"""
import pytest

pytestmark = pytest.mark.journey


@pytest.mark.xfail(
    strict=True,
    reason="J24 runs on its own dispatch lane (recovery-journey.yml), never "
           "per-PR — the legs restart the backend and need a real model "
           "credential. The chain harness is "
           "tests/system_chains/test_j24_recovery_chain.py; this stand-in is "
           "removed when catalog.yaml flips J24 to built: true after the "
           "first green dispatch run (trinity-enterprise#905).",
)
def test_j24_agents_work_survives_cancel_restart_and_rebuild():
    """The promise, not the plumbing — what the chain asserts on its own lane:

    1. a cancel, on the push AND the pull path, ends the execution with a real
       duration, closes every activity row (t#3588, the #1804 class) and leaves
       no process in the container;
    2. a backend restart mid sync turn: the row completes at the AGENT'S finish
       — ``completed_at`` is the retained instant, duration is the real turn,
       never the watchdog claim time (t#3589: rc2 recorded 5m32s for 64 s);
    3. a restart mid agent-to-agent turn: the caller's collaboration record is
       closed at the callee's finish (t#3588; rc2 left it ``started``);
    4. a force-removed pilot container is rebuilt WITH its pull env, the worker
       pool restarts and the queue drains (t#3590);
    5. de-piloting recreates the container exactly once (``config_drift``), the
       baked flag is gone, and a second /start does not recreate again.

    Invariants it rides on: E-01 (terminal-state closure), AC-01 (the call left
    a closed activity record).
    """
    raise AssertionError(
        "J24 does not run on this lane — dispatch recovery-journey.yml; see "
        "tests/system_chains/test_j24_recovery_chain.py"
    )
