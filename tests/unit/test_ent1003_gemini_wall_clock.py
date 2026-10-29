"""trinity-enterprise#1003 — a Gemini run is bounded by wall clock, on a timer.

The chat path waited on its child with a bare ``process.wait()``, so a hung
Gemini chat turn ran forever. The task path checked its budget only when a
stream line arrived, so a child that went silent was never cut off. Both now
run under ``_WallClockDeadline``: a timer that terminates the child's process
group when the budget runs out (the child leads that group since #4064), and a
reap bounded by what is left of the budget.

Drives the real ``GeminiRuntime`` with the real ``ProcessRegistry`` and
disposable local Python children that stay silent; never a model CLI. Each
silent child also starts a grandchild that inherits stdout, so a pid-only kill
would leave the pipe open and the reader blocked: only a group kill ends it.
Signals aimed at this session's own process group are refused and recorded.

Module under test: docker/base-image/agent_server/services/gemini_runtime.py
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from agent_server.services import gemini_runtime
from agent_server.services import process_registry as registry_module
from agent_server.utils import subprocess_pgroup

pytestmark = pytest.mark.unit

_REAL_POPEN = subprocess.Popen

# Reads the prompt, starts a grandchild that holds stdout open, records its pid,
# then writes nothing and sleeps.
_SILENT_WITH_GRANDCHILD = (
    "import subprocess, sys, time; sys.stdin.read(); "
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    "open(sys.argv[1], 'w').write(str(g.pid)); "
    "time.sleep(60)"
)
# Reads the prompt and exits at once, with one assistant line.
_ANSWERS_AT_ONCE = (
    "import sys; sys.stdin.read(); "
    "print('{\"type\": \"message\", \"role\": \"assistant\", \"content\": \"hi\"}', flush=True)"
)


class _GroupSignalRefused(RuntimeError):
    pass


@pytest.fixture
def refused_signals(monkeypatch):
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
def gemini(monkeypatch, refused_signals, tmp_path):
    registry = registry_module.ProcessRegistry()
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: registry)
    monkeypatch.setattr(registry_module, "get_process_registry", lambda: registry)
    # A unit test owns only its children, never the surrounding cgroup.
    monkeypatch.setattr(registry_module, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setattr(subprocess_pgroup, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setattr(gemini_runtime, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-gemini-subproc")
    monkeypatch.setattr(gemini_runtime, "_executor", pool)

    runtime = gemini_runtime.GeminiRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    terminations = []
    real_terminate = gemini_runtime._terminate_process_group

    def terminate(process, *args, **kwargs):
        terminations.append(process.pid)
        return real_terminate(process, *args, **kwargs)

    monkeypatch.setattr(gemini_runtime, "_terminate_process_group", terminate)

    state = {"children": [], "grandchild_file": tmp_path / "grandchild.pid"}

    def use(script):
        def popen(_cmd, **kwargs):
            child = _REAL_POPEN(
                [sys.executable, "-c", script, str(state["grandchild_file"])], **kwargs
            )
            state["children"].append(child)
            return child

        monkeypatch.setattr(subprocess, "Popen", popen)

    state.update(runtime=runtime, registry=registry, use=use, terminations=terminations)
    yield state

    for child in state["children"]:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    grandchild = _grandchild_pid(state)
    if grandchild and _alive(grandchild):
        os.kill(grandchild, signal.SIGKILL)
    pool.shutdown(wait=True, cancel_futures=True)


def _grandchild_pid(state):
    path = state["grandchild_file"]
    return int(path.read_text()) if path.exists() and path.read_text() else None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Our grandchild's parent is gone, so it may linger as a zombie of init.
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


async def _wait_for_grandchild(state):
    for _ in range(200):
        if _grandchild_pid(state):
            return _grandchild_pid(state)
        await asyncio.sleep(0.01)
    raise AssertionError("control: the child never started its grandchild")


def _assert_whole_group_ended(state):
    [child] = state["children"]
    assert child.poll() is not None, "the child outlived its timeout"
    assert state["terminations"] == [child.pid], "the timeout did not terminate the group"
    grandchild = _grandchild_pid(state)
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _alive(grandchild), "a descendant in the run's group survived the timeout"


@pytest.mark.asyncio
async def test_a_silent_task_run_is_ended_on_its_wall_clock_budget(gemini, refused_signals):
    gemini["use"](_SILENT_WITH_GRANDCHILD)
    started = time.monotonic()

    with pytest.raises(HTTPException) as error:
        await asyncio.wait_for(
            gemini["runtime"].execute_headless(prompt="hi", execution_id="ent1003-task", timeout_seconds=1),
            timeout=20,
        )

    assert error.value.status_code == 504
    assert "Task exceeded 1s timeout" in error.value.detail
    assert time.monotonic() - started < 10, "the budget was not enforced on a timer"
    _assert_whole_group_ended(gemini)
    assert not gemini["registry"].is_execution_running("ent1003-task")
    assert refused_signals == []


@pytest.mark.asyncio
async def test_a_silent_chat_run_is_ended_on_the_chat_timeout(gemini, refused_signals, monkeypatch):
    monkeypatch.setattr(gemini_runtime, "_load_guardrails", lambda: {"execution_timeout_sec": 1})
    gemini["use"](_SILENT_WITH_GRANDCHILD)
    started = time.monotonic()

    with pytest.raises(HTTPException) as error:
        await asyncio.wait_for(
            gemini["runtime"].execute(prompt="hi", execution_id="ent1003-chat"),
            timeout=20,
        )

    assert error.value.status_code == 504
    assert error.value.detail == "Chat execution timed out after 1 seconds", (
        "the same status and detail as the Claude Code chat path"
    )
    assert time.monotonic() - started < 10, "the chat run had no wall-clock bound"
    _assert_whole_group_ended(gemini)
    assert not gemini["registry"].is_execution_running("ent1003-chat")
    assert refused_signals == []


def test_the_chat_budget_defaults_to_the_platform_chat_timeout(monkeypatch):
    seen = {}

    class _Recorder(gemini_runtime._WallClockDeadline):
        def __init__(self, process, pgid, seconds, label):
            seen["seconds"] = seconds
            raise RuntimeError("stop after the budget is chosen")

    monkeypatch.setattr(gemini_runtime, "_WallClockDeadline", _Recorder)
    monkeypatch.setattr(gemini_runtime, "_load_guardrails", lambda: {})
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: registry_module.ProcessRegistry())
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _REAL_POPEN(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], **kw
    ))
    runtime = gemini_runtime.GeminiRuntime()

    with pytest.raises(HTTPException):
        asyncio.run(runtime._execute_chat_once("hi", None, None, "ent1003-default", None))

    assert seen["seconds"] == gemini_runtime._DEFAULT_EXECUTION_TIMEOUT_SEC == 1800


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["task", "chat"])
async def test_a_run_that_finishes_in_time_is_never_signalled(gemini, refused_signals, monkeypatch, path):
    """The timer is stopped before the child is reaped, so it never fires late
    at a pgid whose leader is gone."""
    monkeypatch.setattr(gemini_runtime, "_load_guardrails", lambda: {"execution_timeout_sec": 1})
    gemini["use"](_ANSWERS_AT_ONCE)

    if path == "task":
        call = gemini["runtime"].execute_headless(prompt="hi", execution_id="ent1003-ok", timeout_seconds=1)
    else:
        call = gemini["runtime"].execute(prompt="hi", execution_id="ent1003-ok")
    response = (await asyncio.wait_for(call, timeout=20))[0]

    assert response == "hi"
    await asyncio.sleep(1.5)  # past the budget
    assert gemini["terminations"] == [], "the deadline signalled a run that had already finished"
    [child] = gemini["children"]
    assert child.returncode == 0
    assert refused_signals == []
