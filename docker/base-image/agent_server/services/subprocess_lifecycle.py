"""Subprocess lifecycle helpers for Claude Code execution.

Extracted from `claude_code.py` per #122 (issue split). Wraps the
`utils.subprocess_pgroup` primitives with `_drain_bounded` — a daemon-thread
budgeted variant of `drain_reader_threads` that protects the executor
thread from the TextIOWrapper-lock deadlock described in Issue #728.

Re-exports the four `utils.subprocess_pgroup` helpers used by the chat and
headless paths so callers only need a single import target.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from typing import Literal, Optional

from ..utils.subprocess_pgroup import (
    capture_pgid as _capture_pgid,
    terminate_process_group as _terminate_process_group,
    safe_close_pipes as _safe_close_pipes,
    drain_reader_threads as _drain_reader_threads,
)

# Outcome of a bounded drain, returned to the caller so the orchestrator can
# decide whether to treat the run's shared mutable state as trustworthy
# (#1025 / salvaged from #980). Every non-``completed`` outcome leaves a reader
# thread that may still be mutating the run context concurrently; the headless
# path snapshots its finalize-read fields on those outcomes.
DrainOutcome = Literal["completed", "budget_exceeded", "errored", "leaked"]

__all__ = [
    "_DRAIN_BUDGET_SECONDS",
    "DrainOutcome",
    "_drain_bounded",
    "_capture_pgid",
    "_terminate_process_group",
    "_safe_close_pipes",
    "_drain_reader_threads",
]

logger = logging.getLogger(__name__)

# Hard budget for drain_reader_threads when called from an executor thread.
# Python's buffered I/O holds an internal lock during readline(); if that
# thread is stuck waiting for pipe data, a concurrent pipe.close() will
# deadlock on the same lock and block asyncio.run() indefinitely (Issue #728).
# Wrapping every asyncio.run(_drain_reader_threads(...)) call in a daemon
# thread with this budget prevents the executor thread from wedging for the
# full task timeout (up to 7200 s). Leaked reader threads are daemon threads
# and die with the container.
_DRAIN_BUDGET_SECONDS = 90


def _drain_bounded(
    process: subprocess.Popen,
    *threads: Optional[threading.Thread],
    grace: int = 5,
    pgid: Optional[int] = None,
    execution_tag: Optional[str] = None,
) -> DrainOutcome:
    """Run drain_reader_threads with a hard _DRAIN_BUDGET_SECONDS time cap.

    Prevents a TextIOWrapper lock deadlock in safe_close_pipes (Issue #728)
    from wedging the executor thread for the full task timeout.
    Uses the same asyncio.run() pattern required by drain_reader_threads'
    async-to-sync callers (established in #657); adds a daemon-thread wrapper
    so the budget is enforced at the threading level, not the asyncio level.

    Issue #817: ``execution_tag`` is threaded through to the underlying
    ``drain_reader_threads`` so the env-tag sweep runs after every drain.

    Issue #1025 (salvaged from #980): returns the drain outcome instead of
    ``None`` so the headless orchestrator can detect every case that leaves a
    reader thread still mutating the shared run context, and snapshot its
    finalize-read fields before reading them. Only ``"completed"`` guarantees
    no reader survives:

    - ``"completed"``       — the drain finished within budget AND every
      reader thread passed in has exited.
    - ``"budget_exceeded"`` — the daemon thread did not finish within
      ``_DRAIN_BUDGET_SECONDS`` (the #728 safe_close_pipes deadlock); the
      reader thread is leaked.
    - ``"errored"``         — the drain raised. Previously swallowed with
      ``except Exception: pass``, which masked the failure as a clean
      ``"completed"``; now captured and logged (honours the project-wide
      "never swallow exceptions silently" rule).
    - ``"leaked"``          — the drain returned normally within budget but a
      reader thread is still alive. ``drain_reader_threads`` force-closes the
      pipes and *continues* when a grandchild-held reader won't EOF (its own
      ``outcome=leaked`` METRIC), returning normally — so a within-budget,
      non-raising drain does NOT by itself prove the readers are dead. This
      outcome closes that gap (the #586 leaked-reader case).
    """
    done = threading.Event()
    # Single write-once cell, set only by the daemon thread and read only after
    # the ``done`` barrier — no second Event / ordering invariant to maintain.
    outcome: DrainOutcome = "completed"

    def _target() -> None:
        nonlocal outcome
        try:
            asyncio.run(_drain_reader_threads(
                process, *threads, grace=grace, pgid=pgid,
                execution_tag=execution_tag,
            ))
        except Exception:
            # #1025: capture + log instead of swallowing. A drain that raises
            # used to be indistinguishable from a clean completion, hiding a
            # leaked reader thread from the finalize path.
            outcome = "errored"
            logger.exception(
                "[Subprocess] Drain raised inside the daemon thread "
                "(pid=%s) — treating as errored; reader thread(s) may be "
                "leaked. Issue #1025.",
                process.pid,
            )
        finally:
            done.set()

    threading.Thread(target=_target, daemon=True).start()
    if not done.wait(timeout=_DRAIN_BUDGET_SECONDS):
        logger.warning(
            "[Subprocess] Drain budget (%ds) exceeded — safe_close_pipes may have "
            "deadlocked with reader thread's TextIOWrapper lock; reader threads are "
            "leaked daemon threads (pid=%s). Issue #728.",
            _DRAIN_BUDGET_SECONDS, process.pid,
        )
        # #1502: #728 unblocked the executor thread but LEFT THE PROCESS GROUP
        # ALIVE. The leaked reader is blocked reading a pipe a grandchild still
        # holds open — it spins/pegs a core until the container is restarted.
        # SIGKILL the whole group so that pipe EOFs, letting the daemon reader
        # return from readline() and exit, freeing the CPU. Best-effort: never
        # let cleanup raise into the caller, and only meaningful when the pgid
        # was captured at spawn (after wait() reaped the parent, a pid lookup
        # can't reach grandchildren — see terminate_process_group docstring).
        try:
            _terminate_process_group(process, 0, pgid=pgid)
            logger.warning(
                "[Subprocess] #1502: SIGKILLed the process group (pgid=%s, pid=%s) "
                "after the budget-exceeded drain so the leaked reader can EOF and "
                "free the core.",
                pgid, process.pid,
            )
        except Exception as e:  # pragma: no cover — cleanup must never crash the caller
            logger.warning(
                "[Subprocess] #1502: group kill after budget-exceed failed "
                "(pgid=%s, pid=%s): %s", pgid, process.pid, e,
            )
        return "budget_exceeded"
    # done is set → _target finished, so ``outcome`` is fully settled.
    if outcome == "errored":
        return "errored"
    # The drain returned within budget without raising, but drain_reader_threads
    # force-closes and continues on the #586 leaked-reader case. If any reader
    # we were asked to drain is still alive, it can keep mutating ctx — surface
    # that so the caller snapshots instead of trusting the live buffers (#1025).
    if any(t is not None and t.is_alive() for t in threads):
        logger.warning(
            "[Subprocess] Drain returned within budget but %d reader thread(s) "
            "are still alive (pid=%s) — reporting 'leaked' so finalize snapshots "
            "the run context. Issue #1025.",
            sum(1 for t in threads if t is not None and t.is_alive()),
            process.pid,
        )
        return "leaked"
    return "completed"
