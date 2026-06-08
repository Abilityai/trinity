"""Regression tests for Issue #407: agent-server spin after claude becomes defunct.

The production bug: Claude Code spawns hooks (bash-guardrail.py,
file-guardrail.py, output-scanner.py, …) as subprocesses that inherit the
parent's stdout/stderr pipes. When claude exits but a hook grandchild
outlives it and keeps the write end of a pipe open, our reader's
``readline()`` never sees EOF and the agent-server executor thread wedges
— visibly spinning at ~83% CPU with claude as a ``<defunct>`` zombie.

These tests verify the process-group-based fix in
``docker/base-image/agent_server/utils/subprocess_pgroup.py``:

- ``terminate_process_group`` reaps the full tree (parent + grandchildren)
  so pipe write-ends close and readers unwind.
- ``drain_reader_threads`` kills the group and force-closes pipes when
  readers are stuck after the direct child has exited.
- Helpers are safe to call on already-exited processes.

Module under test: docker/base-image/agent_server/utils/subprocess_pgroup.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

# Import the subprocess_pgroup module directly (it has no package-relative
# imports, so we can add its parent dir to sys.path and import it flat).
_project_root = Path(__file__).resolve().parents[2]
_agent_utils_path = str(_project_root / 'docker' / 'base-image' / 'agent_server' / 'utils')
if _agent_utils_path not in sys.path:
    sys.path.insert(0, _agent_utils_path)

import subprocess_pgroup  # noqa: E402
from subprocess_pgroup import (  # noqa: E402
    EXECUTION_TAG_NAME,
    capture_pgid,
    drain_reader_threads,
    safe_close_pipes,
    signal_process_tree,
    terminate_process_group,
)
# kill_processes_by_env_tag (#827), _kill_orphan_pipe_writers (#618/#728),
# and _set_idle_priority (#808) were removed in the #817 follow-up that
# replaced them all with the cgroup-walk in :mod:`orphan_sweep`. Tests for
# those functions are deleted below; cgroup-walk has its own dedicated test
# module (tests/unit/test_orphan_sweep.py).


# ---------------------------------------------------------------------------
# Harness: a parent process that forks a grandchild which keeps stderr open
# after the parent exits. Mirrors the production pattern where claude spawns
# hook children that outlive it.
# ---------------------------------------------------------------------------

# This script is spawned with text=True, line-buffered, stdout/stderr PIPE.
# On stdin "go\n", parent forks a grandchild that holds stderr open for a
# long time. Then parent exits with code 0. Result: parent is <defunct>
# until reaped, grandchild still writes to stderr periodically → the parent
# process.wait() succeeds but readline() on stderr keeps returning data
# (or just blocks) forever.
_HARNESS_SCRIPT = r"""
import os
import sys
import time

# Wait for "go" then fork grandchild that keeps stderr open, then exit parent.
sys.stdin.readline()

pid = os.fork()
if pid == 0:
    # Grandchild: keep stderr open and write heartbeats.
    # No stdin; stdout/stderr are inherited from the parent (the test
    # harness's Popen pipes).
    try:
        for _ in range(600):  # up to 60s of heartbeats at 0.1s cadence
            sys.stderr.write("hb\n")
            sys.stderr.flush()
            time.sleep(0.1)
    finally:
        sys.stderr.close()
    os._exit(0)

# Parent: do not wait on the grandchild; exit immediately.
sys.stdout.write("ready\n")
sys.stdout.flush()
sys.exit(0)
"""


def _spawn_harness() -> subprocess.Popen:
    """Spawn the harness with its own process group (mirrors production)."""
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _HARNESS_SCRIPT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )


def _signal_harness_go(proc: subprocess.Popen) -> None:
    """Tell the harness parent to fork a grandchild and exit."""
    assert proc.stdin is not None
    proc.stdin.write("go\n")
    proc.stdin.flush()
    proc.stdin.close()


def _wait_for_parent_exit(proc: subprocess.Popen, timeout: float = 5.0) -> int:
    """Wait for the HARNESS PARENT to exit (grandchild may still be alive)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            return rc
        time.sleep(0.05)
    raise AssertionError(f"Harness parent pid={proc.pid} did not exit in {timeout}s")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTerminateProcessGroup:
    """terminate_process_group() reaps the whole tree via the process group."""

    def test_kills_parent_and_grandchild(self):
        """The grandchild that holds stderr open must be killed too.

        Without killpg, the grandchild would keep running (and keep the
        inherited stderr FD open), causing readline() to block forever.
        """
        proc = _spawn_harness()
        # Capture the pgid EARLY via the helper, before anything reaps
        # the parent — mirrors production use (capture_pgid right after
        # Popen, pass through to terminate_process_group).
        pgid = capture_pgid(proc)
        assert pgid is not None and pgid > 0
        try:
            _signal_harness_go(proc)
            parent_rc = _wait_for_parent_exit(proc)
            assert parent_rc == 0

            # At this point: parent is reaped, grandchild is alive in
            # the (still-valid) process group. terminate_process_group
            # must reap the grandchild via the captured pgid.
            start = time.monotonic()
            terminate_process_group(proc, graceful_timeout=2, pgid=pgid)
            elapsed = time.monotonic() - start

            # Must not hang: SIGTERM → 2s wait → SIGKILL → reaped.
            assert elapsed < 5.0, f"terminate took {elapsed:.2f}s — too slow"

            # Parent must be reaped.
            assert proc.poll() is not None

            # No process in the old group should still exist.
            # Give the kernel a beat to clean up.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    os.killpg(pgid, 0)  # signal 0 = existence check
                except (ProcessLookupError, PermissionError, OSError):
                    break
                time.sleep(0.05)
            else:
                pytest.fail(f"process group {pgid} still exists after terminate_process_group")
        finally:
            # Best-effort cleanup if the test failed mid-way.
            try:
                terminate_process_group(proc, graceful_timeout=1)
            except Exception:
                pass

    def test_safe_on_already_exited(self):
        """Calling on a process that already exited is a no-op."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=5)
        # Must not raise.
        terminate_process_group(proc, graceful_timeout=1)

    def test_idempotent(self):
        """Calling multiple times is safe."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            terminate_process_group(proc, graceful_timeout=1)
            # Already terminated — second call must be a no-op.
            terminate_process_group(proc, graceful_timeout=1)
            assert proc.poll() is not None
        finally:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass



@pytest.mark.unit
class TestDrainReaderThreads:
    """drain_reader_threads() unwinds readers stuck on pipe held by grandchild."""

    def test_unwinds_reader_stuck_on_grandchild_pipe(self):
        """The production #407 scenario reproduced end-to-end.

        Harness parent exits after forking a grandchild that keeps stderr
        open. Without the fix, the stderr reader's readline() returns data
        forever (never sees EOF). drain_reader_threads() must notice the
        reader is still alive after grace, kill the grandchild via the
        process group, force-close the pipe, and let the reader thread
        exit.
        """
        proc = _spawn_harness()
        pgid = capture_pgid(proc)
        assert pgid is not None
        lines: list[str] = []
        start_event = threading.Event()

        def read_stderr():
            start_event.set()
            try:
                assert proc.stderr is not None
                for line in iter(proc.stderr.readline, ''):
                    if not line:
                        break
                    lines.append(line)
            except Exception:
                # Closing the pipe from the main thread raises ValueError
                # inside readline in some Python versions — acceptable.
                pass

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
        start_event.wait(timeout=2)

        try:
            _signal_harness_go(proc)
            parent_rc = _wait_for_parent_exit(proc)
            assert parent_rc == 0

            # Give the reader a moment to pick up heartbeat lines, so we
            # know the pipe is still producing data (grandchild is alive).
            time.sleep(0.3)
            assert stderr_thread.is_alive(), \
                "stderr reader should still be running — grandchild holds pipe open"

            # Now drain. Helper must kill the grandchild + close the pipe,
            # so the reader thread exits.
            start = time.monotonic()
            asyncio.run(drain_reader_threads(proc, stderr_thread, grace=2, pgid=pgid))
            elapsed = time.monotonic() - start

            assert elapsed < 6.0, f"drain took {elapsed:.2f}s — too slow"
            assert not stderr_thread.is_alive(), \
                "stderr reader thread still alive after drain — helper did not unwind it"
        finally:
            try:
                terminate_process_group(proc, graceful_timeout=1, pgid=pgid)
            except Exception:
                pass

    def test_fast_path_when_readers_already_exited(self):
        """If threads finished on their own, drain is fast and non-destructive."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('hi')"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            out_lines: list[str] = []

            def read_stdout():
                assert proc.stdout is not None
                for line in iter(proc.stdout.readline, ''):
                    if not line:
                        break
                    out_lines.append(line)

            t = threading.Thread(target=read_stdout, daemon=True)
            t.start()
            proc.wait(timeout=5)
            t.join(timeout=2)
            assert not t.is_alive(), "reader should have exited on its own"

            # Drain should be a cheap no-op.
            start = time.monotonic()
            asyncio.run(drain_reader_threads(proc, t, grace=2))
            assert time.monotonic() - start < 1.0
            assert out_lines == ["hi\n"]
        finally:
            try:
                terminate_process_group(proc, graceful_timeout=1)
            except Exception:
                pass

    def test_buffered_data_preserved_after_grandchild_kill(self):
        """Regression for #531: data in the kernel pipe buffer (including the
        final result line) must not be lost when a grandchild is killed.

        Old behavior: safe_close_pipes() was called immediately after
        terminate_process_group(), causing readline() to raise ValueError and
        discard the buffered tail. New behavior: we wait up to post_kill_grace
        seconds for the reader to drain naturally before force-closing.

        The subprocess writes a sentinel line ('RESULT_LINE') then spawns a
        grandchild that holds stdout open. After the parent exits, the reader
        is artificially slowed (simulating backlog processing). drain must
        still surface the sentinel via natural drain, not lose it via early
        close.
        """
        # Script: parent writes a result line then forks a grandchild that
        # keeps stdout open. Parent exits; grandchild sleeps a while.
        script = r"""
import os, sys, time
sys.stdout.write("RESULT_LINE\n")
sys.stdout.flush()
pid = os.fork()
if pid == 0:
    # Grandchild keeps stdout open
    time.sleep(5)
    os._exit(0)
# Parent exits immediately
sys.exit(0)
"""
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        pgid = capture_pgid(proc)
        captured: list[str] = []
        reader_ready = threading.Event()

        def slow_reader():
            reader_ready.set()
            assert proc.stdout is not None
            try:
                for line in iter(proc.stdout.readline, ''):
                    if not line:
                        break
                    captured.append(line.strip())
                    time.sleep(0.05)  # simulate slow processing
            except (ValueError, OSError):
                pass  # pipe closed by force — acceptable in the wedge path

        t = threading.Thread(target=slow_reader, daemon=True)
        t.start()
        reader_ready.wait(timeout=2)

        # Wait for parent to exit (grandchild still alive, holding stdout)
        proc.wait(timeout=5)
        # Reader is still alive because grandchild holds the pipe open.
        time.sleep(0.1)

        try:
            # grace=0 forces the stuck-reader path immediately; post_kill_grace
            # gives the natural-drain window.
            asyncio.run(drain_reader_threads(
                proc, t,
                grace=0,
                post_kill_grace=5,
                pgid=pgid,
            ))
            assert not t.is_alive(), "reader thread should have exited after drain"
            assert "RESULT_LINE" in captured, (
                f"sentinel lost — captured={captured!r}. "
                "drain_reader_threads closed pipe before reader drained buffer."
            )
        finally:
            try:
                terminate_process_group(proc, graceful_timeout=1, pgid=pgid)
            except Exception:
                pass

    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="_kill_orphan_pipe_writers uses /proc (Linux only)",
    )
    def test_setsid_escapee_drained_via_orphan_killer_preserves_result_line(self):
        """Regression for #586: Stop hook → ``git push`` → ``ssh`` calls
        ``setsid()``, escaping claude's process group and holding the stdout
        pipe write-end during network I/O.

        Pathology: terminate_process_group(claude_pgid) leaves the setsid'd
        grandchild alive (different session), so the reader's readline() never
        sees EOF. Without ``_kill_orphan_pipe_writers`` firing inside
        ``drain_reader_threads``, the natural-drain wait times out and the
        force-close fallback discards the kernel pipe buffer — losing the
        final ``{"type":"result"}`` JSON line and recording the execution as a
        502 "no result message" failure.

        This is a sibling of ``test_buffered_data_preserved_after_grandchild_kill``
        (#531) — same shape, except the grandchild calls ``os.setsid()`` so it
        escapes the pgid kill. Together they pin the full production path:
        natural-drain ordering, the 10s scan-timeout cap (#650), the async
        wrapper (#657), and the result-line preservation contract (#531).

        Non-redundant with ``TestKillOrphanPipeWriters.test_kills_orphan_in_different_session``:
        that test calls ``_kill_orphan_pipe_writers`` directly and skips the
        drain wrapper entirely; only this test exercises the full
        ``drain_reader_threads`` production path with the setsid escape +
        data-preservation assertion.
        """
        # Parent writes the result sentinel, forks a grandchild that calls
        # setsid() (new session — survives terminate_process_group(pgid)) and
        # holds stdout open, then exits immediately.
        script = r"""
import os, sys, time
sys.stdout.write("RESULT_LINE\n")
sys.stdout.flush()
pid = os.fork()
if pid == 0:
    os.setsid()              # the #586 escape — new session/pgid
    time.sleep(5)            # holds stdout write-end while parent exits
    os._exit(0)
sys.exit(0)
"""
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        pgid = capture_pgid(proc)
        assert pgid is not None
        captured: list[str] = []
        reader_ready = threading.Event()

        def reader():
            reader_ready.set()
            assert proc.stdout is not None
            try:
                for line in iter(proc.stdout.readline, ''):
                    if not line:
                        break
                    captured.append(line.strip())
            except (ValueError, OSError):
                pass  # pipe force-closed — acceptable on the fallback path

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        reader_ready.wait(timeout=2)

        # Parent exits; setsid'd grandchild stays alive holding the pipe.
        proc.wait(timeout=5)
        time.sleep(0.1)
        assert t.is_alive(), (
            "reader should still be blocked — setsid'd grandchild holds pipe open"
        )

        try:
            # grace=0 forces the stuck-reader path immediately; post_kill_grace
            # gives the natural-drain window after the orphan-killer fires.
            asyncio.run(drain_reader_threads(
                proc, t,
                grace=0,
                post_kill_grace=5,
                pgid=pgid,
            ))
            assert not t.is_alive(), (
                "reader thread should have exited after drain — "
                "_kill_orphan_pipe_writers must catch the setsid escapee"
            )
            assert "RESULT_LINE" in captured, (
                f"sentinel lost — captured={captured!r}. "
                "setsid escapee survived: drain hit the force-close fallback "
                "and discarded the pre-fork buffered result line."
            )
        finally:
            try:
                terminate_process_group(proc, graceful_timeout=1, pgid=pgid)
            except Exception:
                pass

    def test_emits_metric_on_natural_drain(self, monkeypatch, caplog):
        """[METRIC] drain_outcome must fire on the natural-drain branch.

        Operators query the log for ``outcome=natural`` to track how often
        the orphan-killer rescued an execution from the force-close path.
        Reader thread is constructed to exit briefly after the slow path
        engages, simulating EOF arriving after kill phase.
        """
        import logging
        import subprocess_pgroup as spg

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        pgid = capture_pgid(proc)
        assert pgid is not None

        # The reader appears stuck during the initial grace window, then
        # exits cleanly. drain_reader_threads sees it alive after grace
        # (slow path entry), kills the pgid, runs the orphan scan, and
        # finds the reader has finished during the natural-drain join.
        reader_exit_allowed = threading.Event()

        def reader():
            reader_exit_allowed.wait(timeout=10)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        # No-op orphan killer — keeps the test fast on Linux (no /proc walk)
        # and bypasses the macOS-incompatible code path so the test runs on
        # both platforms.
        # No-op the cgroup sweep — macOS lacks the /sys/fs/cgroup layout, and
        # we don't want real orphan-killing in a unit test. Replaces the
        # pre-#817 monkeypatch of _kill_orphan_pipe_writers.
        monkeypatch.setattr(spg, "kill_cgroup_orphans", lambda: 0)

        try:
            with caplog.at_level(logging.INFO, logger="subprocess_pgroup"):
                # Release the reader once the slow path is committed but
                # before the natural-drain join completes.
                def release_soon():
                    time.sleep(0.5)
                    reader_exit_allowed.set()

                threading.Thread(target=release_soon, daemon=True).start()

                asyncio.run(drain_reader_threads(
                    proc, t, grace=0, post_kill_grace=5, pgid=pgid,
                ))

            assert not t.is_alive(), "reader should have exited via natural drain"

            metric_lines = [
                r.getMessage() for r in caplog.records
                if r.getMessage().startswith("[METRIC] drain_outcome")
            ]
            assert metric_lines, "natural-drain path must emit drain_outcome metric"
            msg = metric_lines[0]
            assert "outcome=natural" in msg, msg
            assert "stuck_initial=1" in msg, msg
            assert "drain_elapsed_ms=" in msg, msg
            assert "orphan_kill_count=0" in msg, msg
        finally:
            try:
                terminate_process_group(proc, graceful_timeout=1, pgid=pgid)
            except Exception:
                pass

    def test_emits_metric_on_force_close_path(self, monkeypatch, caplog):
        """[METRIC] drain_outcome must fire on the force-close branch.

        This is the bug-class regression site (Issue #586). Operators alert
        on a non-zero rate of ``outcome=force_close`` / ``outcome=leaked``
        emissions; without an assertion here, a future refactor of
        drain_reader_threads would silently break the metric and the next
        regression of this class would arrive unmetricked.
        """
        import logging
        import subprocess_pgroup as spg

        # Stub orphan killer to do nothing — reader stays blocked through
        # the natural-drain window so force-close fires.
        # No-op the cgroup sweep — macOS lacks the /sys/fs/cgroup layout, and
        # we don't want real orphan-killing in a unit test. Replaces the
        # pre-#817 monkeypatch of _kill_orphan_pipe_writers.
        monkeypatch.setattr(spg, "kill_cgroup_orphans", lambda: 0)

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        pgid = capture_pgid(proc)
        assert pgid is not None

        # Reader is parked in a long sleep — neither natural drain nor
        # safe_close_pipes unblocks it, so it leaks after force-close.
        # Either ``outcome=force_close`` (reader exited on close) or
        # ``outcome=leaked`` (reader survived) is an acceptable signal.
        def slow_processor():
            time.sleep(120)

        t = threading.Thread(target=slow_processor, daemon=True)
        t.start()

        try:
            with caplog.at_level(logging.INFO, logger="subprocess_pgroup"):
                asyncio.run(drain_reader_threads(
                    proc, t, grace=0, post_kill_grace=2, pgid=pgid,
                ))

            metric_lines = [
                r.getMessage() for r in caplog.records
                if r.getMessage().startswith("[METRIC] drain_outcome")
            ]
            assert metric_lines, "force-close path must emit drain_outcome metric"
            msg = metric_lines[0]
            assert "outcome=force_close" in msg or "outcome=leaked" in msg, msg
            assert "stuck_initial=1" in msg, msg
            assert "drain_elapsed_ms=" in msg, msg
            assert "orphan_kill_count=0" in msg, msg
            if "outcome=leaked" in msg:
                assert "leaked_count=" in msg, msg
        finally:
            try:
                terminate_process_group(proc, graceful_timeout=1, pgid=pgid)
            except Exception:
                pass


@pytest.mark.unit
class TestSafeClosePipes:
    """safe_close_pipes() never raises."""

    def test_closes_open_pipes(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            safe_close_pipes(proc)
            assert proc.stdout is None or proc.stdout.closed
            assert proc.stderr is None or proc.stderr.closed
        finally:
            terminate_process_group(proc, graceful_timeout=1)

    def test_safe_when_pipes_already_closed(self):
        """Idempotent — calling on already-closed pipes does not raise."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
        # Must not raise.
        safe_close_pipes(proc)

    def test_safe_with_none_pipes(self):
        class Dummy:
            stdout = None
            stderr = None

        safe_close_pipes(Dummy())


@pytest.mark.unit
class TestSignalProcessTree:
    """signal_process_tree() signals via pgid with fallback."""

    def test_signals_process_group(self):
        """SIGTERM via signal_process_tree should kill a grandchild too."""
        proc = _spawn_harness()
        pgid = capture_pgid(proc)
        assert pgid is not None
        try:
            _signal_harness_go(proc)
            _wait_for_parent_exit(proc)

            import signal as _sig
            signal_process_tree(proc, _sig.SIGKILL, pgid=pgid)

            # Parent pid may still be a zombie until reaped, but the
            # process group should be empty.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    os.killpg(pgid, 0)
                except (ProcessLookupError, PermissionError, OSError):
                    break
                time.sleep(0.05)
            else:
                pytest.fail(f"process group {pgid} still exists after SIGKILL via signal_process_tree")
            # Reap the zombie to keep the test host tidy.
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        finally:
            try:
                terminate_process_group(proc, graceful_timeout=1)
            except Exception:
                pass

    def test_safe_when_pid_gone(self):
        """If the process is already gone, signaling is a no-op, not an error."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=5)
        import signal as _sig
        # Must not raise.
        signal_process_tree(proc, _sig.SIGTERM)
