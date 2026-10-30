"""A failing Gemini headless reader ends its child before it raises.

Part of #3408. The headless reader's ``except`` branch (the inner timeout and
any per-line error) re-raised with its child still alive. Since #3307 the
waiter's ``finally: unregister()`` keeps a live child, so the entry, its log
buffer and its subscribers stayed registered with nothing left to release them.

Drives the real ``GeminiRuntime.execute_headless`` with the real
``ProcessRegistry`` and disposable local Python children; never a model CLI or
a running agent. Since #4064 a Gemini child is spawned with
``start_new_session`` and leads its own group; before that its captured pgid
was this test session's own process group. Every test still runs under a guard
that refuses (raises and records, never forwards) a signal to that group,
because the conftest signal guard is a pass-through where
``/proc/self/cgroup`` is unreadable (macOS).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from agent_server.services import gemini_runtime
from agent_server.services import process_registry as registry_module
from agent_server.utils import subprocess_pgroup

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _owner_turn(monkeypatch):
    """These tests are about the child process, not about whose turn it is: every
    turn here is the owner's, so the ent#926 tier check (test_ent926_runtime_turn_scope.py)
    passes without a platform to ask."""
    from agent_server.services import turn_scope

    async def owner(agent, run):
        return "owner", True, None
    monkeypatch.setattr(turn_scope, "fetch_turn", owner)

_REAL_POPEN = subprocess.Popen

# Reads the prompt, waits 0.1 s, prints one line and stays alive. Run with a
# zero-second budget, so the reader's per-line check finds the budget spent on
# that first line. The delay does not order anything: if the reader starts
# late, the elapsed time is only the line's read, which on a microsecond wall
# clock still exceeds zero.
_LINE_AFTER_THE_BUDGET = (
    "import sys, time; sys.stdin.read(); time.sleep(0.1); "
    "print('{\"type\": \"init\", \"session_id\": \"late\"}', flush=True); "
    "time.sleep(60)"
)
# Valid JSON that is not an object: `_process_stream_line` calls `.get` on it.
_NON_OBJECT_LINE = (
    "import sys, time; sys.stdin.read(); print('[1]', flush=True); time.sleep(60)"
)
# Empty data: reads the prompt, prints nothing, exits 0.
_PRINTS_NOTHING = "import sys; sys.stdin.read()"


class _GroupSignalRefused(RuntimeError):
    """Not an OSError, which production code may swallow; it is also recorded."""


class _Child(_REAL_POPEN):
    """A real disposable child.

    With ``hold_kill`` a ``kill()`` returns at once and its SIGKILL is
    delivered only after the code under test has entered ``wait()``. Code that
    kills without waiting therefore leaves the child alive every time, rather
    than depending on how fast the kernel happens to reap it.

    With ``exit_unobserved`` a bounded ``wait()`` expires at once, as it would
    for a child SIGKILL cannot end (uninterruptible sleep); a held kill is then
    never delivered.
    """

    def __init__(self, *args, hold_kill=False, exit_unobserved=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.hold_kill = hold_kill
        self.exit_unobserved = exit_unobserved
        self.kill_calls = 0
        self.wait_entered = threading.Event()
        self._abandon_held_kills = threading.Event()
        self._deliverers = []

    def kill(self):
        self.kill_calls += 1
        if not self.hold_kill:
            super().kill()
            return
        deliverer = threading.Thread(target=self._kill_once_waited_on, daemon=True)
        self._deliverers.append(deliverer)
        deliverer.start()

    def _kill_once_waited_on(self):
        while not self.wait_entered.wait(timeout=0.01):
            if self._abandon_held_kills.is_set():
                return
        super().kill()

    def wait(self, timeout=None):
        if self.exit_unobserved and timeout is not None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.wait_entered.set()
        return super().wait(timeout)

    def reap(self):
        """Test cleanup: kill now, bypassing any held kill."""
        self._abandon_held_kills.set()
        for deliverer in self._deliverers:
            deliverer.join(timeout=5)
        super().kill()
        super().wait(timeout=5)
        for pipe in (self.stdin, self.stdout, self.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()


@pytest.fixture
def refused_signals(monkeypatch):
    """Refuse and record any signal aimed at this session's own process group."""
    own_group = os.getpgrp()
    forbidden_pids = {0, -1, os.getpid(), own_group, -own_group}
    forbidden_groups = {0, own_group}
    real_kill, real_killpg = os.kill, os.killpg
    refused = []

    def kill(pid, sig):
        if pid in forbidden_pids:
            refused.append(f"os.kill({pid}, {sig})")
            raise _GroupSignalRefused(refused[-1])
        return real_kill(pid, sig)

    def killpg(pgid, sig):
        if pgid in forbidden_groups:
            refused.append(f"os.killpg({pgid}, {sig})")
            raise _GroupSignalRefused(refused[-1])
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", killpg)
    return refused


@pytest.fixture
def registry(monkeypatch):
    value = registry_module.ProcessRegistry()
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: value)
    # `_sweep_allowlist_pids` looks the registry up lazily from this module.
    monkeypatch.setattr(registry_module, "get_process_registry", lambda: value)
    # A unit test owns only its children, never the surrounding cgroup.
    monkeypatch.setattr(registry_module, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setattr(subprocess_pgroup, "kill_cgroup_orphans", lambda **kw: 0)
    return value


class _Harness:
    def __init__(self, monkeypatch, registry):
        self.monkeypatch = monkeypatch
        self.registry = registry
        self.runtime = gemini_runtime.GeminiRuntime()
        monkeypatch.setattr(self.runtime, "is_available", lambda: True)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-gemini-subproc")
        monkeypatch.setattr(gemini_runtime, "_executor", self.pool)
        # The error path's sweep, stubbed: record the allowlist it was given.
        self.sweep_allowlists = []
        monkeypatch.setattr(gemini_runtime, "kill_cgroup_orphans", self._record_sweep)
        self.children = []
        self.child_pgids = []
        self.subscribers = []

    def _record_sweep(self, *, extra_pids=()):
        self.sweep_allowlists.append(list(extra_pids))
        return 0

    async def run(self, execution_id, script, *, hold_kill=False, exit_unobserved=False, **kwargs):
        def popen(_cmd, **popen_kwargs):
            child = _Child(
                [sys.executable, "-c", script],
                hold_kill=hold_kill, exit_unobserved=exit_unobserved, **popen_kwargs,
            )
            self.children.append(child)
            self.child_pgids.append(os.getpgid(child.pid))
            real_close = child.stdin.close

            def close_stdin():
                real_close()
                # The prompt is delivered and the entry registered: watch its stream.
                self.subscribers.append(self.registry.subscribe_logs(execution_id))

            self.monkeypatch.setattr(child.stdin, "close", close_stdin)
            return child

        self.monkeypatch.setattr(subprocess, "Popen", popen)
        return await asyncio.wait_for(
            self.runtime.execute_headless(prompt="hello", execution_id=execution_id, **kwargs),
            timeout=10,
        )

    def cleanup(self):
        for child in self.children:
            child.reap()
        self.pool.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def gemini(monkeypatch, registry, refused_signals):
    harness = _Harness(monkeypatch, registry)
    try:
        yield harness
    finally:
        harness.cleanup()


def _assert_child_was_gemini_shaped(gemini):
    [child] = gemini.children
    assert gemini.child_pgids == [child.pid], (
        "control: the child leads its own group (#4064), never this session's"
    )


def _assert_released(gemini, execution_id):
    registry = gemini.registry
    assert not registry.is_execution_running(execution_id), "the entry outlived its child"
    assert registry.get_buffered_logs(execution_id) is None
    assert execution_id in registry.list_recently_completed_ids()
    stream = gemini.subscribers[0]
    received = []
    while not stream.empty():
        received.append(stream.get_nowait())
    assert received == [{"type": "stream_end"}], "the subscriber was never told the stream ended"


@pytest.mark.asyncio
async def test_inner_timeout_reaps_the_child_before_raising(gemini, refused_signals):
    with pytest.raises(HTTPException) as error:
        await gemini.run("3408-timeout", _LINE_AFTER_THE_BUDGET, hold_kill=True, timeout_seconds=0)

    assert error.value.status_code == 504, "control: the inner per-line timeout fired"
    child = gemini.children[0]
    _assert_child_was_gemini_shaped(gemini)
    # The SIGKILL is held until wait() is entered, so a dead child here also
    # means the reader waited for it.
    assert child.poll() is not None, "the reader raised with its child still alive"
    _assert_released(gemini, "3408-timeout")
    assert gemini.sweep_allowlists and child.pid not in gemini.sweep_allowlists[0], (
        "the error-path sweep ran while the child was still allowlisted"
    )
    assert refused_signals == []


@pytest.mark.asyncio
async def test_a_non_object_json_line_reaps_the_child_before_raising(gemini, refused_signals):
    with pytest.raises(HTTPException) as error:
        await gemini.run("3408-non-object", _NON_OBJECT_LINE)

    assert error.value.status_code == 500
    assert "'list' object has no attribute 'get'" in error.value.detail, (
        "control: a per-line error, not the timeout, ended the reader"
    )
    child = gemini.children[0]
    _assert_child_was_gemini_shaped(gemini)
    assert child.poll() is not None, "the reader raised with its child still alive"
    _assert_released(gemini, "3408-non-object")
    assert gemini.sweep_allowlists and child.pid not in gemini.sweep_allowlists[0], (
        "the error-path sweep ran while the child was still allowlisted"
    )
    assert refused_signals == []


@pytest.mark.asyncio
async def test_an_exit_never_observed_keeps_the_entry_and_the_reader_error(gemini, refused_signals):
    with pytest.raises(HTTPException) as error:
        await gemini.run(
            "3408-unobserved", _LINE_AFTER_THE_BUDGET,
            hold_kill=True, exit_unobserved=True, timeout_seconds=0,
        )

    assert error.value.status_code == 504, "a failed kill must not replace the reader's own error"
    child = gemini.children[0]
    _assert_child_was_gemini_shaped(gemini)
    assert child.kill_calls >= 1, "control: the error path tried to end the child"
    assert child.poll() is None, "control: the child's exit was never observed"
    assert gemini.registry.is_execution_running("3408-unobserved"), (
        "a child whose exit was not observed must stay registered, so it can be stopped again"
    )
    assert gemini.subscribers[0].empty(), "a still-owned child must not end its stream"
    assert refused_signals == []


@pytest.mark.asyncio
async def test_empty_output_takes_the_normal_path_and_never_kills(gemini, refused_signals):
    response, _log, _metadata, _session = await gemini.run("3408-empty", _PRINTS_NOTHING)

    assert response == "(No response from model)"
    child = gemini.children[0]
    _assert_child_was_gemini_shaped(gemini)
    assert child.returncode == 0, "control: the child exited on its own"
    assert child.kill_calls == 0, "the error-path kill fired on a clean exit"
    _assert_released(gemini, "3408-empty")
    assert refused_signals == []
