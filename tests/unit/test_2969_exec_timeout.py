"""#2969 — `execute_command_in_container(..., timeout=N)` actually bounds the exec.

Before #2969 the primitive accepted `timeout` and forwarded it nowhere: docker-py's
`exec_run` has no timeout and the call runs on `docker_utils._docker_executor`
(4 threads shared by every Docker operation in the backend). One hung exec
wedged `SessionCleanupService._sweep_agent` fleet-wide and a hanging
agent-authored `~/.trinity/pre-check` pinned a pool thread per scheduled fire.

The primitive now enforces the bound TWICE, because the two halves free
different resources:

* an in-container ``timeout -k K N`` prefix ends the process (and the pool
  thread blocked reading its output) — SIGTERM at N, SIGKILL K seconds later,
  so a process that ignores TERM or was ``kill -STOP``ped still ends;
* ``asyncio.wait_for`` frees the CALLER even if the thread stays blocked
  (Docker daemon wedged, output pipe held open).

Behaviour, not source text: a fake container whose ``exec_run`` really blocks a
real executor thread, and — where GNU ``timeout`` exists on the host, as on CI —
the exact argv the primitive builds, run for real against a STOPped process.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
import threading
import time

import pytest

from services import docker_service
from services import docker_utils


class _FakeExecResult:
    def __init__(self, exit_code, output):
        self.exit_code = exit_code
        self.output = output


class _FakeContainer:
    """Records what `exec_run` was handed; optionally blocks or sleeps first."""

    def __init__(self, exit_code=0, output=b"ok\n", block: threading.Event = None,
                 sleep_s: float = 0.0):
        self.calls = []
        self._exit_code = exit_code
        self._output = output
        self._block = block
        self._sleep_s = sleep_s

    def exec_run(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self._block is not None:
            # A real blocked pool thread — what a wedged `docker exec` looks like.
            self._block.wait(30)
        if self._sleep_s:
            time.sleep(self._sleep_s)
        return _FakeExecResult(self._exit_code, self._output)


@pytest.fixture
def fake_docker(monkeypatch):
    holder = {}

    def _install(container):
        holder["container"] = container

        async def _get(name):
            return container

        monkeypatch.setattr(docker_service, "docker_client", object())
        monkeypatch.setattr(docker_utils, "container_get", _get)
        return container

    return _install


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The command is wrapped in an in-container `timeout` that escalates to KILL
# ---------------------------------------------------------------------------


class TestCommandIsWrapped:
    def test_string_command_runs_under_timeout_with_kill_escalation(self, fake_docker):
        c = fake_docker(_FakeContainer())
        _run(docker_service.execute_command_in_container(
            "agent-a", "sh -c 'echo hi; sleep 1'", timeout=7))

        argv, _ = c.calls[0]
        k = str(docker_service.EXEC_KILL_AFTER_S)
        assert argv[:4] == ["timeout", "-k", k, "7"]
        # The original command survives byte-for-byte as argv (docker-py would
        # have shlex-split the string the same way).
        assert argv[4:] == shlex.split("sh -c 'echo hi; sleep 1'")

    def test_list_command_is_wrapped_without_resplitting(self, fake_docker):
        c = fake_docker(_FakeContainer())
        _run(docker_service.execute_command_in_container(
            "agent-a", ["bash", "-c", "a b; c"], timeout=3))

        argv, _ = c.calls[0]
        assert argv[4:] == ["bash", "-c", "a b; c"]
        assert argv[3] == "3"

    def test_fractional_timeout_is_kept(self, fake_docker):
        c = fake_docker(_FakeContainer())
        _run(docker_service.execute_command_in_container("agent-a", "true", timeout=0.5))
        assert c.calls[0][0][3] == "0.5"

    def test_user_and_environment_still_reach_the_exec(self, fake_docker):
        c = fake_docker(_FakeContainer())
        _run(docker_service.execute_command_in_container(
            "agent-a", "true", timeout=5, user="root", environment={"X": "1"}))
        _, kwargs = c.calls[0]
        assert kwargs["user"] == "root"
        assert kwargs["environment"] == {"X": "1"}

    @pytest.mark.parametrize("bad", [0, -1, None])
    def test_a_non_positive_timeout_is_refused(self, fake_docker, bad):
        """GNU `timeout 0` means NO timeout — accepting it would silently
        reintroduce the unbounded exec this issue removes."""
        c = fake_docker(_FakeContainer())
        with pytest.raises(ValueError):
            _run(docker_service.execute_command_in_container("agent-a", "true", timeout=bad))
        assert c.calls == []


# ---------------------------------------------------------------------------
# Return contract: unchanged shape, timeout made explicit
# ---------------------------------------------------------------------------


class TestReturnContract:
    def test_a_normal_exec_returns_exit_code_and_output(self, fake_docker):
        fake_docker(_FakeContainer(exit_code=3, output=b"boom\n"))
        r = _run(docker_service.execute_command_in_container("agent-a", "x", timeout=5))
        assert r["exit_code"] == 3
        assert r["output"] == "boom\n"
        assert r["timed_out"] is False

    def test_an_in_container_timeout_is_reported_as_timed_out(self, fake_docker):
        """`timeout` exits 124 once N elapsed — the primitive says so."""
        fake_docker(_FakeContainer(exit_code=124, output=b"partial", sleep_s=0.3))
        r = _run(docker_service.execute_command_in_container("agent-a", "x", timeout=0.2))
        assert r["exit_code"] == 124
        assert r["output"] == "partial"
        assert r["timed_out"] is True

    def test_a_command_that_itself_exits_124_quickly_is_not_a_timeout(self, fake_docker):
        fake_docker(_FakeContainer(exit_code=124, output=b""))
        r = _run(docker_service.execute_command_in_container("agent-a", "x", timeout=30))
        assert r["exit_code"] == 124
        assert r["timed_out"] is False

    def test_kill_escalation_exit_is_reported_as_timed_out(self, fake_docker):
        """After `-k`, `timeout` exits 128+9."""
        fake_docker(_FakeContainer(exit_code=137, output=b"", sleep_s=0.3))
        r = _run(docker_service.execute_command_in_container("agent-a", "x", timeout=0.2))
        assert r["timed_out"] is True


# ---------------------------------------------------------------------------
# The caller is freed even when the exec thread stays blocked
# ---------------------------------------------------------------------------


class TestCallerIsFreed:
    def test_a_wedged_exec_returns_after_the_bound(self, fake_docker, monkeypatch):
        monkeypatch.setattr(docker_service, "EXEC_KILL_AFTER_S", 0.1)
        monkeypatch.setattr(docker_service, "_EXEC_OUTER_GRACE_S", 0.1)
        release = threading.Event()
        fake_docker(_FakeContainer(block=release))
        try:
            t0 = time.monotonic()
            r = _run(docker_service.execute_command_in_container(
                "agent-a", "sh -c 'sleep 1000'", timeout=0.2))
            elapsed = time.monotonic() - t0
        finally:
            release.set()  # let the pool thread go so the suite does not leak it

        assert elapsed < 3, f"caller blocked {elapsed:.1f}s on a wedged exec"
        assert r["timed_out"] is True
        assert r["exit_code"] == docker_service.EXEC_TIMEOUT_EXIT_CODE
        assert isinstance(r["output"], str)

    def test_a_hanging_pre_check_hook_does_not_hold_the_scheduler_path(
        self, fake_docker, monkeypatch
    ):
        """End to end through the caller the issue names: a hook that never
        returns now yields an ordinary non-zero result (fail-open upstream)."""
        from services import pre_check_service

        monkeypatch.setattr(docker_service, "EXEC_KILL_AFTER_S", 0.1)
        monkeypatch.setattr(docker_service, "_EXEC_OUTER_GRACE_S", 0.1)
        monkeypatch.setattr(pre_check_service, "EXEC_TIMEOUT_S", 0.2)
        monkeypatch.setattr(pre_check_service, "get_agent_container", lambda n: object())

        release = threading.Event()

        class _HookHangs(_FakeContainer):
            def exec_run(self, cmd, **kwargs):
                self.calls.append((cmd, kwargs))
                if pre_check_service.HOOK_PATH in cmd and "test" not in cmd:
                    release.wait(30)
                return _FakeExecResult(0, b"")

        fake_docker(_HookHangs())
        try:
            t0 = time.monotonic()
            out = _run(pre_check_service.run_pre_check("a"))
            elapsed = time.monotonic() - t0
        finally:
            release.set()

        assert elapsed < 3
        assert out["hook_present"] is True
        assert out["exit_code"] != 0


# ---------------------------------------------------------------------------
# The built argv against real GNU coreutils (CI is Linux; skipped elsewhere)
# ---------------------------------------------------------------------------


def _gnu_timeout_available() -> bool:
    path = shutil.which("timeout")
    if not path:
        return False
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    return "GNU coreutils" in (out.stdout or "")


@pytest.mark.skipif(not _gnu_timeout_available(), reason="needs GNU coreutils `timeout`")
class TestRealTimeoutBinary:
    def _run_argv(self, monkeypatch, command, timeout):
        monkeypatch.setattr(docker_service, "EXEC_KILL_AFTER_S", 1)
        argv = docker_service._bounded_exec_argv(command, timeout)
        t0 = time.monotonic()
        proc = subprocess.run(argv, capture_output=True, timeout=20)
        return proc.returncode, time.monotonic() - t0

    def test_a_hung_process_is_ended(self, monkeypatch):
        rc, elapsed = self._run_argv(monkeypatch, "sh -c 'sleep 1000'", 0.5)
        assert rc == 124
        assert elapsed < 5

    def test_a_self_stopped_process_is_ended(self, monkeypatch):
        """The issue's verified vector: the agent uid can `kill -STOP` its exec."""
        rc, elapsed = self._run_argv(monkeypatch, "sh -c 'kill -STOP $$; sleep 1000'", 0.5)
        assert rc in (124, 137, -9)
        assert elapsed < 5

    def test_a_process_ignoring_term_is_killed(self, monkeypatch):
        rc, elapsed = self._run_argv(
            monkeypatch, "sh -c 'trap \"\" TERM; sleep 1000'", 0.5)
        # `timeout` re-raises KILL on itself: a parent sees signal 9, which
        # Docker (like a shell) reports as exit 137.
        assert rc in (137, -9)
        assert elapsed < 5

    def test_exit_status_passes_through_when_no_timeout(self, monkeypatch):
        rc, _ = self._run_argv(monkeypatch, "sh -c 'exit 7'", 5)
        assert rc == 7
