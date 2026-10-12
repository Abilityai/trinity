"""/edge-cases 2026-10-06 — Hypothesis properties for `services/loop_service.py`.

The edges file (`test_ec_loop_service_edges.py`) pins discrete boundaries; this
file states the INVARIANTS and lets Hypothesis hunt for the counter-example,
deliberately feeding the extremes #1155 was about (NaN, ±inf, negative, zero,
int costs) and full-unicode / whitespace-heavy text:

  P1  `_DerivedState` equals an independent oracle over any run history
      (accumulated cost, failed_runs, consecutive failures, no-progress chain);
  P2  the accumulator is total, finite, non-negative and monotone under append
      — no cost value can poison it (NaN) or refund it (negative);
  P3  `_fingerprint` is whitespace-invariant and word-boundary preserving;
  P4  `_render_template` never injects more than 2000 chars of history and
      never re-expands a placeholder that arrived INSIDE the previous response;
  P5  `_parse_iso` is total (never raises) and round-trips the ISO-Z form;
  P6  failure policy / promotion / deadline helpers agree with their oracles;
  P7  end to end, the loop stops at exactly the iteration the budget oracle
      predicts (boundary-only gate, `>=`, max_runs first);
  P8  end to end, a continue-mode loop aborts at exactly the run the
      consecutive-failure oracle predicts, and promotes correctly otherwise.

P7/P8 drive the real `LoopService` over a small in-memory DB (same surface as
`test_2523_loops_terminal_driven._DB`; deliberately not imported).
"""
from __future__ import annotations

import asyncio
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from services import loop_service as ls  # noqa: E402

pytestmark = pytest.mark.unit

_TERMINAL = frozenset({"completed", "completed_with_errors", "stopped", "failed", "interrupted"})

costs = st.one_of(
    st.none(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.integers(min_value=-10, max_value=10),
    st.sampled_from([0.0, -0.0, 1e-12, -1e-12, float("nan"), float("inf"), float("-inf")]),
)
run_status = st.sampled_from(["completed", "failed", "skipped", "running", None])
responses = st.one_of(st.none(), st.sampled_from(["a", "a ", " a\n", "b", "", "a b", "ab"]), st.text())
runs_st = st.lists(
    st.fixed_dictionaries({"status": run_status, "cost": costs, "response": responses}),
    max_size=30,
)


def _usable(c) -> bool:
    return c is not None and math.isfinite(c) and c > 0


def _oracle(runs):
    acc = 0.0
    failed = 0
    streak = 0
    fps = []
    for r in runs:
        if r["status"] == "completed":
            streak = 0
            if _usable(r["cost"]):
                acc += r["cost"]
            fps.append(" ".join((r["response"] or "").split()))
        elif r["status"] == "failed":
            failed += 1
            streak += 1
    repeat = 0
    if fps:
        repeat = 1
        for prev, cur in zip(reversed(fps[:-1]), reversed(fps[1:])):
            if prev == cur:
                repeat += 1
            else:
                break
    return acc, failed, streak, repeat


# ---------------------------------------------------------------------------
# P1 / P2 — derived state
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(runs_st)
@example([{"status": "completed", "cost": float("nan"), "response": "x"}])
@example([{"status": "completed", "cost": -1.0, "response": "x"},
          {"status": "completed", "cost": 0.5, "response": "x"}])
@example([{"status": "completed", "cost": None, "response": "a"},
          {"status": "failed", "cost": 9.0, "response": None},
          {"status": "completed", "cost": None, "response": "a  "}])
def test_p1_derived_state_matches_oracle(runs):
    d = ls._DerivedState(runs)
    acc, failed, streak, repeat = _oracle(runs)
    assert d.accumulated_cost == acc
    assert d.failed_runs == failed
    assert d.consecutive_failures == streak
    assert d.repeat_count == repeat


@settings(max_examples=200, deadline=None)
@given(runs_st, st.fixed_dictionaries({"status": run_status, "cost": costs, "response": responses}))
@example([], {"status": "completed", "cost": float("-inf"), "response": None})
def test_p2_accumulator_total_finite_nonnegative_monotone(runs, extra):
    before = ls._DerivedState(runs).accumulated_cost
    after = ls._DerivedState(runs + [extra]).accumulated_cost
    for v in (before, after):
        assert not math.isnan(v)
        assert v >= 0.0
    assert after >= before


# ---------------------------------------------------------------------------
# P3 / P4 / P5 — pure helpers
# ---------------------------------------------------------------------------

_ws = st.text(alphabet=" \t\n\r\x0b\x0c  ", min_size=1, max_size=4)
# Lone surrogates ("Cs") are excluded on purpose: `_fingerprint` would raise
# UnicodeEncodeError on one, but every response it hashes is read back from a
# TEXT column, and neither sqlite3 nor psycopg can bind a lone surrogate — so no
# such response can reach it (reflection-loop verdict: unreachable input).
_word = st.text(alphabet=st.characters(blacklist_categories=("Zs", "Cc", "Zl", "Zp", "Cs")), min_size=1, max_size=8)


@settings(max_examples=200, deadline=None)
@given(st.lists(_word, max_size=6), st.lists(_ws, min_size=7, max_size=7))
def test_p3_fingerprint_whitespace_invariant(words, seps):
    canonical = " ".join(words)
    noisy = seps[0] + "".join(w + seps[i + 1] for i, w in enumerate(words))
    assert ls._fingerprint(noisy) == ls._fingerprint(canonical)


@settings(max_examples=200, deadline=None)
@given(st.lists(_word, min_size=2, max_size=5))
def test_p3_fingerprint_preserves_word_boundaries(words):
    joined, glued = " ".join(words), "".join(words)
    assert ls._fingerprint(joined) != ls._fingerprint(glued)


@settings(max_examples=200, deadline=None)
@given(st.one_of(st.none(), st.text()), st.integers(min_value=1, max_value=100))
@example("{{run}}" * 400, 7)
@example("x" * 2001, 1)
def test_p4_template_history_is_bounded_and_not_reexpanded(prev, run_number):
    out = ls._render_template("[{{previous_response}}]", run_number, prev)
    body = out[1:-1]
    assert len(body) <= ls.PREV_RESPONSE_TRUNCATE_CHARS
    assert body == (prev or "")[-ls.PREV_RESPONSE_TRUNCATE_CHARS:]


@settings(max_examples=200, deadline=None)
@given(st.text())
def test_p4_template_without_placeholders_is_identity(template):
    if "{{run}}" in template or "{{previous_response}}" in template:
        return
    assert ls._render_template(template, 3, "zzz") == template


@settings(max_examples=200, deadline=None)
@given(st.one_of(st.none(), st.text(), st.integers(), st.floats()))
@example("Z")
@example("2026-13-40T00:00:00Z")
def test_p5_parse_iso_is_total(value):
    out = ls._parse_iso(value)
    assert out is None or (isinstance(out, datetime) and out.tzinfo is None)


@settings(max_examples=200, deadline=None)
@given(st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1)))
def test_p5_parse_iso_round_trips_both_utc_spellings(dt):
    z = dt.isoformat(timespec="microseconds") + "Z"
    off = dt.isoformat(timespec="microseconds") + "+00:00"
    assert ls._parse_iso(z) == dt
    assert ls._parse_iso(off) == dt


# ---------------------------------------------------------------------------
# P6 — gate helpers
# ---------------------------------------------------------------------------


class _D:
    def __init__(self, streak=0, failed=0):
        self.consecutive_failures = streak
        self.failed_runs = failed


@settings(max_examples=200, deadline=None)
@given(
    st.sampled_from(["abort", "continue", None, "", "other"]),
    st.one_of(st.none(), st.integers(min_value=0, max_value=20)),
    st.integers(min_value=0, max_value=25),
)
def test_p6_abort_after_failure_oracle(policy, cap, streak):
    out = ls.LoopService._abort_after_failure(
        {"on_failure": policy, "max_consecutive_failures": cap}, _D(streak), 4, "err",
    )
    if (policy or "abort") != "continue":
        assert out == ("failed", "error", "Iteration 4: err")
    elif streak >= (cap or 3):
        assert out[0:2] == ("failed", "max_consecutive_failures")
    else:
        assert out is None


@settings(max_examples=100, deadline=None)
@given(st.sampled_from(["completed", "stopped", "failed"]), st.integers(min_value=0, max_value=5))
def test_p6_promote_only_natural_completion(status, failed):
    out = ls.LoopService._promote({}, status, _D(failed=failed))
    if status == "completed" and failed > 0:
        assert out == "completed_with_errors"
    else:
        assert out == status


@settings(max_examples=200, deadline=None)
@given(
    st.one_of(st.none(), st.integers(min_value=0, max_value=7 * 86400)),
    st.one_of(st.none(), st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1))),
)
def test_p6_deadline_oracle(max_duration, started):
    loop = {
        "max_duration_seconds": max_duration,
        "started_at": None if started is None else started.isoformat() + "Z",
    }
    got = ls.LoopService._deadline(loop)
    if not max_duration or started is None:
        assert got is None
    else:
        assert got == started + timedelta(seconds=max_duration)


# ---------------------------------------------------------------------------
# P7 / P8 — end-to-end over an in-memory DB
# ---------------------------------------------------------------------------


class _Exec:
    def __init__(self, eid):
        self.id, self.status, self.response, self.error = eid, "running", None, None
        self.cost, self.duration_ms = None, None


class _MemDB:
    def __init__(self):
        self.loops, self.runs, self.executions = {}, {}, {}

    def create_loop(self, **kw):
        lid = f"loop_{len(self.loops) + 1}"
        self.loops[lid] = {
            "id": lid, "status": "queued", "runs_completed": 0, "failed_runs": 0,
            "stop_reason": None, "last_response": None, "error": None,
            "started_at": None, "next_run_at": None, "stop_requested_at": None, **kw,
        }
        self.runs[lid] = []
        return dict(self.loops[lid])

    def get_loop(self, lid):
        return dict(self.loops[lid]) if lid in self.loops else None

    def mark_loop_running(self, lid):
        if self.loops[lid]["status"] == "queued":
            self.loops[lid].update(status="running",
                                   started_at=datetime.utcnow().isoformat() + "Z")

    def update_loop_progress(self, lid, *, runs_completed, last_response, failed_runs=None):
        self.loops[lid].update(runs_completed=runs_completed, last_response=last_response)
        if failed_runs is not None:
            self.loops[lid]["failed_runs"] = failed_runs

    def finalize_loop(self, lid, *, status, stop_reason, error=None, failed_runs=None):
        self.loops[lid].update(status=status, stop_reason=stop_reason, error=error)
        if failed_runs is not None:
            self.loops[lid]["failed_runs"] = failed_runs

    def claim_loop_advance(self, lid, n):
        row = self.loops.get(lid)
        if row is None or row["status"] in _TERMINAL or row["runs_completed"] != n - 1:
            return False
        row["runs_completed"] = n
        return True

    def schedule_loop_next_run(self, lid, at):
        self.loops[lid]["next_run_at"] = at

    def start_loop_run(self, lid, n, *, execution_id=None):
        rid = f"{lid}_{n}_{len(self.runs[lid])}"
        self.runs[lid].append({"id": rid, "loop_id": lid, "run_number": n,
                               "execution_id": execution_id, "status": "running",
                               "response": None, "cost": None})
        return rid

    def finalize_loop_run(self, rid, **kw):
        # CAS on status='running', as the real one (#3316).
        for runs in self.runs.values():
            for r in runs:
                if r["id"] == rid:
                    if r["status"] != "running":
                        return False
                    r.update({k: v for k, v in kw.items() if not (k == "execution_id" and v is None)})
                    return True
        return False

    def list_loop_runs(self, lid):
        return [dict(r) for r in sorted(self.runs.get(lid, []), key=lambda r: r["run_number"])]

    def get_loop_run_by_execution(self, eid):
        for runs in self.runs.values():
            for r in runs:
                if r["execution_id"] == eid:
                    return dict(r)
        return None

    def create_task_execution(self, **kw):
        eid = f"e{len(self.executions) + 1}"
        self.executions[eid] = _Exec(eid)
        return self.executions[eid]

    def get_execution(self, eid):
        return self.executions.get(eid)

    def update_execution_status(self, *, execution_id, status, result=None, **_):
        self.executions[execution_id].status = getattr(status, "value", status)
        return True

    def get_gate_requests_by_origin_executions(self, ids):
        return {}


def _drive(script, **start_kw):
    """Run a loop to completion; `script[i]` = (status, cost) of run i+1."""
    db = _MemDB()

    class _TS:
        n = 0

        async def execute_task(self, **kw):
            i = _TS.n
            _TS.n += 1
            status, cost = script[i] if i < len(script) else ("success", 0.0)
            row = db.executions[kw["execution_id"]]
            # Distinct responses so #1157 never interferes with P7/P8.
            row.status, row.response, row.cost = status, f"resp-{i}", cost
            row.error = None if status == "success" else "boom"
            return type("R", (), {"status": status})()

    old = (ls.db, ls.get_task_execution_service, ls._websocket_manager)
    ls.db, ls.get_task_execution_service, ls._websocket_manager = db, (lambda: _TS()), None
    ls._inflight_dispatches.clear()
    try:
        async def go():
            row = await ls.LoopService().start_loop(
                agent_name="a", message_template="m", **start_kw)
            for _ in range(20000):
                if not ls._inflight_dispatches:
                    break
                await asyncio.sleep(0)
            return row["id"]

        lid = asyncio.run(go())
        return db.get_loop(lid), _TS.n
    finally:
        ls.db, ls.get_task_execution_service, ls._websocket_manager = old
        ls._inflight_dispatches.clear()


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(costs, min_size=1, max_size=12),
    st.integers(min_value=1, max_value=12),
    st.one_of(st.floats(min_value=1e-6, max_value=50, allow_nan=False), st.just(float("inf"))),
)
@example([0.25, 0.25, 0.25], 5, 0.5)
@example([float("nan"), float("inf"), -3.0, 0.1], 6, 0.1)
@example([5.0], 1, 0.01)
def test_p7_budget_stops_exactly_where_the_oracle_says(cost_list, max_runs, budget):
    script = [("success", c) for c in cost_list]
    loop, calls = _drive(script, max_runs=max_runs, max_cost_usd=budget,
                         no_progress_threshold=0)
    acc, stop_at = 0.0, None
    for k in range(1, max_runs + 1):
        c = cost_list[k - 1] if k - 1 < len(cost_list) else 0.0
        if _usable(c):
            acc += c
        if k == max_runs:
            break
        if acc >= budget:
            stop_at = k
            break
    if stop_at is None:
        assert (loop["status"], loop["stop_reason"]) == ("completed", "max_runs_reached")
        assert loop["runs_completed"] == max_runs == calls
    else:
        assert (loop["status"], loop["stop_reason"]) == ("stopped", "budget_exhausted")
        assert loop["runs_completed"] == stop_at == calls


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(st.booleans(), min_size=1, max_size=12),
    st.integers(min_value=1, max_value=12),
    st.integers(min_value=1, max_value=5),
)
@example([False, False, True, False, False], 5, 2)
def test_p8_failure_streak_oracle(oks, max_runs, cap):
    script = [("success" if ok else "failed", 0.0) for ok in oks]
    loop, calls = _drive(script, max_runs=max_runs, on_failure="continue",
                         max_consecutive_failures=cap, no_progress_threshold=0)
    streak = failed = 0
    abort_at = None
    for k in range(1, max_runs + 1):
        ok = oks[k - 1] if k - 1 < len(oks) else True
        if ok:
            streak = 0
        else:
            streak += 1
            failed += 1
            if streak >= cap:
                abort_at = k
                break
    if abort_at is not None:
        assert (loop["status"], loop["stop_reason"]) == ("failed", "max_consecutive_failures")
        assert loop["runs_completed"] == abort_at == calls
    else:
        expected = "completed_with_errors" if failed else "completed"
        assert (loop["status"], loop["stop_reason"]) == (expected, "max_runs_reached")
        assert loop["failed_runs"] == failed
        assert calls == max_runs
