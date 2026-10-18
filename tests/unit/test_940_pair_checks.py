"""Pair checks, part 1 (trinity-enterprise#940, checkpoint A).

Five gate checks over a delegated run's edge — `dispatch_exists`,
`one_sided_record`, `duplicate_dispatch`, `resend_after_receipt`,
`wrong_route` — driven end-to-end through `evaluate_pair` on the real schema
(caller run, callee run(s) linked by `parent_execution_id`, the callee's
Tier-0 row, the caller's `agent_collaboration` activity rows), plus the
writer's `evaluator=` fence: the pair row is a second platform row beside the
Tier-0 grade, and the writer refuses any evaluator outside the closed set.
"""
from __future__ import annotations

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
from db_harness import count, db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

PAIR = "platform:tier0:pair"
TIER0 = "platform:tier0"
CALLER, CALLEE = "boss", "worker"
CALLER_RUN = "CallerRun0000000000001"


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _exec(exec_id, agent, *, status="success", message="/review PR 12", parent=None,
          source=None, triggered_by="agent", admitted=300, response="ok", **extra):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    at = extra.pop("at", None) or _ago(admitted)
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id="__agent__", agent_name=agent, status=status,
            triggered_by=triggered_by,
            message=message, response=response, source_agent_name=source,
            parent_execution_id=parent, queued_at=at,
            started_at=at, completed_at=_ago(60), **extra))


def _caller(status="success", response="Delegated, all done."):
    _exec(CALLER_RUN, CALLER, status=status, message="/plan-sprint", triggered_by="schedule",
          admitted=900, response=response)


def _child(exec_id, *, message="/review PR 12", admitted=300, unresolved=False, **extra):
    from database import db
    _exec(exec_id, CALLEE, parent=CALLER_RUN, source=CALLER, message=message,
          admitted=admitted, **extra)
    ref = message.lstrip("/").split()[0] if message.startswith("/") else None
    db.upsert_platform_evaluation(
        CALLEE, execution_id=exec_id, completion=True,
        archetype="playbook" if ref else "message",
        checks={"schema": 1, "headline": {"passed": True, "gate_pass": 1, "gate_fail": 0},
                "intent_ref": {"kind": "playbook" if ref else "message", "ref": ref,
                               "unresolved": unresolved},
                "checks": []})


def _activity(related, *, agent=CALLER, details_exec=None, target=CALLEE):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_activities
    with get_engine().begin() as conn:
        conn.execute(insert(agent_activities).values(
            id=f"act-{related}-{details_exec or 'x'}", agent_name=agent,
            activity_type="agent_collaboration", activity_state="completed", triggered_by="agent",
            related_execution_id=related, created_at=_ago(250), started_at=_ago(250),
            details=json.dumps({"source_agent": agent, "target_agent": target,
                                "action": "chat",
                                "execution_id": details_exec or related})))


@pytest.fixture
def pe(db_backend, monkeypatch):
    from services import tier0_pair_evaluator as mod
    from database import db
    monkeypatch.setattr(db, "get_audit_entries", lambda **kw: [])
    return mod


def _check(row, check_id):
    return next(c for c in row["checks"]["checks"] if c["id"] == check_id)


# --- the writer's evaluator fence ---------------------------------------------

def test_the_pair_row_is_a_second_platform_row_beside_the_tier0_grade(pe):
    from database import db
    _caller()
    _child("ChildRun00000000000001")
    _activity("ChildRun00000000000001")
    row = pe.evaluate_pair("ChildRun00000000000001")
    assert row and row["evaluator"] == PAIR and row["target_id"] == "ChildRun00000000000001"
    assert row["caller_agent"] == CALLER and row["parent_execution_id"] == CALLER_RUN
    # the Tier-0 read still answers with the Tier-0 row; the pair read with the pair row
    assert db.latest_tier0_for_execution("ChildRun00000000000001")["evaluator"] == TIER0
    assert db.latest_tier0_for_execution("ChildRun00000000000001", evaluator=PAIR)["id"] == row["id"]
    # re-grade is in place: one pair row per run
    pe.evaluate_pair("ChildRun00000000000001")
    assert count("agent_evaluations", "evaluator = :e", e=PAIR) == 1
    assert row["checks"]["edge"] == {"caller": CALLER, "callee": CALLEE, "mode": None,
                                     "parent_execution_id": CALLER_RUN}


@pytest.mark.parametrize("bad", ["admin:root", "platform:tier1:x", "platform:tier0:other", ""])
def test_the_writer_refuses_an_evaluator_outside_the_closed_set(db_backend, bad):
    from database import db
    _exec("ex-1", CALLEE)
    for call in (db.upsert_platform_evaluation, db.insert_platform_evaluation_if_absent):
        with pytest.raises(ValueError):
            call(CALLEE, execution_id="ex-1", completion=True, checks={}, evaluator=bad)
    with pytest.raises(ValueError):
        db.latest_tier0_for_execution("ex-1", evaluator=bad)
    assert count("agent_evaluations") == 0


def test_insert_if_absent_files_under_the_named_evaluator_and_never_replaces(db_backend):
    from database import db
    from db.evaluations import PLATFORM_EVALUATORS, TIER0_PAIR_EVALUATOR
    assert PLATFORM_EVALUATORS == (TIER0, PAIR) and TIER0_PAIR_EVALUATOR == PAIR
    _exec("ex-1", CALLEE)
    db.upsert_platform_evaluation(CALLEE, execution_id="ex-1", completion=True, checks={"a": 1})
    first = db.insert_platform_evaluation_if_absent(
        CALLEE, execution_id="ex-1", completion=True, checks={"b": 1}, evaluator=PAIR)
    assert first and first["evaluator"] == PAIR
    assert db.insert_platform_evaluation_if_absent(
        CALLEE, execution_id="ex-1", completion=True, checks={"c": 1}, evaluator=PAIR) is None
    assert db.latest_tier0_for_execution("ex-1")["checks"] == {"a": 1}


def test_candidates_are_tier0_rows_with_a_caller_and_no_pair_row(pe):
    from database import db
    _caller()
    _child("ChildRun00000000000001")
    _exec("solo-run", CALLEE)  # no caller
    db.upsert_platform_evaluation(CALLEE, execution_id="solo-run", completion=True, checks={})
    assert db.unevaluated_pair_execution_ids() == ["ChildRun00000000000001"]
    pe.evaluate_pair("ChildRun00000000000001")
    assert db.unevaluated_pair_execution_ids() == []


def test_a_run_without_a_caller_is_not_a_pair_candidate(pe):
    from database import db
    _exec("solo-run", CALLEE)
    db.upsert_platform_evaluation(CALLEE, execution_id="solo-run", completion=True, checks={})
    assert pe.evaluate_pair("solo-run") is None
    assert pe.evaluate_pair("no-such-run") is None
    assert count("agent_evaluations", "evaluator = :e", e=PAIR) == 0


# --- 1. dispatch_exists ------------------------------------------------------

def test_dispatch_exists_passes_when_every_claimed_id_is_a_spawned_run(pe):
    _caller()
    _child("ChildRun00000000000001")
    _activity("ChildRun00000000000001")
    assert _check(pe.evaluate_pair("ChildRun00000000000001"), "dispatch_exists")["status"] == "pass"


def test_dispatch_exists_fails_on_a_claimed_id_that_resolves_to_nothing(pe):
    _caller()
    _child("ChildRun00000000000001")
    _activity("ChildRun00000000000001", details_exec="GhostRun00000000000009")
    c = _check(pe.evaluate_pair("ChildRun00000000000001"), "dispatch_exists")
    assert c["status"] == "fail" and c["reason"] == "unresolved_id"
    assert c["evidence"]["unresolved"] == ["GhostRun00000000000009"]


def test_dispatch_exists_fails_on_a_reply_naming_an_execution_that_exists_nowhere(pe):
    _caller(response="Sent it — execution Zz9GhostExec000000000X is running.")
    _child("ChildRun00000000000001")
    c = _check(pe.evaluate_pair("ChildRun00000000000001"), "dispatch_exists")
    assert c["status"] == "fail" and "Zz9GhostExec000000000X" in c["evidence"]["unresolved"]


def test_dispatch_exists_skips_when_nothing_is_claimed(pe):
    _caller(response="Done.")
    _child("ChildRun00000000000001")
    c = _check(pe.evaluate_pair("ChildRun00000000000001"), "dispatch_exists")
    assert (c["status"], c["reason"]) == ("skip", "no_claimed_ids")


def test_a_long_english_word_is_never_a_claimed_id(pe):
    assert pe.claimed_tokens("execution counterrevolutionaries x")["executions"] == []
    assert pe.claimed_tokens("execution AbCdEf1234567890_-XyZq")["executions"] == [
        "AbCdEf1234567890_-XyZq"]


# --- 2. one_sided_record -------------------------------------------------------

def test_one_sided_record_fails_when_the_caller_kept_no_record(pe):
    _caller()
    _child("ChildRun00000000000001")
    c = _check(pe.evaluate_pair("ChildRun00000000000001"), "one_sided_record")
    assert (c["status"], c["reason"]) == ("fail", "no_caller_record")


def test_one_sided_record_passes_with_the_callers_activity_row(pe):
    _caller()
    _child("ChildRun00000000000001")
    _activity("ChildRun00000000000001")
    assert _check(pe.evaluate_pair("ChildRun00000000000001"), "one_sided_record")["status"] == "pass"


def test_a_record_on_another_agent_does_not_count_for_the_caller(pe):
    _caller()
    _child("ChildRun00000000000001")
    _activity("ChildRun00000000000001", agent="someone-else")
    assert _check(pe.evaluate_pair("ChildRun00000000000001"), "one_sided_record")["status"] == "fail"


def test_one_sided_record_skips_a_fan_out_child(pe):
    _caller()
    _child("ChildRun00000000000001", fan_out_id="fo_x", fan_out_task_id="t1")
    c = _check(pe.evaluate_pair("ChildRun00000000000001"), "one_sided_record")
    assert (c["status"], c["reason"]) == ("skip", "no_activity_channel")


def test_one_sided_record_skips_when_the_caller_row_is_gone(pe):
    _child("ChildRun00000000000001")   # parent id dangling: no caller row
    row = pe.evaluate_pair("ChildRun00000000000001")
    for cid in ("one_sided_record", "dispatch_exists", "duplicate_dispatch"):
        assert (_check(row, cid)["status"], _check(row, cid)["reason"]) == ("skip", "caller_missing")


def test_unreadable_activities_skip_rather_than_fail(pe, monkeypatch):
    from database import db
    _caller()
    _child("ChildRun00000000000001")
    monkeypatch.setattr(db, "collaboration_rows_for_executions",
                        lambda ids: (_ for _ in ()).throw(RuntimeError("down")))
    c = _check(pe.evaluate_pair("ChildRun00000000000001"), "one_sided_record")
    assert (c["status"], c["reason"]) == ("skip", "activities_unreadable")


# --- 3/4. duplicate_dispatch + resend_after_receipt ---------------------------------

def test_a_reworded_resend_is_a_duplicate_and_a_resend(pe):
    _caller()
    _child("ChildRun00000000000001", message="/review PR 12 and check the tests", admitted=300)
    _child("ChildRun00000000000002", message="/review  pr 12 and check the tests!", admitted=200)
    row = pe.evaluate_pair("ChildRun00000000000002")
    dup = _check(row, "duplicate_dispatch")
    assert dup["status"] == "fail" and dup["evidence"]["siblings"][0]["id"] == "ChildRun00000000000001"
    resend = _check(row, "resend_after_receipt")
    assert resend["status"] == "fail"
    assert resend["evidence"]["resends"] == [{"first": "ChildRun00000000000001",
                                               "resent": "ChildRun00000000000002"}]


def test_two_different_briefs_to_one_callee_are_not_duplicates(pe):
    _caller()
    _child("ChildRun00000000000001", message="/review PR 12")
    _child("ChildRun00000000000002", message="/review PR 13", admitted=200)
    row = pe.evaluate_pair("ChildRun00000000000002")
    assert _check(row, "duplicate_dispatch")["status"] == "pass"
    assert _check(row, "resend_after_receipt")["reason"] == "no_duplicate"


def test_two_prose_briefs_are_compared_by_text_not_by_series_key(pe):
    """Every prose message shares the series key "message" — that alone must
    never make two different asks a duplicate."""
    _caller()
    _child("ChildRun00000000000001", message="summarise the Q3 board deck")
    _child("ChildRun00000000000002", message="draft a reply to the vendor", admitted=200)
    assert _check(pe.evaluate_pair("ChildRun00000000000002"), "duplicate_dispatch")["status"] == "pass"


def test_copies_admitted_at_the_same_instant_are_parallel_not_a_resend(pe):
    _caller()
    same = _ago(300)
    _child("ChildRun00000000000001", at=same)
    _child("ChildRun00000000000002", at=same)
    row = pe.evaluate_pair("ChildRun00000000000002")
    assert _check(row, "duplicate_dispatch")["status"] == "fail"
    assert _check(row, "resend_after_receipt")["status"] == "pass"


def test_a_retry_chain_is_one_dispatch(pe):
    _caller()
    _child("ChildRun00000000000001", status="failed")
    _child("ChildRun00000000000002", admitted=200, retry_of_execution_id="ChildRun00000000000001")
    assert _check(pe.evaluate_pair("ChildRun00000000000002"), "duplicate_dispatch")["status"] == "pass"


def test_loop_iterations_are_never_duplicates(pe):
    _caller()
    _child("ChildRun00000000000001", loop_id="loop-1")
    _child("ChildRun00000000000002", loop_id="loop-1", admitted=200)
    c = _check(pe.evaluate_pair("ChildRun00000000000002"), "duplicate_dispatch")
    assert (c["status"], c["reason"]) == ("skip", "loop_mode")


def test_a_plain_child_does_not_compare_against_fan_out_tasks(pe):
    _caller()
    _child("ChildRun00000000000001", fan_out_id="fo_x", fan_out_task_id="t1")
    _child("ChildRun00000000000002", admitted=200)
    c = _check(pe.evaluate_pair("ChildRun00000000000002"), "duplicate_dispatch")
    assert (c["status"], c["reason"]) == ("skip", "single_dispatch")


def test_duplicates_wait_for_the_caller_to_finish(pe):
    _caller(status="running")
    _child("ChildRun00000000000001")
    _child("ChildRun00000000000002", admitted=200)
    row = pe.evaluate_pair("ChildRun00000000000002")
    for cid in ("duplicate_dispatch", "resend_after_receipt"):
        assert (_check(row, cid)["status"], _check(row, cid)["reason"]) == ("skip", "caller_running")


def test_the_sibling_cap_skips_similarity(pe, monkeypatch):
    _caller()
    _child("ChildRun00000000000001")
    _child("ChildRun00000000000002", admitted=200)
    monkeypatch.setattr(pe, "SIBLING_CAP", 1)
    c = _check(pe.evaluate_pair("ChildRun00000000000002"), "duplicate_dispatch")
    assert (c["status"], c["reason"]) == ("skip", "sibling_cap")


def test_the_jaccard_band_is_inclusive_at_the_threshold(pe):
    a = " ".join(f"w{i}" for i in range(10))
    b = " ".join(f"w{i}" for i in range(9)) + " w0"     # 9 of 10 tokens shared → 0.9
    assert pe.brief_similarity(a, b)["score"] == pytest.approx(0.9)
    assert pe.DUPLICATE_JACCARD == 0.9
    _caller()
    _child("ChildRun00000000000001", message=a)
    _child("ChildRun00000000000002", message=b, admitted=200)
    assert _check(pe.evaluate_pair("ChildRun00000000000002"), "duplicate_dispatch")["status"] == "fail"


# --- 5. wrong_route ----------------------------------------------------------

def test_wrong_route_fails_on_an_unknown_command(pe):
    _caller()
    _child("ChildRun00000000000001", unresolved=True)
    c = _check(pe.evaluate_pair("ChildRun00000000000001"), "wrong_route")
    assert (c["status"], c["reason"]) == ("fail", "unknown_command")


def test_wrong_route_fails_on_a_peer_grant_refusal_for_this_run(pe, monkeypatch):
    from database import db
    _caller()
    _child("ChildRun00000000000001")
    monkeypatch.setattr(db, "get_audit_entries", lambda **kw: [
        {"event_id": "aud-1", "event_action": "peer_skill_refused",
         "details": {"execution_id": "ChildRun00000000000001"}},
        {"event_id": "aud-2", "event_action": "peer_skill_refused",
         "details": {"execution_id": "another-run"}}])
    c = _check(pe.evaluate_pair("ChildRun00000000000001"), "wrong_route")
    assert (c["status"], c["reason"]) == ("fail", "skill_not_granted")
    assert c["evidence"]["audit_ids"] == ["aud-1"]


def test_wrong_route_passes_a_resolved_playbook_and_skips_prose(pe):
    _caller()
    _child("ChildRun00000000000001")
    _child("ChildRun00000000000002", message="please look at PR 12", admitted=200)
    assert _check(pe.evaluate_pair("ChildRun00000000000001"), "wrong_route")["status"] == "pass"
    c = _check(pe.evaluate_pair("ChildRun00000000000002"), "wrong_route")
    assert (c["status"], c["reason"]) == ("skip", "not_a_playbook")


def test_an_unreadable_audit_log_skips_wrong_route(pe, monkeypatch):
    from database import db
    _caller()
    _child("ChildRun00000000000001")
    monkeypatch.setattr(db, "get_audit_entries",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    c = _check(pe.evaluate_pair("ChildRun00000000000001"), "wrong_route")
    assert (c["status"], c["reason"]) == ("skip", "refusals_unreadable")


# --- the row --------------------------------------------------------------

def test_the_headline_counts_gates_and_evidence_carries_no_reply_text(pe):
    _caller(response="secret plan text")
    _child("ChildRun00000000000001", response="callee private reply")
    _activity("ChildRun00000000000001")
    row = pe.evaluate_pair("ChildRun00000000000001")
    head = row["checks"]["headline"]
    assert head["gate_pass"] + head["gate_fail"] + head["gate_skip"] == 5
    blob = json.dumps(row["checks"])
    assert "secret plan text" not in blob and "callee private reply" not in blob


def test_a_raising_check_costs_itself_only(pe, monkeypatch):
    _caller()
    _child("ChildRun00000000000001")
    boom = pe.Check("dispatch_exists", "gate", "platform",
                    lambda ctx: (_ for _ in ()).throw(KeyError("x")))
    monkeypatch.setattr(pe, "CHECKS", [boom] + pe.CHECKS[1:])
    row = pe.evaluate_pair("ChildRun00000000000001")
    assert _check(row, "dispatch_exists")["reason"] == "evaluator_error"
    assert _check(row, "wrong_route")["status"] == "pass"
