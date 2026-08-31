"""#2433 — the Gemini runtime registers its subprocess with the process
registry (it never did: `gemini_runtime.py` had two `Popen` sites and zero
`register()` calls), so:

- the backend watchdog's proof-of-life sees a Gemini turn instead of
  false-orphaning every run longer than the 60s grace;
- a cancel requested while the turn was pending is consumed at spawn;
- the handle is unregistered in a `finally`, even when the reader fails.

Module under test: docker/base-image/agent_server/services/gemini_runtime.py
Harness mirrors tests/unit/test_gemini_runtime_pipe_drop.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from agent_server.services import gemini_runtime  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(monkeypatch):
    rt = gemini_runtime.GeminiRuntime()
    monkeypatch.setattr(rt, "is_available", lambda: True)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(gemini_runtime, "kill_cgroup_orphans", lambda **k: 0)
    return rt


def _fake_process(*, wait_raises=None):
    proc = MagicMock()
    proc.pid = 99
    proc.stdout.readline.return_value = ""
    proc.stderr.read.return_value = ""
    if wait_raises is not None:
        proc.wait.side_effect = wait_raises
    else:
        proc.wait.return_value = 0
    proc.poll.return_value = 0
    proc.returncode = 0
    return proc


@pytest.mark.asyncio
async def test_headless_registers_under_execution_id_and_unregisters(runtime, monkeypatch):
    proc = _fake_process()
    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", lambda *a, **k: proc)
    registry = MagicMock()
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: registry)

    await runtime.execute_headless(prompt="hello", execution_id="exec-1")

    registry.register.assert_called_once()
    args, kwargs = registry.register.call_args
    assert args[0] == "exec-1"
    assert args[1] is proc
    assert kwargs["metadata"]["runtime"] == "gemini"
    registry.unregister.assert_called_once_with("exec-1")


@pytest.mark.asyncio
async def test_headless_unregisters_when_the_reader_fails(runtime, monkeypatch):
    proc = _fake_process(wait_raises=RuntimeError("reader died"))
    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", lambda *a, **k: proc)
    registry = MagicMock()
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: registry)

    with pytest.raises(HTTPException):
        await runtime.execute_headless(prompt="hello", execution_id="exec-2")

    registry.unregister.assert_called_once_with("exec-2")


@pytest.mark.asyncio
async def test_chat_registers_under_execution_id_and_unregisters(runtime, monkeypatch):
    proc = _fake_process()
    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", lambda *a, **k: proc)
    registry = MagicMock()
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: registry)

    await runtime.execute(prompt="hi", execution_id="exec-chat")

    assert registry.register.call_args.args[0] == "exec-chat"
    registry.unregister.assert_called_once_with("exec-chat")
