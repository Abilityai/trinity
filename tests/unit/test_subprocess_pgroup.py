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
    capture_pgid,
    drain_reader_threads,
    safe_close_pipes,
    signal_process_tree,
    terminate_process_group,
)


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
            drain_reader_threads(proc, stderr_thread, grace=2, pgid=pgid)
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
            drain_reader_threads(proc, t, grace=2)
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
            drain_reader_threads(
                proc, t,
                grace=0,
                post_kill_grace=5,
                pgid=pgid,
            )
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


@pytest.mark.unit
class TestDrainOrphanKillerTimeout:
    """Issue #649: drain_reader_threads must complete in bounded time even when
    _kill_orphan_pipe_writers is slow (e.g. blocked on a D-state /proc entry)."""

    def test_drain_bounded_when_orphan_killer_blocks(self, monkeypatch):
        """Monkeypatch the orphan killer to simulate a blocked /proc scan.

        Without the fix the drain would block for the orphan killer's full
        sleep duration (100s).  With it, the 10-second daemon-thread cap
        ensures drain_reader_threads returns in < 20 seconds regardless.

        The reader thread simulates "stuck in processing" (a time.sleep) rather
        than blocked on I/O, so safe_close_pipes completes without contending
        on the BufferedReader lock — the scenario that exercises the orphan-
        killer timeout specifically.
        """
        import subprocess_pgroup as spg

        orphan_killer_started = threading.Event()

        def _slow_orphan_killer(fd: int, our_pgid) -> int:
            orphan_killer_started.set()
            time.sleep(100)  # simulate /proc scan blocked on D-state process
            return 0

        monkeypatch.setattr(spg, "_kill_orphan_pipe_writers", _slow_orphan_killer)

        # Process with a stdout pipe (needed so safe_close_pipes has a FD to close
        # and the orphan-killer branch runs).
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        pgid = capture_pgid(proc)
        assert pgid is not None

        # Reader that simulates being stuck in long CPU/processing work (not I/O
        # blocked on the pipe), so safe_close_pipes does not contend on the
        # BufferedReader lock and can complete without waiting.
        def slow_processor():
            time.sleep(120)

        t = threading.Thread(target=slow_processor, daemon=True)
        t.start()

        try:
            start = time.monotonic()
            # grace=0 → immediately enters the stuck-reader path.
            drain_reader_threads(proc, t, grace=0, post_kill_grace=5, pgid=pgid)
            elapsed = time.monotonic() - start

            # Orphan killer must have been called.
            assert orphan_killer_started.is_set(), "_kill_orphan_pipe_writers was not called"

            # Drain must complete within: 10s orphan timeout + 1s join + 2s join + 3s slack.
            assert elapsed < 20.0, (
                f"drain took {elapsed:.2f}s — orphan killer 10s cap not enforced"
            )
            # Thread is leaked (still sleeping) — that is the expected wedge outcome.
            # The important assertion is that drain_reader_threads itself returned.
        finally:
            try:
                terminate_process_group(proc, graceful_timeout=1, pgid=pgid)
            except Exception:
                pass


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "linux", reason="_kill_orphan_pipe_writers uses /proc (Linux only)")
class TestKillOrphanPipeWriters:
    """_kill_orphan_pipe_writers() handles Issue #618: npx MCP servers in a
    different process group hold the stdout pipe open after terminate_process_group.

    npm calls setsid() when it spawns node, placing the MCP server in a new
    session/pgid that is outside claude's pgid.  terminate_process_group kills
    claude's pgid but the npm → node chain survives, keeping the pipe write FD
    open so our reader thread blocks indefinitely.

    These tests require the Linux /proc filesystem and are skipped on other platforms.
    The production fix runs inside Debian-based Docker containers where /proc is always
    available.
    """

    # Script that simulates "claude" spawning an npx MCP server:
    # parent forks a grandchild that calls setsid() (new session/pgid) and
    # holds stdout open, then parent exits immediately.
    _NPX_SIM_SCRIPT = r"""
import os, sys, time
pid = os.fork()
if pid == 0:
    os.setsid()           # new session — survives terminate_process_group
    time.sleep(30)        # keeps stdout write FD open
    os._exit(0)
# parent ("claude") exits without waiting
sys.exit(0)
"""

    def test_kills_orphan_in_different_session(self):
        """Grandchild in a new session is detected and killed via /proc scan."""
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", self._NPX_SIM_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        claude_pgid = capture_pgid(proc)
        assert claude_pgid is not None

        try:
            proc.wait(timeout=5)  # parent exits; grandchild in new session still alive

            # Start a reader — it will block because grandchild holds stdout open.
            reader_unblocked = threading.Event()

            def read_stdout():
                assert proc.stdout is not None
                try:
                    for line in iter(proc.stdout.readline, ''):
                        if not line:
                            break
                except (ValueError, OSError):
                    pass
                reader_unblocked.set()

            t = threading.Thread(target=read_stdout, daemon=True)
            t.start()
            time.sleep(0.15)
            assert t.is_alive(), "Reader should be blocked — grandchild holds pipe open"

            # Kill claude's pgid — grandchild in new session survives.
            terminate_process_group(proc, graceful_timeout=1, pgid=claude_pgid)
            time.sleep(0.1)
            assert t.is_alive(), "Reader still blocked — grandchild in new session survived"

            # The orphan killer should catch the grandchild.
            assert proc.stdout is not None
            killed = subprocess_pgroup._kill_orphan_pipe_writers(
                proc.stdout.fileno(), claude_pgid
            )
            assert killed >= 1, f"Expected ≥1 orphan killed, got {killed}"

            # Reader gets EOF, exits promptly.
            reader_unblocked.wait(timeout=3)
            assert not t.is_alive(), "Reader still alive after orphan killed"
        finally:
            try:
                terminate_process_group(proc, graceful_timeout=1, pgid=claude_pgid)
            except Exception:
                pass

    def test_does_not_kill_own_reader(self):
        """Our own process holds the read end — must NOT be killed."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        pgid = capture_pgid(proc)
        try:
            assert proc.stdout is not None
            killed = subprocess_pgroup._kill_orphan_pipe_writers(
                proc.stdout.fileno(), pgid
            )
            # The child holds the write end — it IS in pgid so should be skipped.
            # Our own process holds the read end (O_RDONLY) — must not be killed.
            assert killed == 0, f"Should have killed 0 processes, killed {killed}"
            assert os.getpid() != 0  # sanity: we are still alive
        finally:
            terminate_process_group(proc, graceful_timeout=1, pgid=pgid)

    def test_drain_reader_threads_kills_npx_orphan_end_to_end(self):
        """Integration: drain_reader_threads resolves the Issue #618 scenario.

        A process in a new session (simulating an npx MCP server) holds the
        stdout write end open after terminate_process_group kills claude's pgid.
        drain_reader_threads must kill the orphan and let the reader exit without
        reaching the force-close path, preserving buffered data.
        """
        # Parent writes a result line, forks a grandchild with setsid(), exits.
        script = r"""
import os, sys, time
sys.stdout.write("RESULT_LINE\n")
sys.stdout.flush()
pid = os.fork()
if pid == 0:
    os.setsid()       # new session — survives terminate_process_group
    time.sleep(30)    # keeps stdout write FD open
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
        claude_pgid = capture_pgid(proc)
        assert claude_pgid is not None

        captured: list[str] = []
        reader_ready = threading.Event()

        def read_stdout():
            reader_ready.set()
            assert proc.stdout is not None
            try:
                for line in iter(proc.stdout.readline, ''):
                    if not line:
                        break
                    captured.append(line.strip())
            except (ValueError, OSError):
                pass

        t = threading.Thread(target=read_stdout, daemon=True)
        t.start()
        reader_ready.wait(timeout=2)

        try:
            proc.wait(timeout=5)  # parent exits; grandchild (new session) still alive
            time.sleep(0.1)
            assert t.is_alive(), "Reader should be blocked before drain"

            # grace=0 → immediately triggers the stuck-reader path.
            start = time.monotonic()
            drain_reader_threads(proc, t, grace=0, post_kill_grace=5, pgid=claude_pgid)
            elapsed = time.monotonic() - start

            assert elapsed < 7.0, f"drain took {elapsed:.2f}s — too slow"
            assert not t.is_alive(), "Reader still alive after drain"
            assert "RESULT_LINE" in captured, (
                f"Buffered result lost — captured={captured!r}. "
                "Orphan killer may have triggered force-close before drain."
            )
        finally:
            try:
                terminate_process_group(proc, graceful_timeout=1, pgid=claude_pgid)
            except Exception:
                pass
