"""Unit tests for gemini_runtime pipe-drop classification (#474).

`GeminiRuntime.execute_headless` spawns the Gemini CLI as a subprocess and
streams JSON over stdin/stdout. If the child Gemini process exits before
consuming its stdin — e.g. an auth abort, GOOGLE_API_KEY misconfig surfaced
inside the CLI, or upstream cancellation — `subprocess.Popen` (or the
later stdin write) raises BrokenPipeError / ConnectionResetError.

Pre-#474 the surface was the same as the Claude path: ERROR-level
"Execution error: [Errno 32] Broken pipe" + HTTP 500. The branch adds a
parallel except arm in gemini_runtime.py:728-739 that:
  - logs at INFO (not ERROR — operator-confusing noise the branch removes)
  - returns HTTP 502 with a descriptive subprocess-closed detail
  - uses 502 (not 503) to avoid the SUB-003 auto-switch collision in
    task_execution_service.py:628.

The Claude path has tests/unit/test_headless_executor_pipe_drop.py.
This file is the parallel for the Gemini path — found missing during QA
of the #474 follow-up branch.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException

from agent_server.services import gemini_runtime  # noqa: E402


@pytest.fixture
def runtime_with_stubbed_popen(monkeypatch):
    """Build a `GeminiRuntime` whose `execute_headless` reaches the outer
    except block on whatever exception the caller plants in `subprocess.Popen`.

    Strategy:
      - Force `is_available()` to True so the early 503 guard doesn't fire.
      - Set GEMINI_API_KEY so the early 500 guard doesn't fire.
      - Caller patches `gemini_runtime.subprocess.Popen` to raise.
    """
    runtime = gemini_runtime.GeminiRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    return runtime


@pytest.mark.asyncio
async def test_gemini_headless_pipe_close_logs_info_and_raises_502(
    runtime_with_stubbed_popen, monkeypatch, caplog
):
    """BrokenPipeError raised from inside the execute_headless try-block
    must be logged at INFO (not ERROR — that's the operator-confusing log
    #474 removes for the Claude path) and surfaced as HTTP 502 with a
    descriptive `Agent subprocess closed` detail. 502 (not 503) because
    503 collides with SUB-003 auto-switch."""

    def _raise_pipe(*_a, **_kw):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", _raise_pipe)

    with caplog.at_level(logging.INFO, logger=gemini_runtime.logger.name):
        with pytest.raises(HTTPException) as exc_info:
            await runtime_with_stubbed_popen.execute_headless(prompt="hello")

    assert exc_info.value.status_code == 502
    assert "Agent subprocess closed" in str(exc_info.value.detail)

    pipe_logs = [
        r for r in caplog.records
        if "Subprocess pipe closed" in r.getMessage()
    ]
    assert len(pipe_logs) == 1, (
        f"expected one INFO 'Subprocess pipe closed' log, got "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    assert pipe_logs[0].levelname == "INFO"

    # The misleading legacy "Execution error" ERROR must NOT appear for
    # this code path.
    error_exec = [
        r for r in caplog.records
        if r.levelname == "ERROR" and "Execution error" in r.getMessage()
    ]
    assert error_exec == []


@pytest.mark.asyncio
async def test_gemini_headless_connection_reset_also_raises_502(
    runtime_with_stubbed_popen, monkeypatch, caplog
):
    """Parallel coverage for ConnectionResetError — same handler, same
    contract."""

    def _raise_reset(*_a, **_kw):
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", _raise_reset)

    with caplog.at_level(logging.INFO, logger=gemini_runtime.logger.name):
        with pytest.raises(HTTPException) as exc_info:
            await runtime_with_stubbed_popen.execute_headless(prompt="hello")

    assert exc_info.value.status_code == 502
    pipe_logs = [
        r for r in caplog.records
        if "Subprocess pipe closed" in r.getMessage()
    ]
    assert any(r.levelname == "INFO" for r in pipe_logs)


@pytest.mark.asyncio
async def test_gemini_headless_other_exception_still_raises_500(
    runtime_with_stubbed_popen, monkeypatch, caplog
):
    """Regression: a generic RuntimeError still logs at ERROR and raises
    500. The new BrokenPipeError branch must not absorb non-pipe failures
    (otherwise auth misconfig or container-state bugs get downgraded to
    'Bad Gateway' and operators chase the wrong ghost)."""

    def _raise_runtime(*_a, **_kw):
        raise RuntimeError("something else went wrong")

    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", _raise_runtime)

    with caplog.at_level(logging.ERROR, logger=gemini_runtime.logger.name):
        with pytest.raises(HTTPException) as exc_info:
            await runtime_with_stubbed_popen.execute_headless(prompt="hello")

    assert exc_info.value.status_code == 500
    assert "Task execution error" in str(exc_info.value.detail)

    error_exec = [
        r for r in caplog.records
        if r.levelname == "ERROR" and "Execution error" in r.getMessage()
    ]
    assert len(error_exec) == 1


@pytest.mark.asyncio
async def test_gemini_headless_timeout_still_raises_504(
    runtime_with_stubbed_popen, monkeypatch
):
    """Regression: TimeoutError still raises 504, not 502. The branch's
    pipe-drop handler is layered ABOVE the catch-all Exception, but BELOW
    the TimeoutError handler — the new arm must not steal timeout
    classification."""

    def _raise_timeout(*_a, **_kw):
        raise TimeoutError("subprocess wait exceeded")

    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", _raise_timeout)

    with pytest.raises(HTTPException) as exc_info:
        await runtime_with_stubbed_popen.execute_headless(prompt="hello")

    assert exc_info.value.status_code == 504
