"""A failed prompt handoff kills its child and observes its exit before release.

Regression for #3408 (part 2). Every runtime registers its child and then
writes the prompt to the child's stdin. Since #3307, `unregister()` refuses to
drop a live child, so when that write failed with the child alive the old
`except: unregister(); raise` handlers did nothing: the entry, its log buffer
and its orphan-sweep allowlisting stayed, and the child itself blocked forever
on a stdin nobody closed. The trigger here is a natural one: a prompt holding
a lone surrogate (which the agent server's FastAPI accepts from a JSON body)
makes the text-mode write raise UnicodeEncodeError while the child lives.

Claude and Codex children lead their own process group, so their teardown
kills that group. Gemini children share the agent server's group (they are
spawned without `start_new_session`), so the Gemini teardown signals the pid
only. Every case runs under a guard that refuses, and never forwards, a signal
aimed at this test session's own group: tests/signal_guard.py is a
pass-through where there is no cgroup (macOS).

Uses ordinary local Python children, never a model CLI or running agent.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from agent_server.services import claude_code as chat
from agent_server.services import codex_runtime
from agent_server.services import gemini_runtime
from agent_server.services import headless_executor as headless
from agent_server.services import process_registry as registry_module
from agent_server.utils import subprocess_pgroup

pytestmark = pytest.mark.unit

EID = "failed-handoff"
# Accepted in a JSON body by the agent server, unencodable on a text-mode pipe.
UNENCODABLE_PROMPT = "hi " + json.loads('"\\ud800"') + " there"
SITES = ["claude_chat", "codex", "gemini_chat", "gemini_headless"]
GEMINI_SITES = {"gemini_chat", "gemini_headless"}
# Reads its prompt to EOF, then outlives the test: only a kill ends it early.
BLOCKED_CHILD = "import sys,time; sys.stdin.read(); time.sleep(60)"
# Reads its prompt to EOF and exits 0 having printed nothing.
SILENT_CHILD = "import sys; sys.stdin.read()"


@pytest.fixture
def registry(monkeypatch):
    value = registry_module.ProcessRegistry()
    for module in (chat, headless, codex_runtime, gemini_runtime):
        monkeypatch.setattr(module, "get_process_registry", lambda: value)
    # A unit test owns only its children, never the surrounding cgroup.
    monkeypatch.setattr(registry_module, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setattr(subprocess_pgroup, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setattr(gemini_runtime, "kill_cgroup_orphans", lambda **kw: 0)
    return value


@pytest.fixture
def signals(monkeypatch):
    """Record every signal sent; refuse (raise, never forward) one aimed at
    this test session's own process group or at pytest itself. A forwarding
    spy would already have signalled pytest's group before it could assert."""
    own_pgrp, own_pid = os.getpgrp(), os.getpid()
    real_kill, real_killpg = os.kill, os.killpg
    log = {"sent": [], "refused": []}

    def killpg(pgid, sig):
        if pgid in (0, own_pgrp):
            log["refused"].append(("killpg", pgid, sig))
            raise AssertionError(f"refused killpg({pgid}, {sig}): this test session's own group")
        log["sent"].append(("killpg", pgid, sig))
        return real_killpg(pgid, sig)

    def kill(pid, sig):
        if pid in (0, -1, own_pid, own_pgrp, -own_pgrp):
            log["refused"].append(("kill", pid, sig))
            raise AssertionError(f"refused kill({pid}, {sig}): this test session itself")
        log["sent"].append(("kill", pid, sig))
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(os, "kill", kill)
    return log


@pytest.fixture
def pools(monkeypatch):
    """Fresh reader pools, so no case shares a module's long-lived worker."""
    created = []
    for module, name in (
        (chat, "_executor"), (headless, "_HEADLESS_EXECUTOR"),
        (codex_runtime, "_executor"), (gemini_runtime, "_executor"),
    ):
        pool = ThreadPoolExecutor(max_workers=1)
        created.append(pool)
        monkeypatch.setattr(module, name, pool)
    yield
    for pool in created:
        pool.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def children(monkeypatch):
    """Every runtime's Popen spawns a disposable Python child instead, with the
    runtime's own Popen kwargs (pipes, text mode, start_new_session or not)."""
    spawned = []
    script = {"source": BLOCKED_CHILD}
    real_popen = subprocess.Popen

    def spawn(_command, **kwargs):
        process = real_popen([sys.executable, "-c", script["source"]], **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    yield spawned, script
    for process in spawned:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                with contextlib.suppress(OSError, ValueError):
                    pipe.close()


def _prepare(site, monkeypatch, tmp_path):
    """Wire one real handoff site; return `prompt -> coroutine`."""
    if site == "claude_chat":
        monkeypatch.setattr(chat, "_load_guardrails", lambda: {"execution_timeout_sec": 60})
        return lambda prompt: chat._execute_claude_code_once(prompt, False, None, None, EID, None)
    if site == "claude_headless":
        monkeypatch.setattr(headless.agent_state, "claude_code_available", True)

        def setup(**kw):
            return headless.HeadlessRunContext(
                cmd=["unused"], task_session_id=EID, task_start_iso="2026-01-01T00:00:00Z",
                effective_timeout=60, images=None, prompt=kw["prompt"],
            )

        monkeypatch.setattr(headless, "_setup_headless_command", setup)
        return lambda prompt: headless.execute_headless_task(prompt)
    if site == "codex":
        runtime = codex_runtime.CodexRuntime()
        monkeypatch.setattr(codex_runtime, "_load_openai_api_key", lambda: None)
        monkeypatch.setattr(codex_runtime, "_ensure_codex_home", lambda: str(tmp_path))
        monkeypatch.setattr(codex_runtime, "_has_subscription_auth", lambda _home: True)
        return lambda prompt: runtime._execute_codex(
            prompt=prompt, model=None, system_prompt=None, resume_thread_id=None,
            timeout_seconds=60, allowed_tools=None, execution_id=EID,
        )
    runtime = gemini_runtime.GeminiRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    if site == "gemini_chat":
        return lambda prompt: runtime._execute_chat_once(prompt, None, None, EID, None)
    assert site == "gemini_headless", site
    return lambda prompt: runtime.execute_headless(prompt=prompt, execution_id=EID)


def _is_the_handoff_failure(error):
    """Codex lets the write's error escape; the others wrap it in a 500. Either
    way it is the encode failure, not an error raised by the teardown itself
    (an UnboundLocalError from naming the not-yet-assigned `loop`, say)."""
    if isinstance(error, UnicodeEncodeError):
        return True
    return isinstance(error, HTTPException) and "surrogates not allowed" in str(error.detail)


def _assert_released(registry, child):
    assert not registry.is_execution_running(EID), "a dead child's entry must be released"
    assert registry.get_buffered_logs(EID) is None
    assert child.pid not in registry.active_execution_pids(), "no longer allowlisted from the sweep"
    assert EID in registry.list_recently_completed_ids(), "released by unregister(), not lost"


def test_control_the_guard_refuses_this_sessions_own_group(signals):
    with pytest.raises(AssertionError):
        os.killpg(os.getpgrp(), 0)  # signal 0 delivers nothing even if forwarded
    assert signals["refused"] == [("killpg", os.getpgrp(), 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("site", SITES)
async def test_a_failed_handoff_kills_the_child_before_releasing_it(
    site, registry, signals, pools, children, monkeypatch, tmp_path,
):
    spawned, _ = children
    call = _prepare(site, monkeypatch, tmp_path)

    with pytest.raises((UnicodeEncodeError, HTTPException)) as raised:
        await asyncio.wait_for(call(UNENCODABLE_PROMPT), timeout=30)

    assert _is_the_handoff_failure(raised.value), repr(raised.value)
    [child] = spawned
    assert child.poll() is not None, "the child must be dead, and its exit observed, when the call returns"
    assert child.returncode < 0, "control: this child blocks on stdin, so only a kill ends it"
    _assert_released(registry, child)
    assert signals["refused"] == [], "a signal was aimed at this test session's own group"
    if site in GEMINI_SITES:
        # The Gemini pgid is the agent server's own group: pid only, never a group.
        assert {fn for fn, _target, _sig in signals["sent"]} == {"kill"}, signals["sent"]
        assert ("kill", child.pid, signal.SIGKILL) in signals["sent"]
    else:
        # A setsid child leads its own group, so its pgid is its pid.
        assert ("killpg", child.pid, signal.SIGKILL) in signals["sent"], signals["sent"]
    assert {target for _fn, target, _sig in signals["sent"]} == {child.pid}, signals["sent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("site", SITES)
async def test_an_empty_prompt_hands_off_cleanly_and_the_normal_path_releases(
    site, registry, signals, pools, children, monkeypatch, tmp_path,
):
    """Other shape, empty data: the write and close succeed, so the new
    handler never fires (nothing is signalled, the child exits on its own) and
    the runtime's normal path releases the entry."""
    spawned, script = children
    script["source"] = SILENT_CHILD
    call = _prepare(site, monkeypatch, tmp_path)

    try:
        await asyncio.wait_for(call(""), timeout=30)
    except HTTPException as error:
        # An empty turn may be refused by the normal path; never by the handoff.
        assert not _is_the_handoff_failure(error), repr(error)

    [child] = spawned
    assert child.returncode == 0, "the child read its empty prompt to EOF and exited on its own"
    assert signals["sent"] == [] and signals["refused"] == [], signals
    _assert_released(registry, child)


@pytest.mark.asyncio
async def test_the_teardown_runs_off_the_event_loop(
    registry, signals, pools, children, monkeypatch, tmp_path,
):
    """The kill waits up to ~2 s for the child. At an async site it must not
    hold the event loop that /health shares (#523), so it runs in the default
    pool and the release waits for it. Slow the real kill and keep measuring
    the loop meanwhile. Reaching the handoff failure at all also proves the
    handler acquires the running loop itself: the function's own `loop` is
    assigned only after the write, so naming it would raise UnboundLocalError."""
    spawned, _ = children
    loop_thread = threading.get_ident()
    kill_threads = []
    kill_done = threading.Event()
    real_terminate = chat._terminate_process_group

    def slow_terminate(process, *args, **kwargs):
        kill_threads.append(threading.get_ident())
        time.sleep(0.5)
        try:
            return real_terminate(process, *args, **kwargs)
        finally:
            kill_done.set()

    monkeypatch.setattr(chat, "_terminate_process_group", slow_terminate)
    call = _prepare("claude_chat", monkeypatch, tmp_path)
    ticks, held_while_killing = [], []
    stop = asyncio.Event()

    async def tick():
        while not stop.is_set():
            ticks.append(time.monotonic())
            if kill_threads and not kill_done.is_set():
                held_while_killing.append(registry.is_execution_running(EID))
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0)
    started = time.monotonic()
    try:
        with pytest.raises(HTTPException) as raised:
            await asyncio.wait_for(call(UNENCODABLE_PROMPT), timeout=30)
    finally:
        stop.set()
        await ticker
    elapsed = time.monotonic() - started

    assert _is_the_handoff_failure(raised.value), repr(raised.value)
    assert kill_threads, "the teardown never ran"
    assert kill_threads[0] != loop_thread, "the kill ran on the event loop thread"
    assert elapsed >= 0.5, "control: the call returned only after the slow teardown"
    gaps = [later - earlier for earlier, later in itertools.pairwise(ticks)]
    assert gaps and max(gaps) < 0.25, f"the event loop stalled {max(gaps or [0]):.3f}s during the teardown"
    assert held_while_killing and all(held_while_killing), "released before the child was dead"
    [child] = spawned
    assert child.poll() is not None
    _assert_released(registry, child)
    assert signals["refused"] == []


@pytest.mark.asyncio
async def test_a_cancel_during_the_teardown_still_releases_once_the_child_is_dead(
    registry, signals, pools, children, monkeypatch, tmp_path,
):
    """The waiter can be cancelled while the kill is still in flight. The
    teardown is shielded, so it finishes; the release then follows it, never
    precedes it."""
    spawned, _ = children
    kill_started, let_kill_finish = threading.Event(), threading.Event()
    real_terminate = chat._terminate_process_group

    def gated_terminate(process, *args, **kwargs):
        kill_started.set()
        assert let_kill_finish.wait(timeout=5), "the test never released the kill"
        return real_terminate(process, *args, **kwargs)

    monkeypatch.setattr(chat, "_terminate_process_group", gated_terminate)
    call = _prepare("claude_chat", monkeypatch, tmp_path)
    task = asyncio.create_task(call(UNENCODABLE_PROMPT))
    try:
        assert await asyncio.to_thread(kill_started.wait, 5), "the teardown never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        [child] = spawned
        assert child.poll() is None, "control: the kill is still held, so the child lives"
        assert registry.is_execution_running(EID), "released before the child was dead"
        let_kill_finish.set()
        deadline = time.monotonic() + 5
        while registry.is_execution_running(EID) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert child.poll() is not None
        _assert_released(registry, child)
        assert signals["refused"] == []
    finally:
        let_kill_finish.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
