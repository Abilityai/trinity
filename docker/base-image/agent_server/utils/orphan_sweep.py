"""Cgroup-bound orphan sweep — single cleanup mechanism (#817).

Replaces the prior three-pass pile-up:

    1. ``terminate_process_group``   — caught pgid members
    2. ``_kill_orphan_pipe_writers`` — caught processes still holding
                                       claude's stdout pipe write end
                                       (Issue #618, npx MCP servers)
    3. ``kill_processes_by_env_tag`` — caught processes that inherited
                                       ``TRINITY_EXECUTION_ID`` (#827)

Each pass was bolted on after the previous one missed a class. Eugene's
2026-05-13 production capture proved a fourth class can still escape:
no shared pgid, no shared pipe FDs, no env tag (env scrubbed via
``env -i`` / ``sudo`` / re-exec into a clean shell). The only feature
such an orphan still shares with the agent it leaked from is **cgroup
membership** — Docker's cgroup is the container boundary, and any
process whose CPU is being attributed to the container is by definition
inside that cgroup.

This module reads ``/sys/fs/cgroup/cgroup.procs`` (cgroup v2 unified
hierarchy, the modern default on every supported Docker host) and
SIGKILLs every PID that is NOT on the allowlist computed by
:mod:`orphan_allowlist`. One mechanism, one decision per PID, no
escape routes.

The graceful-shutdown side of claude termination — SIGTERM the pgid,
wait a few seconds for it to flush stream-json output — still happens
in :mod:`subprocess_pgroup`. This module is invoked *after* that, to
clean up anything that survived.

Concurrency model: the sweep is best-effort and idempotent. Multiple
calls from different threads / executions are safe — the worst that
happens is duplicate ``os.kill(pid, SIGKILL)`` calls, which the kernel
silently no-ops once the process is gone (ESRCH).
"""
from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path
from typing import Iterable, Optional

# Same flat-vs-relative dance as subprocess_pgroup.py: when this module
# is loaded by the unit tests (sys.path-injected, no package context),
# the relative import fails. Fall back to flat so the test suite can
# import this file standalone.
try:
    from .orphan_allowlist import resolve_allowlist, _read_cmdline
except ImportError:  # pragma: no cover - exercised by unit tests
    from orphan_allowlist import resolve_allowlist, _read_cmdline  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


# Cgroup v2 unified hierarchy. Trinity's supported runtime is modern
# Linux + recent Docker (Desktop or production). cgroup v1 hosts will
# return ENOENT here; we log once and skip the sweep — accepting the
# leak is preferable to misidentifying PIDs in a hierarchy we don't
# understand. The agent-server health check still works; only the
# orphan defense degrades.
_CGROUP_PROCS_PATH = Path("/sys/fs/cgroup/cgroup.procs")


# Cap the per-sweep log volume. A truly broken container could have
# dozens of orphans; we log each one's identity up to this cap then a
# count-only summary. Matches the same pattern in the legacy
# pipe-writer sweep so operators see consistent log shape.
_LOG_DETAIL_CAP = 10


def read_cgroup_procs(path: Path = _CGROUP_PROCS_PATH) -> Optional[list[int]]:
    """Read every PID in the container's cgroup.

    Returns the list (in file order, which is kernel-iteration order —
    not stable, but we don't depend on stability) or ``None`` if the
    cgroup file is missing / unreadable. ``None`` lets callers skip
    the sweep cleanly on cgroup v1 hosts or read errors.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "[OrphanSweep] Cannot read %s: %s — skipping sweep",
            path, exc,
        )
        return None

    pids: list[int] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            # Kernel never emits non-integer lines; defensive only.
            continue
    return pids


def kill_cgroup_orphans(
    *,
    extra_pids: Iterable[int] = (),
    sweep_pid: Optional[int] = None,
    dry_run: bool = False,
) -> int:
    """Single-pass cleanup. SIGKILLs everything in the container cgroup
    that isn't on the allowlist.

    Args:
        extra_pids: PIDs to preserve along with their descendants.
            Typical caller: active claude PIDs/pgids from
            :class:`ProcessRegistry`, so in-flight executions and
            their tool subprocesses stay alive while other tasks'
            orphans get reaped.
        sweep_pid: pid attributed to this sweep (defaults to the
            caller's pid). The allowlist protects this pid and its
            parent chain.
        dry_run: if True, identify orphans and log them but do not
            send SIGKILL. Used by the canary mode and tests.

    Returns the number of PIDs the sweep would kill (or did kill).
    Returns 0 on cgroup-unavailable hosts; the sweep is a no-op there
    and the legacy pgid SIGTERM/SIGKILL in :mod:`subprocess_pgroup`
    remains the only cleanup, matching pre-#817 behavior for those
    environments.
    """
    sweep_pid = sweep_pid if sweep_pid is not None else os.getpid()

    cgroup_pids = read_cgroup_procs()
    if cgroup_pids is None:
        # cgroup v1 or read error — sweep is unavailable. Log was
        # already emitted by read_cgroup_procs.
        return 0

    allowlist = resolve_allowlist(sweep_pid, extra_pids=extra_pids)

    orphan_pids = [pid for pid in cgroup_pids if pid not in allowlist]
    if not orphan_pids:
        return 0

    # Log identity before killing — once SIGKILL lands, /proc/<pid> is
    # gone and we lose the ability to attribute the leak. Cap to
    # _LOG_DETAIL_CAP entries; tail with count-only summary.
    label = "would kill" if dry_run else "SIGKILL"
    for i, pid in enumerate(orphan_pids):
        if i >= _LOG_DETAIL_CAP:
            break
        cmd = _read_cmdline(pid) or "?"
        try:
            pgid_str = str(os.getpgid(pid))
        except OSError:
            pgid_str = "?"
        logger.info(
            "[OrphanSweep] %s cgroup orphan: pid=%s pgid=%s cmd=%s",
            label, pid, pgid_str, cmd,
        )
    if len(orphan_pids) > _LOG_DETAIL_CAP:
        logger.info(
            "[OrphanSweep] %s %d additional orphan PID(s) (detail capped at %d)",
            label, len(orphan_pids) - _LOG_DETAIL_CAP, _LOG_DETAIL_CAP,
        )

    if dry_run:
        return len(orphan_pids)

    killed = 0
    for pid in orphan_pids:
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            # Already gone — fine, counts as success.
            killed += 1
        except PermissionError:
            # We run as developer:1000 inside the container; every
            # other process in the cgroup runs as the same UID, so
            # EPERM should never happen. If it does, log and move on.
            logger.warning(
                "[OrphanSweep] EPERM signalling pid=%s — different UID?",
                pid,
            )
        except OSError as exc:
            logger.warning(
                "[OrphanSweep] os.kill(%s, SIGKILL) raised: %s", pid, exc,
            )

    return killed


def cgroup_available() -> bool:
    """Return True iff the cgroup v2 procs file is readable.

    Cheaper than running a full sweep when callers just want to know
    whether the new mechanism is active. Used by startup logging and
    by tests that should be skipped in environments without cgroup v2.
    """
    try:
        with open(_CGROUP_PROCS_PATH) as f:
            f.read(1)
        return True
    except OSError:
        return False


# Convenience for callers that want to wait briefly after SIGKILL for
# the kernel to reap. The cleanup paths already do this for the parent
# claude process via ``process.wait(timeout=...)``; here we just need
# a coarse "did the cgroup actually shrink" check used by tests.
def wait_for_cgroup_count(
    expected_max: int,
    *,
    timeout_seconds: float = 5.0,
    poll_interval: float = 0.2,
) -> int:
    """Poll the cgroup procs count until it drops to ``expected_max``
    or the timeout elapses. Returns the final observed count.

    Used in tests after :func:`kill_cgroup_orphans` to confirm the
    sweep actually reaped its targets, since SIGKILL delivery is
    asynchronous from the caller's POV.
    """
    deadline = time.monotonic() + timeout_seconds
    last = -1
    while time.monotonic() < deadline:
        pids = read_cgroup_procs() or []
        last = len(pids)
        if last <= expected_max:
            return last
        time.sleep(poll_interval)
    return last
