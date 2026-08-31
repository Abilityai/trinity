"""Unit tests for #2127: a fan-out turn must not be killed mid-wait.

A `claude --print` turn that fans out to background subagents emits **one
`{"type":"result"}` line per turn segment** and deliberately stays alive between
them, because the CLI waits for background subagents/workflows ("their result is
part of the final output"). Trinity's #970 early-finalize treated *any* result
line as the definitive end of the run and SIGTERMed the process group 2s later
with ``return_code = 0`` — recording the turn as SUCCESS with the model's
"I'll wait for the notification" placeholder as its response, the real answer
never emitted, and the subagents killed.

Measured against Claude Code 2.1.220 in a real agent container:

    t+37.82s  result  "Fan-out started - waiting for notifications."
    t+37.82s  result  "Waiting for the second notification (BRAVO)"   <- stored
    t+40.32s  result  "RESULTS: ALPHA BRAVO"                          <- the answer
    t+40.72s  process exit

The kill lands <=t+39.82s, i.e. after the placeholder and before the answer.

The fix pairs `result_seen` with the CLI's own in-flight ledger
(`system/background_tasks_changed`, whose `tasks` array is a full snapshot) and
defers the finalize while work the CLI waits for is still listed. Background
*shells* (`local_bash`) are excluded: the CLI grants them only a ~5s grace and
kills them, so waiting on one would re-open #970's original problem domain.

Assertions here are on **end state** (which answer survives), not on the
mechanism — `docs/memory/learnings.md` (2026-07-21) records that a test which
asserts a trigger rather than the outcome green-lights the miss.

Module under test:
    docker/base-image/agent_server/services/headless_executor.py
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from unittest.mock import MagicMock

import pytest

# conftest.py preloads the real agent_server namespace package.
from agent_server.services import headless_executor as he  # noqa: E402


# ---------------------------------------------------------------------------
# Harness
#
# Deliberately NOT imported from test_headless_executor_970_timeout.py: sibling
# test modules evict `agent_server*` from sys.modules during collection
# (learnings.md 2026-07-07), so cross-test-module imports of a fixture that
# closes over the module object are a known flake source. ~40 duplicated lines
# buys a standalone file.
# ---------------------------------------------------------------------------

def _ctx(**over) -> "he.HeadlessRunContext":
    base = dict(
        cmd=["claude", "--print"],
        task_session_id="t-2127",
        task_start_iso="2026-01-01T00:00:00Z",
        effective_timeout=5,
        images=None,
        prompt="fan out",
    )
    base.update(over)
    return he.HeadlessRunContext(**base)


class _ScriptedPipe:
    """stdout stand-in that can pause between lines.

    Entries are either a ``str`` (emit immediately) or a ``(delay, str)`` tuple
    (sleep first). The pause is what lets the wait loop actually poll between
    turn segments, reproducing the real interleaving rather than delivering the
    whole stream before the first poll.
    """

    def __init__(self, entries):
        self._it = iter(entries)

    def readline(self):
        try:
            entry = next(self._it)
        except StopIteration:
            return ""
        if isinstance(entry, tuple):
            delay, entry = entry
            time.sleep(delay)
        return entry


class _FakePopen:
    def __init__(self, entries, *, never_exits=True, returncode=0):
        self.pid = 4242
        self.stdout = _ScriptedPipe(entries)
        self.stderr = _ScriptedPipe([])
        self.stdin = MagicMock()
        self._never_exits = never_exits
        self._returncode = returncode
        self.returncode = None

    def wait(self, timeout=None):
        if self._never_exits:
            time.sleep(min(0.02, timeout or 0.02))
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        self.returncode = self._returncode
        return self._returncode


@pytest.fixture
def patched_loop(monkeypatch):
    monkeypatch.setattr(he, "_capture_pgid", lambda _p: 4242)
    monkeypatch.setattr(  # #2433: model was_terminated — a bare MagicMock is truthy and reads as "cancelled before start"
        he, "get_process_registry", lambda: MagicMock(**{"was_terminated.return_value": False})
    )
    term = MagicMock()
    monkeypatch.setattr(he, "_terminate_process_group", term)
    monkeypatch.setattr(he, "_drain_bounded", MagicMock())
    monkeypatch.setattr(he, "_WAIT_POLL_S", 0.05)
    # Production idle ceiling is 300s (measured: a 9,840-char message generates
    # with 40.54s of stdout silence). Scale it down rather than removing it, so
    # these tests still exercise the real gate through the real resolver.
    monkeypatch.setenv("AGENT_IDLE_FINALIZE_S", "0.2")
    return monkeypatch, term


def _install_popen(monkeypatch, fake):
    monkeypatch.setattr(he.subprocess, "Popen", lambda *a, **k: fake)


def _result(text: str) -> str:
    return json.dumps({
        "type": "result", "subtype": "success",
        "total_cost_usd": 0.01, "num_turns": 1, "result": text,
    }) + "\n"


def _bg(*task_types: str) -> str:
    return json.dumps({
        "type": "system", "subtype": "background_tasks_changed",
        "tasks": [
            {"task_id": f"id{i}", "task_type": t, "description": "sweep"}
            for i, t in enumerate(task_types)
        ],
    }) + "\n"


# ---------------------------------------------------------------------------
# _count_waited_bg_tasks (pure)
# ---------------------------------------------------------------------------

class TestCountWaitedBgTasks:
    def test_empty_ledger_is_zero(self):
        assert he._count_waited_bg_tasks(json.loads(_bg())) == 0

    def test_counts_subagents(self):
        msg = json.loads(_bg("local_agent", "local_agent"))
        assert he._count_waited_bg_tasks(msg) == 2

    def test_background_shell_is_not_waited_for(self):
        """`claude -p` kills a background shell ~5s after the result rather than
        waiting for it (measured 5.09s), so it must never hold the finalize."""
        msg = json.loads(_bg("local_bash"))
        assert he._count_waited_bg_tasks(msg) == 0

    def test_mixed_counts_only_the_waited_ones(self):
        msg = json.loads(_bg("local_bash", "local_agent", "local_shell"))
        assert he._count_waited_bg_tasks(msg) == 1

    def test_unknown_type_counts_as_waited(self):
        """Denylist, not allowlist: an unrecognised type (a future workflow)
        must gate, because failing the other way silently restores #2127."""
        msg = json.loads(_bg("some_future_workflow"))
        assert he._count_waited_bg_tasks(msg) == 1

    def test_unknown_type_logs_once_per_process(self, caplog):
        he._seen_unknown_bg_task_types.discard("brand_new_type")
        msg = json.loads(_bg("brand_new_type"))
        with caplog.at_level(logging.INFO, logger=he.logger.name):
            he._count_waited_bg_tasks(msg)
            he._count_waited_bg_tasks(msg)
        hits = [r for r in caplog.records if "Unrecognised background task_type" in r.getMessage()]
        assert len(hits) == 1

    @pytest.mark.parametrize("payload", [
        {},                                   # no tasks key at all
        {"tasks": None},
        {"tasks": "not-a-list"},
        {"tasks": [None, 3, "x"]},            # entries that are not dicts
    ])
    def test_malformed_ledger_degrades_to_zero(self, payload):
        """0 == pre-#2127 behaviour. Treating an unreadable ledger as 'busy'
        would invent a hang class that never existed."""
        assert he._count_waited_bg_tasks(payload) == 0


# ---------------------------------------------------------------------------
# Wait-loop gate
# ---------------------------------------------------------------------------

class TestWaitLoopGate:
    def test_late_answer_survives_a_fan_out(self, patched_loop):
        """THE regression (run-2 replay): an intermediate segment result must not
        trigger the kill, so the LATE result is the one that survives."""
        monkeypatch, term = patched_loop
        _install_popen(monkeypatch, _FakePopen([
            _bg("local_agent"),                       # fan-out in flight
            _result("Waiting for the second notification (BRAVO)"),
            (0.45, _bg()),                            # ...work drains
            _result("RESULTS: ALPHA BRAVO"),          # the real answer
        ], never_exits=True))
        ctx = _ctx(effective_timeout=10)

        start = time.monotonic()
        he._run_headless_subprocess(ctx)
        elapsed = time.monotonic() - start

        # End state: the answer, not the placeholder.
        assert ctx.response_parts == ["RESULTS: ALPHA BRAVO"]
        assert ctx.return_code == 0
        # And it genuinely deferred rather than racing: the first result landed
        # almost immediately, so finalizing at the first poll (~0.05s) would have
        # returned long before the 0.45s drain.
        assert elapsed > 0.4, f"finalized too early ({elapsed:.2f}s) — gate did not hold"
        assert term.called

    def test_no_background_tasks_still_finalizes_early(self, patched_loop, caplog):
        """#970 preserved: with no fan-out the ledger stays 0 and the early
        finalize fires exactly as before."""
        monkeypatch, term = patched_loop
        _install_popen(monkeypatch, _FakePopen([_result("done")], never_exits=True))
        ctx = _ctx(effective_timeout=10)

        start = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=he.logger.name):
            he._run_headless_subprocess(ctx)
        elapsed = time.monotonic() - start

        assert ctx.return_code == 0
        assert ctx.waited_bg_tasks == 0
        assert elapsed < 2.0, "no fan-out must not be delayed by the gate"
        assert term.called
        assert any("finalizing early" in r.getMessage() for r in caplog.records)

    def test_background_shell_does_not_hold_the_finalize(self, patched_loop):
        """A lingering `local_bash` is #970's original problem domain — the CLI
        does not wait for it, and neither may we."""
        monkeypatch, term = patched_loop
        _install_popen(monkeypatch, _FakePopen([
            _bg("local_bash"),
            _result("server started"),
        ], never_exits=True))
        ctx = _ctx(effective_timeout=10)

        start = time.monotonic()
        he._run_headless_subprocess(ctx)
        elapsed = time.monotonic() - start

        assert ctx.return_code == 0
        assert ctx.waited_bg_tasks == 0
        assert elapsed < 2.0, "a background shell must not defer the finalize"
        assert term.called

    def test_pending_forever_still_bounded_by_the_budget(self, patched_loop):
        """The gate can defer the kill but never remove it: a subagent that never
        completes falls through to the effective_timeout backstop."""
        monkeypatch, _term = patched_loop
        _install_popen(monkeypatch, _FakePopen([
            _bg("local_agent"),
            _result("waiting"),
        ], never_exits=True))
        ctx = _ctx(effective_timeout=0.4)

        with pytest.raises(subprocess.TimeoutExpired):
            he._run_headless_subprocess(ctx)
        assert ctx.termination_reason == "max_duration"
        assert ctx.waited_bg_tasks == 1

    def test_deferral_logs_once_not_per_poll(self, patched_loop, caplog):
        monkeypatch, _term = patched_loop
        _install_popen(monkeypatch, _FakePopen([
            _bg("local_agent"),
            _result("waiting"),
            (0.4, _bg()),
            _result("final"),
        ], never_exits=True))
        ctx = _ctx(effective_timeout=10)

        with caplog.at_level(logging.INFO, logger=he.logger.name):
            he._run_headless_subprocess(ctx)

        hits = [r for r in caplog.records if "deferring finalize" in r.getMessage()]
        assert len(hits) == 1, f"expected one deferral log, got {len(hits)}"


# ---------------------------------------------------------------------------
# Honest status when the CLI itself stops waiting
# ---------------------------------------------------------------------------

class TestIdleGate:
    """#2127 part 2 — the ledger alone is NOT sufficient.

    Once a fan-out finishes, the ledger drains to `[]` while the model is still
    composing the synthesis those subagents were spawned to produce. Measured: 5
    subagents all reported by t+27.8s (ledger 0) and the real FINAL REPORT
    arrived at t+55.9s — a 28s window where a ledger-only gate stands aside and
    the kill lands on an interim "ECHO reported." line.
    """

    def test_answer_survives_while_the_model_is_still_composing(self, patched_loop):
        """The probe-5 shape: ledger EMPTY the whole time, output still flowing,
        the real deliverable arriving well after the first result."""
        monkeypatch, term = patched_loop
        _install_popen(monkeypatch, _FakePopen([
            _bg("local_agent"),
            _bg(),                                     # fan-out done, ledger empty
            _result("ECHO reported."),                 # interim segment flushes
            (0.1, '{"type":"system","subtype":"thinking_tokens"}\n'),
            (0.1, '{"type":"system","subtype":"thinking_tokens"}\n'),
            (0.1, '{"type":"system","subtype":"thinking_tokens"}\n'),
            _result("FINAL REPORT — the real deliverable"),
        ], never_exits=True))
        ctx = _ctx(effective_timeout=10)

        he._run_headless_subprocess(ctx)

        assert ctx.waited_bg_tasks == 0, "ledger is empty — the ledger gate cannot help here"
        assert ctx.response_parts == ["FINAL REPORT — the real deliverable"]
        assert term.called

    def test_silence_past_the_ceiling_finalizes(self, patched_loop, caplog):
        """#970 preserved: claude alive, ledger empty, stream gone quiet — that is
        the lingering-pipe case and it must still finalize."""
        monkeypatch, term = patched_loop
        _install_popen(monkeypatch, _FakePopen([_result("done")], never_exits=True))
        ctx = _ctx(effective_timeout=10)

        start = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=he.logger.name):
            he._run_headless_subprocess(ctx)
        elapsed = time.monotonic() - start

        assert ctx.return_code == 0
        assert elapsed >= 0.2, "must wait out the idle ceiling before finalizing"
        assert elapsed < 5.0, "must not burn the whole budget"
        assert any("finalizing early" in r.getMessage() for r in caplog.records)
        assert term.called

    def test_unparseable_output_still_counts_as_alive(self, patched_loop):
        """The stamp is taken before parsing: garbage on stdout is still proof the
        process is producing."""
        monkeypatch, _term = patched_loop
        _install_popen(monkeypatch, _FakePopen([
            _result("interim"),
            (0.1, "not json at all\n"),
            (0.1, "still not json\n"),
            _result("real answer"),
        ], never_exits=True))
        ctx = _ctx(effective_timeout=10)

        he._run_headless_subprocess(ctx)
        assert ctx.response_parts == ["real answer"]


class TestKnownBound:
    def test_short_timeout_agents_lose_the_970_early_finalize(self, monkeypatch):
        """DOCUMENTED TRADE-OFF, pinned so it cannot surprise anyone later.

        The idle ceiling (300s) is measured against how long a healthy run can be
        silent, and is independent of the agent's budget. So when
        `effective_timeout` is BELOW the ceiling, the deadline always fires first
        and #970's early finalize is effectively disabled for that agent: a
        lingering-pipe run that used to finalize at result+2s with a SUCCESS now
        burns its budget and raises, becoming a 504.

        Accepted deliberately rather than clamped to a fraction of the budget: the
        cost here is an honest failure on a pathological path, whereas a clamp
        re-admits silent truncation on exactly the agents least able to afford it,
        and silent truncation is the entire defect this issue is about. A
        deployment that runs short timeouts and cares more about #970 recovery can
        lower `AGENT_IDLE_FINALIZE_S`.
        """
        monkeypatch.setattr(he, "_capture_pgid", lambda _p: 4242)
        monkeypatch.setattr(  # #2433: model was_terminated — a bare MagicMock is truthy and reads as "cancelled before start"
            he, "get_process_registry", lambda: MagicMock(**{"was_terminated.return_value": False})
        )
        monkeypatch.setattr(he, "_terminate_process_group", MagicMock())
        monkeypatch.setattr(he, "_drain_bounded", MagicMock())
        monkeypatch.setattr(he, "_WAIT_POLL_S", 0.05)
        monkeypatch.setenv("AGENT_IDLE_FINALIZE_S", "5")     # >> the 0.4s budget
        _install_popen(monkeypatch, _FakePopen([_result("done")], never_exits=True))
        ctx = _ctx(effective_timeout=0.4)

        with pytest.raises(subprocess.TimeoutExpired):
            he._run_headless_subprocess(ctx)
        assert ctx.termination_reason == "max_duration"


class TestResolveIdleFinalize:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("AGENT_IDLE_FINALIZE_S", raising=False)
        assert he._resolve_idle_finalize_s() == he._IDLE_FINALIZE_DEFAULT_S

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_IDLE_FINALIZE_S", "45")
        assert he._resolve_idle_finalize_s() == 45.0

    @pytest.mark.parametrize("bad", ["", "   ", "not-a-number", "inf", "nan", "0", "-5"])
    def test_invalid_or_disabling_values_fall_back(self, monkeypatch, bad):
        """No disable sentinel: <=0 would restore the exact #2127 kill."""
        monkeypatch.setenv("AGENT_IDLE_FINALIZE_S", bad)
        assert he._resolve_idle_finalize_s() == he._IDLE_FINALIZE_DEFAULT_S

    def test_default_has_headroom_over_the_measured_worst_silence(self):
        """Measured on Claude Code 2.1.220: a 9,840-char assistant message
        generates with 40.54s of stdout silence (no --include-partial-messages,
        so one message is one line). Anything near that truncates a healthy run."""
        assert he._IDLE_FINALIZE_DEFAULT_S >= 40.54 * 5


class TestBackgroundWaitCeiling:
    """#2127 part 2: the CLI stops waiting for background subagents after its own
    10-minute default, which silently truncates a fan-out that is still well
    inside the agent's configured budget."""

    def test_headless_spawn_derives_ceiling_from_the_timeout(self, patched_loop):
        monkeypatch, _term = patched_loop
        captured = {}

        def _popen(*a, **kw):
            captured.update(kw.get("env") or {})
            return _FakePopen([_result("done")], never_exits=False)

        monkeypatch.setattr(he.subprocess, "Popen", _popen)
        he._run_headless_subprocess(_ctx(effective_timeout=1800))

        assert captured.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS") == "1800000"

    def test_both_spawn_sites_set_the_ceiling(self):
        """Parity guard. The gate belongs only to the headless path (the chat path
        has no early-finalize), but BOTH spawn the same CLI and both inherit its
        10-minute wait ceiling. Applying an env fix to only the reported path is
        the recurring one-of-two-call-sites escape (#686, #1264)."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2] / "docker/base-image/agent_server/services"
        for name in ("headless_executor.py", "claude_code.py"):
            src = (root / name).read_text()
            assert "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS" in src, (
                f"{name} spawns `claude` without the #2127 background-wait "
                f"ceiling — a fan-out there is still truncated at 10 minutes"
            )


class TestPendingAtExitNotice:
    def _finalize(self, ctx):
        return he._finalize_headless_result(ctx)

    def test_notice_and_flag_when_work_was_still_pending(self):
        ctx = _ctx(return_code=0)
        ctx.response_parts = ["I'll wait for the notification and then write it up."]
        ctx.metadata.cost_usd = 0.42
        ctx.waited_bg_tasks = 3

        response, _log, metadata, _sid = self._finalize(ctx)

        assert response.startswith(he._BG_PENDING_NOTICE)
        assert "I'll wait for the notification" in response
        assert metadata.background_tasks_pending_at_exit == 3

    def test_clean_fan_out_gets_no_notice(self):
        ctx = _ctx(return_code=0)
        ctx.response_parts = ["CORPUS AUDIT COMPLETE"]
        ctx.metadata.cost_usd = 0.42
        ctx.waited_bg_tasks = 0

        response, _log, metadata, _sid = self._finalize(ctx)

        assert he._BG_PENDING_NOTICE not in response
        assert response == "CORPUS AUDIT COMPLETE"
        assert metadata.background_tasks_pending_at_exit == 0

    def test_empty_response_still_takes_the_160_path(self):
        """The notice must not make an empty result look populated — it is
        applied after the #160 empty-response branch, not before."""
        ctx = _ctx(return_code=0)
        ctx.response_parts = []
        ctx.metadata.cost_usd = 0.42        # clean exit -> #160 placeholder
        ctx.waited_bg_tasks = 2

        response, _log, _metadata, _sid = self._finalize(ctx)

        assert "context: fork" in response          # the #160 placeholder survived
        assert response.startswith(he._BG_PENDING_NOTICE)
