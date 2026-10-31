"""The pair sweep (trinity-enterprise#940, checkpoint B part 2).

Phase 2 of `tier0_sweep_service.run_cycle`, under the Tier-0 lease (TD-5: no
second lease, no new setting): after the Tier-0 batch, grade up to 25 pair
candidates — Tier-0 rows that name a caller and have no pair row yet — each
in a thread under the same 10 s bound. A hung evaluation leaves an all-skip
`evaluator_timeout` pair row through the INSERT-only writer, so it is not
re-picked every tick and never replaces a grade that landed.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent.parent / "src" / "backend"
for p in (str(_BACKEND), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)
from db_harness import count, db_backend  # noqa: E402,F401
from test_940_pair_checks import CALLEE, PAIR, TIER0, _activity, _caller, _child  # noqa: E402

pytestmark = pytest.mark.unit

A, B = "ChildRun00000000000001", "ChildRun00000000000002"
EMPTY = {"candidates": 0, "evaluated": 0, "timed_out": 0, "failed": 0}


@pytest.fixture
def pe(db_backend, monkeypatch):
    from services import tier0_pair_evaluator as mod
    from database import db
    monkeypatch.setattr(db, "get_audit_entries", lambda **kw: [])
    return mod


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _pair(exec_id):
    from database import db
    return db.latest_tier0_for_execution(exec_id, evaluator=PAIR)


def _two_children():
    _caller()
    _child(A, admitted=300)
    _activity(A)
    _child(B, message="/deploy staging", admitted=200)
    _activity(B)


# --- what one pair tick grades -------------------------------------------------

def test_a_tick_writes_a_pair_row_for_each_delegated_run(pe):
    _two_children()
    result = _run(pe.sweep())
    assert result == {"candidates": 2, "evaluated": 2, "timed_out": 0, "failed": 0}
    assert _pair(A)["evaluator"] == PAIR and _pair(B)["evaluator"] == PAIR
    # the Tier-0 rows are untouched beside them, and nothing is picked again
    assert count("agent_evaluations", "evaluator = :e", e=TIER0) == 2
    assert _run(pe.sweep()) == EMPTY


def test_a_tick_is_bounded_and_the_next_tick_continues(pe):
    _two_children()
    assert _run(pe.sweep(limit=1))["evaluated"] == 1
    assert _pair(A) is not None and _pair(B) is None      # oldest Tier-0 row first
    assert _run(pe.sweep(limit=1))["evaluated"] == 1
    assert _pair(B) is not None


def test_one_raising_evaluation_does_not_stop_the_batch(pe, monkeypatch):
    _two_children()
    real = pe.evaluate_pair

    def flaky(execution_id):
        if execution_id == A:
            raise RuntimeError("boom")
        return real(execution_id)
    monkeypatch.setattr(pe, "evaluate_pair", flaky)
    result = _run(pe.sweep())
    assert result["failed"] == 1 and result["evaluated"] == 1
    assert _pair(B) is not None


def test_an_evaluation_that_wrote_no_row_counts_as_failed(pe, monkeypatch):
    _two_children()
    monkeypatch.setattr(pe, "evaluate_pair", lambda execution_id: None)
    assert _run(pe.sweep())["failed"] == 2


def test_an_unreadable_candidate_list_is_an_empty_tick(pe, monkeypatch):
    from database import db

    def broken(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "unevaluated_pair_execution_ids", broken)
    assert _run(pe.sweep()) == EMPTY


# --- the timeout row ----------------------------------------------------------

def test_a_timed_out_evaluation_leaves_a_pair_timeout_row(pe, monkeypatch):
    _two_children()
    real = pe.evaluate_pair
    release = threading.Event()

    def slow(execution_id):
        if execution_id == A:
            release.wait(5)
            return None
        return real(execution_id)
    monkeypatch.setattr(pe, "evaluate_pair", slow)
    result = _run(pe.sweep(timeout_seconds=0.2))
    release.set()
    assert result["timed_out"] == 1 and result["evaluated"] == 1
    row = _pair(A)
    assert row["evaluator"] == PAIR and row["agent_name"] == CALLEE
    checks = row["checks"]["checks"]
    assert {c["id"] for c in checks} == {c.id for c in pe.CHECKS}
    assert all(c["status"] == "skip" and c["reason"] == "evaluator_timeout" for c in checks)
    assert row["checks"]["headline"]["gate_pass"] == 0
    assert row["checks"]["headline"]["gate_fail"] == 0
    # the Tier-0 row is still the Tier-0 grade, and the run is not re-picked
    from database import db
    assert db.latest_tier0_for_execution(A)["checks"]["headline"]["passed"] is True
    assert _run(pe.sweep(timeout_seconds=0.2))["candidates"] == 0


def test_a_timeout_never_overwrites_a_pair_grade_that_landed(pe):
    """The INSERT-only writer: a grade already on the index wins."""
    _two_children()
    graded = pe.evaluate_pair(A)
    assert pe.record_timeout(A) is None
    assert _pair(A)["id"] == graded["id"]
    assert _pair(A)["checks"] == graded["checks"]


def test_a_grade_landing_after_the_existence_check_is_kept(pe, monkeypatch):
    """The reverse race: the existence read said "none", the grade landed,
    then the timeout write ran — it loses on the index, never overwrites."""
    from database import db
    _two_children()
    graded = pe.evaluate_pair(A)
    real = db.latest_tier0_for_execution

    def stale(execution_id, **kw):
        return None if kw.get("evaluator") == PAIR else real(execution_id, **kw)
    monkeypatch.setattr(db, "latest_tier0_for_execution", stale)
    assert pe.record_timeout(A) is None
    assert count("agent_evaluations", "evaluator = :e", e=PAIR) == 1
    assert real(A, evaluator=PAIR)["checks"] == graded["checks"]


def test_a_late_grade_regrades_a_timeout_row_in_place(pe):
    _two_children()
    before = pe.record_timeout(A)
    assert before is not None
    after = pe.evaluate_pair(A)
    assert after["id"] == before["id"]
    assert not any(c.get("reason") == "evaluator_timeout" for c in after["checks"]["checks"])


def test_a_run_with_no_caller_gets_no_timeout_row(pe):
    """The timeout row is a pair row: only a pair candidate can have one."""
    from database import db
    db.upsert_platform_evaluation(CALLEE, execution_id="Solo0000000000000000001",
                                  completion=True, archetype="message",
                                  checks={"schema": 1, "checks": []})
    assert pe.record_timeout("Solo0000000000000000001") is None
    assert count("agent_evaluations", "evaluator = :e", e=PAIR) == 0


# --- phase 2 of the Tier-0 cycle, under the Tier-0 lease ----------------------

def _phases(monkeypatch):
    from services import tier0_pair_evaluator, tier0_sweep_service as sweep_mod
    order, seen = [], {}

    async def t0_sweep(**kw):
        order.append("tier0")
        return dict(EMPTY)

    async def pair_sweep(**kw):
        order.append("pair")
        seen.update(kw)
        return dict(EMPTY)
    monkeypatch.setattr(sweep_mod.tier0_evaluator, "sweep", t0_sweep)
    monkeypatch.setattr(tier0_pair_evaluator, "sweep", pair_sweep)
    return sweep_mod, order, seen


def test_the_cycle_runs_the_pair_phase_after_the_tier0_batch_at_its_bounds(monkeypatch):
    sweep_mod, order, seen = _phases(monkeypatch)
    result = _run(sweep_mod.Tier0SweepService().run_cycle())
    assert order == ["tier0", "pair"]
    assert seen == {"limit": 25, "lookback_hours": 24, "timeout_seconds": 10,
                    "concurrency": 4}
    assert result["pair"] == EMPTY
    assert {k: result[k] for k in EMPTY} == EMPTY       # the Tier-0 counts are unchanged


@pytest.mark.parametrize("leader, expected", [(True, ["tier0", "pair"]), (False, [])])
def test_the_pair_phase_runs_only_under_the_tier0_lease(monkeypatch, leader, expected):
    """TD-5: one leader, one lease. A worker that does not hold `tier0:leader`
    grades nothing — Tier-0 or pair."""
    sweep_mod, order, _ = _phases(monkeypatch)
    svc = sweep_mod.Tier0SweepService()
    monkeypatch.setattr(svc, "_try_acquire_leadership", lambda ttl: leader)
    monkeypatch.setattr(svc, "run_nightly_if_due", AsyncMock(return_value=None))

    async def _sleep_once(_):
        svc._running = False
    monkeypatch.setattr(sweep_mod.asyncio, "sleep", _sleep_once)
    svc._running = True
    _run(svc._run_loop())
    assert order == expected


def test_there_is_still_exactly_one_lease_key():
    """No `tier0pair:leader` or any second key: the module holds one."""
    from services import tier0_sweep_service as sweep_mod
    text = Path(sweep_mod.__file__).read_text(encoding="utf-8")
    assert sweep_mod._LEADER_KEY == "tier0:leader"
    assert text.count(":leader\"") == 1


def test_a_hanging_pair_phase_is_bounded_by_its_own_timeouts(pe, monkeypatch):
    """The phase delays the next Tier-0 tick by at most its bound."""
    _two_children()

    def hang(execution_id):
        time.sleep(1)
        return None
    monkeypatch.setattr(pe, "evaluate_pair", hang)
    t = time.monotonic()
    result = _run(pe.sweep(timeout_seconds=0.1, concurrency=2))
    assert time.monotonic() - t < 0.9
    assert result["timed_out"] == 2
