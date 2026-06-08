"""Unit tests for headless_executor pipe-drop classification (#474).

`execute_headless_task` wraps `_run_headless_subprocess` (which writes to the
spawned Claude process's stdin pipe). When the child process exits before
consuming its stdin — e.g. an auth abort, permission-mode validation kill,
or upstream cancellation — `process.stdin.write()` raises BrokenPipeError.

Pre-#474 this surfaced as `[Headless Task] Execution error: [Errno 32] Broken
pipe` at ERROR level + HTTP 500 with `"Task execution error: …"` detail —
misleading because the agent itself is healthy.

These tests verify the new behaviour:
  - BrokenPipeError / ConnectionResetError → INFO log + HTTP 502 with a
    descriptive subprocess-closed detail.
  - Any other exception → unchanged: ERROR log + HTTP 500.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import HTTPException

# Ensure agent_server namespace is wired by conftest before the import.
_REPO = Path(__file__).resolve().parent.parent.parent
_AGENT_SERVICES = (
    _REPO / "docker" / "base-image" / "agent_server" / "services"
)


from agent_server.services import headless_executor  # noqa: E402


@pytest.fixture
def patched_executor(monkeypatch):
    """Wire up `execute_headless_task` so it reaches the outer except block
    on whatever exception we plant in `_run_headless_subprocess`.

    Strategy:
      - Force `agent_state.claude_code_available = True` so the early 503
        guard doesn't fire.
      - Stub `_setup_headless_command` to return a minimal fake context
        (just `task_session_id` + `effective_timeout` are referenced before
        the executor runs).
      - Stub `get_process_registry` to a no-op so the `finally: unregister`
        doesn't blow up.
      - Caller plants `_run_headless_subprocess` to raise.
    """
    monkeypatch.setattr(
        headless_executor.agent_state, "claude_code_available", True
    )

    class _FakeCtx:
        task_session_id = "test-session-id"
        effective_timeout = 10  # outer wait_for=70s — never fires in unit test

        def terminate(self):  # noqa: D401
            return None

    def _fake_setup(**_kw):
        return _FakeCtx()

    monkeypatch.setattr(
        headless_executor, "_setup_headless_command", _fake_setup
    )

    class _FakeRegistry:
        def unregister(self, _id):
            return None

    monkeypatch.setattr(
        headless_executor, "get_process_registry", lambda: _FakeRegistry()
    )

    return headless_executor


@pytest.mark.asyncio
async def test_headless_task_pipe_close_logs_info_and_raises_502(
    patched_executor, monkeypatch, caplog
):
    """BrokenPipeError from the subprocess executor must be logged at INFO
    (not ERROR — that's the operator-confusing log #474 fixes) and
    surfaced as HTTP 502 with a descriptive `Agent subprocess closed`
    detail. 502 (not 503) because 503 collides with SUB-003 auth-switch."""

    def _raise_pipe(_ctx):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(
        patched_executor, "_run_headless_subprocess", _raise_pipe
    )

    with caplog.at_level(logging.INFO, logger=patched_executor.logger.name):
        with pytest.raises(HTTPException) as exc_info:
            await patched_executor.execute_headless_task(prompt="hello")

    assert exc_info.value.status_code == 502
    assert "Agent subprocess closed" in str(exc_info.value.detail)

    # Should have an INFO-level pipe-closed log, NOT the legacy ERROR.
    pipe_logs = [
        r for r in caplog.records
        if "Subprocess pipe closed" in r.getMessage()
    ]
    assert len(pipe_logs) == 1, (
        f"expected one INFO 'Subprocess pipe closed' log, got "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    assert pipe_logs[0].levelname == "INFO"

    # And the misleading legacy "Execution error" ERROR must NOT appear
    # for this code path.
    error_exec = [
        r for r in caplog.records
        if r.levelname == "ERROR" and "Execution error" in r.getMessage()
    ]
    assert error_exec == []


@pytest.mark.asyncio
async def test_headless_task_connection_reset_also_raises_502(
    patched_executor, monkeypatch, caplog
):
    """Parallel coverage for ConnectionResetError."""

    def _raise_reset(_ctx):
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(
        patched_executor, "_run_headless_subprocess", _raise_reset
    )

    with caplog.at_level(logging.INFO, logger=patched_executor.logger.name):
        with pytest.raises(HTTPException) as exc_info:
            await patched_executor.execute_headless_task(prompt="hello")

    assert exc_info.value.status_code == 502
    pipe_logs = [
        r for r in caplog.records
        if "Subprocess pipe closed" in r.getMessage()
    ]
    assert any(r.levelname == "INFO" for r in pipe_logs)


@pytest.mark.asyncio
async def test_headless_task_other_exception_still_raises_500(
    patched_executor, monkeypatch, caplog
):
    """Regression: generic RuntimeError still logs at ERROR and raises 500.
    The new BrokenPipeError branch must not absorb non-pipe failures."""

    def _raise_runtime(_ctx):
        raise RuntimeError("something else went wrong")

    monkeypatch.setattr(
        patched_executor, "_run_headless_subprocess", _raise_runtime
    )

    with caplog.at_level(logging.ERROR, logger=patched_executor.logger.name):
        with pytest.raises(HTTPException) as exc_info:
            await patched_executor.execute_headless_task(prompt="hello")

    assert exc_info.value.status_code == 500
    assert "Task execution error" in str(exc_info.value.detail)

    error_exec = [
        r for r in caplog.records
        if r.levelname == "ERROR" and "Execution error" in r.getMessage()
    ]
    assert len(error_exec) == 1
