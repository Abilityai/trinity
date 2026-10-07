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

    def test_m16_infinite_budget_never_trips(self, env):
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
        # good: lost its dispatch (no open run). bad: its run read raises.
        from sqlalchemy import delete

        from db.engine import get_engine
        from db.tables import agent_loop_runs

        with get_engine().begin() as conn:
            conn.execute(delete(agent_loop_runs).where(agent_loop_runs.c.loop_id == good))
        orig = db.ops.list_runs

        def _list(lid):
            if lid == bad:
                raise RuntimeError("row read failed")
            return orig(lid)

        db.ops.list_runs = _list
        try:
            moved = _run(ls.LoopService().reconcile_after_restart())
        finally:
            db.ops.list_runs = orig
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
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG: a fault between claim_loop_advance and finalize_loop_run strands "
            "the loop forever — runs_completed moved, run row still 'running', and "
            f"neither a redelivered terminal nor reconcile_after_restart can re-claim — #3316"
        ),
    )
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

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG: reconcile_after_restart (worker B booting) re-arms a loop that worker "
            "A is mid-advance on (run N closed, run N+1 not yet started), so the sweep "
            f"dispatches run N+1 a second time — #3317"
        ),
    )
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
