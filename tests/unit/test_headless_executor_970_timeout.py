"""Unit tests for #970: headless 2h false-timeout fixes.

Two defects shared one symptom (a scheduled task wedged for the full
``effective_timeout`` — up to 2h — then marked FAILED, with the slot held
the whole time):

  1. The executor finalized on process EXIT, not on claude's response. When
     claude emitted ``{"type":"result"}`` (turn definitively over) but the
     process lingered in teardown (e.g. a stdio MCP child holding the pipe),
     ``process.wait`` burned the entire budget on already-complete work.
     Fix: ``result_seen`` event + early-completion in the wait loop, with
     ``return_code`` forced to 0 so finalize treats it as the success it is.

  2. A hung stdio MCP ``tools/call`` (Claude Code has NO per-MCP-tool
     timeout) leaves an open ``tool_use`` with no ``tool_result`` forever.
     Fix: ``_open_tool_exceeding`` stall watchdog in the same loop.

These tests cover the pure watchdog signal and the four wait-loop branches
(early-completion, stall, natural-exit regression, budget-timeout
regression) by driving ``_run_headless_subprocess`` with a fake Popen.

Module under test:
    docker/base-image/agent_server/services/headless_executor.py
"""
from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

# conftest.py preloads the real agent_server namespace package.
from agent_server.models import ExecutionLogEntry  # noqa: E402
from agent_server.services import headless_executor as he  # noqa: E402


# ---------------------------------------------------------------------------
# Context + fake-subprocess helpers
# ---------------------------------------------------------------------------

def _ctx(**over) -> "he.HeadlessRunContext":
    base = dict(
        cmd=["claude", "--print"],
        task_session_id="t-970",
        task_start_iso="2026-01-01T00:00:00Z",
        effective_timeout=5,
        images=None,
        prompt="hello",
    )
    base.update(over)
    return he.HeadlessRunContext(**base)


def _tool_use_entry(tool_id: str, tool: str) -> ExecutionLogEntry:
    return ExecutionLogEntry(id=tool_id, type="tool_use", tool=tool,
                             timestamp="2026-01-01T00:00:00Z")


def _tool_result_entry(tool_id: str, tool: str) -> ExecutionLogEntry:
    return ExecutionLogEntry(id=tool_id, type="tool_result", tool=tool,
                             success=True, timestamp="2026-01-01T00:00:01Z")


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
    term = MagicMock()
    monkeypatch.setattr(he, "_terminate_process_group", term)
    monkeypatch.setattr(he, "_drain_bounded", MagicMock())
    monkeypatch.setattr(he, "_WAIT_POLL_S", 0.05)
    return monkeypatch, term


def _install_popen(monkeypatch, fake):
    monkeypatch.setattr(he.subprocess, "Popen", lambda *a, **k: fake)


# ---------------------------------------------------------------------------
# _open_tool_exceeding (pure)
# ---------------------------------------------------------------------------

class TestOpenToolExceeding:
    def test_none_when_no_tools(self):
        assert he._open_tool_exceeding(_ctx(), 300) is None

    def test_returns_name_when_open_tool_older_than_limit(self):
        ctx = _ctx()
        ctx.execution_log = [_tool_use_entry("t1", "mcp__x__hang")]
        ctx.tool_start_times = {"t1": datetime.now() - timedelta(seconds=400)}
        assert he._open_tool_exceeding(ctx, 300) == "mcp__x__hang"

    def test_none_when_open_tool_younger_than_limit(self):
        ctx = _ctx()
        ctx.execution_log = [_tool_use_entry("t1", "mcp__x__hang")]
        ctx.tool_start_times = {"t1": datetime.now() - timedelta(seconds=10)}
        assert he._open_tool_exceeding(ctx, 300) is None

    def test_none_when_tool_completed(self):
        """A tool with a matching tool_result is closed — never a stall, even if old."""
        ctx = _ctx()
        ctx.execution_log = [
            _tool_use_entry("t1", "mcp__x__hang"),
            _tool_result_entry("t1", "mcp__x__hang"),
        ]
        ctx.tool_start_times = {"t1": datetime.now() - timedelta(seconds=400)}
        assert he._open_tool_exceeding(ctx, 300) is None

    def test_only_the_open_tool_is_flagged(self):
        ctx = _ctx()
        ctx.execution_log = [
            _tool_use_entry("done", "Bash"),
            _tool_result_entry("done", "Bash"),
            _tool_use_entry("stuck", "mcp__x__hang"),
        ]
        now = datetime.now()
        ctx.tool_start_times = {
            "done": now - timedelta(seconds=400),
            "stuck": now - timedelta(seconds=400),
        }
        assert he._open_tool_exceeding(ctx, 300) == "mcp__x__hang"

    # --- #1369: scope to mcp__*, exempt built-ins, None-safe --------------

    def test_builtin_tool_exempt_even_when_long_open(self):
        """A long-running built-in (Bash / Task sub-agent) is NOT a stall — it is
        bounded by execution_timeout, not the per-tool watchdog (#1369)."""
        for builtin in ("Bash", "Task", "Read", "WebFetch"):
            ctx = _ctx()
            ctx.execution_log = [_tool_use_entry("t1", builtin)]
            ctx.tool_start_times = {"t1": datetime.now() - timedelta(seconds=4000)}
            assert he._open_tool_exceeding(ctx, 300) is None, builtin

    def test_builtin_open_but_mcp_stalled_flags_only_mcp(self):
        ctx = _ctx()
        ctx.execution_log = [
            _tool_use_entry("b", "Bash"),
            _tool_use_entry("m", "mcp__srv__call"),
        ]
        now = datetime.now()
        ctx.tool_start_times = {
            "b": now - timedelta(seconds=4000),   # long Bash, exempt
            "m": now - timedelta(seconds=400),    # stalled mcp, caught
        }
        assert he._open_tool_exceeding(ctx, 300) == "mcp__srv__call"

    def test_blank_or_non_mcp_name_not_flagged(self):
        """A non-mcp__ name (incl. an empty string) is exempt and must not crash
        — the `(e.tool or "")` guard keeps a falsy name from raising."""
        ctx = _ctx()
        ctx.execution_log = [_tool_use_entry("t1", "")]
        ctx.tool_start_times = {"t1": datetime.now() - timedelta(seconds=400)}
        assert he._open_tool_exceeding(ctx, 300) is None


# ---------------------------------------------------------------------------
# _resolve_stall_limit_s (env-configurable ceiling, #1369)
# ---------------------------------------------------------------------------

class TestResolveStallLimit:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("AGENT_TOOL_STALL_LIMIT_S", raising=False)
        assert he._resolve_stall_limit_s() == he._STALL_LIMIT_DEFAULT_S

    def test_default_when_blank(self, monkeypatch):
        monkeypatch.setenv("AGENT_TOOL_STALL_LIMIT_S", "   ")
        assert he._resolve_stall_limit_s() == he._STALL_LIMIT_DEFAULT_S

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_TOOL_STALL_LIMIT_S", "600")
        assert he._resolve_stall_limit_s() == 600.0

    def test_invalid_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AGENT_TOOL_STALL_LIMIT_S", "not-a-number")
        assert he._resolve_stall_limit_s() == he._STALL_LIMIT_DEFAULT_S

    def test_non_finite_falls_back_to_default(self, monkeypatch):
        # inf/nan parse as floats but would silently make the watchdog
        # inert/disabled — they must fall back to the default, not pass through.
        for bad in ("inf", "-inf", "Infinity", "nan", "NaN"):
            monkeypatch.setenv("AGENT_TOOL_STALL_LIMIT_S", bad)
            assert he._resolve_stall_limit_s() == he._STALL_LIMIT_DEFAULT_S, bad

    def test_zero_and_negative_pass_through_as_disabled_sentinel(self, monkeypatch):
        # <=0 is returned verbatim; the call site (not the helper) gates disable.
        monkeypatch.setenv("AGENT_TOOL_STALL_LIMIT_S", "0")
        assert he._resolve_stall_limit_s() == 0.0
        monkeypatch.setenv("AGENT_TOOL_STALL_LIMIT_S", "-5")
        assert he._resolve_stall_limit_s() == -5.0


# ---------------------------------------------------------------------------
# Wait-loop branches via _run_headless_subprocess
# ---------------------------------------------------------------------------

_RESULT_LINE = (
    '{"type":"result","subtype":"success","total_cost_usd":0.01,'
    '"num_turns":1,"result":"done"}\n'
)
_TOOL_USE_LINE = (
    '{"type":"assistant","message":{"content":[{"type":"tool_use",'
    '"id":"t1","name":"mcp__x__hang","input":{}}]}}\n'
)


class TestWaitLoop:
    def test_early_completion_forces_success_when_process_wont_exit(
        self, patched_loop, caplog
    ):
        """Result emitted + process lingers → finalize early with return_code=0
        (not a signal-exit failure), and the teardown is killed."""
        monkeypatch, term = patched_loop
        _install_popen(monkeypatch, _FakePopen([_RESULT_LINE], never_exits=True))
        ctx = _ctx(effective_timeout=10)

        with caplog.at_level(logging.WARNING, logger=he.logger.name):
            he._run_headless_subprocess(ctx)

        assert ctx.return_code == 0
        assert ctx.result_seen.is_set()
        assert term.called  # teardown terminated
        assert any("finalizing early" in r.getMessage() for r in caplog.records)

    def test_stall_watchdog_raises_for_open_tool(self, patched_loop, caplog):
        """An open mcp__ tool_use with no result for >stall_limit raises
        TimeoutExpired well before the budget (#970)."""
        monkeypatch, _term = patched_loop
        # A tiny POSITIVE limit makes the open mcp tool trip immediately.
        # (0 means *disabled* under #1369 — see the disable-path test.)
        monkeypatch.setenv("AGENT_TOOL_STALL_LIMIT_S", "0.01")
        _install_popen(monkeypatch, _FakePopen([_TOOL_USE_LINE], never_exits=True))
        ctx = _ctx(effective_timeout=30)  # large — must NOT be what fires

        start = time.monotonic()
        with caplog.at_level(logging.ERROR, logger=he.logger.name):
            with pytest.raises(subprocess.TimeoutExpired):
                he._run_headless_subprocess(ctx)
        elapsed = time.monotonic() - start

        assert elapsed < 5.0, "stall should fire fast, not at the 30s budget"
        assert any("stalled with no result" in r.getMessage() for r in caplog.records)
        assert ctx.termination_reason == "stall_no_output"

    def test_stall_watchdog_disabled_falls_to_budget(self, patched_loop, caplog):
        """#1369: AGENT_TOOL_STALL_LIMIT_S=0 disables the per-tool watchdog; an
        open mcp__ tool is NOT stall-killed — only the execution_timeout budget
        bounds it (termination_reason 'max_duration', not 'stall_no_output')."""
        monkeypatch, _term = patched_loop
        monkeypatch.setenv("AGENT_TOOL_STALL_LIMIT_S", "0")
        monkeypatch.setattr(he, "_WAIT_POLL_S", 0.1)
        _install_popen(monkeypatch, _FakePopen([_TOOL_USE_LINE], never_exits=True))
        ctx = _ctx(effective_timeout=0.3)  # the budget is what must fire

        with caplog.at_level(logging.ERROR, logger=he.logger.name):
            with pytest.raises(subprocess.TimeoutExpired):
                he._run_headless_subprocess(ctx)

        assert ctx.termination_reason == "max_duration"
        assert ctx.stalled_tool is None
        assert any("timed out after" in r.getMessage() for r in caplog.records)

    def test_natural_exit_still_returns_real_code(self, patched_loop):
        """Regression: a process that exits on its own is unchanged."""
        monkeypatch, _term = patched_loop
        _install_popen(
            monkeypatch, _FakePopen([_RESULT_LINE], never_exits=False, returncode=0)
        )
        ctx = _ctx()
        he._run_headless_subprocess(ctx)
        assert ctx.return_code == 0

    def test_budget_timeout_still_fires_with_no_progress(self, patched_loop, caplog):
        """Regression: no result and no open tool → the overall budget still
        bounds the wait and raises TimeoutExpired."""
        monkeypatch, _term = patched_loop
        monkeypatch.setattr(he, "_WAIT_POLL_S", 0.1)
        _install_popen(monkeypatch, _FakePopen([], never_exits=True))
        ctx = _ctx(effective_timeout=0.3)

        with caplog.at_level(logging.ERROR, logger=he.logger.name):
            with pytest.raises(subprocess.TimeoutExpired):
                he._run_headless_subprocess(ctx)
        assert any("timed out after" in r.getMessage() for r in caplog.records)
