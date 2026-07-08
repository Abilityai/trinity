"""Process-group lifecycle helpers for Claude Code subprocess management.

Issue #407: Claude Code spawns hooks (bash-guardrail.py, file-guardrail.py,
output-scanner.py, …) as child subprocesses that inherit our stdout/stderr
pipes. If a hook — or any grandchild it forks — outlives claude itself, the
inherited pipe write-ends stay open and our readline() blocks forever, even
though ``claude`` is already a ``<defunct>`` zombie. Result: agent-server
wedges at ~83% CPU and stops serving HTTP.

Cleanup is a two-step pipeline:

  1. Graceful pgid SIGTERM/SIGKILL of the direct claude process so its
     stream-json output flushes cleanly. (#407)
  2. Cgroup orphan sweep — kill anything in the container cgroup that
     isn't on the allowlist. Replaces the prior three-pass sequence
     (pgid + pipe-writer #618/#728 + env-tag #827) with one inescapable
     mechanism (#817 follow-up). See :mod:`orphan_sweep`.

Important: the caller must capture the pgid **right after** ``Popen()``
(via ``capture_pgid``) and pass it to every helper that operates on the
group. Once ``process.wait()`` / ``process.poll()`` reaps the parent, the
pid is gone and ``os.getpgid(pid)`` raises — but the process group itself
lives on in the kernel as long as it has any member (the grandchildren we
need to kill).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import threading
import time
from typing import Optional

# The unit tests in tests/unit/test_subprocess_pgroup.py import this
# module flat (sys.path.insert(0, utils/) + import subprocess_pgroup),
# without loading the agent_server package. A package-relative import
# would fail there at collection time. Try the relative form first
# (production path inside agent_server) and fall back to the flat name
# (tests + any future standalone reuse).
try:
    from .orphan_sweep import kill_cgroup_orphans
except ImportError:  # pragma: no cover - exercised by unit tests
    from orphan_sweep import kill_cgroup_orphans  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


# Informational env tag set on every Claude Popen (#817). The cleanup
# path used to look for this tag in /proc/<pid>/environ as its third
# pass. That pass was subsumed by the cgroup-walk sweep
# (:mod:`orphan_sweep`) because a process can scrub its env (via
# ``env -i`` / ``sudo`` / re-exec) and escape detection — but it
# cannot leave its container cgroup. The tag is retained because it
# is still useful for log identity and for operators correlating
# /proc/<pid>/environ dumps to execution_ids during incident response.
EXECUTION_TAG_NAME = "TRINITY_EXECUTION_ID"


def capture_pgid(process: subprocess.Popen) -> Optional[int]:
    """Return the process group id for ``process``.

    Must be called before the process is reaped (``wait()`` / ``poll()``
    collecting the exit status) — afterwards the pid is gone and
    ``os.getpgid`` raises ``ProcessLookupError``.

    Returns ``None`` on error; helpers fall back to single-process
    signaling in that case.
    """
    try:
        return os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def _signal_group_or_process(
    process: subprocess.Popen,
    pgid: Optional[int],
    sig: int,
) -> None:
    """Send ``sig`` to the process group if ``pgid`` is known, otherwise
    fall back to signaling the single process. Silent on ESRCH / EPERM."""
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        process.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass


def terminate_process_group(
    process: subprocess.Popen,
    graceful_timeout: int = 5,
    *,
    pgid: Optional[int] = None,
    execution_tag: Optional[str] = None,
) -> None:
    """Terminate the subprocess AND its entire process group.

    Sends SIGTERM to the group, waits up to ``graceful_timeout`` seconds
    for the direct child to exit, then sends SIGKILL to the group.

    If ``pgid`` is not provided, it's looked up from ``process.pid`` —
    which only works while the process is still alive or is a zombie.
    After ``process.wait()`` has reaped it, callers MUST pass the pgid
    they captured at spawn time, otherwise the helper falls back to
    signaling the single (already-reaped) pid and grandchildren are
    left running.

    The ``execution_tag`` argument is accepted for backward
    compatibility but no longer affects behavior. The final orphan
    sweep (#817) is now the cgroup-walk in :mod:`orphan_sweep`, which
    runs from :func:`drain_reader_threads` regardless of tag. Callers
    that spawn Claude with ``TRINITY_EXECUTION_ID`` still set the env
    var for log identity, but cleanup no longer depends on it.

    Safe to call on already-exited processes and safe to call multiple
    times.
    """
    del execution_tag  # retained for API compat; see docstring

    if pgid is None:
        pgid = capture_pgid(process)

    # Always attempt a SIGTERM on the group — even if the direct child is
    # already reaped, grandchildren may still be alive in the group.
    _signal_group_or_process(process, pgid, signal.SIGTERM)

    if process.poll() is None:
        try:
            process.wait(timeout=graceful_timeout)
        except subprocess.TimeoutExpired:
            pass

    # SIGKILL the group unconditionally. If it's already empty, the
    # kernel returns ESRCH and we swallow it.
    _signal_group_or_process(process, pgid, signal.SIGKILL)

    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            logger.error(
                "[Subprocess] pid=%s did not exit after SIGKILL", process.pid
            )

    # The post-kill orphan sweep used to run here as an env-tag pass.
    # That mechanism is now the cgroup-walk in
    # :func:`drain_reader_threads`, which always runs in the executor's
    # try/finally and catches descendants regardless of how they
    # escaped (setsid, FD detachment, env stripping). Running the sweep
    # again here would be wasted work — the executor will call
    # drain_reader_threads next.


def safe_close_pipes(process: subprocess.Popen) -> None:
    """Close subprocess stdout/stderr without raising.

    Used to unblock reader threads that are stuck on readline() because a
    surviving grandchild still holds the write end of the pipe open.
    Closing our read end causes readline() to raise ValueError or return
    EOF, which lets the thread exit.
    """
    for pipe in (process.stdout, process.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


async def drain_reader_threads(
    process: subprocess.Popen,
    *threads: Optional[threading.Thread],
    grace: int = 5,
    post_kill_grace: int = 30,
    pgid: Optional[int] = None,
    execution_tag: Optional[str] = None,
) -> None:
    """Join subprocess reader threads with a bounded timeout, then sweep
    the cgroup for any orphans left behind.

    If a reader is still alive after ``grace`` seconds, a surviving
    process-group member is holding a pipe write end open. Kill the
    group (via ``pgid`` if provided, else looked up), then wait up to
    ``post_kill_grace`` seconds for natural drain before force-closing
    pipes as a last resort.

    Ordering matters: once all writers (claude + hook grandchildren)
    are dead the kernel will EOF the read end as soon as buffered
    bytes are consumed. If we close our read FD first the reader
    raises ``ValueError: I/O operation on closed file`` and the
    kernel buffer — including the final ``{"type":"result"}`` JSON
    line — is discarded silently. Waiting for natural drain preserves
    that data. (#531)

    Callers that have already reaped the parent via ``process.wait()``
    must pass ``pgid`` — after reaping the pid is gone and we'd
    otherwise lose the ability to signal grandchildren.

    Issue #817 follow-up: the function unconditionally calls
    :func:`kill_cgroup_orphans` in a try/finally at the end. This
    runs on every exit path (no-stuck, drained-naturally,
    force-closed) and catches descendants regardless of how they
    tried to escape — setsid, FD detachment, env stripping. Replaces
    the prior pipe-writer sweep (#618, #728, #808) and env-tag sweep
    (#827); both are subsumed by cgroup membership being the
    inescapable boundary.

    The ``execution_tag`` parameter is informational; the cgroup
    sweep does not depend on it (kept for API stability).

    This function is ``async`` so every ``t.join()`` runs off the
    event-loop thread via ``asyncio.to_thread``. ``asyncio.wait_for``
    enforces the deadline at the event-loop level, preventing the
    asyncio loop from blocking even when orphan subprocesses consume
    all available CPUs and starve ordinary OS-level thread timeouts.
    Sync callers (executor thread functions) must wrap this with
    ``asyncio.run(drain_reader_threads(…))``. (#657)
    """
    del execution_tag  # informational only — see docstring

    try:
        alive_threads = [t for t in threads if t is not None]

        # Initial grace-period joins — each runs in a worker thread so
        # the event loop enforces the deadline even under CPU pressure.
        for t in alive_threads:
            try:
                await asyncio.wait_for(asyncio.to_thread(t.join, grace), timeout=grace + 1)
            except asyncio.TimeoutError:
                pass

        stuck = [t for t in alive_threads if t.is_alive()]
        if not stuck:
            return

        drain_start = time.monotonic()
        logger.warning(
            "[Subprocess] Reader thread(s) still busy after process exit "
            "(pid=%s, stuck_count=%s) — killing process group, then waiting "
            "%ss for natural drain",
            process.pid, len(stuck), post_kill_grace,
        )
        terminate_process_group(process, graceful_timeout=1, pgid=pgid)

        # The cgroup sweep at the bottom of this function will SIGKILL
        # any pipe-holder that survived the pgid kill (npx MCP servers
        # in their own session, etc.), which closes their FDs and lets
        # the reader EOF naturally. The previous explicit
        # pipe-writer-by-fd-inode scan (#618/#728) is no longer
        # necessary — killing on cgroup membership is strictly more
        # general and avoids the D-state ``os.stat`` deadlock.

        # #586 metric: capture initial stuck count for the per-branch
        # [METRIC] drain_outcome emissions below. orphan_kill_count is
        # vestigial in the cgroup-sweep era (#817) — the explicit
        # pipe-writer killer is gone; kill_cgroup_orphans() runs in
        # finally and logs its own count. Field is held at 0 for
        # operator-tooling stability.
        stuck_initial = len(stuck)

        # Natural-drain joins — run off the event loop so the deadline
        # is enforced even if a CPU-heavy orphan starves the reader.
        elapsed = time.monotonic() - drain_start
        join_timeout = max(1.0, post_kill_grace - elapsed)
        for t in stuck:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(t.join, join_timeout),
                    timeout=join_timeout + 1,
                )
            except asyncio.TimeoutError:
                pass

        still_stuck = [t for t in stuck if t.is_alive()]
        if not still_stuck:
            logger.info(
                "[Subprocess] Reader thread(s) drained naturally after "
                "grandchild termination (pid=%s, elapsed=%.1fs)",
                process.pid, time.monotonic() - drain_start,
            )
            # #586 Site B — natural drain after kill phase.
            logger.info(
                "[METRIC] drain_outcome pid=%s pgid=%s orphan_kill_count=%s "
                "outcome=%s drain_elapsed_ms=%s stuck_initial=%s",
                process.pid, pgid, 0, "natural",
                int((time.monotonic() - drain_start) * 1000),
                stuck_initial,
            )
            return

        # Genuine wedge — readers did not return even after grandchildren
        # died and the kernel should have EOF'd. Force-close and accept
        # data loss.
        elapsed = time.monotonic() - drain_start
        logger.error(
            "[Subprocess] Reader thread(s) still stuck after %.1fs post-kill "
            "grace — force-closing pipes; some buffered data may be lost "
            "(pid=%s, stuck_count=%s)",
            elapsed, process.pid, len(still_stuck),
        )
        safe_close_pipes(process)

        for t in still_stuck:
            try:
                await asyncio.wait_for(asyncio.to_thread(t.join, 2), timeout=3)
            except asyncio.TimeoutError:
                pass

        leaked = [t for t in still_stuck if t.is_alive()]
        if leaked:
            logger.error(
                "[Subprocess] %s reader thread(s) leaked for pid=%s after "
                "force-close; continuing anyway",
                len(leaked), process.pid,
            )

        # #586 Site C — force-close path. Bug-class regression site:
        # operators alert on a non-zero rate of these emissions.
        # leaked_count appended only when daemon reader threads
        # survived the force-close pipe-close.
        if leaked:
            logger.info(
                "[METRIC] drain_outcome pid=%s pgid=%s orphan_kill_count=%s "
                "outcome=%s drain_elapsed_ms=%s stuck_initial=%s leaked_count=%s",
                process.pid, pgid, 0, "leaked",
                int((time.monotonic() - drain_start) * 1000),
                stuck_initial,
                len(leaked),
            )
        else:
            logger.info(
                "[METRIC] drain_outcome pid=%s pgid=%s orphan_kill_count=%s "
                "outcome=%s drain_elapsed_ms=%s stuck_initial=%s",
                process.pid, pgid, 0, "force_close",
                int((time.monotonic() - drain_start) * 1000),
                stuck_initial,
            )
    finally:
        # Issue #817 follow-up: cgroup-walk runs on every exit path.
        # Best-effort — never fail the drain on sweep exceptions.
        # Issue #912: forward all *other* in-flight executions' pids/pgids
        # as the allowlist so the sweep doesn't SIGKILL a legitimate
        # concurrent claude subprocess running another task in the same
        # cgroup. The pre-#912 bare call was the bug — it left only the
        # sweep's own pid + parents on the allowlist, so any concurrent
        # task's claude got killed whenever a sibling task drained.
        # The draining process itself has already exited at this point
        # so it doesn't need to be in the allowlist.
        try:
            extra_pids = _active_execution_pids_for_drain()
            killed = kill_cgroup_orphans(extra_pids=extra_pids)
            if killed:
                logger.info(
                    "[Subprocess] Cgroup sweep killed %d orphan(s) after drain "
                    "(preserved %d allowlisted pid(s): other in-flight "
                    "executions + transient subprocesses)",
                    killed, len(extra_pids),
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[Subprocess] cgroup sweep raised in drain_reader_threads — "
                "continuing"
            )


def _active_execution_pids_for_drain() -> list[int]:
    """Lazy registry read for the #912 drain-time allowlist.

    Lives in ``utils`` and lazy-imports ``services.process_registry`` to
    avoid pulling FastAPI into the import graph of this low-level helper.
    Returns an empty list on any failure path — the allowlist is best-
    effort; a registry hiccup at drain time must never crash the drain.
    """
    try:
        from ..services.process_registry import get_process_registry  # lazy
    except Exception:  # noqa: BLE001
        return []
    try:
        return get_process_registry().active_execution_pids()
    except Exception:  # noqa: BLE001
        logger.exception(
            "[Subprocess] active_execution_pids() raised during drain sweep — "
            "continuing with empty allowlist"
        )
        return []


def signal_process_tree(
    process: subprocess.Popen,
    sig: int,
    *,
    pgid: Optional[int] = None,
) -> None:
    """Send ``sig`` to the subprocess's process group if it is a group
    leader; otherwise fall back to signaling the single process.

    Used by ProcessRegistry.terminate() to propagate SIGINT/SIGKILL to
    the full tree instead of just the subprocess parent.
    """
    if pgid is None:
        pgid = capture_pgid(process)
    _signal_group_or_process(process, pgid, sig)
