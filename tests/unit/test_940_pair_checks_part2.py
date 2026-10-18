"""Pair checks, part 2 (trinity-enterprise#940, checkpoint B).

The relay and signal checks — `relay_unverified`, `relayed_failed_work`,
`failed_with_side_effects`, `rework_bounds` — and the CP-A follow-up: a pair
row written while the caller was still running (`skip: caller_running`) is
not final; the candidate query re-picks it once the caller ends. Same real-
schema harness as part 1.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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
from test_940_pair_checks import (  # noqa: E402
    CALLEE, CALLER, CALLER_RUN, PAIR, _activity, _ago, _check, _child, _exec,
)

pytestmark = pytest.mark.unit

CHILD = "ChildRun00000000000001"


@pytest.fixture
def pe(db_backend, monkeypatch):
    from services import tier0_pair_evaluator as mod
    from database import db
    monkeypatch.setattr(db, "get_audit_entries", lambda **kw: [])
    return mod


def _caller(status="success", response="Delegated, all done.", completed=30):
    _exec(CALLER_RUN, CALLER, status=status, message="/plan-sprint", triggered_by="schedule",
          admitted=900, response=response)
    _set(CALLER_RUN, completed_at=_ago(completed) if status != "running" else None)


def _set(exec_id, **values):
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(update(schedule_executions)
                     .where(schedule_executions.c.id == exec_id).values(**values))


def _read_receipt(related, *, at, agent=CALLER, act_id=None):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_activities
    with get_engine().begin() as conn:
        conn.execute(insert(agent_activities).values(
            id=act_id or f"read-{related}-{agent}", agent_name=agent,
            activity_type="agent_collaboration", activity_state="completed",
            triggered_by="agent", related_execution_id=related, created_at=at,
            started_at=at, details=json.dumps({"source_agent": agent, "action": "read_result",
                                               "execution_id": related})))


def _tier0_headline(exec_id, passed):
    from database import db
    db.upsert_platform_evaluation(
        CALLEE, execution_id=exec_id, completion=passed, archetype="playbook",
        checks={"schema": 1, "headline": {"passed": passed, "gate_pass": 1,
                                          "gate_fail": 0 if passed else 1},
                "intent_ref": {"kind": "playbook", "ref": "review", "unresolved": False},
                "checks": []})


# --- the caller_running re-pick (CP-A follow-up) ------------------------------

def test_a_caller_running_pair_row_is_re_picked_once_the_caller_ends(pe):
    from database import db
    _caller(status="running")
    _child(CHILD)
    _activity(CHILD)
    row = pe.evaluate_pair(CHILD)
    assert _check(row, "duplicate_dispatch")["reason"] == "caller_running"
    # still running: a skip row is the latest word, nothing to re-pick yet
    assert CHILD not in db.unevaluated_pair_execution_ids()
    _set(CALLER_RUN, status="success", completed_at=_ago(5))
    assert db.unevaluated_pair_execution_ids() == [CHILD]
    row = pe.evaluate_pair(CHILD)
    assert _check(row, "duplicate_dispatch")["status"] != "skip" or \
        _check(row, "duplicate_dispatch")["reason"] != "caller_running"
    # graded with the caller ended: final, never re-picked again
    assert db.unevaluated_pair_execution_ids() == []
    assert count("agent_evaluations", "evaluator = :e", e=PAIR) == 1


def test_a_final_pair_row_is_never_re_picked(pe):
    from database import db
    _caller()
    _child(CHILD)
    _activity(CHILD)
    pe.evaluate_pair(CHILD)
    assert db.unevaluated_pair_execution_ids() == []


# --- relay_unverified / relayed_failed_work -----------------------------------

def _relaying_caller():
    _caller(response=f"Done: worker finished execution {CHILD}, PR approved.")


def test_no_relay_skips_both_relay_checks(pe):
    _caller()
    _child(CHILD)
    _activity(CHILD)
    row = pe.evaluate_pair(CHILD)
    assert _check(row, "relay_unverified")["reason"] == "no_relay"
    assert _check(row, "relayed_failed_work")["reason"] == "no_relay"


def test_a_relay_before_the_receipt_channel_existed_skips(pe):
    _relaying_caller()
    _child(CHILD)
    _activity(CHILD)
    row = pe.evaluate_pair(CHILD)
    assert _check(row, "relay_unverified")["reason"] == "no_readback_record"


def test_a_relay_without_reading_the_result_fails(pe):
    _relaying_caller()
    _child(CHILD)
    _activity(CHILD)
    # the channel is live (some other read happened before this turn ended)
    _read_receipt("SomeOtherRun0000000001", at=_ago(800), agent="someone")
    row = pe.evaluate_pair(CHILD)
    c = _check(row, "relay_unverified")
    assert c["status"] == "fail" and c["reason"] == "unread_relay"
    assert row["checks"]["headline"]["passed"] is False


def test_a_relay_after_reading_the_result_passes(pe):
    _relaying_caller()
    _child(CHILD)
    _activity(CHILD)
    _read_receipt(CHILD, at=_ago(45))      # after callee end (60s), before caller end (30s)
    assert _check(pe.evaluate_pair(CHILD), "relay_unverified")["status"] == "pass"


def test_a_read_before_the_callee_ended_does_not_count(pe):
    _relaying_caller()
    _child(CHILD)
    _activity(CHILD)
    _read_receipt(CHILD, at=_ago(200))     # read while the callee was still running
    assert _check(pe.evaluate_pair(CHILD), "relay_unverified")["reason"] == "unread_relay"


def test_another_agents_read_does_not_verify_the_callers_relay(pe):
    _relaying_caller()
    _child(CHILD)
    _activity(CHILD)
    _read_receipt(CHILD, at=_ago(45), agent="bystander")
    assert _check(pe.evaluate_pair(CHILD), "relay_unverified")["status"] == "fail"


def test_relaying_failed_work_fails_and_honest_work_passes(pe):
    _relaying_caller()
    _child(CHILD)
    _activity(CHILD)
    _tier0_headline(CHILD, passed=False)
    assert _check(pe.evaluate_pair(CHILD), "relayed_failed_work")["status"] == "fail"
    _tier0_headline(CHILD, passed=True)
    assert _check(pe.evaluate_pair(CHILD), "relayed_failed_work")["status"] == "pass"


def test_relay_checks_wait_for_the_caller(pe):
    _caller(status="running", response=f"working on {CHILD}")
    _child(CHILD)
    _activity(CHILD)
    row = pe.evaluate_pair(CHILD)
    for cid in ("relay_unverified", "relayed_failed_work", "rework_bounds"):
        assert _check(row, cid)["reason"] == "caller_running", cid


# --- failed_with_side_effects --------------------------------------------------

def test_side_effects_skip_a_successful_callee(pe):
    _caller()
    _child(CHILD)
    _activity(CHILD)
    c = _check(pe.evaluate_pair(CHILD), "failed_with_side_effects")
    assert c["reason"] == "not_failed" and c["tier"] == "signal"


def test_a_failed_callee_with_an_attributable_receipt_fails_the_signal(pe):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import idempotency_keys
    _caller()
    _child(CHILD, status="failed")
    _activity(CHILD)
    row = pe.evaluate_pair(CHILD)
    assert _check(row, "failed_with_side_effects")["status"] == "pass"
    with get_engine().begin() as conn:
        conn.execute(insert(idempotency_keys).values(
            scope=f"effect:{CHILD}", idempotency_key="k1", execution_id=CHILD,
            status="completed", created_at=_ago(70), updated_at=_ago(70)))
    row = pe.evaluate_pair(CHILD)
    c = _check(row, "failed_with_side_effects")
    assert c["status"] == "fail" and c["evidence"]["effects"] == 1
    # a signal never moves the headline
    assert row["checks"]["headline"]["passed"] is True


# --- rework_bounds --------------------------------------------------------------

def _history(n_turns, trips_each):
    for t in range(n_turns):
        parent = f"HistCaller{t:012d}"
        _exec(parent, CALLER, triggered_by="schedule", message="/plan-sprint", admitted=5000)
        for k in range(trips_each):
            _exec(f"Hist{t:04d}x{k:04d}abcdefgh", CALLEE, parent=parent, source=CALLER,
                  message=f"/review PR {k}", admitted=4000)


def test_rework_skips_without_enough_history(pe):
    _caller()
    _child(CHILD)
    _activity(CHILD)
    _history(2, 1)
    c = _check(pe.evaluate_pair(CHILD), "rework_bounds")
    assert c["reason"] == "insufficient_history" and c["evidence"]["n"] == 2


def test_rework_above_the_pair_p95_fails_and_within_passes(pe):
    _caller()
    _history(6, 1)
    _child(CHILD)
    _activity(CHILD)
    assert _check(pe.evaluate_pair(CHILD), "rework_bounds")["status"] == "pass"
    for i in range(2, 4):
        _child(f"ChildRun0000000000000{i}", message=f"/review PR {i}")
    c = _check(pe.evaluate_pair(CHILD), "rework_bounds")
    assert c["status"] == "fail" and c["evidence"]["round_trips"] == 3
    assert c["evidence"]["p95"] == 1
