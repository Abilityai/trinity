"""Process-group lifecycle helpers for Claude Code subprocess management.

Issue #407: Claude Code spawns hooks (bash-guardrail.py, file-guardrail.py,
output-scanner.py, …) as child subprocesses that inherit our stdout/stderr
pipes. If a hook — or any grandchild it forks — outlives claude itself, the
inherited pipe write-ends stay open and our readline() blocks forever, even
though ``claude`` is already a ``<defunct>`` zombie. Result: agent-server
wedges at ~83% CPU and stops serving HTTP.

Fix: launch claude with ``start_new_session=True`` so it becomes its own
process-group leader, and kill the entire group on shutdown. That reaps
hook grandchildren too, which closes the inherited pipe FDs and lets our
reader threads unwind naturally.

Important: the caller must capture the pgid **right after** ``Popen()``
(via ``capture_pgid``) and pass it to every helper that operates on the
group. Once ``process.wait()`` / ``process.poll()`` reaps the parent, the
pid is gone and ``os.getpgid(pid)`` raises — but the process group itself
lives on in the kernel as long as it has any member (the grandchildren we
need to kill).

This module is kept free of package-relative imports so it can be
unit-tested without loading the rest of ``agent_server``.
"""
from __future__ import annotations

import logging
import os
import signal
import stat as _stat
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


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

    Safe to call on already-exited processes and safe to call multiple
    times.
    """
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


def _kill_orphan_pipe_writers(pipe_read_fd: int, our_pgid: Optional[int]) -> int:
    """Kill any process outside *our_pgid* that holds the same pipe's write end open.

    Issue #618: npx-based MCP servers (spawned by npm which calls setsid())
    start in their own process group, survive ``terminate_process_group``, and
    keep the pipe write FD open indefinitely so the kernel never delivers EOF
    to our reader thread.

    We identify the target pipe by its inode (shared by both ends of the pipe)
    and confirm write access via ``/proc/{pid}/fdinfo/{fd}`` flags.  All errors
    are silently swallowed — this is best-effort cleanup inside the drain path.

    Only meaningful on Linux (requires ``/proc`` filesystem).

    Returns the number of processes that were sent SIGKILL.
    """
    try:
        target_ino = os.fstat(pipe_read_fd).st_ino
    except OSError:
        return 0

    killed = 0
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return 0

    for pid_str in proc_entries:
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)

        # Skip processes already inside the killed process group.
        if our_pgid is not None:
            try:
                if os.getpgid(pid) == our_pgid:
                    continue
            except OSError:
                continue

        fd_dir = f"/proc/{pid}/fd"
        try:
            fd_names = os.listdir(fd_dir)
        except OSError:
            continue

        for fd_name in fd_names:
            try:
                st = os.stat(f"{fd_dir}/{fd_name}")
            except OSError:
                continue
            if not _stat.S_ISFIFO(st.st_mode) or st.st_ino != target_ino:
                continue
            # Matching pipe inode — confirm it's the write end via fdinfo flags.
            try:
                with open(f"/proc/{pid}/fdinfo/{fd_name}") as finfo:
                    fdinfo = finfo.read()
                if "flags:" not in fdinfo:
                    continue
                flags = int(fdinfo.split("flags:")[1].split()[0], 8)
                if flags & os.O_ACCMODE:  # O_WRONLY=1 or O_RDWR=2, not O_RDONLY=0
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                    break  # no need to inspect other FDs of this pid
            except OSError:
                continue

    return killed


def drain_reader_threads(
    process: subprocess.Popen,
    *threads: Optional[threading.Thread],
    grace: int = 5,
    post_kill_grace: int = 30,
    pgid: Optional[int] = None,
) -> None:
    """Join subprocess reader threads with a bounded timeout.

    If any thread is still alive after ``grace`` seconds, a surviving
    process-group member is holding a pipe write end open. Kill the group
    (via ``pgid`` if provided, else looked up), then wait up to
    ``post_kill_grace`` seconds for the reader to drain naturally before
    force-closing pipes as a last resort.

    Ordering matters: once all writers (claude + hook grandchildren) are
    dead the kernel will EOF the read end as soon as the buffered bytes are
    consumed. If we close our read FD first the reader raises
    ``ValueError: I/O operation on closed file`` and the kernel buffer —
    including the final ``{"type":"result"}`` JSON line — is discarded
    silently. Waiting for natural drain preserves that data. (#531)

    Callers that have already reaped the parent via ``process.wait()``
    must pass ``pgid`` — after reaping, the pid is gone and we'd
    otherwise lose the ability to signal grandchildren.
    """
    alive_threads = [t for t in threads if t is not None]
    for t in alive_threads:
        t.join(timeout=grace)

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

    # Issue #618: kill any processes outside our pgid that still hold the
    # stdout pipe's write end open.  The primary culprit is npm → node MCP
    # server chains: npm calls setsid() when it spawns node, placing it in a
    # new process group that survives terminate_process_group(claude_pgid).
    # The orphan keeps the write FD open so the kernel never delivers EOF to
    # our reader.  Killing it releases all its FDs (stdout AND stderr write
    # ends), unblocking both reader threads simultaneously.
    #
    # Issue #649: run in a daemon thread with a hard 10-second timeout.
    # Iterating /proc can block indefinitely when a process is in D state
    # (uninterruptible sleep), causing the drain to stall for tens of minutes
    # instead of the expected ~30 seconds.
    if process.stdout is not None and not process.stdout.closed:
        try:
            stdout_fd = process.stdout.fileno()
        except Exception:
            stdout_fd = None
        if stdout_fd is not None:
            _orphan_result: list[int] = [0]

            def _run_orphan_killer() -> None:
                try:
                    _orphan_result[0] = _kill_orphan_pipe_writers(stdout_fd, pgid)
                except Exception:
                    pass  # best-effort; never fail the drain path

            _orphan_thread = threading.Thread(target=_run_orphan_killer, daemon=True)
            _orphan_thread.start()
            _orphan_thread.join(timeout=10)

            if _orphan_thread.is_alive():
                logger.warning(
                    "[Subprocess] _kill_orphan_pipe_writers still running after "
                    "10s (pid=%s) — /proc scan may be blocked on a D-state process",
                    process.pid,
                )
            elif _orphan_result[0]:
                logger.info(
                    "[Subprocess] Killed %s orphan stdout pipe-writer(s) "
                    "outside pgid=%s (pid=%s) — likely npx MCP server(s)",
                    _orphan_result[0], pgid, process.pid,
                )

    # All writers are now dead (or we timed out trying to kill them).
    # Use wall-clock accounting: subtract time already spent so the caller's
    # post_kill_grace budget is measured from when the warning fired, not from
    # after the orphan scan.  If the orphan scan itself consumed more than
    # post_kill_grace, clamp to 1 second so we still attempt a natural drain.
    elapsed = time.monotonic() - drain_start
    join_timeout = max(1.0, post_kill_grace - elapsed)
    for t in stuck:
        t.join(timeout=join_timeout)

    still_stuck = [t for t in stuck if t.is_alive()]
    if not still_stuck:
        logger.info(
            "[Subprocess] Reader thread(s) drained naturally after "
            "grandchild termination (pid=%s, elapsed=%.1fs)",
            process.pid, time.monotonic() - drain_start,
        )
        return

    # Genuine wedge — reader did not return even after grandchildren died
    # and the kernel should have EOF'd. Force-close and accept data loss.
    elapsed = time.monotonic() - drain_start
    logger.error(
        "[Subprocess] Reader thread(s) still stuck after %.1fs post-kill "
        "grace — force-closing pipes; some buffered data may be lost "
        "(pid=%s, stuck_count=%s)",
        elapsed, process.pid, len(still_stuck),
    )
    safe_close_pipes(process)
    for t in still_stuck:
        t.join(timeout=2)

    leaked = [t for t in still_stuck if t.is_alive()]
    if leaked:
        logger.error(
            "[Subprocess] %s reader thread(s) leaked for pid=%s after "
            "force-close; continuing anyway",
            len(leaked), process.pid,
        )


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
