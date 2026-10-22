"""Tier-1 (judge) row writer pair + candidate query + judge_json schema guard
(trinity-enterprise#936, checkpoint A).

Related flow: docs/memory/feature-flows/agent-evaluations.md

The judge layer (an enterprise module, not here) writes a SECOND platform row
per execution, `evaluator='platform:tier1:<model>'`, beside the Tier-0 row. The
OSS seam is four db primitives and one schema guard, and these tests pin them
on the real schema (`tests/db_harness.py`, so the partial unique index the
claim rule lives in exists):

1. **The claim is a compare-and-set.** `claim_tier1_row` INSERTs the run's
   Tier-1 row only if the run has none — any model, any state — and the index
   makes a racing duplicate a loser (`None`), never a second row. Two
   concurrent claims on one execution yield exactly one row and one claim.
2. **A verdict lands once.** `finish_tier1_row` writes a verdict or a skip
   onto a row that is still `claimed`; a finished row is final (a skip is
   final per run, TD-10), and an invalid judge_json never reaches the table.
3. **Candidates are Tier-0 rows without a Tier-1 row**, bounded, oldest
   first; a claimed, judged or skipped run is never offered again — unless the
   claim went stale, which `reclaim_stale_tier1` releases.
4. **A Tier-0 re-grade never clobbers the judge's row** (the second-row
   contract, TD-5), and a Tier-1 row never enters a rating read.
"""
from __future__ import annotations

import os
import sys
import threading
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

TIER0 = "platform:tier0"
MODEL = "claude-sonnet-5"
TIER1 = f"platform:tier1:{MODEL}"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ago(seconds: float) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def seed_exec(exec_id: str, *, agent="agent-a", status="success",
              triggered_by="schedule", completed_ago=120.0, **cols) -> str:
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    values = dict(
        id=exec_id, schedule_id=cols.pop("schedule_id", "sched-1"),
        agent_name=agent, status=status, triggered_by=triggered_by,
        started_at=_ago((completed_ago or 0) + 30), message="/report-weekly",
        response="Done", completed_at=_ago(completed_ago),
    )
    values.update(cols)
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(**values))
    return exec_id


def tier0(exec_id, agent="agent-a", **kw):
    """A Tier-0 row for the run, the way the sweep writes it."""
    from database import db
    kw.setdefault("completion", True)
    kw.setdefault("checks", {"schema": 1, "headline": {"passed": True, "gate_fail": 0},
                             "checks": []})
    kw.setdefault("archetype", "playbook")
    return db.upsert_platform_evaluation(agent, execution_id=exec_id, **kw)


def _age_row(eval_id: str, seconds: float) -> None:
    """Backdate a row's `created_at`/`updated_at` (the clock the queries read)."""
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import agent_evaluations
    with get_engine().begin() as conn:
        conn.execute(update(agent_evaluations)
                     .where(agent_evaluations.c.id == eval_id)
                     .values(created_at=_ago(seconds), updated_at=_ago(seconds)))


def judged(**over) -> dict:
    j = {
        "schema": 1, "state": "judged", "evaluator": TIER1, "model": MODEL,
        "sampled_because": "passing_quota",
        "dimensions": {
            "scope_fidelity": {"verdict": "pass", "reason": "did only what the playbook asked"},
            "escalation_correctness": {"verdict": "n/a"},
            "intent_alignment": {"verdict": "pass", "reason": "the ask was the weekly report"},
            "helpfulness": {"verdict": "fail", "reason": "no next step named"},
        },
        "quality": 2 / 3, "verdict": "graded",
    }
    j.update(over)
    return j


def skipped(reason="run_too_cheap") -> dict:
    return {"schema": 1, "state": "skipped", "skip": reason,
            "sampling": {"lane": "pass", "run_cost": 0.001}}


# --- 1. the claim is a compare-and-set ----------------------------------------------

def test_a_claim_needs_the_runs_tier0_row(db_backend):
    """The judge grades on top of Tier-0 evidence; a run the deterministic
    evaluator has not graded is not claimable."""
    from database import db
    seed_exec("ex-1")
    assert db.claim_tier1_row("ex-1", model=MODEL) is None
    assert count("agent_evaluations") == 0


def test_a_claim_writes_the_second_platform_row(db_backend):
    from database import db
    from db.evaluations import TIER1_EVALUATOR_PREFIX
    seed_exec("ex-1", fan_out_id="fo-1", source_agent_name="caller-x",
              parent_execution_id="parent-1")
    t0 = tier0("ex-1")
    row = db.claim_tier1_row("ex-1", model=MODEL)
    assert row is not None and row["id"] != t0["id"]
    assert row["evaluator"] == TIER1 and row["evaluator"].startswith(TIER1_EVALUATOR_PREFIX)
    assert row["target_kind"] == "execution" and row["target_id"] == "ex-1"
    assert row["execution_id"] == "ex-1" and row["agent_name"] == "agent-a"
    # copied from the Tier-0 row, never re-derived or passed in
    assert row["archetype"] == "playbook"
    assert (row["caller_agent"], row["parent_execution_id"], row["delegation_mode"]) == \
        ("caller-x", "parent-1", "fan_out")
    # a claim is not a grade
    assert row["quality"] is None and row["completion"] is None and row["checks"] is None
    assert row["judge"]["state"] == "claimed" and row["judge"]["schema"] == 1
    assert row["judge"]["evaluator"] == TIER1 and row["judge"]["claimed_at"]
    assert count("agent_evaluations") == 2
    assert db.latest_tier1_for_execution("ex-1")["id"] == row["id"]
    assert db.latest_tier0_for_execution("ex-1")["id"] == t0["id"]


def test_a_second_claim_on_the_same_run_is_a_loser(db_backend):
    from database import db
    seed_exec("ex-1")
    tier0("ex-1")
    first = db.claim_tier1_row("ex-1", model=MODEL)
    assert first is not None
    assert db.claim_tier1_row("ex-1", model=MODEL) is None
    # a different configured model is still the same run: one judge row per run
    assert db.claim_tier1_row("ex-1", model="claude-opus-5") is None
    assert count("agent_evaluations") == 2
    assert db.latest_tier1_for_execution("ex-1")["id"] == first["id"]


def test_two_concurrent_claims_yield_exactly_one_row_and_one_claim(db_backend):
    """Two sweep leaders (the lease fails open) pick the same run and claim it
    at the same instant on two connections. The index decides: one INSERT
    lands, the other is `None`, and no second row — so the provider call that
    follows a claim happens exactly once."""
    from database import db
    seed_exec("ex-1")
    tier0("ex-1")
    gate = threading.Barrier(2)
    results, errors = [], []

    def claim():
        try:
            gate.wait(timeout=10)
            results.append(db.claim_tier1_row("ex-1", model=MODEL))
        except Exception as e:  # noqa: BLE001 — surfaced below
            errors.append(e)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert len(results) == 2
    assert sum(r is not None for r in results) == 1, results
    assert count("agent_evaluations") == 2
    tier1_rows = [r for r in db.list_agent_evaluations("agent-a") if r["evaluator"] == TIER1]
    assert len(tier1_rows) == 1


# --- 2. a verdict lands once --------------------------------------------------------

def test_finish_writes_the_verdict_onto_the_claimed_row(db_backend):
    from database import db
    seed_exec("ex-1")
    tier0("ex-1")
    claim = db.claim_tier1_row("ex-1", model=MODEL)
    row = db.finish_tier1_row(claim["id"], judge=judged())
    assert row is not None and row["id"] == claim["id"]
    assert row["quality"] == pytest.approx(2 / 3)
    assert row["judge"]["state"] == "judged" and row["judge"]["verdict"] == "graded"
    assert row["judge"]["dimensions"]["helpfulness"]["verdict"] == "fail"
    assert row["judge"]["finished_at"]
    assert row["evaluator"] == TIER1 and row["completion"] is None
    assert count("agent_evaluations") == 2


def test_finish_writes_a_skip_with_null_quality(db_backend):
    from database import db
    seed_exec("ex-1")
    tier0("ex-1")
    claim = db.claim_tier1_row("ex-1", model=MODEL)
    row = db.finish_tier1_row(claim["id"], judge=skipped("run_too_cheap"))
    assert row["quality"] is None
    assert row["judge"]["state"] == "skipped" and row["judge"]["skip"] == "run_too_cheap"


def test_a_finished_row_is_final(db_backend):
    """A skip is final per run (TD-10) and so is a verdict: a late second
    finish — the original worker returning after a stale reclaim, a retried
    tick — is dropped, never an overwrite."""
    from database import db
    seed_exec("ex-1")
    tier0("ex-1")
    claim = db.claim_tier1_row("ex-1", model=MODEL)
    assert db.finish_tier1_row(claim["id"], judge=skipped("provider_error")) is not None
    assert db.finish_tier1_row(claim["id"], judge=judged()) is None
    row = db.latest_tier1_for_execution("ex-1")
    assert row["judge"]["state"] == "skipped" and row["quality"] is None


def test_finish_on_a_missing_row_is_none(db_backend):
    from database import db
    assert db.finish_tier1_row("eval_nope", judge=judged()) is None


def test_finish_cannot_address_a_tier0_row(db_backend):
    """The finish writer is keyed on the Tier-1 evaluator prefix: handed a
    Tier-0 row's id, it writes nothing — the judge never touches Tier-0."""
    from database import db
    seed_exec("ex-1")
    t0 = tier0("ex-1")
    assert db.finish_tier1_row(t0["id"], judge=judged()) is None
    after = db.latest_tier0_for_execution("ex-1")
    assert after["judge"] is None and after["quality"] is None


def test_an_invalid_judge_json_never_reaches_the_table(db_backend):
    from database import db
    from db.evaluations import JudgeJsonError
    seed_exec("ex-1")
    tier0("ex-1")
    claim = db.claim_tier1_row("ex-1", model=MODEL)
    with pytest.raises(JudgeJsonError) as e:
        db.finish_tier1_row(claim["id"], judge=judged(verdict="maybe"))
    assert e.value.code == "judge_verdict_invalid"
    row = db.latest_tier1_for_execution("ex-1")
    assert row["judge"]["state"] == "claimed" and row["quality"] is None
    # and the row is still finishable
    assert db.finish_tier1_row(claim["id"], judge=judged()) is not None


# --- the schema guard ---------------------------------------------------------------

@pytest.mark.parametrize("mutate, code", [
    (lambda j: "not a dict", "judge_json_not_object"),
    (lambda j: {**j, "schema": 2}, "judge_schema_unsupported"),
    (lambda j: {**j, "state": "claimed"}, "judge_state_invalid"),
    (lambda j: {**j, "state": "pending"}, "judge_state_invalid"),
    (lambda j: {k: v for k, v in j.items() if k != "model"}, "judge_model_missing"),
    (lambda j: {**j, "dimensions": []}, "judge_dimensions_invalid"),
    (lambda j: {**j, "dimensions": {**j["dimensions"], "vibes": {"verdict": "pass"}}},
     "judge_dimension_unknown"),
    (lambda j: {**j, "dimensions": {**j["dimensions"],
                                    "helpfulness": {"verdict": "meh", "reason": "x"}}},
     "judge_dimension_verdict_invalid"),
    (lambda j: {**j, "dimensions": {**j["dimensions"],
                                    "helpfulness": {"verdict": "fail", "reason": "r" * 161}}},
     "judge_reason_too_long"),
    (lambda j: {**j, "dimensions": {**j["dimensions"],
                                    "helpfulness": {"verdict": "fail",
                                                    "reason": 'the reply said "all done"'}}},
     "judge_reason_quoted"),
    (lambda j: {**j, "dimensions": {**j["dimensions"],
                                    "helpfulness": {"verdict": "fail", "reason": 7}}},
     "judge_reason_invalid"),
    (lambda j: {**j, "quality": 1.5}, "judge_quality_out_of_range"),
    (lambda j: {**j, "quality": "0.5"}, "judge_quality_out_of_range"),
    (lambda j: {**j, "verdict": "maybe"}, "judge_verdict_invalid"),
])
def test_validate_judge_json_names_the_structural_defect(mutate, code):
    from db.evaluations import JudgeJsonError, validate_judge_json
    with pytest.raises(JudgeJsonError) as e:
        validate_judge_json(mutate(judged()))
    assert e.value.code == code


def test_validate_judge_json_fills_an_omitted_dimension_as_na():
    from db.evaluations import JUDGE_DIMENSIONS, validate_judge_json
    j = judged()
    del j["dimensions"]["escalation_correctness"]
    out = validate_judge_json(j)
    assert set(out["dimensions"]) == set(JUDGE_DIMENSIONS)
    assert out["dimensions"]["escalation_correctness"] == {"verdict": "n/a"}
    # a reason exactly at the cap is accepted
    j = judged()
    j["dimensions"]["helpfulness"]["reason"] = "r" * 160
    assert validate_judge_json(j)["dimensions"]["helpfulness"]["reason"] == "r" * 160


@pytest.mark.parametrize("mutate, code", [
    (lambda j: {**j, "skip": "felt_like_it"}, "judge_skip_unknown"),
    (lambda j: {k: v for k, v in j.items() if k != "skip"}, "judge_skip_unknown"),
])
def test_validate_judge_json_pins_the_skip_enum(mutate, code):
    from db.evaluations import JUDGE_SKIP_REASONS, JudgeJsonError, validate_judge_json
    with pytest.raises(JudgeJsonError) as e:
        validate_judge_json(mutate(skipped()))
    assert e.value.code == code
    for reason in JUDGE_SKIP_REASONS:
        assert validate_judge_json(skipped(reason))["skip"] == reason


def test_the_structural_guard_is_not_a_scorer():
    """Structural fields only: a `quality` that disagrees with the dimensions
    passes here — consistency is the rubric's job, in the module that
    computed it."""
    from db.evaluations import validate_judge_json
    assert validate_judge_json(judged(quality=0.0))["quality"] == 0.0
    assert validate_judge_json(judged(quality=None, verdict="indeterminate"))["quality"] is None


# --- 3. candidates and stale claims ---------------------------------------------------

def test_candidates_are_tier0_rows_without_a_tier1_row_oldest_first(db_backend):
    from database import db
    for i in (1, 2, 3):
        seed_exec(f"ex-{i}")
    r3 = tier0("ex-3")
    r1 = tier0("ex-1")
    r2 = tier0("ex-2")
    _age_row(r1["id"], 300)
    _age_row(r2["id"], 200)
    _age_row(r3["id"], 100)
    ids = [c["execution_id"] for c in db.tier1_candidates(limit=25)]
    assert ids == ["ex-1", "ex-2", "ex-3"]
    # the candidate IS the Tier-0 row — the sampler needs its checks
    assert db.tier1_candidates(limit=1)[0]["checks"]["schema"] == 1
    assert db.tier1_candidates(limit=1)[0]["evaluator"] == TIER0


def test_a_claimed_judged_or_skipped_run_is_never_offered_again(db_backend):
    from database import db
    for i in (1, 2, 3, 4):
        seed_exec(f"ex-{i}")
        tier0(f"ex-{i}")
    c1 = db.claim_tier1_row("ex-1", model=MODEL)
    c2 = db.claim_tier1_row("ex-2", model=MODEL)
    db.finish_tier1_row(c2["id"], judge=judged())
    c3 = db.claim_tier1_row("ex-3", model=MODEL)
    db.finish_tier1_row(c3["id"], judge=skipped("not_sampled"))
    assert c1 is not None
    assert [c["execution_id"] for c in db.tier1_candidates()] == ["ex-4"]


def test_candidates_are_bounded_by_the_lookback(db_backend):
    from database import db
    seed_exec("ex-old")
    seed_exec("ex-new")
    old = tier0("ex-old")
    tier0("ex-new")
    _age_row(old["id"], 25 * 3600)
    assert [c["execution_id"] for c in db.tier1_candidates(lookback_hours=24)] == ["ex-new"]
    assert db.tier1_candidates(limit=0) == []


def test_a_stale_claim_is_released_and_the_run_offered_again(db_backend):
    """A worker that died mid-call leaves a `claimed` row. After
    `stale_seconds` the reclaim deletes it, the run is a candidate again, and
    a fresh claim lands. A fresh claim and a finished row are never touched."""
    from database import db
    for i in (1, 2, 3):
        seed_exec(f"ex-{i}")
        tier0(f"ex-{i}")
    stale = db.claim_tier1_row("ex-1", model=MODEL)
    fresh = db.claim_tier1_row("ex-2", model=MODEL)
    done = db.claim_tier1_row("ex-3", model=MODEL)
    db.finish_tier1_row(done["id"], judge=skipped("judge_budget"))
    _age_row(stale["id"], 900)
    _age_row(done["id"], 900)
    assert db.tier1_candidates() == []
    assert db.reclaim_stale_tier1(stale_seconds=600) == 1
    assert [c["execution_id"] for c in db.tier1_candidates()] == ["ex-1"]
    assert db.latest_tier1_for_execution("ex-1") is None
    assert db.latest_tier1_for_execution("ex-2")["id"] == fresh["id"]
    assert db.latest_tier1_for_execution("ex-3")["judge"]["state"] == "skipped"
    again = db.claim_tier1_row("ex-1", model=MODEL)
    assert again is not None and again["id"] != stale["id"]
    # the late finish of the dead worker's claim id is dropped
    assert db.finish_tier1_row(stale["id"], judge=judged()) is None


# --- 4. the second-row contract -------------------------------------------------------

def test_a_tier0_regrade_keeps_the_judges_row(db_backend):
    """Verified fact 1 of the plan: a Tier-0 re-grade (FAILED → SUCCESS)
    UPDATEs `quality=NULL` on ITS row. The judge's verdict lives on a second
    row and survives; the Tier-0 row still carries no judge."""
    from database import db
    seed_exec("ex-1")
    tier0("ex-1", completion=False)
    claim = db.claim_tier1_row("ex-1", model=MODEL)
    db.finish_tier1_row(claim["id"], judge=judged())
    regraded = tier0("ex-1", completion=True)
    assert regraded["completion"] is True and regraded["quality"] is None
    assert regraded["judge"] is None
    t1 = db.latest_tier1_for_execution("ex-1")
    assert t1["id"] == claim["id"] and t1["quality"] == pytest.approx(2 / 3)
    assert t1["judge"]["state"] == "judged"
    assert count("agent_evaluations") == 2


def test_a_tier1_row_never_enters_a_rating_read(db_backend):
    from database import db
    seed_exec("ex-1")
    tier0("ex-1")
    claim = db.claim_tier1_row("ex-1", model=MODEL)
    db.finish_tier1_row(claim["id"], judge=judged(quality=1.0))
    assert db.workspace_rating_tally("agent-a") == {"up": 0, "down": 0, "total": 0}
    assert db.get_workspace_rating("workspace:x@example.com", "execution", "ex-1") is None
    assert db.list_workspace_ratings_for_targets("workspace:x@example.com", "execution",
                                                 ["ex-1"]) == {}


def test_the_tier1_evaluator_is_a_prefixed_model_id():
    from db.evaluations import TIER0_EVALUATOR, TIER1_EVALUATOR_PREFIX, tier1_evaluator
    assert TIER1_EVALUATOR_PREFIX == "platform:tier1:"
    assert tier1_evaluator("claude-sonnet-5") == "platform:tier1:claude-sonnet-5"
    assert not TIER0_EVALUATOR.startswith(TIER1_EVALUATOR_PREFIX)
    with pytest.raises(ValueError):
        tier1_evaluator("")
    with pytest.raises(ValueError):
        tier1_evaluator("a model")
