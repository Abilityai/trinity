"""An abandoned Codex waiter hands release to its reader worker.

Regression for #3408, the Codex half of #3307's cleanup. Once the HTTP waiter
stops waiting (it is cancelled, or its outer timeout fires and the stop is not
confirmed), the registry must keep the live child, its log subscribers and its
orphan-sweep allowlist entry, and the reader worker must release it after the
child exits: the ``-o`` result file goes first, then the registry entry, with
``stream_end`` to every subscriber.

Runs the real ``CodexRuntime._execute_codex`` against the real
``ProcessRegistry`` with ordinary local Python children standing in for the
Codex CLI. No model, provider or live agent is called.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from agent_server.services import codex_runtime
from agent_server.services import process_registry as registry_module
from agent_server.services.codex_runtime import CodexRuntime
from agent_server.utils import subprocess_pgroup
from fastapi import HTTPException

pytestmark = pytest.mark.unit

EXECUTION_ID = "codex-3408"
TIMEOUT_SECONDS = 60
OUTER_TIMEOUT = TIMEOUT_SECONDS + 60

# The stand-in for `codex exec`: read the prompt to EOF, optionally emit one
# JSONL event, then wait until the test says the turn is over. At turn end,
# optionally write the `-o` result file (and a marker so the test can see the
# file existed), optionally make CODEX_HOME read-only, and exit 0.
_CHILD = r"""
import json, os, sys, time
release, marker, result, emit, lock_home = sys.argv[1:6]
sys.stdin.read()
if emit == "1":
    print(json.dumps({"type": "thread.started", "thread_id": "t-3408"}), flush=True)
deadline = time.monotonic() + 60
while not os.path.exists(release) and time.monotonic() < deadline:
    time.sleep(0.01)
if result != "-":
    with open(result, "w") as fh:
        fh.write("final answer")
    open(marker, "w").close()
    if lock_home == "1":
        os.chmod(os.path.dirname(result), 0o555)
"""


@pytest.fixture
def registry(monkeypatch):
    value = registry_module.ProcessRegistry()
    monkeypatch.setattr(codex_runtime, "get_process_registry", lambda: value)
    # A unit test owns only its children, never the surrounding cgroup.
    monkeypatch.setattr(registry_module, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setattr(subprocess_pgroup, "kill_cgroup_orphans", lambda **kw: 0)
    return value


@pytest.fixture
def refused_signals(monkeypatch):
    """Refuse (never forward) a signal aimed at this test session's own
    process or group. The conftest net is a pass-through without cgroup v2
    (macOS), so the file carries its own."""
    refused = []
    own_pid, own_pgrp = os.getpid(), os.getpgrp()
    real_kill, real_killpg = os.kill, os.killpg

    def kill(pid, sig):
        if pid in (own_pid, 0, -own_pgrp):
            refused.append(("kill", pid, sig))
            raise RuntimeError(f"refused os.kill({pid}, {sig}) at the test session")
        return real_kill(pid, sig)

    def killpg(pgid, sig):
        if pgid in (own_pgrp, 0):
            refused.append(("killpg", pgid, sig))
            raise RuntimeError(f"refused os.killpg({pgid}, {sig}) at the test session")
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", killpg)
    yield refused
    assert refused == [], f"a signal was aimed at the test session: {refused}"


class _Harness:
    """One `_execute_codex` call driven against a real child."""

    def __init__(self, monkeypatch, tmp_path, *, pool_kind, queued, emit=True,
                 write_result=True, lock_home=False):
        self.monkeypatch = monkeypatch
        self.loop = asyncio.get_running_loop()
        self.codex_home = tmp_path / "codex"
        self.codex_home.mkdir()
        control = tmp_path / "control"
        control.mkdir()
        self.release_file = control / "release"
        self.marker = control / "wrote-result"
        self.result_file = self.codex_home / f"{EXECUTION_ID}-last.txt"
        self.emit, self.write_result, self.lock_home = emit, write_result, lock_home
        self.pool_kind, self.queued = pool_kind, queued
        self.registered = asyncio.Event()
        self.reader_started = asyncio.Event()
        self.children = []
        self.submissions = []
        self.unblock_pool = threading.Event()
        # Production's chat pool has one worker. A running task reader shares
        # the default pool with the outer timeout's cleanup, so it gets two.
        self.pool = ThreadPoolExecutor(max_workers=2 if pool_kind == "task" and not queued else 1)
        real_submit = self.pool.submit

        def submit(fn, *args, **kwargs):
            is_reader = not self.submissions

            def run(*a, **kw):
                if is_reader:
                    # In the worker: the pool has started the reader job, so
                    # its future is RUNNING and can no longer be cancelled.
                    self.loop.call_soon_threadsafe(self.reader_started.set)
                return fn(*a, **kw)

            future = real_submit(run, *args, **kwargs)
            self.submissions.append((fn, future))
            return future

        if queued:
            # Saturate the reader pool before the turn starts, so the reader
            # is still queued when the waiter goes away.
            real_submit(self.unblock_pool.wait)
        monkeypatch.setattr(self.pool, "submit", submit)
        if pool_kind == "chat":
            monkeypatch.setattr(codex_runtime, "_executor", self.pool)
        else:
            # /api/task (concurrent_reader=True) reads on the loop's default pool.
            self.loop.set_default_executor(self.pool)

        monkeypatch.setattr(codex_runtime, "_load_openai_api_key", lambda: None)
        monkeypatch.setattr(codex_runtime, "_ensure_codex_home", lambda: str(self.codex_home))
        monkeypatch.setattr(codex_runtime, "_has_subscription_auth", lambda _home: True)
        real_popen = subprocess.Popen

        def spawn(cmd, **kwargs):
            result = cmd[cmd.index("-o") + 1] if self.write_result else "-"
            if self.write_result:
                assert result == str(self.result_file), "control: -o names CODEX_HOME's result file"
            process = real_popen(
                [sys.executable, "-c", _CHILD, str(self.release_file), str(self.marker),
                 result, "1" if self.emit else "0", "1" if self.lock_home else "0"],
                **kwargs,
            )
            self.children.append(process)
            real_close = process.stdin.close

            def close_stdin():
                real_close()
                # Abandon the waiter only once the prompt is delivered: before
                # this boundary the case would test a broken stdin instead.
                self.loop.call_soon_threadsafe(self.registered.set)

            monkeypatch.setattr(process.stdin, "close", close_stdin)
            return process

        monkeypatch.setattr(codex_runtime.subprocess, "Popen", spawn)

    @property
    def child(self):
        return self.children[0]

    @property
    def reader_future(self):
        # The reader is the first job on the reader pool; on the task pool the
        # outer timeout's cleanup jobs follow it there.
        fn, future = self.submissions[0]
        assert fn.__name__ in ("read_owned_subprocess_output", "read_subprocess_output"), fn
        return future

    async def wait_until_abandonable(self):
        """The prompt is delivered and, unless the pool is saturated, the
        reader has really started (a free worker alone does not prove it)."""
        await _real_wait_for(self.registered.wait(), timeout=5)
        if not self.queued:
            await _real_wait_for(self.reader_started.wait(), timeout=5)

    def start(self):
        rt = CodexRuntime()
        return asyncio.create_task(rt._execute_codex(
            prompt="hello", model=None, system_prompt=None, resume_thread_id=None,
            timeout_seconds=TIMEOUT_SECONDS, allowed_tools=None,
            execution_id=EXECUTION_ID, concurrent_reader=self.pool_kind == "task",
        ))

    def finish_turn(self):
        self.release_file.touch()

    async def stream_until_end(self, subscriber):
        entries = []
        while True:
            entry = await _real_wait_for(subscriber.get(), timeout=5)
            entries.append(entry)
            if entry == {"type": "stream_end"}:
                return entries

    def close(self):
        self.unblock_pool.set()
        self.release_file.touch()
        for process in self.children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.pool.shutdown(wait=True, cancel_futures=True)
        for process in self.children:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
        if self.codex_home.exists():
            os.chmod(self.codex_home, 0o755)


_real_wait_for = asyncio.wait_for


def _hold_outer_timeout_until_abandonable(monkeypatch, harness):
    """Expire only `_execute_codex`'s outer wait, and only once the prompt is
    delivered and the reader is running (or queued), so the child is
    demonstrably alive when the waiter gives up."""

    async def short_outer_timeout(awaitable, timeout):
        if timeout == OUTER_TIMEOUT:
            await harness.wait_until_abandonable()
            timeout = 0.01
        return await _real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", short_outer_timeout)
    # An attempted but unconfirmed stop: the child stays alive after the 504.
    monkeypatch.setattr(codex_runtime, "_terminate_process_group", lambda *a, **kw: None)
    monkeypatch.setattr(codex_runtime, "_safe_close_pipes", lambda *a: None)


async def _abandon(harness, task, waiter_end):
    await harness.wait_until_abandonable()
    assert harness.child.poll() is None
    assert harness.registry.is_execution_running(EXECUTION_ID)
    if waiter_end == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(HTTPException) as error:
            await _real_wait_for(task, timeout=5)
        assert error.value.status_code == 504


def _assert_retained(harness):
    registry = harness.registry
    assert harness.child.poll() is None, "control: the abandoned child is still alive"
    assert registry.is_execution_running(EXECUTION_ID), "an abandoned waiter must not forget its live child"
    assert EXECUTION_ID not in registry.list_recently_completed_ids()
    # #3408 AC 3: an abandoned live child stays off the orphan sweep.
    assert harness.child.pid in registry.active_execution_pids()
    if harness.queued:
        assert not harness.reader_started.is_set(), "control: the reader was still queued"
    else:
        assert harness.reader_future.running(), "control: the reader was running"


async def _assert_released(harness, entries):
    registry = harness.registry
    assert entries[-1] == {"type": "stream_end"}
    assert harness.child.poll() == 0, "control: the child ended its turn normally"
    assert not registry.is_execution_running(EXECUTION_ID)
    assert registry.get_buffered_logs(EXECUTION_ID) is None
    assert EXECUTION_ID in registry.list_recently_completed_ids()
    assert sorted(p.name for p in harness.codex_home.glob("*-last.txt")) == []
    # `stream_end` can arrive before the executor marks the reader done.
    _, return_code = await _real_wait_for(asyncio.wrap_future(harness.reader_future), timeout=5)
    assert return_code == 0, "the owning reader returned cleanly, having seen the exit"


# chat reads on Codex's 1-worker `_executor`; task (`concurrent_reader=True`)
# on the loop's default pool. A queued reader is one still waiting for a pool
# worker when the waiter goes away. Not listed: a queued task reader with an
# outer timeout. There the timeout's own cleanup queues on the same saturated
# default pool behind the reader, so the waiter outlives the child and its own
# `finally` releases it; the case cannot tell the abandoned flag apart.
_ABANDONMENTS = [
    ("chat", False, "cancel"),
    ("chat", False, "timeout"),
    ("task", False, "cancel"),
    ("task", False, "timeout"),
    ("chat", True, "cancel"),
    ("chat", True, "timeout"),
    ("task", True, "cancel"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pool_kind,queued,waiter_end",
    _ABANDONMENTS,
    ids=[f"{k}{'_queued_reader' if q else ''}-{e}" for k, q, e in _ABANDONMENTS],
)
async def test_abandoned_codex_waiter_releases_after_child_exits(
    pool_kind, queued, waiter_end, registry, refused_signals, monkeypatch, tmp_path,
):
    harness = _Harness(monkeypatch, tmp_path, pool_kind=pool_kind, queued=queued)
    harness.registry = registry
    if waiter_end == "timeout":
        _hold_outer_timeout_until_abandonable(monkeypatch, harness)
    task = None
    try:
        task = harness.start()
        await _abandon(harness, task, waiter_end)
        _assert_retained(harness)
        subscriber = registry.subscribe_logs(EXECUTION_ID)
        assert subscriber is not None
        harness.unblock_pool.set()
        harness.finish_turn()
        entries = await harness.stream_until_end(subscriber)
        assert harness.marker.exists(), "control: the child wrote its -o file at turn end"
        assert {"type": "thread.started", "thread_id": "t-3408"} in entries
        await _assert_released(harness, entries)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        harness.close()


@pytest.mark.asyncio
async def test_abandoned_child_with_no_output_and_no_result_file_is_still_released(
    registry, refused_signals, monkeypatch, tmp_path,
):
    """Other shape, empty data: the abandoned child prints nothing and never
    writes `-o`. The release and `stream_end` still happen, and removing the
    missing result file is harmless."""
    harness = _Harness(monkeypatch, tmp_path, pool_kind="chat", queued=False,
                       emit=False, write_result=False)
    harness.registry = registry
    task = None
    try:
        task = harness.start()
        await _abandon(harness, task, "cancel")
        _assert_retained(harness)
        subscriber = registry.subscribe_logs(EXECUTION_ID)
        assert subscriber is not None
        harness.finish_turn()
        entries = await harness.stream_until_end(subscriber)
        assert not harness.marker.exists(), "control: no -o file was ever written"
        assert entries == [{"type": "stream_end"}]
        await _assert_released(harness, entries)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        harness.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root bypasses directory permissions, so the unlink would not fail",
)
async def test_result_file_that_cannot_be_removed_does_not_block_the_release(
    registry, refused_signals, monkeypatch, tmp_path,
):
    """A real read-only CODEX_HOME (the child removes its own write bit at
    turn end): the worker's unlink fails, and the release still happens."""
    harness = _Harness(monkeypatch, tmp_path, pool_kind="chat", queued=False, lock_home=True)
    harness.registry = registry
    task = None
    try:
        task = harness.start()
        await _abandon(harness, task, "cancel")
        _assert_retained(harness)
        subscriber = registry.subscribe_logs(EXECUTION_ID)
        harness.finish_turn()
        entries = await harness.stream_until_end(subscriber)
        assert not os.access(harness.codex_home, os.W_OK), "control: CODEX_HOME is read-only"
        assert harness.result_file.exists(), "control: the unlink really was refused"
        assert entries[-1] == {"type": "stream_end"}
        assert not registry.is_execution_running(EXECUTION_ID)
        assert registry.get_buffered_logs(EXECUTION_ID) is None
        _, return_code = await _real_wait_for(asyncio.wrap_future(harness.reader_future), timeout=5)
        assert return_code == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_kind", ["chat", "task"])
async def test_worker_finishing_before_the_cancel_lands_is_released_by_the_waiter(
    pool_kind, registry, refused_signals, monkeypatch, tmp_path,
):
    """The worker returns before it can see the abandoned flag, and the waiter
    is cancelled before it receives the result: the waiter's own `finally`
    owns the release in this ordering (modelled on #3307's headless race)."""
    harness = _Harness(monkeypatch, tmp_path, pool_kind=pool_kind, queued=False)
    harness.registry = registry
    task_holder = []
    cancel_delivered = threading.Event()
    worker_finished_first = []
    real_drain = codex_runtime._drain_bounded

    def cancel_waiter_then_let_worker_finish():
        # On the loop, while the waiter is parked on the still-pending worker:
        # the cancel always lands before the result can.
        task_holder[0].cancel()
        cancel_delivered.set()
        # Hold the loop until the worker has fully returned, so the waiter
        # cannot set the abandoned flag before the worker checks it.
        worker_finished_first.append(harness.reader_future.exception(timeout=5) is None)

    def drain_then_race(*args, **kwargs):
        outcome = real_drain(*args, **kwargs)
        # The child's exit is observed and drained; now deliver the cancel,
        # and only then let the worker return.
        harness.loop.call_soon_threadsafe(cancel_waiter_then_let_worker_finish)
        assert cancel_delivered.wait(timeout=5)
        return outcome

    monkeypatch.setattr(codex_runtime, "_drain_bounded", drain_then_race)
    try:
        task_holder.append(harness.start())
        await _real_wait_for(harness.registered.wait(), timeout=5)
        subscriber = registry.subscribe_logs(EXECUTION_ID)
        assert subscriber is not None
        harness.finish_turn()
        with pytest.raises(asyncio.CancelledError):
            await task_holder[0]
        assert worker_finished_first == [True], "control: the worker returned before the waiter's cleanup ran"
        entries = await harness.stream_until_end(subscriber)
        assert harness.marker.exists(), "control: the child wrote its -o file at turn end"
        await _assert_released(harness, entries)
    finally:
        if task_holder and not task_holder[0].done():
            task_holder[0].cancel()
            await asyncio.gather(task_holder[0], return_exceptions=True)
        harness.close()
