"""Run a subprocess vouched to the orphan sweep (#1595).

Any subprocess the agent-server spawns is invisible to the cgroup orphan
sweep unless explicitly vouched for (#1501): the allowlist's hard-protect
walk goes from the sweeper UP to PID 1, never down to the agent-server's
other children, so a bare child is indistinguishable from a leaked orphan
and is SIGKILLed by whichever sweep tick it straddles (~30s cadence, plus
drain-time sweeps from finishing executions' reader threads). This module
is the one seam for spawning a protected child:

    Popen → ProcessRegistry.add_transient_pid → wait → finally remove

Timeout kills the process GROUP, not just the direct child: git maintenance
supervisors (``git repack``) fork workers (``git pack-objects``) that hold
the inherited stderr pipe — killing only the supervisor leaves
``communicate()`` blocked on the open pipe until the transient-pid TTL
lapses and the sweep SIGKILLs the worker mid-pack-write, which is exactly
the tmp_pack-litter mechanism #1595 exists to stop. ``start_new_session=True``
gives the child its own process group so ``killpg`` reaps supervisor and
workers together; the allowlist's descendant resolution is ppid-based and
unaffected by the new session.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# Registration windows longer than this are INFO-logged: a missed removal
# (helper bug, registry glitch) shields a reused PID for the whole window,
# and the registry docstring warns that the TTL is damage-bounding, not the
# removal mechanism. Operators tuning GIT_MAINTENANCE_TIMEOUT_SECONDS into
# hours should see the widened window in the logs.
_LONG_WINDOW_LOG_SECONDS = 1800

# #4001: `fetch`, `pull`, `commit`, `merge` and `rebase` each run `git
# maintenance run --auto` when a repo-local config re-enables it, and wait for
# it. A bounded platform child then dies at its timeout mid-repack and strands
# `objects/maintenance.lock`, which git never expires — auto-maintenance is off
# for that repo from then on, silently. Command-line `-c` outranks repo config.
# Every git child spawned here carries it; the raw `subprocess.run` git sites in
# `routers/git.py` prepend it themselves. The platform's own maintenance is an
# explicit `repack`/`prune` (#4000), which this does not touch.
GIT_NO_AUTO_MAINTENANCE = ("-c", "maintenance.auto=false")


def _get_registry():
    """Resolve the process registry lazily (avoids import cycles; fail-open).

    Returns None when unavailable — the subprocess still runs, merely
    unprotected, matching the sweeper's own fail-open posture. A sync that
    cannot register must not become a sync that cannot run.
    """
    try:
        from ..services.process_registry import get_process_registry
        return get_process_registry()
    except Exception:  # noqa: BLE001 — protection is best-effort by design
        return None


def _kill_process_group(proc: subprocess.Popen, *, grace_seconds: float = 5.0) -> None:
    """SIGTERM the child's process group, wait briefly, then SIGKILL it."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        # Child already reaped — nothing to signal.
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass  # group already gone


def run_registered(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout: float = 60,
    check: bool = False,
    ttl_headroom: float = 60,
    errors: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Drop-in replacement for ``subprocess.run(capture_output=True, text=True)``
    that registers the child with the orphan-sweep allowlist for its lifetime.

    The TTL is derived from ``timeout`` at CALL time (never a module-import
    env read — see #1595 plan: an import-time copy makes env monkeypatching
    silently inert and lets the TTL drift from the real timeout).

    Raises ``subprocess.TimeoutExpired`` after killing the process group, and
    ``subprocess.CalledProcessError`` when ``check=True`` and the child fails —
    the same contract callers already handle for ``subprocess.run``.

    ``errors`` is forwarded to ``Popen`` (default ``None`` = strict decode, as
    before). A caller reading raw paths (``git status -z``) passes a lenient
    handler so one non-UTF-8 filename cannot raise ``UnicodeDecodeError`` (#2957).
    """
    window = float(timeout) + float(ttl_headroom)
    if window > _LONG_WINDOW_LOG_SECONDS:
        logger.info(
            "[RegisteredRun] long protection window %.0fs for %s",
            window, argv[0] if argv else "?",
        )

    argv = list(argv)
    spawn = argv
    if argv and argv[0] == "git":
        # #4001. The child only: results and exceptions keep the caller's argv
        # (`_with_stash` reads the verb from `exc.cmd[1]`).
        spawn = [argv[0], *GIT_NO_AUTO_MAINTENANCE, *argv[1:]]
    proc = subprocess.Popen(
        spawn,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors=errors,
        start_new_session=True,
    )
    # NOTE: there is an inherent spawn→register race — a sweep that snapshots
    # cgroup.procs after the fork but resolves its allowlist before this call
    # can still kill the fresh child. Accepted in the #1501 brain-orb seam;
    # the window is microseconds against a 30s sweep cadence.
    registry = _get_registry()
    if registry is not None:
        try:
            registry.add_transient_pid(proc.pid, ttl_seconds=window)
        except Exception:  # noqa: BLE001
            registry = None
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            # Group is dead — this returns promptly and reaps the child. The
            # bound is a last-ditch guard: a descendant that escaped the group
            # (setsid) can keep the pipe open, and an unbounded communicate()
            # would wedge this worker thread — and any lock it holds — forever.
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = "", ""
            raise subprocess.TimeoutExpired(
                list(argv), timeout, output=stdout, stderr=stderr
            )
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, list(argv), output=stdout, stderr=stderr
            )
        return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)
    finally:
        if registry is not None:
            try:
                registry.remove_transient_pid(proc.pid)
            except Exception:  # noqa: BLE001
                pass
