"""Process ownership survives a cancelled waiter or an unconfirmed kill.

Regression for #3307.

Uses ordinary local Python children, never a model CLI or running agent.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from agent_server.services import claude_code as chat
from agent_server.services import headless_executor as headless
from agent_server.services import process_registry as registry_module
from agent_server.utils import subprocess_pgroup

pytestmark = pytest.mark.unit


@pytest.fixture
def registry(monkeypatch):
    value = registry_module.ProcessRegistry()
    monkeypatch.setattr(headless, "get_process_registry", lambda: value)
    monkeypatch.setattr(chat, "get_process_registry", lambda: value)
    # A unit test owns only its children, never the surrounding cgroup.
    monkeypatch.setattr(registry_module, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setattr(subprocess_pgroup, "kill_cgroup_orphans", lambda **kw: 0)
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["headless", "chat", "chat_queued_reader"])
@pytest.mark.parametrize("waiter_end", ["cancel", "timeout"])
async def test_abandoned_waiter_keeps_live_subprocess_owned(runtime, waiter_end, registry, monkeypatch):
    loop = asyncio.get_running_loop()
    registered = asyncio.Event()
    children = []
    real_popen = subprocess.Popen
    real_wait_for = asyncio.wait_for
    if waiter_end == "timeout":
        async def short_outer_timeout(awaitable, timeout):
            if timeout == 120:
                # Let the actual worker deliver its prompt before expiring
                # the outer wait; the child must still be demonstrably alive.
                await real_wait_for(registered.wait(), timeout=5)
                timeout = 0.01
            return await real_wait_for(awaitable, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", short_outer_timeout)
        # Simulate an attempted but unconfirmed stop. The real child remains
        # alive until the test explicitly releases it after the 504 response.
        monkeypatch.setattr(headless.HeadlessRunContext, "terminate", lambda self: None)
        monkeypatch.setattr(chat, "_terminate_process_group", lambda *a, **kw: None)
        monkeypatch.setattr(chat, "_safe_close_pipes", lambda *a: None)

    def spawn(_command, **kwargs):
        process = real_popen(
            [sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(60)"],
            **kwargs,
        )
        children.append(process)
        real_close = process.stdin.close

        def close_stdin():
            real_close()
            # Start cancellation only once prompt delivery is complete. Killing
            # before this boundary tests broken stdin instead of waiter loss.
            loop.call_soon_threadsafe(registered.set)

        monkeypatch.setattr(process.stdin, "close", close_stdin)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    pool = ThreadPoolExecutor(max_workers=1)
    release_reader = threading.Event()
    if runtime == "chat_queued_reader":
        pool.submit(release_reader.wait)
    task = None
    try:
        if runtime == "headless":
            monkeypatch.setattr(headless, "_HEADLESS_EXECUTOR", pool)
            monkeypatch.setattr(headless.agent_state, "claude_code_available", True)
            ctx = headless.HeadlessRunContext(
                cmd=["unused"], task_session_id="cancelled-waiter",
                task_start_iso="2026-01-01T00:00:00Z", effective_timeout=60,
                images=None, prompt="hello",
            )
            monkeypatch.setattr(headless, "_setup_headless_command", lambda **kw: ctx)
            task = asyncio.create_task(headless.execute_headless_task("hello"))
        else:
            monkeypatch.setattr(chat, "_executor", pool)
            monkeypatch.setattr(chat, "_load_guardrails", lambda: {"execution_timeout_sec": 60})
            task = asyncio.create_task(chat._execute_claude_code_once(
                "hello", False, None, None, "cancelled-waiter", None,
            ))
        await asyncio.wait_for(registered.wait(), timeout=5)
        assert children[0].poll() is None
        assert registry.is_execution_running("cancelled-waiter")
        if waiter_end == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(HTTPException) as error:
                await real_wait_for(task, timeout=5)
            assert error.value.status_code == 504
        assert children[0].poll() is None, "control: the executor thread's child is still alive"
        assert registry.is_execution_running("cancelled-waiter"), "an abandoned waiter must not forget its live child"
        assert "cancelled-waiter" not in registry.list_recently_completed_ids()
        assert children[0].pid in registry.active_execution_pids()
        subscriber = registry.subscribe_logs("cancelled-waiter")
        assert subscriber is not None
        children[0].kill()
        release_reader.set()
        await asyncio.to_thread(children[0].wait, timeout=5)
        end = await asyncio.wait_for(subscriber.get(), timeout=5)
        assert end == {"type": "stream_end"}
        assert not registry.is_execution_running("cancelled-waiter")
        assert registry.get_buffered_logs("cancelled-waiter") is None
    finally:
        release_reader.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        pool.shutdown(wait=True, cancel_futures=True)
        for process in children:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()


def test_terminate_does_not_confirm_a_process_that_survived_both_waits(registry, monkeypatch):
    process = Mock(pid=12345, returncode=None)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("test-child", 0)
    signal = Mock()
    monkeypatch.setattr(registry_module, "_signal_process_tree", signal)
    registry.register("unconfirmed", process)
    result = registry.terminate("unconfirmed", graceful_timeout=0)
    assert signal.call_count == 2
    assert process.wait.call_count == 2
    assert result["success"] is False, "sending SIGKILL is not proof of process exit"
    assert result["reason"] == "termination_unconfirmed"
    assert registry.is_execution_running("unconfirmed")
    assert "unconfirmed" not in registry.list_recently_completed_ids()


def test_unregister_keeps_live_child_logs_and_allows_later_completion(registry):
    process = Mock(pid=12345)
    process.poll.return_value = None
    registry.register("owned", process)
    subscriber = registry.subscribe_logs("owned")
    registry.publish_log_entry("owned", {"type": "test"})
    assert subscriber.get_nowait() == {"type": "test"}
    registry.unregister("owned")
    registry.unregister("owned")
    assert registry.is_execution_running("owned")
    assert subscriber.empty(), "a live child must not publish stream_end"
    assert registry.get_buffered_logs("owned") == [{"type": "test"}]
    process.poll.return_value = 0
    registry.unregister("owned")
    assert not registry.is_execution_running("owned")
    assert subscriber.get_nowait() == {"type": "stream_end"}
    assert "owned" in registry.list_recently_completed_ids()


def test_failed_signal_retains_child(registry, monkeypatch):
    process = Mock(pid=12345, returncode=None)
    process.poll.return_value = None
    registry.register("signal-error", process)
    monkeypatch.setattr(registry_module, "_signal_process_tree", Mock(side_effect=PermissionError("denied")))
    result = registry.terminate("signal-error", graceful_timeout=0)
    assert result["success"] is False
    assert result["reason"] == "error"
    assert registry.is_execution_running("signal-error")
    process.wait.assert_not_called()


def test_pending_cancel_remains_pending_until_spawn_or_discard(registry):
    registry.register_pending("pending")
    assert registry.terminate("pending") == {
        "success": True, "returncode": None, "reason": "cancelled_before_start",
    }
    assert registry.list_pending_ids() == ["pending"]
    assert registry.was_terminated("pending")
    registry.discard_pending("pending")
    assert registry.list_pending_ids() == []


@pytest.mark.asyncio
async def test_cancelled_headless_waiter_before_worker_start_never_spawns(registry, monkeypatch):
    loop = asyncio.get_running_loop()
    submitted = asyncio.Event()
    release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    pool.submit(release.wait)
    real_submit = pool.submit

    def submit(*args, **kwargs):
        future = real_submit(*args, **kwargs)
        loop.call_soon_threadsafe(submitted.set)
        return future

    monkeypatch.setattr(pool, "submit", submit)
    monkeypatch.setattr(headless, "_HEADLESS_EXECUTOR", pool)
    monkeypatch.setattr(headless.agent_state, "claude_code_available", True)
    ctx = headless.HeadlessRunContext(
        cmd=["unused"], task_session_id="queued-cancel", task_start_iso="",
        effective_timeout=60, images=None, prompt="hello",
    )
    monkeypatch.setattr(headless, "_setup_headless_command", lambda **kw: ctx)
    spawn = Mock(side_effect=AssertionError("cancelled queued task spawned"))
    monkeypatch.setattr(headless, "_run_headless_subprocess", spawn)
    registry.register_pending("queued-cancel")
    task = asyncio.create_task(headless.execute_headless_task("hello"))
    try:
        await asyncio.wait_for(submitted.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=False)
        spawn.assert_not_called()
        assert registry.list_pending_ids() == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_normal_headless_result_is_finalized_before_stream_end(registry, monkeypatch):
    monkeypatch.setattr(headless.agent_state, "claude_code_available", True)
    ctx = headless.HeadlessRunContext(
        cmd=["unused"], task_session_id="normal-finish", task_start_iso="",
        effective_timeout=60, images=None, prompt="hello",
    )
    monkeypatch.setattr(headless, "_setup_headless_command", lambda **kw: ctx)
    process = Mock(pid=12345)
    process.poll.return_value = 0

    def run(_ctx):
        registry.register("normal-finish", process)

    def finalize(_ctx):
        assert registry.is_execution_running("normal-finish")
        assert registry.get_buffered_logs("normal-finish") == []
        return "result"

    monkeypatch.setattr(headless, "_run_headless_subprocess", run)
    monkeypatch.setattr(headless, "_finalize_headless_result", finalize)
    assert await headless.execute_headless_task("hello") == "result"
    assert not registry.is_execution_running("normal-finish")


@pytest.mark.asyncio
async def test_headless_worker_finish_racing_waiter_cancellation_still_cleans_up(registry, monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(headless.agent_state, "claude_code_available", True)
    ctx = headless.HeadlessRunContext(
        cmd=["unused"], task_session_id="finish-race", task_start_iso="",
        effective_timeout=60, images=None, prompt="hello",
    )
    monkeypatch.setattr(headless, "_setup_headless_command", lambda **kw: ctx)
    process = Mock(pid=12345)
    process.poll.return_value = 0
    task_holder = []
    worker_futures = []
    cancel_delivered = threading.Event()
    worker_finished_first = []
    pool = ThreadPoolExecutor(max_workers=1)
    real_submit = pool.submit

    def submit(*args, **kwargs):
        future = real_submit(*args, **kwargs)
        worker_futures.append(future)
        return future

    def cancel_waiter_then_let_worker_finish():
        # Runs on the loop, so the waiter is parked on a still-pending worker
        # future: this cancel always lands before any result can.
        task_holder[0].cancel()
        cancel_delivered.set()
        # Hold the loop until the worker has fully returned. The waiter cannot
        # run its except clause meanwhile, so the worker never sees the flag.
        worker_finished_first.append(worker_futures[0].exception(timeout=5) is None)

    def run(_ctx):
        registry.register("finish-race", process)
        # Worker finishes before it can observe the abandoned flag, but the
        # caller is cancelled before receiving its result. Its finally owns
        # cleanup in this ordering.
        #
        # The worker must not return until that cancel is delivered. A
        # run_in_executor future whose worker already finished can be handed
        # back completed (CPython 3.13.12+), and the caller then gets the
        # result without ever yielding to a cancel merely queued behind it.
        loop.call_soon_threadsafe(cancel_waiter_then_let_worker_finish)
        assert cancel_delivered.wait(timeout=5)

    monkeypatch.setattr(pool, "submit", submit)
    monkeypatch.setattr(headless, "_HEADLESS_EXECUTOR", pool)
    monkeypatch.setattr(headless, "_run_headless_subprocess", run)
    task_holder.append(asyncio.create_task(headless.execute_headless_task("hello")))
    try:
        with pytest.raises(asyncio.CancelledError):
            await task_holder[0]
        assert worker_finished_first == [True], "control: the worker returned before the waiter's cleanup ran"
        assert not registry.is_execution_running("finish-race")
        assert registry.get_buffered_logs("finish-race") is None
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
