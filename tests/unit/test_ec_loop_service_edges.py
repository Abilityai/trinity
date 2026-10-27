"""/edge-cases 2026-10-06 — boundary + race edges for `services/loop_service.py`.

Companion to `test_loop_service.py` (behaviour of the seven stop conditions on
an in-memory fake) and `test_2523_loops_terminal_driven.py` (the terminal-driven
properties). This file works what those two do not:

* the #1155 cost-accumulator boundaries the suite never hits — +inf, -inf,
  NEGATIVE and zero costs, an int cost, an accumulator landing EXACTLY on the
  budget (`>=`), a cost on a failed / skipped run, and every pairwise precedence
  of `budget_exhausted` against `max_runs_reached` / `user_stopped` /
  `deadline_exceeded` / `no_progress`;
* the never-executed defensive branches of the driver (`advance_on_terminal`
  with no id / a vanished loop / a raising read, a sweep read failure, a lost
  sweep claim, a reconcile that raises for one loop, `_dispatch_run` when the
  execution row cannot be created, `_spawn` with no running loop, the secret
  scrub and the failed fail-write on the dispatch-raise path);
* the CAS-guarded status transitions run against the REAL `db/loops.py` on a
  temp SQLite (`db_harness.db_backend`) — the claim/park/stop primitives are
  exactly what a fake reimplements, so the races here use the real SQL;
* restart-mid-loop and stop-while-running interleavings, including two that
  the reflection loop classified as REAL BUGS (strict-xfail, see the report
  the 2026-10-06 /edge-cases matrix).

Execution rows, the task service, the WebSocket and the runtime-secret store are
faked at the service seam (no Docker / network / Redis). Row numbers in the
test ids (`m12` …) refer to the matrix in the report.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

_BUG = "found by /edge-cases 2026-10-06"


# ---------------------------------------------------------------------------
# Harness — real loop rows (db/loops.py on temp SQLite), faked executions
# ---------------------------------------------------------------------------


class _Exec:
    def __init__(self, eid: str):
        self.id = eid
        self.status = "running"
        self.response = None
        self.error = None
        self.cost = None
        self.duration_ms = None


class _HybridDB:
    """`database.db`'s loop surface delegated to the REAL `LoopOperations`
    (so every CAS is the production SQL), plus an in-memory execution table.

    ``hooks[name]`` runs before the delegated call (raise from it to model a
    transient DB fault); ``gate_records`` answers the ent#751 lookup.
    """

    _NAMES = {
        "list_loop_runs": "list_runs",
        "get_loop_run_by_execution": "get_run_by_execution",
        "schedule_loop_next_run": "schedule_next_run",
    }

    def __init__(self):
        from db.loops import LoopOperations

        self.ops = LoopOperations()
        self.executions: dict[str, _Exec] = {}
        self.hooks: dict[str, Any] = {}
        self.gate_records: dict = {}
        self.create_returns_none = False

    def __getattr__(self, name):
        target = getattr(self.ops, self._NAMES.get(name, name))

        def _call(*a, **kw):
            hook = self.hooks.get(name)
            if hook is not None:
                hook(*a, **kw)
            return target(*a, **kw)

        return _call

    # executions
    def create_task_execution(self, **kw):
        if self.create_returns_none:
            return None
        eid = f"exec_{len(self.executions) + 1}"
        self.executions[eid] = _Exec(eid)
        return self.executions[eid]

    def get_execution(self, eid):
        hook = self.hooks.get("get_execution")
        if hook is not None:
            hook(eid)
        return self.executions.get(eid)

    def update_execution_status(self, *, execution_id, status, result=None, **_kw):
        hook = self.hooks.get("update_execution_status")
        if hook is not None:
            hook(execution_id)
        row = self.executions.get(execution_id)
        if row is None:
            return False
        row.status = getattr(status, "value", status)
        row.error = result.error if result is not None else None
        return True

    def get_gate_requests_by_origin_executions(self, ids):
        return {i: self.gate_records[i] for i in ids if i in self.gate_records}

    # test helpers
    def set_loop(self, loop_id, **values):
        from sqlalchemy import update

        from db.engine import get_engine
        from db.tables import agent_loops

        with get_engine().begin() as conn:
            conn.execute(update(agent_loops).where(agent_loops.c.id == loop_id).values(**values))


class _TaskService:
    """Scripted `execute_task`. Each entry: a dict (written onto the execution
    row as its terminal), the string ``"queued"`` (pull: row stays running), or
    an exception instance (raised). Past the script → success, cost 0.01."""

    def __init__(self, db: _HybridDB):
        self.db = db
        self.script: list = []
        self.calls: list = []

    async def execute_task(self, **kw):
        self.calls.append(kw)
        idx = len(self.calls) - 1
        step = self.script[idx] if idx < len(self.script) else {}
        if isinstance(step, BaseException):
            raise step
        if step == "queued":
            return _Res("queued")
        row = self.db.executions.get(kw["execution_id"])
        row.status = step.get("status", "success")
        row.response = step.get("response", f"resp {idx + 1}")
        row.error = step.get("error")
        row.cost = step.get("cost", 0.01)
        return _Res(row.status)


class _Res:
    def __init__(self, status):
        self.status = status


@pytest.fixture
def env(db_backend, monkeypatch):  # noqa: F811
    from services import loop_service as ls

    db = _HybridDB()
    ts = _TaskService(db)
    monkeypatch.setattr(ls, "db", db)
    monkeypatch.setattr(ls, "get_task_execution_service", lambda: ts)
    monkeypatch.setattr(ls, "_websocket_manager", None)
    monkeypatch.setattr(ls, "get_staged_values", lambda: [])
    from services import activity_service as act_mod

    monkeypatch.setattr(
        act_mod.activity_service, "close_execution_activity", AsyncMock(return_value=None)
    )
    ls._inflight_dispatches.clear()
    yield ls, db, ts
    ls._inflight_dispatches.clear()


def _run(coro):
    async def _driver():
        result = await coro
        from services import loop_service as ls

        for _ in range(20000):
            if not ls._inflight_dispatches:
                break
            await asyncio.sleep(0)
        return result

    return asyncio.run(_driver())


def _start(ls, **kw):
    kw.setdefault("agent_name", "a1")
    kw.setdefault("message_template", "m {{run}}")
    kw.setdefault("max_runs", 5)
    return _run(ls.LoopService().start_loop(**kw))["id"]


def _loop(db, lid):
    return db.get_loop(lid)


def _runs(db, lid):
    return db.list_loop_runs(lid)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds") + "Z"


# ---------------------------------------------------------------------------
# #1155 — cost accumulator boundaries (driven end to end)
# ---------------------------------------------------------------------------


class TestCostAccumulator:
    @pytest.mark.parametrize(
        "bad_cost",
        [
            pytest.param(float("inf"), id="m3-pos-inf"),
            pytest.param(float("-inf"), id="m4-neg-inf"),
            pytest.param(-5.0, id="m5-negative"),
            pytest.param(0.0, id="m6-zero"),
        ],
    )
    def test_unusable_cost_contributes_nothing(self, env, bad_cost):
        """A non-finite / non-positive cost on run 1 neither trips the budget
        (+inf would, `inf >= 0.05`) nor REFUNDS it (-5 would let 0.10 of real
        spend look like -4.90 and keep the loop running past its budget)."""
        ls, db, ts = env
        ts.script = [{"cost": bad_cost}, {"cost": 0.04}, {"cost": 0.04}, {"cost": 0.04}]
        lid = _start(ls, max_runs=5, max_cost_usd=0.05, no_progress_threshold=0)
        loop = _loop(db, lid)
        # run1 contributes 0; run2 → 0.04 (<0.05, continue); run3 → 0.08 ≥ 0.05.
        assert loop["stop_reason"] == "budget_exhausted"
        assert loop["runs_completed"] == 3
        assert len(ts.calls) == 3

    def test_m9_int_cost_counts(self, env):
        ls, db, ts = env
        ts.script = [{"cost": 1}, {"cost": 1}]
        lid = _start(ls, max_runs=5, max_cost_usd=2, no_progress_threshold=0)
        assert _loop(db, lid)["stop_reason"] == "budget_exhausted"
        assert _loop(db, lid)["runs_completed"] == 2

    def test_m12_accumulator_exactly_at_budget_stops(self, env):
        """`>=`, not `>`: 0.25 + 0.25 is exactly 0.5 in binary floating point."""
        ls, db, ts = env
        ts.script = [{"cost": 0.25}, {"cost": 0.25}, {"cost": 0.25}]
        lid = _start(ls, max_runs=5, max_cost_usd=0.5, no_progress_threshold=0)
        loop = _loop(db, lid)
        assert loop["stop_reason"] == "budget_exhausted"
        assert loop["runs_completed"] == 2

    def test_m13_just_below_budget_continues(self, env):
        ls, db, ts = env
        ts.script = [{"cost": 0.25}, {"cost": 0.2499}, {"cost": 0.0001}]
        lid = _start(ls, max_runs=5, max_cost_usd=0.5, no_progress_threshold=0)
        loop = _loop(db, lid)
        # 0.4999 < 0.5 → run 3; 0.5 → stop before run 4.
        assert loop["runs_completed"] == 3
        assert loop["stop_reason"] == "budget_exhausted"

    def test_m16_infinite_budget_never_trips(self, env, db_backend, request):
        if db_backend == "postgres":
            # Not pollution: on PostgreSQL the cost columns are float4 (`REAL`
            # is not translated to DOUBLE PRECISION), so writing 1e300 raises
            # NumericValueOutOfRange in finalize_loop_run and the loop never
            # records its stop reason. Strict, so it flags the fix landing.
            request.applymarker(pytest.mark.xfail(
                strict=True,
                reason="BUG: REAL cost columns are float4 on PostgreSQL — #3441",
            ))
        ls, db, ts = env
        ts.script = [{"cost": 1e300}] * 3
        lid = _start(ls, max_runs=3, max_cost_usd=float("inf"), no_progress_threshold=0)
        assert _loop(db, lid)["stop_reason"] == "max_runs_reached"

    def test_m7_failed_run_cost_is_not_counted(self, env):
        """Pinned pre-#2523 behaviour (the old runner accumulated on the success
        branch only). See the report: GET total_cost DOES include it — UNSPECIFIED."""
        ls, db, ts = env
        ts.script = [
            {"status": "failed", "cost": 10.0, "error": "boom"},
            {"cost": 0.01},
            {"cost": 0.01},
        ]
        lid = _start(
            ls, max_runs=3, max_cost_usd=0.05, on_failure="continue",
            no_progress_threshold=0,
        )
        loop = _loop(db, lid)
        assert loop["stop_reason"] == "max_runs_reached"
        assert loop["status"] == "completed_with_errors"
        assert _runs(db, lid)[0]["cost"] == 10.0  # persisted, just not budgeted

    def test_m11_inf_cost_warns_under_budget(self, env, caplog):
        ls, db, ts = env
        ts.script = [{"cost": float("inf")}]
        with caplog.at_level(logging.WARNING):
            _start(ls, max_runs=1, max_cost_usd=1.0)
        assert any("non-finite cost" in r.getMessage() for r in caplog.records)

    def test_m11b_no_warning_without_budget(self, env, caplog):
        ls, db, ts = env
        ts.script = [{"cost": None}]
        with caplog.at_level(logging.WARNING):
            _start(ls, max_runs=1)
        assert not any("toward the" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Stop-condition precedence at the budget boundary
# ---------------------------------------------------------------------------


class TestBudgetPrecedence:
    def test_m17_budget_crossed_on_final_run_reads_max_runs(self, env):
        ls, db, ts = env
        ts.script = [{"cost": 0.01}, {"cost": 99.0}]
        lid = _start(ls, max_runs=2, max_cost_usd=0.05, no_progress_threshold=0)
        loop = _loop(db, lid)
        assert (loop["status"], loop["stop_reason"]) == ("completed", "max_runs_reached")

    def test_m18_pending_stop_outranks_budget(self, env):
        ls, db, ts = env
        holder = {}

        async def _exec(**kw):
            ts.calls.append(kw)
            # The stop lands while run 1 is in flight; run 1 also blows the budget.
            db.request_loop_stop(holder["lid"])
            row = db.executions[kw["execution_id"]]
            row.status, row.response, row.cost = "success", "x", 99.0
            return _Res("success")

        ts.execute_task = _exec
        orig = db.create_task_execution

        def _create(**kw):
            holder.setdefault("lid", kw["fields"].loop_id)
            return orig(**kw)

        db.create_task_execution = _create
        lid = _start(ls, max_runs=5, max_cost_usd=0.05)
        assert _loop(db, lid)["stop_reason"] == "user_stopped"

    def test_m19_deadline_outranks_budget(self, env, monkeypatch):
        ls, db, ts = env

        class _Clock:
            now = datetime(2030, 1, 1)

            @classmethod
            def utcnow(cls):
                return cls.now

            fromisoformat = staticmethod(datetime.fromisoformat)

        async def _exec(**kw):
            ts.calls.append(kw)
            lid = kw["loop_id"]
            db.set_loop(lid, started_at=_iso(datetime(2030, 1, 1)))
            _Clock.now = datetime(2030, 1, 1) + timedelta(seconds=120)
            row = db.executions[kw["execution_id"]]
            row.status, row.response, row.cost = "success", "x", 99.0
            return _Res("success")

        monkeypatch.setattr(ls, "datetime", _Clock)
        ts.execute_task = _exec
        lid = _start(ls, max_runs=5, max_cost_usd=0.05, max_duration_seconds=60)
        assert _loop(db, lid)["stop_reason"] == "deadline_exceeded"

    def test_m20_no_progress_outranks_budget_on_the_same_run(self, env):
        """no_progress is a POST-run gate; budget is the NEXT-iteration gate."""
        ls, db, ts = env
        ts.script = [{"response": "same", "cost": 0.01}, {"response": "same", "cost": 99.0}]
        lid = _start(ls, max_runs=5, max_cost_usd=0.05, no_progress_threshold=2)
        assert _loop(db, lid)["stop_reason"] == "no_progress"

    def test_m48_stop_signal_outranks_a_pending_stop(self, env):
        """Stop-signal is deliberately UNGUARDED (loop_service.py:539)."""
        ls, db, ts = env

        async def _exec(**kw):
            ts.calls.append(kw)
            db.request_loop_stop(kw["loop_id"])
            row = db.executions[kw["execution_id"]]
            row.status, row.response, row.cost = "success", "ok DONE", 0.0
            return _Res("success")

        ts.execute_task = _exec
        lid = _start(ls, max_runs=5, stop_signal="DONE")
        loop = _loop(db, lid)
        assert (loop["status"], loop["stop_reason"]) == ("completed", "stop_signal_matched")

    def test_m78_max_runs_outranks_a_pending_stop_on_the_last_run(self, env):
        ls, db, ts = env

        async def _exec(**kw):
            ts.calls.append(kw)
            db.request_loop_stop(kw["loop_id"])
            row = db.executions[kw["execution_id"]]
            row.status, row.response, row.cost = "success", "r", 0.0
            return _Res("success")

        ts.execute_task = _exec
        lid = _start(ls, max_runs=1)
        assert _loop(db, lid)["stop_reason"] == "max_runs_reached"

    def test_m_promote_stop_signal_with_tolerated_failure(self, env):
        ls, db, ts = env
        ts.script = [{"status": "failed", "error": "e"}, {"response": "fin DONE"}]
        lid = _start(ls, max_runs=5, stop_signal="DONE", on_failure="continue")
        loop = _loop(db, lid)
        assert (loop["status"], loop["stop_reason"]) == (
            "completed_with_errors", "stop_signal_matched",
        )
        assert loop["failed_runs"] == 1


# ---------------------------------------------------------------------------
# Failure policy + no-progress chain through failures / skips
# ---------------------------------------------------------------------------


class TestFailureAndProgressChains:
    def test_m28_failure_between_identical_successes_keeps_the_chain(self, env):
        ls, db, ts = env
        ts.script = [
            {"response": "same"}, {"status": "failed", "error": "e"}, {"response": "same"},
        ]
        lid = _start(ls, max_runs=5, on_failure="continue", no_progress_threshold=2)
        loop = _loop(db, lid)
        assert loop["stop_reason"] == "no_progress"
        assert loop["runs_completed"] == 3

    def test_m22_skipped_without_gate_record_counts_as_failure(self, env):
        ls, db, ts = env
        ts.script = [{"status": "skipped", "error": "capacity"}]
        lid = _start(ls, max_runs=3)
        loop = _loop(db, lid)
        assert (loop["status"], loop["stop_reason"]) == ("failed", "error")
        assert _runs(db, lid)[0]["status"] == "failed"

    def test_m22b_gate_held_run_is_skipped_not_failed(self, env):
        ls, db, ts = env
        db.gate_records["exec_1"] = {"request_id": "g1"}
        ts.script = [{"status": "skipped", "error": "held"}]
        lid = _start(ls, max_runs=3, on_failure="continue")
        loop = _loop(db, lid)
        assert (loop["status"], loop["stop_reason"]) == ("stopped", "approval_required")
        assert loop["failed_runs"] == 0
        assert _runs(db, lid)[0]["status"] == "skipped"

    @pytest.mark.parametrize(
        "cap,fails_before_abort",
        [pytest.param(1, 1, id="m24-cap1"), pytest.param(2, 2, id="m24-cap2")],
    )
    def test_m24_consecutive_cap_boundary(self, env, cap, fails_before_abort):
        ls, db, ts = env
        ts.script = [{"status": "failed", "error": "e"}] * 5
        lid = _start(ls, max_runs=5, on_failure="continue", max_consecutive_failures=cap)
        loop = _loop(db, lid)
        assert loop["stop_reason"] == "max_consecutive_failures"
        assert loop["runs_completed"] == fails_before_abort

    def test_m26_failed_iteration_keeps_last_successful_response(self, env):
        ls, db, ts = env
        ts.script = [{"response": "first"}, {"status": "failed", "error": "e"}, {}]
        lid = _start(
            ls, max_runs=3, on_failure="continue",
            message_template="prev={{previous_response}}",
        )
        assert ts.calls[2]["message"] == "prev=first"
        assert _loop(db, lid)["status"] == "completed_with_errors"


# ---------------------------------------------------------------------------
# Defensive branches of the driver
# ---------------------------------------------------------------------------


class TestDefensiveBranches:
    @pytest.mark.parametrize("eid", [None, ""], ids=["m52-none", "m52-empty"])
    def test_advance_without_execution_id_is_a_noop(self, env, eid):
        ls, db, ts = env
        assert _run(ls.LoopService().advance_on_terminal(eid)) is False

    def test_m54_advance_for_a_vanished_loop_is_a_noop(self, env):
        ls, db, ts = env
        ts.script = ["queued"]
        lid = _start(ls, max_runs=2)
        from sqlalchemy import delete

        from db.engine import get_engine
        from db.tables import agent_loops

        with get_engine().begin() as conn:
            conn.execute(delete(agent_loops).where(agent_loops.c.id == lid))
        db.executions["exec_1"].status = "success"
        assert _run(ls.LoopService().advance_on_terminal("exec_1")) is False

    def test_m55_advance_never_raises(self, env):
        ls, db, ts = env

        def _boom(*a, **k):
            raise RuntimeError("db down")

        db.hooks["get_loop_run_by_execution"] = _boom
        assert _run(ls.LoopService().advance_on_terminal("exec_9")) is False

    def test_m64_sweep_read_failure_returns_zero(self, env):
        ls, db, ts = env

        def _boom(*a, **k):
            raise RuntimeError("db down")

        db.hooks["list_due_loops"] = _boom
        assert _run(ls.LoopService().dispatch_due_loops()) == 0

    def test_m65_sweep_lost_claim_dispatches_nothing(self, env):
        ls, db, ts = env
        ts.script = [{}, "queued"]
        lid = _start(ls, max_runs=3, delay_seconds=30)
        loop = _loop(db, lid)
        assert loop["next_run_at"]  # parked after run 1
        db.set_loop(lid, next_run_at=_iso(datetime.utcnow() - timedelta(seconds=1)))
        stale = db.list_due_loops(_iso(datetime.utcnow()))
        # Another worker claims first.
        assert db.claim_due_loop(lid, stale[0]["next_run_at"])
        orig_list = db.ops.list_due_loops
        db.ops.list_due_loops = lambda now, limit=100: stale
        try:
            assert _run(ls.LoopService().dispatch_due_loops()) == 0
        finally:
            db.ops.list_due_loops = orig_list
        assert len(ts.calls) == 1

    def test_m58_reconcile_read_failure_returns_zero(self, env):
        ls, db, ts = env

        def _boom(*a, **k):
            raise RuntimeError("db down")

        db.hooks["list_non_terminal_loops"] = _boom
        assert _run(ls.LoopService().reconcile_after_restart()) == 0

    def test_m59_one_bad_loop_does_not_stall_reconcile(self, env):
        ls, db, ts = env
        ts.script = ["queued", "queued"]
        bad = _start(ls, max_runs=2)
        good = _start(ls, max_runs=2)
        # good: lost its dispatch (no open run). bad: its re-arm raises.
        from sqlalchemy import delete

        from db.engine import get_engine
        from db.tables import agent_loop_runs

        with get_engine().begin() as conn:
            conn.execute(delete(agent_loop_runs).where(agent_loop_runs.c.loop_id == good))
        # #3317: reconcile reads a loop's runs inside `rearm_lost_dispatch` now,
        # so the fault goes there (an injection into `list_runs` would no
        # longer fire, and the test would pass without exercising the guard).
        orig = db.ops.rearm_lost_dispatch
        raised = []

        def _rearm(lid, **kw):
            if lid == bad:
                raised.append(lid)
                raise RuntimeError("row read failed")
            return orig(lid, **kw)

        db.ops.rearm_lost_dispatch = _rearm
        try:
            moved = _run(ls.LoopService().reconcile_after_restart())
        finally:
            db.ops.rearm_lost_dispatch = orig
        assert raised == [bad]
        assert moved == 1
        assert _loop(db, good)["next_run_at"]

    def test_m67_execution_row_not_created_fails_the_loop(self, env):
        ls, db, ts = env
        db.create_returns_none = True
        lid = _start(ls, max_runs=3)
        loop = _loop(db, lid)
        assert (loop["status"], loop["stop_reason"]) == ("failed", "error")
        assert "could not create the execution record" in loop["error"]
        assert ts.calls == []

    def test_m68_spawn_without_running_loop_closes_the_coroutine(self, env, caplog):
        ls, db, ts = env
        closed = {"v": False}

        async def _coro():
            return None

        c = _coro()
        orig_close = c.close

        class _Wrap:
            def __await__(self):
                return c.__await__()

            def close(self):
                closed["v"] = True
                orig_close()

        with caplog.at_level(logging.ERROR):
            ls._spawn(_Wrap())
        assert closed["v"]
        assert not ls._inflight_dispatches
        assert any("no running event loop" in r.getMessage() for r in caplog.records)

    def test_m70_dispatch_raise_scrubs_staged_secret_before_write(self, env, monkeypatch):
        ls, db, ts = env
        secret = "sk-test-staged-value-123"
        monkeypatch.setattr(ls, "get_staged_values", lambda: [secret])
        ts.script = [RuntimeError(f"upstream said {secret}")]
        lid = _start(ls, max_runs=2)
        err = db.executions["exec_1"].error
        assert secret not in err
        assert "RuntimeError" in err
        assert secret not in (_loop(db, lid)["error"] or "")

    def test_m71_failed_fail_write_still_advances(self, env):
        ls, db, ts = env

        def _boom(eid):
            raise RuntimeError("write failed")

        db.hooks["update_execution_status"] = _boom
        ts.script = [RuntimeError("dispatch crash")]
        lid = _start(ls, max_runs=2)
        # The row never reached FAILED (still "running") so _close_run reads a
        # non-success status → the run is failed and the abort-mode loop ends.
        loop = _loop(db, lid)
        assert (loop["status"], loop["stop_reason"]) == ("failed", "error")

    def test_m73_continue_on_unknown_loop(self, env):
        ls, db, ts = env
        assert _run(ls.LoopService()._continue_or_finalize("loop_nope")) is False

    @pytest.mark.parametrize(
        "status", ["completed", "completed_with_errors", "stopped", "failed", "interrupted"]
    )
    def test_m74_continue_on_terminal_loop_dispatches_nothing(self, env, status):
        ls, db, ts = env
        ts.script = ["queued"]
        lid = _start(ls, max_runs=3)
        db.finalize_loop(lid, status=status, stop_reason="x")
        assert _run(ls.LoopService()._continue_or_finalize(lid, parked=True)) is False
        assert len(ts.calls) == 1
        assert _loop(db, lid)["stop_reason"] == "x"

    def test_m76_rearmed_first_run_ignores_delay(self, env):
        """A re-armed queued loop's FIRST run is not delayed (next_run_number==1)."""
        ls, db, ts = env
        loop = db.create_loop(agent_name="a1", message_template="m", max_runs=2,
                              delay_seconds=300)
        assert _run(ls.LoopService()._continue_or_finalize(loop["id"])) is True
        row = _loop(db, loop["id"])
        assert row["status"] == "running" and row["started_at"]
        assert len(ts.calls) >= 1


# ---------------------------------------------------------------------------
# Deadline arithmetic at the boundary
# ---------------------------------------------------------------------------


class _TickClock:
    """`datetime` stand-in whose utcnow() walks a scripted list of instants."""

    instants: list = []
    fromisoformat = staticmethod(datetime.fromisoformat)

    @classmethod
    def utcnow(cls):
        return cls.instants.pop(0) if len(cls.instants) > 1 else cls.instants[0]


class TestDeadlineBoundary:
    T0 = datetime(2030, 1, 1)

    def _loop(self, **kw):
        base = {"max_duration_seconds": 60, "started_at": _iso(self.T0)}
        base.update(kw)
        return base

    @pytest.mark.parametrize(
        "offset,passed",
        [(-1e-6, False), (0.0, True), (1e-6, True)],
        ids=["m38-1us-before", "m38-exactly-at", "m38-1us-after"],
    )
    def test_m38_deadline_is_inclusive(self, env, monkeypatch, offset, passed):
        ls, db, ts = env
        _TickClock.instants = [self.T0 + timedelta(seconds=60 + offset)]
        monkeypatch.setattr(ls, "datetime", _TickClock)
        assert ls.LoopService._deadline_passed(self._loop()) is passed

    @pytest.mark.parametrize(
        "loop",
        [
            {"max_duration_seconds": 0, "started_at": "2030-01-01T00:00:00Z"},
            {"max_duration_seconds": None, "started_at": "2030-01-01T00:00:00Z"},
            {"max_duration_seconds": 60, "started_at": None},
            {"max_duration_seconds": 60, "started_at": "not-a-date"},
        ],
        ids=["m34-zero", "m34-null", "m35-unstarted", "m36-unparseable"],
    )
    def test_m34_36_no_deadline(self, env, loop):
        ls, db, ts = env
        assert ls.LoopService._deadline(loop) is None
        assert ls.LoopService._deadline_passed(loop) is False

    def test_m40_clock_crossing_between_gate_and_park_dispatches(self, env, monkeypatch):
        """Accepted residual: the deadline gate reads the clock 1us BEFORE the
        deadline, the park arithmetic 1us AFTER → remaining<=0, delay 0, and the
        next run is dispatched rather than parked. Equivalent in effect to a run
        that started 2us earlier (the spec bounds overshoot by one run either
        way), so this pins behaviour rather than flagging a bug."""
        ls, db, ts = env
        loop = db.create_loop(agent_name="a1", message_template="m", max_runs=3,
                              delay_seconds=30, max_duration_seconds=60)
        db.mark_loop_running(loop["id"])
        db.set_loop(loop["id"], started_at=_iso(self.T0), runs_completed=1)
        _TickClock.instants = [
            self.T0 + timedelta(seconds=60, microseconds=-1),  # _deadline_passed
            self.T0 + timedelta(seconds=60, microseconds=1),   # remaining
        ]
        monkeypatch.setattr(ls, "datetime", _TickClock)
        ts.script = ["queued"]
        assert _run(ls.LoopService()._continue_or_finalize(loop["id"])) is True
        assert _loop(db, loop["id"])["next_run_at"] is None
        assert len(ts.calls) == 1

    def test_module_entry_point_delegates(self, env):
        ls, db, ts = env
        assert _run(ls.advance_loop_on_terminal(None)) is False


# ---------------------------------------------------------------------------
# stop_loop — status transitions and CAS races (real SQL)
# ---------------------------------------------------------------------------


class TestStopRaces:
    def test_m41_stop_unknown_loop(self, env):
        ls, db, ts = env
        assert _run(ls.LoopService().stop_loop("loop_missing")) == "not_found"

    def test_m46_stop_is_idempotent_while_in_flight(self, env):
        ls, db, ts = env
        ts.script = ["queued"]
        lid = _start(ls, max_runs=3)
        svc = ls.LoopService()
        assert _run(svc.stop_loop(lid)) == "stopping"
        assert _run(svc.stop_loop(lid)) == "stopping"
        assert _loop(db, lid)["status"] == "running"  # in flight: not finalized
        # The in-flight run's terminal arrives → user_stopped, exactly once.
        db.executions["exec_1"].status = "success"
        assert _run(svc.advance_on_terminal("exec_1")) is True
        assert _run(svc.advance_on_terminal("exec_1")) is False
        loop = _loop(db, lid)
        assert (loop["status"], loop["stop_reason"]) == ("stopped", "user_stopped")
        assert _run(svc.stop_loop(lid)) == "already_done"

    def test_m47_stop_racing_the_sweep_claim_is_still_honoured(self, env):
        """The sweep claims the park first; stop_loop's own claim loses. The
        sweep's re-read sees the stamp and finalizes user_stopped, no dispatch."""
        ls, db, ts = env
        ts.script = [{}]
        lid = _start(ls, max_runs=3, delay_seconds=30)
        parked = _loop(db, lid)["next_run_at"]
        assert parked
        svc = ls.LoopService()
        # Interleave: stamp the stop, then the sweep worker wins the park claim
        # before stop_loop reaches its own claim.
        assert db.request_loop_stop(lid)
        assert db.claim_due_loop(lid, parked)
        assert db.claim_due_loop(lid, parked) is False  # stop_loop's claim loses
        assert _run(svc._continue_or_finalize(lid, parked=True)) is False
        loop = _loop(db, lid)
        assert (loop["status"], loop["stop_reason"]) == ("stopped", "user_stopped")
        assert len(ts.calls) == 1

    def test_m45_stop_on_queued_loop_is_honoured_on_rearm(self, env):
        ls, db, ts = env
        loop = db.create_loop(agent_name="a1", message_template="m", max_runs=2)
        svc = ls.LoopService()
        assert _run(svc.stop_loop(loop["id"])) == "stopping"
        assert _run(svc.reconcile_after_restart()) == 1
        db.set_loop(loop["id"], next_run_at=_iso(datetime.utcnow() - timedelta(seconds=1)))
        _run(svc.dispatch_due_loops())
        row = _loop(db, loop["id"])
        assert (row["status"], row["stop_reason"]) == ("stopped", "user_stopped")
        assert ts.calls == []

    def test_m50_late_terminal_after_abort_cannot_resurrect(self, env):
        ls, db, ts = env
        ts.script = ["queued"]
        lid = _start(ls, max_runs=3)
        db.finalize_loop(lid, status="failed", stop_reason="error")
        db.executions["exec_1"].status = "success"
        assert _run(ls.LoopService().advance_on_terminal("exec_1")) is False
        assert _loop(db, lid)["status"] == "failed"
        assert len(ts.calls) == 1


# ---------------------------------------------------------------------------
# _execution_is_live — the restart classifier
# ---------------------------------------------------------------------------


class TestExecutionIsLive:
    @pytest.mark.parametrize(
        "status,live",
        [
            ("queued", True), ("running", True), ("pending_retry", True),
            ("success", False), ("failed", False), ("cancelled", False),
            ("skipped", False),
        ],
    )
    def test_m62_string_and_enum_status(self, env, status, live):
        ls, db, ts = env
        from models import TaskExecutionStatus

        e = db.create_task_execution()
        e.status = status
        assert ls.LoopService._execution_is_live(e.id) is live
        e.status = TaskExecutionStatus(status)
        assert ls.LoopService._execution_is_live(e.id) is live

    @pytest.mark.parametrize("eid", [None, "", "exec_gone"])
    def test_m62_missing_counts_as_not_live(self, env, eid):
        ls, db, ts = env
        assert ls.LoopService._execution_is_live(eid) is False


# ---------------------------------------------------------------------------
# Restart mid-loop — REAL BUGS (strict xfail)
# ---------------------------------------------------------------------------


class TestRestartMidAdvance:
    def test_m56_fault_after_claim_is_recoverable_on_restart(self, env):
        ls, db, ts = env
        calls = {"n": 0}

        def _fault_once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")  # transient write fault

        db.hooks["finalize_loop_run"] = _fault_once
        ts.script = [{"response": "r1"}, {}, {}]
        lid = _start(ls, max_runs=3)
        svc = ls.LoopService()

        # Redelivery of the same terminal, then a full restart reconcile.
        _run(svc.advance_on_terminal("exec_1"))
        _run(svc.reconcile_after_restart())
        _run(svc.dispatch_due_loops())

        loop = _loop(db, lid)
        runs = _runs(db, lid)
        # "A restart loses nothing" (module docstring): run 1 must get closed and
        # the loop must make progress (dispatch run 2, or reach a terminal).
        assert runs[0]["status"] != "running"
        assert loop["status"] in ("completed", "completed_with_errors", "stopped", "failed") \
            or len(runs) >= 2

    def test_m56_repair_that_loses_the_close_does_not_dispatch(self, env):
        """#3316: the repair path must stay exactly-once. A repairing delivery
        whose run-row close is beaten by another advance (closed between its read
        and its write) must not dispatch run 2 — only the closer does."""
        ls, db, ts = env
        calls = {"n": 0}

        def _hook(run_id, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")  # strands run 1
            if calls["n"] == 2:
                # A concurrent advance closes run 1 first.
                db.ops.finalize_loop_run(run_id, **kw)

        db.hooks["finalize_loop_run"] = _hook
        ts.script = [{"response": "r1"}, "queued", "queued"]
        lid = _start(ls, max_runs=3)

        assert _run(ls.LoopService().advance_on_terminal("exec_1")) is False
        assert [r["run_number"] for r in _runs(db, lid)] == [1]
        assert len(ts.calls) == 1

    def test_m56_control_fault_after_finalize_run_is_rearmed(self, env):
        """Control for m56: the same fault one write LATER (update_loop_progress)
        leaves no open run, so reconcile re-arms and the loop completes."""
        ls, db, ts = env
        calls = {"n": 0}

        def _fault_once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")

        db.hooks["update_loop_progress"] = _fault_once
        ts.script = [{"response": "r1"}, {"response": "r2"}, {"response": "r3"}]
        lid = _start(ls, max_runs=3)
        svc = ls.LoopService()
        assert _run(svc.reconcile_after_restart()) == 1
        db.set_loop(lid, next_run_at=_iso(datetime.utcnow() - timedelta(seconds=1)))
        _run(svc.dispatch_due_loops())
        loop = _loop(db, lid)
        assert loop["stop_reason"] == "max_runs_reached"
        assert [r["run_number"] for r in _runs(db, lid)] == [1, 2, 3]

    def test_m61_reconcile_during_live_advance_double_dispatches(self, env, monkeypatch):
        ls, db, ts = env
        worker_b = ls.LoopService()
        fired = {"v": False}

        async def _broadcast(event):
            # Worker A has finalized run 1 and is about to dispatch run 2 —
            # exactly here worker B's boot-time reconcile reads the loop.
            if event.get("type") == "loop_run_completed" and not fired["v"]:
                fired["v"] = True
                await worker_b.reconcile_after_restart()

        monkeypatch.setattr(ls, "_broadcast", _broadcast)
        ts.script = [{"response": "r1"}, "queued", "queued"]  # run 2 stays in flight
        lid = _start(ls, max_runs=3)
        assert fired["v"]
        _run(worker_b.dispatch_due_loops())  # the 5s sweep tick

        numbers = [r["run_number"] for r in _runs(db, lid)]
        assert numbers.count(2) == 1, numbers
        assert len(ts.calls) == 2


# ---------------------------------------------------------------------------
# #3317 — one owner for run N+1: the dispatch claim, the locked re-arm, the grace
# ---------------------------------------------------------------------------
#
# Worker A advances run N to run N+1 in separate commits; between its close of
# run N and its claim of run N+1 the loop reads exactly like a lost dispatch.
# Worker B's boot-time reconcile can land there. These tests put B into that
# window at each place it can open (the terminal path, `start_loop`, the sweep
# path) and with each kind of stale read, on the real SQL. "Worker B" is a second
# `LoopService`; where A is inside a synchronous DB call, B runs on its own event
# loop in a second thread, as a second process would.


def _on_another_worker(make_coro):
    """Run ``make_coro()`` on a fresh event loop in a second thread, and wait for
    the dispatches it spawned there (``asyncio.run`` would cancel them)."""
    import concurrent.futures

    from services import loop_service as ls

    async def _drive():
        result = await make_coro()
        me = asyncio.get_running_loop()
        for _ in range(20000):
            if not any(
                t.get_loop() is me and not t.done() for t in list(ls._inflight_dispatches)
            ):
                break
            await asyncio.sleep(0)
        return result

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda: asyncio.run(_drive())).result()


class _SnapshotReadAs:
    """Make reconcile's snapshot read return ``snap`` (a read taken earlier)."""

    def __init__(self, db, snap):
        self.db, self.snap = db, snap

    def __enter__(self):
        self.db.ops.list_non_terminal_loops = lambda: self.snap

    def __exit__(self, *exc):
        del self.db.ops.list_non_terminal_loops


def _record_rearms(db) -> list:
    """Spy on ``rearm_lost_dispatch``: one ``(loop_id, outcome)`` per call."""
    seen: list = []
    orig = db.ops.rearm_lost_dispatch

    def _spy(loop_id, **kw):
        out = orig(loop_id, **kw)
        seen.append((loop_id, out[0]))
        return out

    db.ops.rearm_lost_dispatch = _spy
    return seen


def _naive(stamp: str) -> datetime:
    from services import loop_service as ls

    parsed = ls._utc_naive(stamp)
    assert parsed is not None, stamp
    return parsed


def _grace_end(db, lid) -> datetime:
    """The instant a re-arm park written now would fall due (`_rearm_due_at`'s
    anchor + grace), from the loop's own stored stamps."""
    from services import loop_service as ls

    closed = [r["completed_at"] for r in _runs(db, lid) if r["status"] != "running"]
    anchor = max(_naive(s) for s in closed) if closed else _naive(_loop(db, lid)["started_at"])
    return anchor + timedelta(seconds=ls.LOOP_REARM_GRACE_SECONDS)


class _ServiceClockAt:
    """Pin the loop service's clock at ``at``: `datetime.utcnow()` (reconcile's
    grace) and `utc_now_iso()` (the sweep's due test). A race then means "worker
    B acts at this instant" however long a loaded machine took to get there."""

    def __init__(self, ls, at: datetime):
        from unittest import mock

        class _Frozen:
            @staticmethod
            def utcnow():
                return at

            fromisoformat = staticmethod(datetime.fromisoformat)

        self._patches = [mock.patch.object(ls, "datetime", _Frozen),
                         mock.patch.object(ls, "utc_now_iso", lambda: _iso(at))]

    def __enter__(self):
        for p in self._patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


def _last_close(db, lid) -> datetime:
    return max(_naive(r["completed_at"]) for r in _runs(db, lid) if r["status"] != "running")


def _pin_closes(db, lid, at_by_run: dict):
    """Overwrite stored close stamps (run_number -> instant)."""
    from sqlalchemy import and_, update

    from db.engine import get_engine
    from db.tables import agent_loop_runs

    with get_engine().begin() as conn:
        for k, at in at_by_run.items():
            conn.execute(update(agent_loop_runs).where(and_(
                agent_loop_runs.c.loop_id == lid, agent_loop_runs.c.run_number == k,
            )).values(completed_at=_iso(at)))


_PINNED = datetime(2026, 10, 27, 12, 0, 0)


def _sweep_at(ls, monkeypatch, when: datetime) -> int:
    """One sweep tick with the sweep's clock at ``when``."""
    with monkeypatch.context() as m:
        m.setattr(ls, "utc_now_iso", lambda: _iso(when))
        return _run(ls.LoopService().dispatch_due_loops())


def _nums(db, lid) -> list:
    return [r["run_number"] for r in _runs(db, lid)]


def _close_run(db, run_id, response="r"):
    assert db.ops.finalize_loop_run(
        run_id, status="completed", response=response, error=None, cost=0.0, duration_ms=1,
    )


def _advance_by_hand(db, lid, k):
    """What a worker's terminal path does for run k, minus the dispatch of k+1:
    claim the run, claim the advance, close the run."""
    rid = db.ops.start_loop_run(lid, k, execution_id=f"x{k}")
    assert rid is not None
    assert db.ops.claim_loop_advance(lid, k)
    _close_run(db, rid)


def _skipped(db) -> list:
    return sorted(e.id for e in db.executions.values() if e.status == "skipped")


def _dead_after_close(ls, db, ts):
    """Run 1 in flight; worker B snapshots the loop (runs_completed=0); then
    worker A claims run 1's advance, closes it, and dies at its progress write.
    Returns (loop_id, B's snapshot)."""
    ts.script = ["queued", {"response": "r2"}, {"response": "r3"}]
    lid = _start(ls, max_runs=3)
    snap = db.ops.list_non_terminal_loops()
    calls = {"n": 0}

    def _fault_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")

    db.hooks["update_loop_progress"] = _fault_once
    db.executions["exec_1"].status, db.executions["exec_1"].response = "success", "r1"
    _run(ls.LoopService().advance_on_terminal("exec_1"))
    db.hooks.pop("update_loop_progress")
    assert (_loop(db, lid)["runs_completed"], _runs(db, lid)[0]["status"]) == (1, "completed")
    assert _loop(db, lid)["next_run_at"] is None
    return lid, snap


class TestDispatchClaim:
    """M1: inserting run K is a claim on the loop row (real SQL)."""

    def _loop_after_run1(self, db, *, park=None):
        loop = db.create_loop(agent_name="a1", message_template="m", max_runs=5)
        lid = loop["id"]
        db.mark_loop_running(lid)
        _advance_by_hand(db, lid, 1)
        if park:
            db.set_loop(lid, next_run_at=park)
        return lid

    def test_a_claim_spends_a_pending_park(self, env):
        ls, db, ts = env
        lid = self._loop_after_run1(db, park="2030-01-01T00:00:00.000000Z")
        rid = db.ops.start_loop_run(lid, 2, execution_id="e2")
        assert rid is not None
        assert _loop(db, lid)["next_run_at"] is None
        assert [(r["run_number"], r["execution_id"]) for r in _runs(db, lid)] == [(1, "x1"), (2, "e2")]

    @pytest.mark.parametrize("why", ["counter_moved", "run_exists", "loop_finished"])
    def test_a_lost_claim_writes_nothing(self, env, why):
        ls, db, ts = env
        park = "2030-01-01T00:00:00.000000Z"
        lid = self._loop_after_run1(db)
        k = 2
        if why == "counter_moved":
            k = 3  # runs_completed is 1, so run 3 is not next
        elif why == "run_exists":
            assert db.ops.start_loop_run(lid, 2, execution_id="first") is not None
        else:
            db.finalize_loop(lid, status="stopped", stop_reason="user_stopped")
        db.set_loop(lid, next_run_at=park)
        before = (_loop(db, lid), _runs(db, lid))
        assert db.ops.start_loop_run(lid, k, execution_id="loser") is None
        assert (_loop(db, lid), _runs(db, lid)) == before  # the park survives a refusal

    def test_t5_a_duplicate_already_in_the_table_does_not_cascade(self, env):
        """Before the fix a duplicate run-2 row's terminal took the #3316 repair
        path and dispatched run 3 a second time, and so on for every later
        iteration. The repair path is unchanged; the claim refuses the second
        run 3, and that dispatch's own execution row is closed `skipped`."""
        ls, db, ts = env
        from sqlalchemy import insert

        from db.engine import get_engine
        from db.tables import agent_loop_runs

        ts.script = [{"response": "r1"}, "queued", "queued", "queued", "queued"]
        lid = _start(ls, max_runs=5)
        assert _nums(db, lid) == [1, 2]
        dup = db.create_task_execution()
        with get_engine().begin() as conn:  # a row this bug wrote before the fix
            conn.execute(insert(agent_loop_runs).values(
                id="lr_legacy_dup", loop_id=lid, run_number=2, execution_id=dup.id,
                status="running", started_at=_iso(datetime(2026, 10, 1)),
            ))
        first = [r for r in _runs(db, lid) if r["run_number"] == 2 and r["execution_id"] != dup.id]
        first_id = first[0]["execution_id"]
        db.executions[first_id].status, db.executions[first_id].response = "success", "a"
        _run(ls.LoopService().advance_on_terminal(first_id))
        db.executions[dup.id].status, db.executions[dup.id].response = "success", "b"
        _run(ls.LoopService().advance_on_terminal(dup.id))

        assert _nums(db, lid).count(3) == 1, _nums(db, lid)
        assert len(ts.calls) == 3  # run 1, run 2, run 3, once each
        run3 = [r for r in _runs(db, lid) if r["run_number"] == 3][0]["execution_id"]
        (loser,) = _skipped(db)
        assert loser != run3
        assert db.executions[loser].error == f"superseded: run 3 of loop {lid} was already dispatched"


class TestReconcileDuringLiveAdvance:
    """The three windows and the overtaken shapes, each with worker B inside."""

    def test_t1d_m61_then_a_sweep_after_the_grace_dispatches_nothing_more(self, env, monkeypatch):
        """m61 with B's sweep run again once the grace has passed. With the
        grace alone (no claim) this was still [1, 2, 2]: m61 would pass while the
        duplicate was only delayed."""
        ls, db, ts = env
        worker_b = ls.LoopService()
        fired = {"v": False}

        async def _broadcast(event):
            if event.get("type") == "loop_run_completed" and not fired["v"]:
                fired["v"] = True
                await worker_b.reconcile_after_restart()

        monkeypatch.setattr(ls, "_broadcast", _broadcast)
        ts.script = [{"response": "r1"}, "queued", "queued"]
        lid = _start(ls, max_runs=3)
        assert fired["v"]
        end = _grace_end(db, lid)
        swept = (
            _sweep_at(ls, monkeypatch, end - timedelta(microseconds=1)),
            _sweep_at(ls, monkeypatch, end + timedelta(microseconds=1)),
        )
        assert _nums(db, lid) == [1, 2], _nums(db, lid)
        assert len(ts.calls) == 2
        assert swept == (0, 0)
        assert _loop(db, lid)["next_run_at"] is None  # A's claim spent B's park

    def test_t3_start_loop_window_on_a_fresh_install(self, env, monkeypatch):
        """The first loop on empty tables. B's reconcile lands after
        `mark_loop_running` and before run 1's row exists: it re-arms (nothing
        says otherwise), and A's claim of run 1 then spends that park."""
        ls, db, ts = env
        from db_harness import count

        assert count("agent_loops") == 0 and count("agent_loop_runs") == 0
        worker_b = ls.LoopService()
        seen = _record_rearms(db)
        b: dict = {}

        def _b_reconciles(*a, **k):
            if "moved" not in b:
                b["moved"] = _on_another_worker(worker_b.reconcile_after_restart)

        db.hooks["start_loop_run"] = _b_reconciles
        ts.script = ["queued", "queued"]
        lid = _start(ls, max_runs=3)
        park_after_claim = _loop(db, lid)["next_run_at"]
        swept = _sweep_at(ls, monkeypatch, _grace_end(db, lid) + timedelta(seconds=1))
        assert _nums(db, lid) == [1], _nums(db, lid)
        assert len(ts.calls) == 1
        assert (b["moved"], [o for _, o in seen], swept) == (1, ["armed"], 0)
        assert park_after_claim is None  # A's claim spent B's park

    def test_t3b_a_queued_read_overtaken_leaves_one_run_1_and_no_park(self, env, monkeypatch):
        """B reads the loop while it is still `queued`, then decides after A has
        flipped it to `running` but before A's run 1 exists."""
        ls, db, ts = env
        worker_b = ls.LoopService()
        snap: dict = {}
        db.hooks["mark_loop_running"] = lambda lid: snap.setdefault("v", db.ops.list_non_terminal_loops())
        b: dict = {}

        def _b_reconciles(*a, **k):
            if "moved" not in b:
                with _SnapshotReadAs(db, snap["v"]):
                    b["moved"] = _on_another_worker(worker_b.reconcile_after_restart)

        db.hooks["start_loop_run"] = _b_reconciles
        ts.script = ["queued", "queued"]
        lid = _start(ls, max_runs=3)
        park_after_claim = _loop(db, lid)["next_run_at"]
        swept = _sweep_at(ls, monkeypatch, _grace_end(db, lid) + timedelta(seconds=1))
        assert _nums(db, lid) == [1], _nums(db, lid)
        assert len(ts.calls) == 1
        assert snap["v"][0]["status"] == "queued"
        assert (b["moved"], swept, park_after_claim) == (1, 0, None)

    def test_t4_sweep_window(self, env, monkeypatch):
        """A's own sweep took the delay park and is about to insert run 2 when
        B's reconcile re-arms the loop; B's next tick must find nothing."""
        ls, db, ts = env
        ts.script = [{"response": "r1"}, "queued", "queued"]
        lid = _start(ls, max_runs=3, delay_seconds=30)
        park = _naive(_loop(db, lid)["next_run_at"])
        worker_b = ls.LoopService()
        b: dict = {}

        def _b_reconciles(*a, **k):
            if "moved" not in b:
                b["moved"] = _on_another_worker(worker_b.reconcile_after_restart)

        db.hooks["start_loop_run"] = _b_reconciles
        a_swept = _sweep_at(ls, monkeypatch, park)  # worker A's tick
        db.hooks.pop("start_loop_run")
        park_after_claim = _loop(db, lid)["next_run_at"]
        b_swept = _sweep_at(ls, monkeypatch, _grace_end(db, lid) + timedelta(seconds=1))
        assert _nums(db, lid) == [1, 2], _nums(db, lid)
        assert len(ts.calls) == 2
        assert (a_swept, b["moved"], park_after_claim, b_swept) == (1, 1, None, 0)

    def test_t7_a_stale_read_does_not_erase_a_pause(self, env):
        """B read the loop before A parked it for an hour; B's decision lands
        after the park. The pause must survive."""
        ls, db, ts = env
        snap: dict = {}
        db.hooks["schedule_loop_next_run"] = lambda lid, at: snap.setdefault("v", db.ops.list_non_terminal_loops())
        ts.script = [{"response": "r1"}, "queued"]
        lid = _start(ls, max_runs=3, delay_seconds=3600)
        db.hooks.pop("schedule_loop_next_run")
        parked = _loop(db, lid)["next_run_at"]
        assert parked and snap["v"][0]["next_run_at"] is None
        seen = _record_rearms(db)
        with _SnapshotReadAs(db, snap["v"]):
            moved = _run(ls.LoopService().reconcile_after_restart())
        assert _loop(db, lid)["next_run_at"] == parked, "the 1-hour pause was overwritten"
        assert (moved, seen) == (0, [(lid, "parked")])

    def test_t7r_a_rearm_before_the_pause_does_not_bypass_it(self, env, monkeypatch):
        """B re-arms and sweeps while A sits between run 1's progress write and
        its 1-hour park. B's park is inside the grace, so B's tick dispatches
        nothing, and A's pause stands."""
        ls, db, ts = env
        worker_b = ls.LoopService()
        b: dict = {}

        async def _broadcast(event):
            if event.get("type") == "loop_run_completed" and "moved" not in b:
                with _ServiceClockAt(ls, _last_close(db, event["loop_id"]) + timedelta(seconds=1)):
                    b["moved"] = await worker_b.reconcile_after_restart()
                    b["swept"] = await worker_b.dispatch_due_loops()

        monkeypatch.setattr(ls, "_broadcast", _broadcast)
        ts.script = [{"response": "r1"}, "queued"]
        lid = _start(ls, max_runs=3, delay_seconds=3600)
        assert _nums(db, lid) == [1], _nums(db, lid)
        assert len(ts.calls) == 1
        assert (b["moved"], b["swept"]) == (1, 0)
        park = _naive(_loop(db, lid)["next_run_at"])
        assert park - _naive(_runs(db, lid)[0]["completed_at"]) >= timedelta(seconds=3500)

    def test_t8b_a_rearm_decided_after_run_n_plus_1_exists_arms_nothing(self, env, monkeypatch):
        """B's read saw no open run (A between close and claim); B's decision
        lands after A inserted run 2. It finds run 2 in flight and leaves it."""
        ls, db, ts = env
        snap: dict = {}

        async def _broadcast(event):
            if event.get("type") == "loop_run_completed" and "v" not in snap:
                snap["v"] = db.ops.list_non_terminal_loops()

        monkeypatch.setattr(ls, "_broadcast", _broadcast)
        ts.script = [{"response": "r1"}, "queued", "queued"]
        lid = _start(ls, max_runs=3)
        assert snap["v"][0]["next_run_at"] is None
        seen = _record_rearms(db)
        with _SnapshotReadAs(db, snap["v"]):
            moved = _run(ls.LoopService().reconcile_after_restart())
        assert _loop(db, lid)["next_run_at"] is None, "a park was armed with run 2 in flight"
        assert (moved, seen) == (0, [(lid, "open_run")])
        assert _sweep_at(ls, monkeypatch, _grace_end(db, lid) + timedelta(seconds=1)) == 0
        assert _nums(db, lid) == [1, 2]
        assert len(ts.calls) == 2


class TestLiveWorkerKeepsItsDecision:
    """M3: inside the grace, the worker that ran run N's post-run gates decides
    what follows it, even when B re-arms and sweeps in its window."""

    def test_t13r_a_stop_signal_match_is_not_overtaken(self, env, monkeypatch):
        ls, db, ts = env
        worker_b = ls.LoopService()
        b: dict = {}

        async def _broadcast(event):
            if event.get("type") == "loop_run_completed" and "moved" not in b:
                with _ServiceClockAt(ls, _last_close(db, event["loop_id"]) + timedelta(seconds=1)):
                    b["moved"] = await worker_b.reconcile_after_restart()
                    b["swept"] = await worker_b.dispatch_due_loops()

        monkeypatch.setattr(ls, "_broadcast", _broadcast)
        ts.script = [{"response": "r1 DONE"}, "queued", "queued"]
        lid = _start(ls, max_runs=3, stop_signal="DONE")
        loop = _loop(db, lid)
        assert _nums(db, lid) == [1], _nums(db, lid)
        assert len(ts.calls) == 1
        assert (loop["status"], loop["stop_reason"]) == ("completed", "stop_signal_matched")
        assert (b["moved"], b["swept"]) == (1, 0)
        # B's park outlives the loop; every reader of a park skips a terminal loop.
        assert loop["next_run_at"] is not None
        assert _sweep_at(ls, monkeypatch, _grace_end(db, lid) + timedelta(seconds=1)) == 0

    def test_t13r_an_abort_after_failure_is_not_overtaken(self, env, monkeypatch):
        """A failed run has no broadcast in its tail, so B lands at A's progress
        write instead (A blocked in a synchronous call, B on its own loop)."""
        ls, db, ts = env
        worker_b = ls.LoopService()
        b: dict = {}

        async def _b():
            return (await worker_b.reconcile_after_restart(),
                    await worker_b.dispatch_due_loops())

        def _b_reconciles_and_sweeps(loop_id, **k):
            if "moved_swept" not in b:
                with _ServiceClockAt(ls, _last_close(db, loop_id) + timedelta(seconds=1)):
                    b["moved_swept"] = _on_another_worker(_b)

        db.hooks["update_loop_progress"] = _b_reconciles_and_sweeps
        ts.script = [{"status": "failed", "error": "boom"}, "queued", "queued"]
        lid = _start(ls, max_runs=3, on_failure="abort")
        loop = _loop(db, lid)
        assert _nums(db, lid) == [1], _nums(db, lid)
        assert len(ts.calls) == 1
        assert (loop["status"], loop["stop_reason"]) == ("failed", "error")
        assert b["moved_swept"] == (1, 0)

    def test_t14_runs_completed_is_not_rewound(self, env):
        """B re-arms and sweeps while A sits before run 1's progress write. Had
        B dispatched there, A's late `runs_completed = 1` would land on top of
        B's run 2 and strand the loop."""
        ls, db, ts = env
        worker_b = ls.LoopService()
        b: dict = {}

        async def _b():
            await worker_b.reconcile_after_restart()
            return await worker_b.dispatch_due_loops()

        def _b_reconciles_and_sweeps(loop_id, **k):
            if "swept" not in b:
                with _ServiceClockAt(ls, _last_close(db, loop_id) + timedelta(seconds=1)):
                    b["swept"] = _on_another_worker(_b)

        db.hooks["update_loop_progress"] = _b_reconciles_and_sweeps
        ts.script = [{"response": "r1"}, {"status": "failed", "error": "boom"}, "queued", "queued"]
        lid = _start(ls, max_runs=5, on_failure="continue")
        loop = _loop(db, lid)
        runs = _runs(db, lid)
        closed = [r["run_number"] for r in runs if r["status"] != "running"]
        assert loop["runs_completed"] == max(closed) == 2, (loop["runs_completed"], closed)
        assert [(r["run_number"], r["status"]) for r in runs] == [
            (1, "completed"), (2, "failed"), (3, "running"),
        ]
        assert loop["status"] == "running"
        assert b["swept"] == 0

    def test_t13_rtail_a_park_forced_due_inside_the_window_still_dispatches_once(self, env, monkeypatch):
        """The stated residual (R-tail): when the park is due inside A's window
        (A's tail outlasting the grace), B's sweep dispatches run 2 before A
        reaches its own dispatch. A then claims run 2 as well and must lose:
        run 2 exists exactly once and its turn is spawned once."""
        ls, db, ts = env
        worker_b = ls.LoopService()
        b: dict = {}

        async def _broadcast(event):
            if event.get("type") == "loop_run_completed" and "swept" not in b:
                b["moved"] = await worker_b.reconcile_after_restart()
                lid_ = event["loop_id"]
                with monkeypatch.context() as m:
                    when = _grace_end(db, lid_) + timedelta(seconds=1)
                    m.setattr(ls, "utc_now_iso", lambda: _iso(when))
                    b["swept"] = await worker_b.dispatch_due_loops()

        monkeypatch.setattr(ls, "_broadcast", _broadcast)
        ts.script = [{"response": "r1"}, "queued", "queued"]
        lid = _start(ls, max_runs=3)
        assert (b["moved"], b["swept"]) == (1, 1), b
        assert _nums(db, lid) == [1, 2], _nums(db, lid)
        assert len(ts.calls) == 2


class TestDeadAdvanceStillRecovers:
    """The re-arm reconcile exists for: the advancing worker really died."""

    def _dead_advance(self, ls, db, ts, **kw):
        calls = {"n": 0}

        def _fault_once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")

        db.hooks["update_loop_progress"] = _fault_once
        lid = _start(ls, **kw)
        db.hooks.pop("update_loop_progress")
        return lid

    def test_t16_recovered_only_once_the_grace_has_passed(self, env, monkeypatch):
        ls, db, ts = env
        ts.script = [{"response": "r1"}, {"response": "r2"}, {"response": "r3"}]
        lid = self._dead_advance(ls, db, ts, max_runs=3)
        _pin_closes(db, lid, {1: _PINNED})
        with _ServiceClockAt(ls, _PINNED + timedelta(seconds=1)):
            assert _run(ls.LoopService().reconcile_after_restart()) == 1
        park = _loop(db, lid)["next_run_at"]
        assert park == "2026-10-27T12:03:00.000000Z"  # 180 s after run 1's stored close
        assert _sweep_at(ls, monkeypatch, _naive(park) - timedelta(microseconds=1)) == 0
        assert _nums(db, lid) == [1]
        assert _sweep_at(ls, monkeypatch, _naive(park)) == 1
        loop = _loop(db, lid)
        assert loop["stop_reason"] == "max_runs_reached"
        assert _nums(db, lid) == [1, 2, 3]

    def test_t15_clock_30_days_ahead_a_month_old_loss_is_due_at_once(self, env, monkeypatch):
        """Other shape: the clock frozen at a pinned instant 30 days after the
        loop's last stored close. The park is due at that instant, not 180 s
        later — the grace never delays an old loss."""
        ls, db, ts = env
        from sqlalchemy import update

        from db.engine import get_engine
        from db.tables import agent_loop_runs

        ts.script = [{"response": "r1"}, {}, {}]
        lid = self._dead_advance(ls, db, ts, max_runs=3)
        closed_at = datetime(2026, 9, 27, 12, 0, 0)
        frozen = closed_at + timedelta(days=30)
        with get_engine().begin() as conn:
            conn.execute(update(agent_loop_runs).where(agent_loop_runs.c.loop_id == lid)
                         .values(completed_at=_iso(closed_at)))

        class _Frozen:
            @staticmethod
            def utcnow():
                return frozen

            fromisoformat = staticmethod(datetime.fromisoformat)

        monkeypatch.setattr(ls, "datetime", _Frozen)
        assert _run(ls.LoopService().reconcile_after_restart()) == 1
        assert _loop(db, lid)["next_run_at"] == "2026-10-27T12:00:00.000000Z"

    def test_t18_a_real_loss_behind_a_stale_snapshot_is_rearmed(self, env, monkeypatch):
        ls, db, ts = env
        lid, snap = _dead_after_close(ls, db, ts)
        assert snap[0]["runs_completed"] == 0  # B's read predates A's claim
        _pin_closes(db, lid, {1: _PINNED})
        seen = _record_rearms(db)
        with _SnapshotReadAs(db, snap), _ServiceClockAt(ls, _PINNED + timedelta(seconds=1)):
            moved = _run(ls.LoopService().reconcile_after_restart())
        assert seen == [(lid, "armed")], f"a real loss was not re-armed: {seen}"
        assert moved == 1
        assert _loop(db, lid)["next_run_at"] is not None
        assert _sweep_at(ls, monkeypatch, _grace_end(db, lid)) == 1
        assert _nums(db, lid)[:2] == [1, 2]

    def test_t19_a_queued_read_then_a_crash_before_run_1_is_rearmed(self, env):
        ls, db, ts = env
        loop = db.create_loop(agent_name="a1", message_template="m", max_runs=2)
        snap = db.ops.list_non_terminal_loops()
        db.mark_loop_running(loop["id"])  # ... and worker A dies here
        db.set_loop(loop["id"], started_at=_iso(_PINNED))
        seen = _record_rearms(db)
        with _SnapshotReadAs(db, snap), _ServiceClockAt(ls, _PINNED + timedelta(seconds=1)):
            moved = _run(ls.LoopService().reconcile_after_restart())
        assert seen == [(loop["id"], "armed")], f"a real loss was not re-armed: {seen}"
        assert moved == 1
        assert _loop(db, loop["id"])["next_run_at"] == "2026-10-27T12:03:00.000000Z"

    def test_t21_overtaken_four_times_before_b_decides_then_death(self, env):
        """Between B's read and B's decision a worker runs four whole iterations
        and dies. One call, on the state it finds: re-armed, due from run 4."""
        ls, db, ts = env
        loop = db.create_loop(agent_name="a1", message_template="m", max_runs=10)
        lid = loop["id"]
        db.mark_loop_running(lid)
        overtakes = {"n": 0}

        def _four_iterations(*a, **k):
            overtakes["n"] += 1
            for k_ in range(1, 5):
                _advance_by_hand(db, lid, k_)
            _pin_closes(db, lid, {k_: _PINNED - timedelta(minutes=5 - k_) for k_ in range(1, 5)})

        db.hooks["rearm_lost_dispatch"] = _four_iterations
        with _ServiceClockAt(ls, _PINNED):
            assert _run(ls.LoopService().reconcile_after_restart()) == 1
        assert overtakes["n"] == 1
        assert _loop(db, lid)["runs_completed"] == 4
        # due from run 4's close (P - 1 min), not run 1's
        assert _loop(db, lid)["next_run_at"] == "2026-10-27T12:02:00.000000Z"

    def test_t24_a_queued_read_then_a_whole_iteration_and_a_death_is_not_stuck(self, env, caplog):
        """B read `queued`; before B decides, A marks the loop running, claims
        and runs iteration 1, closes it, and dies. A reading of the counter from
        B's snapshot (0) beside run 1 would call this stuck; the locked read
        does not."""
        ls, db, ts = env
        loop = db.create_loop(agent_name="a1", message_template="m", max_runs=5)
        lid = loop["id"]
        snap = db.ops.list_non_terminal_loops()
        done = {"v": False}

        def _a_runs_one_iteration(*a, **k):
            if not done["v"]:
                done["v"] = True
                db.mark_loop_running(lid)
                _advance_by_hand(db, lid, 1)
                _pin_closes(db, lid, {1: _PINNED - timedelta(minutes=1)})

        db.hooks["rearm_lost_dispatch"] = _a_runs_one_iteration
        seen = _record_rearms(db)
        with caplog.at_level(logging.WARNING), _SnapshotReadAs(db, snap), _ServiceClockAt(ls, _PINNED):
            moved = _run(ls.LoopService().reconcile_after_restart())
        assert seen == [(lid, "armed")], f"a real loss was not re-armed: {seen}"
        assert moved == 1
        assert not any("not re-armed" in r.getMessage() for r in caplog.records)
        assert _loop(db, lid)["next_run_at"] == "2026-10-27T12:02:00.000000Z"

    def test_t25_a_park_aba_before_b_decides_is_rearmed_in_one_call(self, env):
        """Another reconcile parks the loop and a sweep takes the park and
        crashes, all between B's read and B's decision. Nothing parked, nothing
        in flight: re-armed, one call."""
        ls, db, ts = env
        loop = db.create_loop(agent_name="a1", message_template="m", max_runs=5)
        lid = loop["id"]
        db.mark_loop_running(lid)
        aba = {"n": 0}

        def _park_then_take(*a, **k):
            aba["n"] += 1
            at = "2026-10-01T00:00:00.000000Z"
            db.ops.schedule_next_run(lid, at)
            assert db.ops.claim_due_loop(lid, at)  # ... and that sweep crashes

        db.hooks["rearm_lost_dispatch"] = _park_then_take
        seen = _record_rearms(db)
        assert _run(ls.LoopService().reconcile_after_restart()) == 1
        assert aba["n"] == 1 and seen == [(lid, "armed")]
        assert _loop(db, lid)["next_run_at"] is not None

    def test_t23_the_stuck_shape_is_reported_once_and_not_rearmed(self, env, caplog):
        """A closed run numbered past `runs_completed` with nothing in flight
        (the counter-rewind shape). Re-arming would dispatch run 2 again."""
        ls, db, ts = env
        loop = db.create_loop(agent_name="a1", message_template="m", max_runs=10)
        lid = loop["id"]
        db.mark_loop_running(lid)
        _advance_by_hand(db, lid, 1)
        _advance_by_hand(db, lid, 2)
        db.set_loop(lid, runs_completed=1)  # rewound under a closed run 2
        seen = _record_rearms(db)
        with caplog.at_level(logging.WARNING):
            moved = _run(ls.LoopService().reconcile_after_restart())
        assert _loop(db, lid)["next_run_at"] is None, "re-armed over run number 2, which exists"
        assert (moved, seen) == (0, [(lid, "stuck")])
        warned = [r for r in caplog.records if "not re-armed" in r.getMessage()]
        assert len(warned) == 1 and lid in warned[0].getMessage()


class TestRearmOutcomes:
    """One case per `rearm_lost_dispatch` outcome, on the real SQL: the outcome,
    reconcile's count, and that every refusal writes nothing."""

    def _due_for(self, calls):
        def _due_for(loop, runs):
            calls.append((loop["id"], [r["run_number"] for r in runs]))
            return "2030-01-01T00:00:00.000000Z"
        return _due_for

    def _running_loop(self, db):
        loop = db.create_loop(agent_name="a1", message_template="m", max_runs=5)
        db.mark_loop_running(loop["id"])
        return loop["id"]

    def test_armed(self, env):
        ls, db, ts = env
        lid = self._running_loop(db)
        _advance_by_hand(db, lid, 1)
        calls: list = []
        assert db.ops.rearm_lost_dispatch(lid, due_for=self._due_for(calls)) == ("armed", None)
        assert calls == [(lid, [1])]
        assert _loop(db, lid)["next_run_at"] == "2030-01-01T00:00:00.000000Z"

    def test_open_run_with_a_live_execution_is_left_alone(self, env):
        ls, db, ts = env
        ts.script = ["queued"]
        lid = _start(ls, max_runs=3)
        before = (_loop(db, lid), _runs(db, lid))
        calls: list = []
        outcome, run = db.ops.rearm_lost_dispatch(lid, due_for=self._due_for(calls))
        assert (outcome, run["run_number"], run["execution_id"]) == ("open_run", 1, "exec_1")
        assert calls == [] and (_loop(db, lid), _runs(db, lid)) == before
        assert _run(ls.LoopService().reconcile_after_restart()) == 0
        assert (_loop(db, lid), _runs(db, lid)) == before

    def test_open_run_with_a_terminal_execution_is_advanced_and_counted_once(self, env):
        ls, db, ts = env
        ts.script = ["queued", "queued"]
        lid = _start(ls, max_runs=3)
        db.executions["exec_1"].status, db.executions["exec_1"].response = "success", "r1"
        assert _run(ls.LoopService().reconcile_after_restart()) == 1
        assert _loop(db, lid)["runs_completed"] == 1
        assert _nums(db, lid) == [1, 2]
        assert _loop(db, lid)["next_run_at"] is None
        assert len(ts.calls) == 2

    def test_stuck(self, env):
        ls, db, ts = env
        lid = self._running_loop(db)
        _advance_by_hand(db, lid, 1)
        _advance_by_hand(db, lid, 2)
        db.set_loop(lid, runs_completed=1)
        before = (_loop(db, lid), _runs(db, lid))
        calls: list = []
        assert db.ops.rearm_lost_dispatch(lid, due_for=self._due_for(calls)) == ("stuck", None)
        assert calls == [] and (_loop(db, lid), _runs(db, lid)) == before

    @pytest.mark.parametrize("case", ["parked", "parked_empty_string", "terminal", "missing"])
    def test_a_loop_that_moved_since_the_snapshot_is_left_alone(self, env, case):
        ls, db, ts = env
        outcome = case.split("_")[0]
        lid = self._running_loop(db)
        snap = db.ops.list_non_terminal_loops()  # unparked, running
        if case == "parked":
            db.ops.schedule_next_run(lid, "2030-01-01T00:00:00.000000Z")
        elif case == "parked_empty_string":
            # Not NULL, so not unparked: the SQL leaves it, and so must a fake.
            db.ops.schedule_next_run(lid, "")
        elif outcome == "terminal":
            db.finalize_loop(lid, status="stopped", stop_reason="user_stopped")
        else:
            from sqlalchemy import delete

            from db.engine import get_engine
            from db.tables import agent_loops

            with get_engine().begin() as conn:
                conn.execute(delete(agent_loops).where(agent_loops.c.id == lid))
        before = _loop(db, lid)
        calls: list = []
        assert db.ops.rearm_lost_dispatch(lid, due_for=self._due_for(calls)) == (outcome, None)
        assert calls == [] and _loop(db, lid) == before
        seen = _record_rearms(db)
        with _SnapshotReadAs(db, snap):
            assert _run(ls.LoopService().reconcile_after_restart()) == 0
        assert seen == [(lid, outcome)]
        assert _loop(db, lid) == before


class TestRearmDueAt:
    """M3's pure helper: exact due times at a pinned instant (no clock read)."""

    NOW = datetime(2026, 10, 27, 12, 0, 0)

    @staticmethod
    def _closed(stamp):
        return {"status": "completed", "completed_at": stamp}

    @pytest.mark.parametrize(
        "loop,runs,want,warns",
        [
            pytest.param({}, [_closed.__func__("2026-10-27T11:58:20+00:00"),
                              _closed.__func__("2026-10-27T11:59:50.000000Z")],
                         datetime(2026, 10, 27, 12, 2, 50), False, id="t22-newest_of_two_closed"),
            pytest.param({}, [_closed.__func__("2026-10-27T13:59:00+02:00")],
                         datetime(2026, 10, 27, 12, 2, 0), False, id="t20-plus_two_offset_normalised"),
            pytest.param({}, [_closed.__func__("not-a-time"),
                              _closed.__func__("2026-09-27T12:00:00.000000Z")],
                         datetime(2026, 10, 27, 12, 3, 0), True, id="t22-garbage_beside_an_old_valid_stamp"),
            pytest.param({}, [_closed.__func__(None)],
                         datetime(2026, 10, 27, 12, 3, 0), True, id="closed_run_without_a_stamp"),
            pytest.param({}, [_closed.__func__("2026-10-27T11:57:00.000000Z")],
                         datetime(2026, 10, 27, 12, 0, 0), False, id="closed_exactly_one_grace_ago"),
            pytest.param({}, [_closed.__func__("2026-10-27T11:57:01.000000Z")],
                         datetime(2026, 10, 27, 12, 0, 1), False, id="closed_just_inside_the_grace"),
            pytest.param({}, [_closed.__func__("2026-09-27T12:00:00.000000Z")],
                         datetime(2026, 10, 27, 12, 0, 0), False, id="t22-old_loss"),
            pytest.param({}, [_closed.__func__("2026-11-26T12:00:00.000000Z")],
                         datetime(2026, 10, 27, 12, 3, 0), True, id="t20-future_anchor"),
            pytest.param({}, [_closed.__func__("9999-12-31T23:59:59.999999Z")],
                         datetime(2026, 10, 27, 12, 3, 0), True, id="t22-far_future_anchor"),
            pytest.param({}, [_closed.__func__("0001-01-01T00:00:00+02:00")],
                         datetime(2026, 10, 27, 12, 3, 0), True, id="offset_that_cannot_normalise"),
            pytest.param({}, [_closed.__func__("2026-10-27T11:59:10.000000Z"),
                              {"status": "running", "completed_at": None}],
                         datetime(2026, 10, 27, 12, 2, 10), False, id="a_running_row_is_not_a_close"),
            pytest.param({"started_at": "2026-10-27T11:59:30.000000Z",
                          "created_at": "2026-10-26T12:00:00.000000Z"}, [],
                         datetime(2026, 10, 27, 12, 2, 30), False, id="no_runs_started_recently"),
            pytest.param({"started_at": None, "created_at": "2026-10-26T12:00:00.000000Z"}, [],
                         datetime(2026, 10, 27, 12, 0, 0), False, id="t22-no_runs_absent_started_old_created"),
            pytest.param({"created_at": "2026-10-27T11:59:00.000000Z"}, [],
                         datetime(2026, 10, 27, 12, 2, 0), False, id="no_runs_missing_started_recent_created"),
            pytest.param({"started_at": "garbage", "created_at": "2026-10-26T12:00:00.000000Z"}, [],
                         datetime(2026, 10, 27, 12, 3, 0), True, id="t22-no_runs_unreadable_started"),
            pytest.param({"started_at": "", "created_at": "2026-10-26T12:00:00.000000Z"}, [],
                         datetime(2026, 10, 27, 12, 3, 0), True, id="t22b-empty_started_at"),
            pytest.param({"started_at": None, "created_at": None}, [],
                         datetime(2026, 10, 27, 12, 3, 0), True, id="no_stamp_at_all"),
        ],
    )
    def test_exact_due_time(self, loop, runs, want, warns, caplog):
        from services import loop_service as ls

        with caplog.at_level(logging.WARNING, logger="services.loop_service"):
            got = ls._rearm_due_at({"id": "loop_x", **loop}, runs, self.NOW)
        assert got == want
        warned = [r for r in caplog.records if "re-arm" in r.getMessage()]
        assert bool(warned) is warns, [r.getMessage() for r in warned]
        if warns:
            assert len(warned) == 1 and "loop_x" in warned[0].getMessage()

    def test_an_unrepresentable_instant_saturates_to_now(self):
        from services import loop_service as ls

        assert ls._rearm_due_at({}, [self._closed("2026-10-27T11:59:00Z")], datetime.max) == datetime.max

    def test_the_park_is_written_in_the_sweeps_format(self, monkeypatch):
        """`list_due_loops` compares `next_run_at <= utc_now_iso()` as text, so
        the park must be written in exactly that shape."""
        from services import loop_service as ls

        class _Frozen:
            @staticmethod
            def utcnow():
                return datetime(2026, 10, 27, 12, 0, 0)

            fromisoformat = staticmethod(datetime.fromisoformat)

        monkeypatch.setattr(ls, "datetime", _Frozen)
        due = ls.LoopService._rearm_due_for({}, [self._closed("2026-10-27T11:59:00.000000Z")])
        assert due == "2026-10-27T12:02:00.000000Z"
