"""Evaluation scorecard writer (trinity-enterprise#937, C4).

`services/eval_scorecard_service.py` writes the platform's daily `eval.*`
points under the reserved owner `platform:eval`, on the real schema
(`tests/db_harness.py`, both dialects):

  * one point per UTC day the runs completed in, per agent and per pair;
  * the five counts (incl. `eval.graded`, 08:50Z ruling) beside the rates, a
    rate with no denominator not written;
  * a second write of the same day restates (`corrected`), never duplicates;
  * `tier0_evaluator.evaluate_execution` and `record_timeout` call the hook;
  * the nightly restate runs from the sweep loop, once per UTC day after 00:10;
  * T5: a Workspace rating carries the rated message's `execution_id`.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

OWNER = "platform:eval"
NOW = datetime.now(timezone.utc)
DAY0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def seed(exec_id, *, agent="callee", source=None, completed=None, status="success"):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    completed = completed or (DAY0 + timedelta(minutes=5))
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id="s1", agent_name=agent, status=status,
            triggered_by="agent" if source else "schedule", message="/x",
            source_agent_name=source, started_at=_iso(completed - timedelta(minutes=1)),
            completed_at=_iso(completed), duration_ms=1000))
    return exec_id


def grade(exec_id, *, agent="callee", passed=True, completion=True):
    """The caller is stamped by the writer from the run's `source_agent_name`."""
    from database import db
    db.upsert_platform_evaluation(
        agent, execution_id=exec_id, completion=completion,
        checks={"schema": 1, "headline": {"passed": passed, "gate_pass": int(passed),
                                          "gate_fail": int(not passed), "gate_skip": 0},
                "checks": [] if passed else [{"id": "credential_echo", "tier": "gate",
                                              "status": "fail", "reason": "x"}]})


def points(**dims):
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import metric_points
    with get_engine().connect() as conn:
        rows = conn.execute(select(metric_points).where(
            metric_points.c.agent_name == OWNER)).mappings().all()
    out = {}
    for r in rows:
        d = r["dims"] if isinstance(r["dims"], dict) else __import__("json").loads(r["dims"])
        if d == dims:
            out[r["metric"]] = (r["value_numeric"], r["ts"])
    return out


@pytest.fixture
def svc():
    from services import eval_scorecard_service
    return eval_scorecard_service


# --- the points ---------------------------------------------------------------

def test_points_for_day_writes_counts_and_only_rates_that_have_a_denominator(svc):
    rows = [
        {"execution_id": "e1", "agent_name": "a", "caller_agent": None, "completion": True,
         "checks": {"headline": {"passed": True, "gate_pass": 1, "gate_fail": 0}}},
        {"execution_id": "e2", "agent_name": "a", "caller_agent": "boss", "completion": False,
         "checks": {"headline": {"passed": False, "gate_pass": 0, "gate_fail": 1}}},
    ]
    ts = _iso(DAY0)
    out = svc.points_for_day(ts, rows, {}, fleet=False)
    by = {(p["metric"], tuple(sorted(p["dims"].items()))): p for p in out}
    agent = (("agent", "a"),)
    pair = (("callee", "a"), ("caller", "boss"))
    assert by[("eval.evaluated", agent)]["value_numeric"] == 2
    assert by[("eval.graded", agent)]["value_numeric"] == 2
    assert by[("eval.passed", agent)]["value_numeric"] == 1
    assert by[("eval.checks_passed_rate", agent)]["value_numeric"] == 0.5
    assert by[("eval.evaluated", pair)]["value_numeric"] == 1
    assert by[("eval.judged", agent)]["value_numeric"] == 0
    # No judge verdict → no quality / agreement point at all (absence, not 0).
    assert ("eval.quality_mean", agent) not in by
    assert ("eval.judge_human_agreement", agent) not in by
    assert all(p["ts"] == ts for p in out)
    # ent#938: a scoped fold writes per-agent and per-pair series only — 9
    # agent + 9 pair metric slots minus the absent quality/agreement pairs.
    assert {tuple(sorted(p["dims"])) for p in out} == {("agent",), ("callee", "caller")}
    assert len(out) == 14


def test_on_tier0_row_writes_the_runs_day_under_the_reserved_owner(db_backend, svc):
    seed("e1")
    grade("e1")
    counts = svc.on_tier0_row("e1")
    assert counts is not None and counts.recorded > 0
    got = points(agent="callee")
    assert got["eval.evaluated"][0] == 1
    assert got["eval.graded"][0] == 1
    assert got["eval.checks_passed_rate"][0] == 1.0
    assert got["eval.evaluated"][1] == _iso(DAY0)          # bucketed on the day


def test_a_second_write_of_the_same_day_restates_rather_than_duplicates(db_backend, svc):
    seed("e1")
    grade("e1")
    svc.on_tier0_row("e1")
    seed("e2", completed=DAY0 + timedelta(minutes=9))
    grade("e2", passed=False)
    counts = svc.on_tier0_row("e2")
    assert counts.corrected >= 1 and counts.recorded == 0
    got = points(agent="callee")
    assert got["eval.evaluated"][0] == 2
    assert got["eval.passed"][0] == 1
    again = svc.on_tier0_row("e2")
    assert again.corrected == 0 and again.recorded == 0       # unchanged → dedup


def test_pair_series_is_written_for_a_delegated_run(db_backend, svc):
    seed("e1", source="boss")
    grade("e1")
    svc.on_tier0_row("e1")
    assert points(caller="boss", callee="callee")["eval.evaluated"][0] == 1


def test_on_tier0_row_never_raises(db_backend, svc, monkeypatch):
    from database import db
    monkeypatch.setattr(db, "get_execution", lambda _id: (_ for _ in ()).throw(RuntimeError()))
    assert svc.on_tier0_row("nope") is None
    monkeypatch.undo()
    assert svc.on_tier0_row("missing") is None


def test_nightly_pass_restates_recent_days_for_the_fleet_and_is_idempotent(db_backend, svc):
    seed("old", agent="x", completed=DAY0 - timedelta(days=2) + timedelta(hours=3))
    grade("old", agent="x")
    seed("new", agent="y")
    grade("new", agent="y")
    first = svc.nightly_pass(NOW)
    assert first["failed"] == 0 and first["written"] > 0
    assert points(agent="x")["eval.evaluated"][1] == _iso(DAY0 - timedelta(days=2))
    assert points(agent="y")["eval.evaluated"][0] == 1
    assert svc.nightly_pass(NOW)["written"] == 0


def test_a_capped_window_is_never_written_as_a_count(db_backend, svc, monkeypatch):
    from database import db
    monkeypatch.setattr(db, "evaluation_window_rows",
                        lambda **kw: ([{"agent_name": "a", "execution_id": "e"}], True))
    seed("e1")
    assert svc.on_tier0_row("e1") is None
    assert points(agent="a") == {}


def _capped(monkeypatch, cap):
    """Shrink the row cap so a page is smaller than the agent set."""
    from database import db
    real = db.evaluation_window_rows
    monkeypatch.setattr(db, "evaluation_window_rows", lambda **kw: real(cap=cap, **kw))


def test_an_over_cap_fleet_day_retries_every_agent_of_the_day_not_the_page(
        db_backend, svc, monkeypatch):
    """A capped fleet page (newest first) names only the newest agents; the
    per-agent retry must come from the day's distinct agents, or an agent
    whose rows sit past the page is silently left stale."""
    for i, agent in enumerate(("a", "b", "c")):
        seed(f"e-{agent}", agent=agent, completed=DAY0 + timedelta(minutes=5 + i))
        grade(f"e-{agent}", agent=agent)
    _capped(monkeypatch, 1)
    result = svc.nightly_pass(NOW, days=0)
    assert result["failed"] == 0
    for agent in ("a", "b", "c"):
        assert points(agent=agent)["eval.evaluated"][0] == 1, agent


def test_an_agent_over_its_own_cap_is_skipped_and_logged(db_backend, svc, monkeypatch, caplog):
    seed("a1", agent="a", completed=DAY0 + timedelta(minutes=5))
    grade("a1", agent="a")
    seed("a2", agent="a", completed=DAY0 + timedelta(minutes=6))
    grade("a2", agent="a")
    seed("b1", agent="b", completed=DAY0 + timedelta(minutes=1))
    grade("b1", agent="b")
    _capped(monkeypatch, 1)
    with caplog.at_level("WARNING", logger="services.eval_scorecard_service"):
        result = svc.nightly_pass(NOW, days=0)
    assert result["failed"] == 1
    assert points(agent="a") == {}
    assert points(agent="b")["eval.evaluated"][0] == 1
    assert any("a over the row cap" in r.getMessage() for r in caplog.records)


# --- the hooks ----------------------------------------------------------------

def test_evaluate_execution_restates_the_scorecard_after_its_write(db_backend, monkeypatch):
    from services import eval_scorecard_service, tier0_evaluator
    seen = []
    monkeypatch.setattr(eval_scorecard_service, "on_tier0_row", seen.append)
    seed("e1")
    assert tier0_evaluator.evaluate_execution("e1") is not None
    assert seen == ["e1"]


def test_the_hook_lands_real_points_end_to_end(db_backend):
    from services import tier0_evaluator
    seed("e1")
    tier0_evaluator.evaluate_execution("e1")
    assert points(agent="callee")["eval.evaluated"][0] == 1


def test_a_timeout_row_restates_the_scorecard_too(db_backend, monkeypatch):
    from services import eval_scorecard_service, tier0_evaluator
    seen = []
    monkeypatch.setattr(eval_scorecard_service, "on_tier0_row", seen.append)
    seed("e1")
    assert tier0_evaluator.record_timeout("e1") is not None
    assert seen == ["e1"]
    assert tier0_evaluator.record_timeout("e1") is None      # already graded: no restate
    assert seen == ["e1"]


def test_nightly_is_due_once_per_day_after_0010(svc):
    assert not svc.nightly_due(DAY0 + timedelta(minutes=9), None)
    assert svc.nightly_due(DAY0 + timedelta(minutes=10), None)
    assert not svc.nightly_due(DAY0 + timedelta(hours=3), DAY0.strftime("%Y-%m-%d"))


def test_the_sweep_loop_runs_the_nightly_restate_under_its_lease(monkeypatch):
    """Executes the real `_run_loop` for one leader tick (production wiring)."""
    from services import eval_scorecard_service, tier0_sweep_service as mod
    svc = mod.Tier0SweepService()
    calls = []

    async def _cycle():
        return {}

    monkeypatch.setattr(svc, "run_cycle", _cycle)
    monkeypatch.setattr(svc, "_try_acquire_leadership", lambda ttl: True)
    monkeypatch.setattr(eval_scorecard_service, "nightly_due", lambda now, last: last is None)
    monkeypatch.setattr(eval_scorecard_service, "nightly_pass",
                        lambda now: calls.append(now) or {"days": 4})

    async def _stop(_s):
        svc._running = False

    monkeypatch.setattr(mod.asyncio, "sleep", _stop)
    svc._running = True
    asyncio.run(svc._run_loop())
    assert len(calls) == 1
    assert svc._nightly_day is not None
    # A second tick the same day does not run it again.
    svc._running = True
    asyncio.run(svc._run_loop())
    assert len(calls) == 1


# --- T5 -----------------------------------------------------------------------

def test_a_workspace_rating_carries_the_rated_messages_execution_id(monkeypatch):
    import database
    from client_portal import service as mod
    seen = {}
    monkeypatch.setattr(mod, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(mod.db, "get_portal_message", lambda mid: {
        "id": "m1", "agent_name": "scribe", "client_email": "c@example.com",
        "role": "assistant", "execution_id": "exec-42"})
    monkeypatch.setattr(database.db, "upsert_workspace_rating",
                        lambda agent, **kw: seen.update(kw) or {"created_at": "t"})
    mod.submit_rating("scribe", "c@example.com", target_kind="message", target_id="m1",
                      rating="up")
    assert seen["execution_id"] == "exec-42"


def test_the_portal_message_read_returns_execution_id(db_backend):
    from client_portal import db as pdb
    try:
        pdb.add_portal_message("m1", "scribe", "c@example.com", "assistant", "hi", None,
                               _iso(NOW), execution_id="exec-42")
    except Exception as e:  # noqa: BLE001 — the table is created by the private runner
        pytest.skip(f"portal messages table absent on this harness: {type(e).__name__}")
    assert pdb.get_portal_message("m1")["execution_id"] == "exec-42"
