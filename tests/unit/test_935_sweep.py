"""Tier-0 sweep + leader lease (trinity-enterprise#935, checkpoint B).

The single writer path of the deterministic evaluator is a backend loop
(TD-1): every 60 s the Redis leader grades up to 25 settled terminal runs. Two
things are pinned here on the real schema (`tests/db_harness.py`, so the
partial unique index the one-row rule lives in exists):

1. **The sweep is bounded and complete on its own terms** — it picks only
   terminal `schedule`/`agent` runs completed at least `settle` ago and at most
   `lookback` ago with no Tier-0 row yet, at most `limit` per tick; one failing
   or slow evaluation never costs the batch; a timed-out evaluation leaves a
   row (`skip: evaluator_timeout`) so the run is not re-picked every tick.
2. **Concurrent ticks are safe** — two sweepers racing for one execution end
   with ONE row, because the write is idempotent on the index (TD-2), so the
   lease can fail OPEN (TD-8): a lost or contended lease skips the tick and
   never blocks anything, and two leaders at once are merely wasteful.

The lease tests mirror `test_1464_monitoring_leader_lock.py` (FakeRedis
verbatim in shape); the loop is driven one iteration at a time the way that
file drives `_run_loop`.
"""
from __future__ import annotations

import asyncio
import ast
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import count, db_backend  # noqa: E402,F401

from services import tier0_evaluator as t0  # noqa: E402
from services import tier0_sweep_service as sweep_mod  # noqa: E402

pytestmark = pytest.mark.unit

TIER0 = "platform:tier0"


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _seed(exec_id, *, completed_ago=120, **cols):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    values = dict(id=exec_id, schedule_id="sched-1", agent_name="agent-a",
                  status="success", triggered_by="schedule", message="/report-weekly",
                  response="Done", started_at=_ago(completed_ago + 180),
                  completed_at=_ago(completed_ago))
    values.update(cols)
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(**values))
    return exec_id


def _tier0_rows():
    from database import db
    rows = db.list_fleet_evaluations(None, 500)
    return {r["target_id"]: r for r in rows if r["evaluator"] == TIER0}


@pytest.fixture
def no_staged(monkeypatch):
    monkeypatch.setattr(t0, "_staged_values", lambda: [])


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --- what one tick grades ---------------------------------------------------------

def test_a_tick_grades_only_settled_terminal_schedule_and_agent_runs(db_backend, no_staged):
    _seed("ex-sched")                                   # schedule, settled → graded
    _seed("ex-agent", triggered_by="agent", schedule_id="__manual__")   # agent → graded
    _seed("ex-failed", status="failed", error="boom")   # failed is terminal → graded
    _seed("ex-user", triggered_by="user")               # outside the trigger set
    _seed("ex-running", status="running", completed_at=None)
    _seed("ex-fresh", completed_ago=5)                  # inside the settle window
    _seed("ex-old", completed_ago=25 * 3600)            # past the lookback

    result = _run(t0.sweep(limit=25, settle_seconds=30, lookback_hours=24))

    assert set(_tier0_rows()) == {"ex-sched", "ex-agent", "ex-failed"}
    assert result["evaluated"] == 3 and result["candidates"] == 3


def test_a_tick_is_bounded_and_the_next_tick_continues(db_backend, no_staged):
    for i in range(4):
        _seed(f"ex-{i}", completed_ago=600 - i)         # oldest first
    first = _run(t0.sweep(limit=3))
    assert first["candidates"] == 3 and len(_tier0_rows()) == 3
    assert "ex-3" not in _tier0_rows()                  # the newest waits its turn
    second = _run(t0.sweep(limit=3))
    assert second["candidates"] == 1 and set(_tier0_rows()) == {"ex-0", "ex-1", "ex-2", "ex-3"}
    # Re-run safe: nothing left, nothing rewritten.
    third = _run(t0.sweep(limit=3))
    assert third == {"candidates": 0, "evaluated": 0, "timed_out": 0, "failed": 0}


def test_an_evaluated_run_is_never_picked_again(db_backend, no_staged):
    _seed("ex-1")
    _run(t0.sweep())
    first_id = _tier0_rows()["ex-1"]["id"]
    _run(t0.sweep())
    rows = _tier0_rows()
    assert list(rows) == ["ex-1"] and rows["ex-1"]["id"] == first_id
    assert count("agent_evaluations") == 1


def test_one_raising_evaluation_does_not_stop_the_batch(db_backend, no_staged, monkeypatch):
    _seed("ex-bad", completed_ago=300)
    _seed("ex-good", completed_ago=200)
    real = t0.evaluate_execution

    def flaky(execution_id):
        if execution_id == "ex-bad":
            raise RuntimeError("evaluator crashed")
        return real(execution_id)
    monkeypatch.setattr(t0, "evaluate_execution", flaky)

    result = _run(t0.sweep())
    assert "ex-good" in _tier0_rows()
    assert result["failed"] == 1 and result["evaluated"] == 1


def test_an_evaluation_that_wrote_no_row_counts_as_failed(db_backend, no_staged, monkeypatch):
    """Review I6: `evaluate_execution` returns None when the run is not
    eligible or its write failed. That is not an evaluation — the tick's
    counts (the only tick-level log line) must say `failed`, or a run whose
    write fails every tick reads as evaluated for 24 h."""
    _seed("ex-nowrite", completed_ago=300)
    _seed("ex-good", completed_ago=200)
    real = t0.evaluate_execution
    monkeypatch.setattr(t0, "evaluate_execution",
                        lambda i: None if i == "ex-nowrite" else real(i))
    result = _run(t0.sweep())
    assert (result["evaluated"], result["failed"], result["timed_out"]) == (1, 1, 0)
    assert set(_tier0_rows()) == {"ex-good"}


def test_a_timed_out_evaluation_leaves_a_timeout_row(db_backend, no_staged, monkeypatch):
    """`wait_for` cannot stop a thread, so the bound is enforced by recording
    the timeout: the run gets a row every check of which is
    `skip: evaluator_timeout`, and the sweep moves on instead of re-picking it
    every 60 s for as long as the evaluation keeps hanging."""
    _seed("ex-slow", completed_ago=300)
    _seed("ex-fast", completed_ago=200)
    real = t0.evaluate_execution
    release = threading.Event()

    def slow(execution_id):
        if execution_id == "ex-slow":
            release.wait(5)          # outlives the sweep's bound below
            return None
        return real(execution_id)
    monkeypatch.setattr(t0, "evaluate_execution", slow)

    result = _run(t0.sweep(timeout_seconds=0.2))
    release.set()
    rows = _tier0_rows()
    assert result["timed_out"] == 1 and "ex-fast" in rows
    slow_row = rows["ex-slow"]
    checks = slow_row["checks"]["checks"]
    assert checks and all(c["status"] == "skip" and c["reason"] == "evaluator_timeout"
                          for c in checks)
    assert {c["id"] for c in checks} == {c.id for c in t0.CHECKS}
    headline = slow_row["checks"]["headline"]
    assert headline["gate_pass"] == 0 and headline["gate_fail"] == 0 and headline["gate_skip"] > 0
    assert slow_row["completion"] is True           # status is still the run's own
    # ...and it is not picked again.
    assert _run(t0.sweep(timeout_seconds=0.2))["candidates"] == 0


def test_a_timeout_never_overwrites_a_grade_that_landed(db_backend, no_staged, monkeypatch):
    """The slow thread may finish a moment after the bound fires. The timeout
    row is written only when no row exists, so a real grade is never replaced
    by a skip; the reverse order (skip, then the late grade) re-grades in place
    — the TD-2 update path."""
    _seed("ex-1")
    real = t0.evaluate_execution

    def finishes_late(execution_id):
        time.sleep(0.3)
        return real(execution_id)
    monkeypatch.setattr(t0, "evaluate_execution", finishes_late)
    result = _run(t0.sweep(timeout_seconds=0.1))
    assert result["timed_out"] == 1
    time.sleep(0.5)                                 # let the thread land its grade
    row = _tier0_rows()["ex-1"]
    assert count("agent_evaluations") == 1
    assert any(c["status"] != "skip" or c["reason"] != "evaluator_timeout"
               for c in row["checks"]["checks"]), "the late grade must win"


def test_a_grade_landing_between_the_check_and_the_timeout_write_is_kept(
        db_backend, no_staged, monkeypatch):
    """The reverse race (review I1): `record_timeout` reads "no row", the slow
    thread lands its grade, THEN the timeout row is written. The timeout write
    is INSERT-only, so it loses to the grade instead of overwriting it with six
    `evaluator_timeout` skips (the run is never re-picked, so that loss would
    be permanent)."""
    from database import db
    _seed("ex-1")
    graded = t0.evaluate_execution("ex-1")           # the grade lands first...
    assert graded is not None
    # ...but the timeout path's existence check read before it did.
    monkeypatch.setattr(db, "latest_tier0_for_execution", lambda _id: None)
    assert t0.record_timeout("ex-1") is None
    row = _tier0_rows()["ex-1"]
    assert count("agent_evaluations") == 1
    assert row["id"] == graded["id"]
    assert not any(c.get("reason") == "evaluator_timeout" for c in row["checks"]["checks"])
    assert row["checks"] == graded["checks"]


def test_a_late_grade_still_regrades_a_timeout_row_in_place(db_backend, no_staged):
    """The intended direction stays open: timeout row first, real grade after."""
    _seed("ex-1")
    skipped = t0.record_timeout("ex-1")
    assert skipped is not None
    graded = t0.evaluate_execution("ex-1")
    assert graded["id"] == skipped["id"] and count("agent_evaluations") == 1
    assert not any(c.get("reason") == "evaluator_timeout" for c in graded["checks"]["checks"])


def test_evaluation_runs_off_the_event_loop(db_backend, no_staged, monkeypatch):
    """Sync DB + regex work must not block the loop (the wait_for ledger entry)."""
    _seed("ex-1")
    seen = {}
    real = t0.evaluate_execution

    def record(execution_id):
        seen["thread"] = threading.current_thread()
        return real(execution_id)
    monkeypatch.setattr(t0, "evaluate_execution", record)
    _run(t0.sweep())
    assert seen["thread"] is not threading.main_thread()


# --- the one-row rule under a race ---------------------------------------------------

def test_two_sweepers_racing_for_one_execution_leave_one_row(db_backend, no_staged, monkeypatch):
    """Two leaders (the lease failed open on both) see the same unevaluated
    run and grade it at once. The second write becomes the UPDATE (TD-2), so
    the table holds exactly one Tier-0 row for the run."""
    from database import db
    _seed("ex-race")
    # Both tickers read the candidate list before either has written.
    monkeypatch.setattr(db, "unevaluated_execution_ids", lambda **kw: ["ex-race"])
    barrier = threading.Barrier(2, timeout=5)
    real = t0.evaluate_execution

    def synced(execution_id):
        barrier.wait()
        return real(execution_id)
    monkeypatch.setattr(t0, "evaluate_execution", synced)

    async def both():
        return await asyncio.gather(t0.sweep(), t0.sweep())
    a, b = _run(both())
    assert a["evaluated"] == 1 and b["evaluated"] == 1
    assert count("agent_evaluations") == 1
    assert list(_tier0_rows()) == ["ex-race"]


def test_a_replayed_terminal_regrades_the_same_row(db_backend, no_staged):
    """A FAILED terminal later overwritten by SUCCESS (a cleanup replay) is
    re-graded in place by the writer — same row id, completion flipped. The
    sweep itself does not re-pick an evaluated run; a re-stamp trigger is the
    D-3 follow-up (ent#937)."""
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import schedule_executions
    _seed("ex-1", status="failed", error="boom")
    _run(t0.sweep())
    before = _tier0_rows()["ex-1"]
    assert before["completion"] is False
    with get_engine().begin() as conn:
        conn.execute(update(schedule_executions)
                     .where(schedule_executions.c.id == "ex-1")
                     .values(status="success", error=None))
    after = t0.evaluate_execution("ex-1")
    assert after["id"] == before["id"] and after["completion"] is True
    assert count("agent_evaluations") == 1


# --- the leader lease (fail-open) ------------------------------------------------------

class FakeRedis:
    """SET NX EX / GET / EXPIRE / DELETE — the subset the lease uses. Expiry
    is modelled by the test deleting the key (what a lapsed TTL presents)."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def expire(self, key, ttl):
        return key in self.store

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


def _svc():
    return sweep_mod.Tier0SweepService()


def _use_redis(monkeypatch, fake):
    monkeypatch.setattr(sweep_mod, "get_breaker_redis", lambda: fake)


def test_only_one_worker_holds_the_lease(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()
    assert a._worker_id != b._worker_id
    assert a._try_acquire_leadership(180) is True
    assert b._try_acquire_leadership(180) is False
    assert fake.get(sweep_mod._LEADER_KEY) == a._worker_id
    assert a._try_acquire_leadership(180) is True       # the holder refreshes


def test_the_lease_fails_open_without_redis(monkeypatch):
    monkeypatch.setattr(sweep_mod, "get_breaker_redis", lambda: None)
    assert _svc()._try_acquire_leadership(180) is True
    assert _svc()._try_acquire_leadership(180) is True


def test_the_lease_fails_open_on_a_redis_error(monkeypatch):
    class BoomRedis(FakeRedis):
        def set(self, *a, **k):
            raise RuntimeError("redis boom")
    _use_redis(monkeypatch, BoomRedis())
    assert _svc()._try_acquire_leadership(180) is True


def test_release_hands_the_lease_to_a_sibling(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()
    assert a._try_acquire_leadership(180) is True
    a._release_leadership()
    assert b._try_acquire_leadership(180) is True
    # A non-holder's release is a no-op: b still holds it.
    a._release_leadership()
    assert fake.get(sweep_mod._LEADER_KEY) == b._worker_id


def test_a_lapsed_lease_lets_a_sibling_take_over(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()
    assert a._try_acquire_leadership(180) is True
    fake.delete(sweep_mod._LEADER_KEY)                  # TTL lapsed: a is gone
    assert b._try_acquire_leadership(180) is True


@pytest.mark.asyncio
async def test_a_contended_lease_skips_the_tick_and_never_raises(monkeypatch):
    """The loop grades iff this worker holds the lease; a lost lease is a
    skipped tick, not an error and not a wait."""
    for leader, expected in ((True, 1), (False, 0)):
        svc = _svc()
        monkeypatch.setattr(svc, "_try_acquire_leadership", lambda ttl, _l=leader: _l)
        cycle = AsyncMock()
        monkeypatch.setattr(svc, "run_cycle", cycle)

        async def _sleep_once(_):
            svc._running = False
        monkeypatch.setattr(sweep_mod.asyncio, "sleep", _sleep_once)
        svc._running = True
        await svc._run_loop()
        assert cycle.await_count == expected


@pytest.mark.asyncio
async def test_a_failing_cycle_does_not_kill_the_loop(monkeypatch):
    svc = _svc()
    monkeypatch.setattr(svc, "_try_acquire_leadership", lambda ttl: True)
    monkeypatch.setattr(svc, "run_cycle", AsyncMock(side_effect=RuntimeError("tick failed")))
    slept = []

    async def _sleep_once(seconds):
        slept.append(seconds)
        svc._running = False
    monkeypatch.setattr(sweep_mod.asyncio, "sleep", _sleep_once)
    svc._running = True
    await svc._run_loop()                               # no raise
    assert slept == [sweep_mod.SWEEP_INTERVAL_SECONDS]


@pytest.mark.asyncio
async def test_the_cycle_runs_the_sweep_at_the_ruled_bounds(monkeypatch):
    """TD-8: 60 s cadence, 30 s settle, 24 h lookback, 25 rows, 10 s per
    evaluation. The cycle hands exactly those to the sweep."""
    seen = {}

    async def fake_sweep(**kw):
        seen.update(kw)
        return {"candidates": 0, "evaluated": 0, "timed_out": 0, "failed": 0}
    monkeypatch.setattr(sweep_mod.tier0_evaluator, "sweep", fake_sweep)
    await _svc().run_cycle()
    assert seen == {"limit": 25, "settle_seconds": 30, "lookback_hours": 24,
                    "timeout_seconds": 10, "concurrency": 4}
    assert sweep_mod.SWEEP_INTERVAL_SECONDS == 60


def test_the_lease_ttl_outlives_a_missed_tick():
    """3× the cadence, the #1464 shape: one or two missed refreshes keep the
    holder, a dead holder's lease lapses within ~3 cycles."""
    assert sweep_mod.LEASE_TTL_SECONDS == 3 * sweep_mod.SWEEP_INTERVAL_SECONDS


def test_stop_releases_the_lease(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    svc = _svc()
    assert svc._try_acquire_leadership(180) is True
    svc.stop()
    assert sweep_mod._LEADER_KEY not in fake.store


# --- wiring: the loop starts and stops with the backend ----------------------------------

MAIN_PY = _BACKEND / "main.py"


def _phase(name: str) -> ast.AST:
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"main.py no longer defines {name}")


def _method_calls(node: ast.AST, receiver: str) -> set[str]:
    return {
        n.func.attr for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == receiver
    }


def test_main_starts_the_sweep_in_a_pinned_startup_phase_and_stops_it_at_shutdown():
    """Pinned by AST, not by importing `main` (that pulls the whole backend).
    The phases are the #1028 ones the lifespan test already orders; the sweep
    is a watch loop, so it starts beside the heartbeat watch and the
    monitoring loop, and stops with the other background services."""
    assert "start" in _method_calls(_phase("_schedule_watch_loops"), "tier0_sweep_service")
    assert "stop" in _method_calls(_phase("_shutdown_background_services"), "tier0_sweep_service")


def test_the_sweep_never_touches_the_terminal_write_path():
    """TD-1/decision 16: the sweep is the hook, so no terminal writer imports
    the evaluator and the evaluator imports no terminal writer. A lost lease
    therefore cannot delay a terminal execution write — the two never meet."""
    def imports(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                names.add(n.module)
                names.update(f"{n.module}.{a.name}" for a in n.names)
        return names
    svc_dir = _BACKEND / "services"
    for writer in ("task_execution_service.py", "chat_execution_service.py"):
        assert not {m for m in imports(svc_dir / writer) if "tier0" in m}, writer
    for mine in ("tier0_evaluator.py", "tier0_sweep_service.py"):
        assert not {m for m in imports(svc_dir / mine)
                    if "task_execution_service" in m or "chat_execution_service" in m}, mine
