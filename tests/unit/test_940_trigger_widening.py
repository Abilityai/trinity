"""Tier-0 for delegated fan-out, loop and reminder runs (trinity-enterprise#940,
checkpoint D2 — TD-2, ent#935 addendum A carried here).

Tier-0 graded `schedule` and `agent` runs only, so a fan-out child had no
Tier-0 row and therefore no pair row: the pair scorecard could not see a
single fan-out edge. Now `fan_out`, `loop` and `reminder` runs are graded
too — but only when another agent sent them (`source_agent_name` set). Their
`budget_vs_baseline` series is keyed on `(agent_name, triggered_by,
source_agent_name, playbook)`, so a fan-out child is compared with that
caller's earlier fan-outs of the same playbook, not with every run the callee
ever had. Pinned:

  * the sweep picks the widened triggers with a source, and still skips them
    without one (and still skips `user`);
  * `evaluate_execution` applies the same rule (the second gate);
  * the series key for a delegated run, and the unchanged key for an
    unsourced `agent` run;
  * end to end: a fan-out of three tasks with one brief gives three Tier-0
    rows, three pair rows, and no `duplicate_dispatch` failure (TD-3: distinct
    fan-out tasks are never duplicates).
"""
from __future__ import annotations

import asyncio
import json
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

TIER0, PAIR = "platform:tier0", "platform:tier0:pair"
CALLER, CALLEE = "boss", "worker"
CALLER_RUN = "CallerRun0000000000001"


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _seed(exec_id, *, completed_ago=120, **cols):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    values = dict(id=exec_id, schedule_id="__agent__", agent_name=CALLEE,
                  status="success", triggered_by="fan_out", message="/review PR 12",
                  response="Done", started_at=_ago(completed_ago + 180),
                  queued_at=_ago(completed_ago + 180), completed_at=_ago(completed_ago))
    values.update(cols)
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(**values))
    return exec_id


@pytest.fixture
def t0(db_backend, monkeypatch):
    from services import tier0_evaluator as mod
    from database import db
    monkeypatch.setattr(mod, "_staged_values", lambda: [])
    monkeypatch.setattr(db, "get_audit_entries", lambda **kw: [])
    return mod


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _rows(evaluator):
    from database import db
    return {r["target_id"]: r for r in db.list_fleet_evaluations(None, 500)
            if r["evaluator"] == evaluator}


# --- which runs Tier-0 grades ---------------------------------------------------

def test_delegated_fan_out_loop_and_reminder_runs_are_graded(t0):
    _seed("fo-1", fan_out_id="fo_x", fan_out_task_id="t1", source_agent_name=CALLER)
    _seed("lp-1", triggered_by="loop", loop_id="loop-1", source_agent_name=CALLER)
    _seed("rm-1", triggered_by="reminder", source_agent_name=CALLER)
    result = _run(t0.sweep(limit=25))
    assert set(_rows(TIER0)) == {"fo-1", "lp-1", "rm-1"}
    assert result["evaluated"] == 3
    # the delegation stamp rides along, so these rows are pair candidates
    assert {r["delegation_mode"] for r in _rows(TIER0).values()} == {"fan_out", "loop", "reminder"}
    assert {r["caller_agent"] for r in _rows(TIER0).values()} == {CALLER}


def test_the_same_triggers_without_a_sending_agent_stay_ungraded(t0):
    _seed("fo-1", fan_out_id="fo_x", fan_out_task_id="t1")
    _seed("lp-1", triggered_by="loop", loop_id="loop-1")
    _seed("rm-1", triggered_by="reminder")
    _seed("us-1", triggered_by="user", source_agent_name=CALLER)   # never in the set
    assert _run(t0.sweep(limit=25))["candidates"] == 0
    assert _rows(TIER0) == {}


@pytest.mark.parametrize("trigger,source,graded", [
    ("fan_out", CALLER, True), ("loop", CALLER, True), ("reminder", CALLER, True),
    ("fan_out", None, False), ("loop", None, False), ("reminder", None, False),
    ("user", CALLER, False), ("agent", None, True), ("schedule", None, True),
])
def test_evaluate_execution_applies_the_same_rule(t0, trigger, source, graded):
    _seed("ex-1", triggered_by=trigger, source_agent_name=source)
    assert (t0.evaluate_execution("ex-1") is not None) is graded


# --- the series a delegated run is compared with --------------------------------

def test_a_delegated_runs_series_is_caller_trigger_and_playbook(db_backend):
    from database import db
    for i in range(5):
        _seed(f"h-{i}", source_agent_name=CALLER, cost=1.0, completed_ago=5000 + i)
    _seed("h-other-caller", source_agent_name="someone", cost=9.0, completed_ago=5000)
    _seed("h-other-trigger", triggered_by="agent", source_agent_name=CALLER, cost=9.0,
          completed_ago=5000)
    _seed("h-other-playbook", source_agent_name=CALLER, message="/deploy", cost=9.0,
          completed_ago=5000)
    _seed("now", source_agent_name=CALLER)
    rows = db.recent_series_metrics("now")
    assert len(rows) == 5 and {r["cost"] for r in rows} == {1.0}


def test_an_agent_runs_series_is_keyed_on_its_caller_too(db_backend):
    from database import db
    for i in range(5):
        _seed(f"h-{i}", triggered_by="agent", source_agent_name=CALLER, cost=1.0,
              completed_ago=5000 + i)
    _seed("h-other-caller", triggered_by="agent", source_agent_name="someone", cost=9.0,
          completed_ago=5000)
    _seed("now", triggered_by="agent", source_agent_name=CALLER)
    rows = db.recent_series_metrics("now")
    assert len(rows) == 5 and {r["cost"] for r in rows} == {1.0}


# --- end to end: a fan-out of three ------------------------------------------------

def test_a_fan_out_of_three_gives_three_edges_and_no_duplicate(t0):
    from services import tier0_pair_evaluator as pe
    _seed(CALLER_RUN, agent_name=CALLER, triggered_by="schedule", schedule_id="sched-1",
          message="/plan-sprint", completed_ago=40)
    for i in range(3):
        _seed(f"fo-{i}", fan_out_id="fo_batch", fan_out_task_id=f"t{i}",
              source_agent_name=CALLER, parent_execution_id=CALLER_RUN,
              message="/review PR 12", completed_ago=120 + i)
    _run(t0.sweep(limit=25))
    assert {"fo-0", "fo-1", "fo-2"} <= set(_rows(TIER0))
    _run(pe.sweep(limit=25))
    pairs = _rows(PAIR)
    assert set(pairs) == {"fo-0", "fo-1", "fo-2"}
    for row in pairs.values():
        checks = row["checks"] if isinstance(row["checks"], dict) else json.loads(row["checks"])
        dup = next(c for c in checks["checks"] if c["id"] == "duplicate_dispatch")
        assert dup["status"] != "fail", dup
