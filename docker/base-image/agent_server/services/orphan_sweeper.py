"""Periodic cgroup orphan sweep — Eugene's fix #2 for Issue #817.

The per-task cleanup in :mod:`subprocess_pgroup` runs the cgroup sweep
from ``drain_reader_threads``'s try/finally. That covers the case where
an execution ends normally (success / timeout / failure) and the
executor gets a chance to clean up.

It does **not** cover Eugene's observed production failure mode:

  1. A long-running execution leaks an orphan.
  2. The Trinity backend's circuit breaker opens because agent-server's
     event loop is starved by the orphan; the backend then terminates
     the execution externally (via Docker / SIGKILL / API call).
  3. ``drain_reader_threads`` never runs — the kill arrives from
     outside agent-server's normal path.
  4. Every subsequent task is fast-failed at the CB layer before
     reaching the agent, so no future per-task cleanup runs either.
  5. The orphan survives for hours and only dies when the operator
     deletes the Redis CB key and restarts the container.

This service breaks that cycle. It runs the same
:func:`kill_cgroup_orphans` on a fixed interval, independent of any
task lifecycle. Once the orphan dies, agent-server's event loop
unsticks, health probes succeed, the backend CB recovers, and normal
operation resumes — all without operator intervention.

The sweep preserves every PID in :class:`ProcessRegistry` (so
in-flight executions are not killed) plus the user-configured cmdline
patterns (so persistent daemons like cornelius-m's moltbook-http-mcp
survive). When nothing is orphaned the sweep is a fast no-op; the
allowed cost of running it every interval is ~one ``/proc`` walk plus
one ``/sys/fs/cgroup/cgroup.procs`` read, both bounded by the number
of processes in the container (typically <30).

Disabled when cgroup v2 unified hierarchy is not available — the
sweep is a no-op there and we don't want to spam logs.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

from ..utils.orphan_sweep import cgroup_available, kill_cgroup_orphans

logger = logging.getLogger(__name__)


# Default sweep cadence. 30s strikes a balance: short enough that an
# orphan burning a CPU core does not run for long, long enough that
# the /proc walk overhead is negligible. Overridable via
# TRINITY_ORPHAN_SWEEP_INTERVAL env var for tests.
_DEFAULT_INTERVAL_SECONDS = 30

# Initial grace period before the first sweep fires.
#
# The base image runs `~/.trinity/setup.sh` at container startup —
# templates use this to install custom packages (apt-get, npm install
# -g, etc.). Those processes are direct children of PID 1 but are NOT
# part of the platform essentials — they're user-template owned. The
# grace period lets setup.sh and its package-manager subprocesses
# complete before the sweep starts evaluating them as orphans.
#
# 90s is comfortable: typical setup.sh completes in <60s even on slow
# networks. Overridable for tests via TRINITY_ORPHAN_SWEEP_STARTUP_DELAY.
_DEFAULT_STARTUP_DELAY_SECONDS = 90


def _interval_seconds() -> float:
    raw = os.environ.get("TRINITY_ORPHAN_SWEEP_INTERVAL")
    if not raw:
        return float(_DEFAULT_INTERVAL_SECONDS)
    try:
        v = float(raw)
        return max(1.0, v)  # never poll faster than 1s
    except ValueError:
        return float(_DEFAULT_INTERVAL_SECONDS)


def _startup_delay_seconds() -> float:
    raw = os.environ.get("TRINITY_ORPHAN_SWEEP_STARTUP_DELAY")
    if raw is None:
        return float(_DEFAULT_STARTUP_DELAY_SECONDS)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(_DEFAULT_STARTUP_DELAY_SECONDS)


def _active_execution_pids() -> Iterable[int]:
    """Return PIDs (and pgids) of every currently-registered execution.

    These get added to the cgroup sweep's allowlist so the periodic
    sweep cannot kill an in-flight task. Imported lazily to avoid the
    circular import :mod:`process_registry` ⇄ :mod:`subprocess_pgroup`.

    Returns an empty iterable on any error — preserve nothing extra,
    fall back to hard-protected + cmdline-pattern allowlist. Safer
    than risking a false kill of the agent-server itself on a registry
    glitch.
    """
    # #912: delegated to `ProcessRegistry.active_execution_pids()` so the
    # periodic sweep and the drain-time sweep in `subprocess_pgroup` share
    # one canonical source. The registry method returns a list (with
    # duplicate-tolerant pid/pgid entries — the allowlist resolver dedupes
    # via descendant walk); convert to a set here for the sweeper's
    # interface contract.
    try:
        from .process_registry import get_process_registry  # lazy
    except Exception:  # noqa: BLE001
        return ()

    try:
        return set(get_process_registry().active_execution_pids())
    except Exception:  # noqa: BLE001
        logger.exception("[OrphanSweeper] failed to enumerate active executions")
        return ()


async def run_orphan_sweep_loop(
    interval_seconds: float | None = None,
    startup_delay_seconds: float | None = None,
) -> None:
    """Run :func:`kill_cgroup_orphans` on a fixed interval until cancelled.

    Sleeps ``startup_delay_seconds`` before the first sweep so that
    container-startup processes (the user-template ``setup.sh`` and
    its apt-get / npm subprocesses) can finish without being killed.
    After the initial delay the sweep runs every
    ``interval_seconds`` indefinitely.

    Errors are logged and swallowed; the loop must not die because a
    single sweep failed. Cancellation via ``task.cancel()`` exits
    cleanly.
    """
    interval = interval_seconds if interval_seconds is not None else _interval_seconds()
    startup_delay = (
        startup_delay_seconds
        if startup_delay_seconds is not None
        else _startup_delay_seconds()
    )
    logger.info(
        "[OrphanSweeper] starting periodic sweep "
        "(startup_delay=%.0fs, interval=%.0fs)",
        startup_delay, interval,
    )

    try:
        # Initial grace — let setup scripts finish before the first sweep.
        if startup_delay > 0:
            await asyncio.sleep(startup_delay)

        while True:
            try:
                extra_pids = list(_active_execution_pids())
                killed = kill_cgroup_orphans(extra_pids=extra_pids)
                if killed:
                    logger.info(
                        "[OrphanSweeper] periodic sweep killed %d orphan(s) "
                        "(preserved %d allowlisted pid(s): in-flight "
                        "executions + transient subprocesses)",
                        killed, len(extra_pids),
                    )
            except Exception:  # noqa: BLE001
                logger.exception("[OrphanSweeper] sweep iteration raised")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("[OrphanSweeper] periodic sweep cancelled — exiting cleanly")
        raise


def schedule_orphan_sweeper(app) -> None:
    """Register the periodic sweep as a FastAPI startup/shutdown task.

    Mirrors the registration shape of :func:`auto_sync.schedule_auto_sync_if_enabled`
    so wiring in :mod:`main` is one line. Disabled at startup if
    cgroup v2 isn't available — the sweep would be a no-op and the
    INFO log every interval is just noise.
    """
    if os.environ.get("TRINITY_ORPHAN_SWEEP_DISABLED") == "1":
        logger.info("[OrphanSweeper] disabled by TRINITY_ORPHAN_SWEEP_DISABLED=1")
        return
    if not cgroup_available():
        logger.warning(
            "[OrphanSweeper] cgroup v2 unified hierarchy not available — "
            "periodic sweep disabled. Per-task cleanup still runs."
        )
        return

    task_ref: list[asyncio.Task] = []

    @app.on_event("startup")
    async def _start_orphan_sweeper() -> None:  # pragma: no cover - wiring
        task = asyncio.create_task(run_orphan_sweep_loop())
        task_ref.append(task)

    @app.on_event("shutdown")
    async def _stop_orphan_sweeper() -> None:  # pragma: no cover - wiring
        for task in task_ref:
            task.cancel()
