"""#2467 agent-side NEGATIVE CONTROLS — the agent stays byte-identical.

Deterministic replay of the killed-background-task failure shape: a
`local_bash` background task is in flight when `claude --print` exits; the CLI
kills it ~5s later and reports the kill in its own stdout stream
(`task_updated {"status":"killed"}` + `task_notification {"status":"stopped"}`).

The #2467 fix is BACKEND-side by design (`services/execution_integrity.py`
derives the structured record + notice from `execution_log` at terminal write
— the #1741 precedent, so it reaches every deployed agent image with no
rebuild). These tests therefore stay green as PERMANENT negative controls
pinning the agent-side contract the backend derivation depends on:

1. the agent's finalize/metadata/notice behaviour is UNCHANGED — the #2127
   marker stays scoped to waited types, no new agent-side field, no agent-side
   notice for a killed shell;
2. the kill lifecycle events ARE present, sanitized, in `ctx.raw_messages` —
   the exact list the agent returns as `execution_log`, i.e. the backend
   scan's input;
3. the ledger-widening "fix" is inert (the CLI drains the ledger BEFORE
   exiting), which is why the backend keys on the kill events instead.

Backend-side coverage: tests/unit/test_2467_turn_integrity.py (which
duplicates these stream shapes rather than importing them — sibling test
modules evict `agent_server*` from sys.modules, learnings 2026-07-07).

Stream shape transcribed from the evidence (sanitized):
  init(bypassPermissions) -> assistant text -> tool_use Bash (foreground,
  timeout) -> background_tasks_changed [local_bash] -> task_updated
  {is_backgrounded: true}  (harness promotion) -> tool_result -> assistant
  announcement -> result(is_error=false) -> background_tasks_changed [] ->
  task_updated {status: killed} -> task_notification {status: stopped} -> EOF.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from unittest.mock import MagicMock

import pytest

from agent_server.services import headless_executor as he

ANNOUNCEMENT = "Report delivery moved to background. Waiting for completion notification."


def _ctx(**over) -> "he.HeadlessRunContext":
    base = dict(
        cmd=["claude", "--print"],
        task_session_id="t-repro-bgkill",
        task_start_iso="2026-01-01T00:00:00Z",
        effective_timeout=30,
        images=None,
        prompt="deliver the report",
    )
    base.update(over)
    return he.HeadlessRunContext(**base)


class _ExhaustiblePipe:
    """stdout stand-in; flags exhaustion so the fake process can 'exit' at EOF,
    mirroring the real CLI (stdout EOF and process exit coincide)."""

    def __init__(self, entries):
        self._it = iter(entries)
        self.exhausted = threading.Event()

    def readline(self):
        try:
            return next(self._it)
        except StopIteration:
            self.exhausted.set()
            return ""


class _NaturalExitPopen:
    """Popen stand-in that exits ONLY after its stdout is fully consumed —
    the natural-exit path (the client's case), never the early-finalize one."""

    def __init__(self, entries, returncode=0):
        self.pid = 4242
        self.stdout = _ExhaustiblePipe(entries)
        self.stderr = _ExhaustiblePipe([])
        self.stdin = MagicMock()
        self._returncode = returncode
        self.returncode = None

    def wait(self, timeout=None):
        deadline = time.monotonic() + (timeout if timeout is not None else 0.05)
        while time.monotonic() < deadline:
            if self.stdout.exhausted.is_set():
                self.returncode = self._returncode
                return self._returncode
            time.sleep(0.005)
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)


def _j(obj) -> str:
    return json.dumps(obj) + "\n"


def _incident_stream(*, promoted_by_timeout: bool):
    """The incident tail. promoted_by_timeout=True is the Incident-A shape
    (foreground Bash promoted mid-call); False is Incident-B
    (model passed run_in_background=true up front)."""
    tool_id = "toolu_repro01"
    bash_input = {
        "command": "python3 scripts/deliver_report.py --markdown out/report.md",
        "description": "Deliver report",
        "timeout": 120000,
    }
    if not promoted_by_timeout:
        bash_input["run_in_background"] = True

    events = [
        _j({"type": "system", "subtype": "init", "session_id": "sess-repro",
            "permissionMode": "bypassPermissions"}),
        _j({"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-4-5",
            "content": [{"type": "text", "text": "Now delivering the report."}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}}),
        _j({"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": "Bash",
                         "input": bash_input}]}}),
        _j({"type": "system", "subtype": "background_tasks_changed",
            "tasks": [{"task_id": "bg1", "task_type": "local_bash",
                       "description": "Deliver report"}]}),
    ]
    if promoted_by_timeout:
        # Harness promotion signal, present only in the Incident-A shape.
        events.append(_j({"type": "system", "subtype": "task_updated",
                          "task_id": "bg1", "patch": {"is_backgrounded": True}}))
    events += [
        _j({"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id,
                         "content": "Command running in background with ID bg1"}]}}),
        _j({"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": ANNOUNCEMENT}]}}),
        _j({"type": "result", "subtype": "success", "is_error": False,
            "stop_reason": "end_turn", "terminal_reason": "completed",
            "total_cost_usd": 1.9083, "duration_ms": 746590, "num_turns": 25,
            "result": ANNOUNCEMENT, "session_id": "sess-repro"}),
        # --- the CLI stops waiting: ledger drains, then the kill is reported ---
        _j({"type": "system", "subtype": "background_tasks_changed", "tasks": []}),
        _j({"type": "system", "subtype": "task_updated", "task_id": "bg1",
            "patch": {"status": "killed", "end_time": 1788170254093}}),
        _j({"type": "system", "subtype": "task_notification", "task_id": "bg1",
            "status": "stopped", "output_file": "/home/developer/.tmp/tasks/bg1.output"}),
    ]
    return events


@pytest.fixture
def quiet_runtime(monkeypatch):
    monkeypatch.setattr(he, "_capture_pgid", lambda _p: 4242)
    monkeypatch.setattr(
        he, "get_process_registry",
        lambda: MagicMock(**{"was_terminated.return_value": False}),
    )
    monkeypatch.setattr(he, "_terminate_process_group", MagicMock())
    monkeypatch.setattr(he, "_drain_bounded", MagicMock(return_value="completed"))
    monkeypatch.setattr(he, "_WAIT_POLL_S", 0.05)
    # Do NOT lower AGENT_IDLE_FINALIZE_S: with the 300s default the #2127
    # early-finalize can never fire inside this test, proving the kill/exit is
    # entirely the CLI's own (natural-exit path) — as in production.
    return monkeypatch


def _run(stream) -> "he.HeadlessRunContext":
    ctx = _ctx()
    fake = _NaturalExitPopen(stream)
    orig_popen = he.subprocess.Popen
    he.subprocess.Popen = lambda *a, **k: fake
    try:
        he._run_headless_subprocess(ctx)
    finally:
        he.subprocess.Popen = orig_popen
    assert fake.stdout.exhausted.is_set(), "harness bug: stream not fully consumed"
    return ctx


class TestAgentSideStaysByteIdentical:
    """The agent-side contract the backend derivation depends on, pinned.

    Before #2467 each of these passes documented the bug; now they pin that
    the fix deliberately changed NOTHING here — the kill stays invisible to
    agent-side structured metadata (the backend owns the record), while the
    events stay present in the transcript the backend reads."""

    @pytest.mark.parametrize("promoted", [True, False],
                             ids=["incidentA-tool-timeout-promotion",
                                  "incidentB-run_in_background"])
    def test_killed_local_bash_records_clean_success_with_no_marker(
            self, quiet_runtime, promoted):
        ctx = _run(_incident_stream(promoted_by_timeout=promoted))
        response, raw_messages, metadata, _sid = he._finalize_headless_result(ctx)

        # 1. The stored answer is the model's announcement, verbatim.
        assert response == ANNOUNCEMENT
        # 2. Clean success — nothing downstream will classify this as a failure.
        assert ctx.return_code == 0
        assert metadata.error_type is None
        # 3. The #2192/#2127 marker did NOT fire (scoped to waited types).
        assert metadata.background_tasks_pending_at_exit == 0
        assert he._BG_PENDING_NOTICE not in response
        # 4. The truth IS in the transcript (what the backend stores as
        #    execution_log) ...
        kills = [m for m in raw_messages
                 if m.get("subtype") == "task_updated"
                 and (m.get("patch") or {}).get("status") == "killed"]
        stops = [m for m in raw_messages
                 if m.get("subtype") == "task_notification"
                 and m.get("status") == "stopped"]
        assert kills and stops, "kill events must be present in the raw stream"
        # 5. ... and NOWHERE in the structured metadata: the kill leaves no
        #    queryable trace at all.
        assert "killed" not in json.dumps(metadata.model_dump())

    def test_promotion_vs_requested_is_indistinguishable_in_the_record(
            self, quiet_runtime):
        """The is_backgrounded promotion patch (harness promoted a FOREGROUND
        command) is discarded: both shapes finalize to identical structured
        records, so an operator cannot separate 'fire-and-forget by choice'
        from 'foreground work silently moved and killed'."""
        ctx_a = _run(_incident_stream(promoted_by_timeout=True))
        resp_a, _raw_a, meta_a, _ = he._finalize_headless_result(ctx_a)
        ctx_b = _run(_incident_stream(promoted_by_timeout=False))
        resp_b, _raw_b, meta_b, _ = he._finalize_headless_result(ctx_b)

        assert resp_a == resp_b
        dump_a, dump_b = meta_a.model_dump(), meta_b.model_dump()
        for volatile in ("execution_id", "session_id"):
            dump_a.pop(volatile, None), dump_b.pop(volatile, None)
        assert dump_a == dump_b

    def test_naive_fix_widening_the_denylist_still_misses_the_kill(
            self, quiet_runtime, monkeypatch):
        """The handoff's proposed fix ('count non-waited types too') would NOT
        work: the CLI empties the ledger (background_tasks_changed []) BEFORE
        exiting, so even with local_bash counted as waited, finalize reads 0.
        The fix must key on the kill lifecycle events, not the ledger."""
        monkeypatch.setattr(he, "_NON_WAITED_BG_TASK_TYPES", frozenset())
        ctx = _run(_incident_stream(promoted_by_timeout=True))
        _resp, _raw, metadata, _sid = he._finalize_headless_result(ctx)

        assert metadata.background_tasks_pending_at_exit == 0  # still invisible
