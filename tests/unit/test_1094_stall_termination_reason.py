"""Unit tests for #1094: stall-watchdog kill must not be mislabeled as a
max-duration timeout.

The per-tool no-output stall watchdog and the max-duration budget are two
distinct termination paths that both raise ``subprocess.TimeoutExpired`` out
of ``_run_headless_subprocess``. Before #1094 the terminal 504 always claimed
"Task execution timed out after {effective_timeout} seconds" regardless of
which fired — so a 300s stall-kill at ~531s wall-clock was recorded as
"timed out after 2400 seconds", misleading operators into bumping the
execution timeout (the wrong knob).

Fix: the watchdog records ``ctx.termination_reason`` ("stall_no_output" vs
"max_duration") and ``ctx.stalled_tool`` before re-raising, and the
orchestrator builds a reason-specific 504 from those fields.

These tests assert the context-level signal (the load-bearing propagation).
The harness is intentionally self-contained (mirrors the #970 wait-loop
tests) rather than importing from the sibling test module — a bare
``from test_headless_executor_970_timeout import ...`` fails collection when
pytest imports this file alone (``tests/unit`` not yet on sys.path).

Module under test:
    docker/base-image/agent_server/services/headless_executor.py
"""
from __future__ import annotations

import subprocess
import time
from unittest.mock import MagicMock

import pytest

# conftest.py preloads the real agent_server namespace package.
from agent_server.services import headless_executor as he  # noqa: E402


_TOOL_USE_LINE = (
    '{"type":"assistant","message":{"content":[{"type":"tool_use",'
    '"id":"t1","name":"mcp__x__hang","input":{}}]}}\n'
)


def _ctx(**over) -> "he.HeadlessRunContext":
    base = dict(
        cmd=["claude", "--print"],
        task_session_id="t-1094",
        task_start_iso="2026-01-01T00:00:00Z",
        effective_timeout=5,
        images=None,
        prompt="hello",
    )
    base.update(over)
    return he.HeadlessRunContext(**base)


class _FakePipe:
    """Minimal stdout/stderr stand-in: readline() yields lines then '' (EOF)."""

    def __init__(self, lines):
        self._it = iter(lines)

    def readline(self):
        try:
            return next(self._it)
        except StopIteration:
            return ""


class _FakePopen:
    def __init__(self, stdout_lines, *, never_exits, returncode=0, wait_sleep=0.02):
        self.pid = 4242
        self.stdout = _FakePipe(stdout_lines)
        self.stderr = _FakePipe([])
        self.stdin = MagicMock()
        self._never_exits = never_exits
        self._returncode = returncode
        self._wait_sleep = wait_sleep
        self.returncode = None

    def wait(self, timeout=None):
        if self._never_exits:
            time.sleep(min(self._wait_sleep, timeout or self._wait_sleep))
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        self.returncode = self._returncode
        return self._returncode


@pytest.fixture
def patched_loop(monkeypatch):
    """Neutralise OS-level side effects so we test only the wait-loop logic."""
    monkeypatch.setattr(he, "_capture_pgid", lambda _p: 4242)
    monkeypatch.setattr(he, "get_process_registry", lambda: MagicMock())
    monkeypatch.setattr(he, "_terminate_process_group", MagicMock())
    monkeypatch.setattr(he, "_drain_bounded", MagicMock())
    monkeypatch.setattr(he, "_WAIT_POLL_S", 0.05)
    return monkeypatch


def _install_popen(monkeypatch, fake):
    monkeypatch.setattr(he.subprocess, "Popen", lambda *a, **k: fake)


def test_stall_sets_stall_no_output_reason(patched_loop):
    """An open tool_use silent past the stall limit records
    termination_reason='stall_no_output' and the offending tool name."""
    monkeypatch = patched_loop
    # Tiny POSITIVE limit trips the open mcp tool immediately (#1369: 0 disables).
    monkeypatch.setenv("AGENT_TOOL_STALL_LIMIT_S", "0.01")
    _install_popen(monkeypatch, _FakePopen([_TOOL_USE_LINE], never_exits=True))
    ctx = _ctx(effective_timeout=30)  # large — budget must NOT be what fires

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        he._run_headless_subprocess(ctx)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, "stall should fire fast, not at the 30s budget"
    assert ctx.termination_reason == "stall_no_output"
    assert ctx.stalled_tool == "mcp__x__hang"


def test_budget_exhaustion_sets_max_duration_reason(patched_loop):
    """No result and no open tool → the budget bounds the wait and records
    termination_reason='max_duration' with no stalled tool."""
    monkeypatch = patched_loop
    monkeypatch.setattr(he, "_WAIT_POLL_S", 0.1)
    _install_popen(monkeypatch, _FakePopen([], never_exits=True))
    ctx = _ctx(effective_timeout=0.3)

    with pytest.raises(subprocess.TimeoutExpired):
        he._run_headless_subprocess(ctx)

    assert ctx.termination_reason == "max_duration"
    assert ctx.stalled_tool is None
