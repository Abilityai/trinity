"""An abandoned Gemini waiter hands release to its shielded reader.

Regression for #3408 (the Gemini half of AC 1, with AC 3 pinned).

#3307 made ``ProcessRegistry.unregister()`` keep an entry whose child is still
alive, and gave Claude chat and headless a reader worker that releases the
entry once the child has exited. Gemini chat and headless spawn the child and
write its prompt before they submit the reader to the shared single worker,
and only the waiter's ``finally`` unregistered. A cancelled waiter therefore
left the entry, its log subscribers and its sweep allowlisting behind for good,
and a reader still queued behind that worker was cancelled with the waiter, so
nothing drained or reaped the child.

Drives the real ``_execute_chat_once`` and ``execute_headless`` with a real
``ProcessRegistry`` and ordinary local Python children, never the Gemini CLI, a
model or a running agent. ``kill_cgroup_orphans`` is stubbed in
``process_registry``, ``subprocess_pgroup`` and ``gemini_runtime``.

A Gemini child is not a process-group leader, so its captured pgid is the
spawning process's own group: here, pytest's. ``tests/signal_guard.py`` is a
pass-through where ``/proc/self/cgroup`` is unreadable (macOS), so every case
here also refuses, never forwards, any signal aimed at this session's group,
and asserts that nothing was refused.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_server.services import gemini_runtime
from agent_server.services import process_registry as registry_module
from agent_server.utils import subprocess_pgroup

pytestmark = pytest.mark.unit

EXECUTION_ID = "abandoned-gemini"

_ASSISTANT_LINE = json.dumps({"type": "message", "role": "assistant", "content": "done"})
# Reads its prompt, prints one assistant message, marks the path in argv[1] as
# printed, then stays alive until the test kills it.
ANSWER_THEN_HOLD = (
    f"import pathlib,sys,time; sys.stdin.read(); print({_ASSISTANT_LINE!r}, flush=True); "
    "pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
)
# Reads its prompt, prints one assistant message, exits 0.
ANSWER = f"import sys; sys.stdin.read(); print({_ASSISTANT_LINE!r}, flush=True)"
# Reads its prompt, prints nothing, exits 0 once the path in argv[1] exists.
SILENT_UNTIL_RELEASED = (
    "import os,sys,time; sys.stdin.read(); deadline = time.time() + 60\n"
    "while not os.path.exists(sys.argv[1]) and time.time() < deadline: time.sleep(0.01)"
)


class _OwnGroupSignal(RuntimeError):
    """A signal aimed at this test session's own process group (never sent).

    Not an ``OSError``: production code swallows those."""


@pytest.fixture
def own_group_signals(monkeypatch):
    """Refuse (raise and record, never forward) every signal that would reach
    this session's own process group, which is what a group kill of a Gemini
    child's captured pgid does. A forwarding spy would already have signalled
    pytest's group by the time it could assert."""
    refused = []
    real_kill, real_killpg = os.kill, os.killpg
    own_pid, own_group = os.getpid(), os.getpgrp()

    def kill(pid, sig):
        if pid in (0, -1, own_pid, -own_group):
            refused.append(f"os.kill({pid}, {sig})")
            raise _OwnGroupSignal(refused[-1])
        return real_kill(pid, sig)

    def killpg(pgid, sig):
        if pgid in (0, own_group):
            refused.append(f"os.killpg({pgid}, {sig})")
            raise _OwnGroupSignal(refused[-1])
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", killpg)
    yield refused
    assert refused == [], f"refused own-group signals: {refused}"


@pytest.fixture
def registry(monkeypatch):
    value = registry_module.ProcessRegistry()
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: value)
    # A unit test owns only its children, never the surrounding cgroup.
    monkeypatch.setattr(registry_module, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setattr(subprocess_pgroup, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setattr(gemini_runtime, "kill_cgroup_orphans", lambda **kw: 0)
    return value


@pytest.fixture
def runtime(monkeypatch):
    rt = gemini_runtime.GeminiRuntime()
    monkeypatch.setattr(rt, "is_available", lambda: True)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    return rt


class _Turn:
    """One real Gemini turn whose CLI is a disposable Python child."""

    def __init__(self, monkeypatch, registry, argv, *, queued=False):
        self.loop = asyncio.get_running_loop()
        self.monkeypatch = monkeypatch
        self.children = []
        self.lines = []
        self.line_processed = asyncio.Event()
        self.prompt_delivered = asyncio.Event()
        self.reader_started = asyncio.Event()
        self.subscriber = None
        self.worker_futures = []
        self.release_queue = threading.Event()
        self.task = None
        # Chat and headless share the module's single reader worker; give the
        # test its own so a "queued" reader is queued behind the test's job.
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gemini-3408")
        monkeypatch.setattr(gemini_runtime, "_executor", self.pool)
        if queued:
            self.pool.submit(self.release_queue.wait)
        real_submit = self.pool.submit

        def submit(fn, /, *args, **kwargs):
            def run():
                self.loop.call_soon_threadsafe(self.reader_started.set)
                return fn(*args, **kwargs)

            future = real_submit(run)
            self.worker_futures.append(future)
            return future

        monkeypatch.setattr(self.pool, "submit", submit)
        real_popen = subprocess.Popen

        def spawn(_command, **kwargs):
            process = real_popen([sys.executable, "-c", *argv], **kwargs)
            self.children.append(process)
            real_close = process.stdin.close

            def close_stdin():
                real_close()
                # Registered and prompt delivered. Subscribe before any reader
                # exists, so stream_end cannot be missed; and cancel only after
                # this boundary, since a cancel before it tests a broken
                # handoff (another mechanism), not a lost waiter.
                self.subscriber = registry.subscribe_logs(EXECUTION_ID)
                self.prompt_delivered.set()

            monkeypatch.setattr(process.stdin, "close", close_stdin)
            return process

        monkeypatch.setattr(subprocess, "Popen", spawn)

    def start(self, runtime, path):
        real_process_line = runtime._process_stream_line

        def process_line(line, *args, **kwargs):
            # Runs on the reader thread, once per line the child printed.
            self.lines.append(line)
            self.loop.call_soon_threadsafe(self.line_processed.set)
            return real_process_line(line, *args, **kwargs)

        self.monkeypatch.setattr(runtime, "_process_stream_line", process_line)
        if path == "chat":
            coro = runtime._execute_chat_once("hello", None, None, EXECUTION_ID, None)
        else:
            coro = runtime.execute_headless("hello", execution_id=EXECUTION_ID)
        self.task = asyncio.create_task(coro)

    async def cancel_waiter(self):
        self.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await self.task

    async def close(self):
        self.release_queue.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for process in self.children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.pool.shutdown(wait=True, cancel_futures=True)
        for process in self.children:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()


async def _wait_for_path(path):
    for _ in range(500):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"control: the child never reached {path.name}")


def _assert_retained(registry, child):
    """AC 3 (#3408): an abandoned live child stays owned, listed as running and
    on the orphan-sweep allowlist, until its exit is observed."""
    assert child.poll() is None, "control: cancelling the waiter does not stop the child"
    assert registry.is_execution_running(EXECUTION_ID), "an abandoned waiter must not forget its live child"
    assert EXECUTION_ID not in registry.list_recently_completed_ids()
    assert child.pid in registry.active_execution_pids(), "AC 3: a live abandoned child stays off the sweep"
    assert registry.get_buffered_logs(EXECUTION_ID) == []


async def _assert_released(registry, subscriber, child):
    assert subscriber is not None, "control: the subscriber attached while the entry was registered"
    try:
        end = await asyncio.wait_for(subscriber.get(), timeout=5)
    except asyncio.TimeoutError:
        pytest.fail(
            "no stream_end: the entry outlived its child, and nothing released it "
            "after the waiter was abandoned"
        )
    assert end == {"type": "stream_end"}
    assert not registry.is_execution_running(EXECUTION_ID)
    assert registry.get_buffered_logs(EXECUTION_ID) is None
    assert child.pid not in registry.active_execution_pids()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "headless"])
@pytest.mark.parametrize("reader", ["running", "queued"])
async def test_a_cancelled_waiter_hands_release_to_its_reader(
    path, reader, runtime, registry, own_group_signals, monkeypatch, tmp_path
):
    printed = tmp_path / "printed"
    turn = _Turn(monkeypatch, registry, [ANSWER_THEN_HOLD, str(printed)], queued=reader == "queued")
    try:
        turn.start(runtime, path)
        await asyncio.wait_for(turn.prompt_delivered.wait(), timeout=5)
        if reader == "running":
            # Executing (so its executor future can no longer be cancelled),
            # and it has already processed the child's output.
            await asyncio.wait_for(turn.reader_started.wait(), timeout=5)
            await asyncio.wait_for(turn.line_processed.wait(), timeout=5)
        child = turn.children[0]
        await turn.cancel_waiter()
        if reader == "queued":
            assert not turn.reader_started.is_set(), "control: the reader was still queued at the cancel"
            assert turn.lines == [], "control: nothing was read before the cancel"
        _assert_retained(registry, child)
        # The child's output is in the pipe before it dies, so a queued reader
        # has something to drain once it runs.
        await _wait_for_path(printed)
        child.kill()
        turn.release_queue.set()
        await asyncio.to_thread(child.wait, 5)
        await _assert_released(registry, turn.subscriber, child)
        assert len(turn.lines) == 1, "the reader drained what the child printed"
        assert own_group_signals == []
    finally:
        await turn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "headless"])
async def test_a_reader_that_finishes_before_the_cancel_lands_is_released_by_the_waiter(
    path, runtime, registry, own_group_signals, monkeypatch
):
    """The other ordering: the reader reaps its child and returns before it can
    see the waiter abandoned, and the cancel reaches the waiter before the
    result does. The waiter's own ``finally`` owns the release then (modelled
    on #3307's headless finish-race case)."""
    turn = _Turn(monkeypatch, registry, [ANSWER])
    cancel_delivered = threading.Event()
    worker_finished_first = []

    def cancel_waiter_then_let_worker_finish():
        # Runs on the loop while the waiter is parked on the pending worker
        # future, so this cancel lands before any result can.
        turn.task.cancel()
        cancel_delivered.set()
        # Hold the loop until the worker has fully returned. The waiter cannot
        # run its except clause meanwhile, so the worker never sees the flag.
        worker_finished_first.append(turn.worker_futures[0].exception(timeout=5) is None)

    def sweep_after_the_child_was_reaped(**_kw):
        # The reader runs its post-exit sweep right after process.wait(). It
        # must not return before the cancel is delivered: an executor future
        # that already finished can be handed back completed, and the waiter
        # would then never yield to a cancel queued behind it.
        turn.loop.call_soon_threadsafe(cancel_waiter_then_let_worker_finish)
        assert cancel_delivered.wait(timeout=5)
        return 0

    monkeypatch.setattr(gemini_runtime, "kill_cgroup_orphans", sweep_after_the_child_was_reaped)
    try:
        turn.start(runtime, path)
        with pytest.raises(asyncio.CancelledError):
            await turn.task
        assert worker_finished_first == [True], "control: the worker returned before the waiter's cleanup ran"
        child = turn.children[0]
        assert child.returncode == 0, "control: the child exited on its own and was reaped"
        await _assert_released(registry, turn.subscriber, child)
        assert own_group_signals == []
    finally:
        await turn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "headless"])
async def test_empty_data_an_abandoned_child_that_printed_nothing_still_ends_the_stream(
    path, runtime, registry, own_group_signals, monkeypatch, tmp_path
):
    """Other shape (empty data): the abandoned child exits 0 on its own having
    printed nothing, so its reader meets EOF on the first read; a subscriber on
    the empty buffer still gets ``stream_end`` and the entry is released."""
    sentinel = tmp_path / "release-child"
    turn = _Turn(monkeypatch, registry, [SILENT_UNTIL_RELEASED, str(sentinel)])
    try:
        turn.start(runtime, path)
        await asyncio.wait_for(turn.prompt_delivered.wait(), timeout=5)
        await asyncio.wait_for(turn.reader_started.wait(), timeout=5)
        child = turn.children[0]
        await turn.cancel_waiter()
        _assert_retained(registry, child)
        sentinel.touch()
        assert await asyncio.to_thread(child.wait, 5) == 0, "control: the child exited by itself"
        await _assert_released(registry, turn.subscriber, child)
        assert turn.lines == [], "control: the child printed nothing, so no line was processed"
        assert own_group_signals == []
    finally:
        await turn.close()


def test_the_signal_guard_refuses_this_sessions_own_group(own_group_signals):
    """Control for the guard every case above relies on. Signal 0 delivers
    nothing even if a broken guard forwarded it."""
    for send in (
        lambda: os.killpg(os.getpgrp(), 0),
        lambda: os.kill(-os.getpgrp(), 0),
        lambda: os.kill(0, 0),
    ):
        with pytest.raises(_OwnGroupSignal):
            send()
    assert len(own_group_signals) == 3
    own_group_signals.clear()
