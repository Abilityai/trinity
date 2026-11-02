"""Tier-1 row review fixes (trinity-enterprise#936, review of PR #3790).

Related flow: docs/memory/feature-flows/agent-evaluations.md

1. **One Tier-1 row per run, whatever the model.** The claim's existence check
   and its INSERT are not serialised, and the evaluator key embeds the model,
   so two claimers under different model ids could both pass the check and
   both insert. The partial unique index `idx_agent_evaluations_tier1_target`
   on `(target_kind, target_id) WHERE evaluator LIKE 'platform:tier1:%'` makes
   the second INSERT the loser on both backends.
2. **The guard checks the whole document, not only `dimensions`.** `findings`
   is closed tokens with a null reason, at most `JUDGE_FINDINGS_CAP` of them;
   top-level keys are an allowlist; every other string leaf is capped.
3. **A finished document never carries the claim marker**, so the substring
   compare-and-set in finish/reclaim cannot match a verdict.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_harness import count, db_backend  # noqa: E402,F401
from test_936_tier1_writer import _age_row, judged, seed_exec, skipped, tier0  # noqa: E402

pytestmark = pytest.mark.unit


def _tier1_rows(exec_id: str) -> int:
    from sqlalchemy import select, func
    from db.engine import get_engine
    from db.tables import agent_evaluations
    with get_engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(agent_evaluations).where(
            agent_evaluations.c.evaluator.like("platform:tier1:%"),
            agent_evaluations.c.target_id == exec_id,
        )).scalar()


# --- 1. one Tier-1 row per run, whatever the model -----------------------------------

def test_racing_claims_under_different_models_leave_one_row(db_backend, monkeypatch):
    """Both claimers pass the existence check (forced here); the index decides."""
    from database import db
    # Patch the class the facade actually holds: another file may have
    # reloaded `db.evaluations`, and the facade keeps the module it was built with.
    EvaluationOperations = type(db._evaluation_ops)
    seed_exec("e-race")
    tier0("e-race")
    monkeypatch.setattr(EvaluationOperations, "_tier1_row_for", lambda self, conn, eid: None)
    first = db.claim_tier1_row("e-race", model="claude-sonnet-5")
    second = db.claim_tier1_row("e-race", model="claude-haiku-4-5-20251001")
    assert first is not None
    assert second is None
    assert _tier1_rows("e-race") == 1


def test_the_index_refuses_a_second_tier1_row_for_a_run(db_backend):
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError
    from db.engine import get_engine
    from db.tables import agent_evaluations
    row = dict(target_kind="execution", target_id="e-idx", agent_name="agent-a",
               created_at="2026-11-01T00:00:00Z", updated_at="2026-11-01T00:00:00Z")
    with get_engine().begin() as conn:
        conn.execute(insert(agent_evaluations).values(id="ev-1", evaluator="platform:tier1:a", **row))
    with pytest.raises(IntegrityError):
        with get_engine().begin() as conn:
            conn.execute(insert(agent_evaluations).values(id="ev-2", evaluator="platform:tier1:b", **row))
    # Other evaluators on the same run are untouched by the rule.
    with get_engine().begin() as conn:
        conn.execute(insert(agent_evaluations).values(id="ev-3", evaluator="platform:tier0", **row))
        conn.execute(insert(agent_evaluations).values(id="ev-4", evaluator="operator:x", **row))


def test_the_sqlite_migration_keeps_one_row_per_run_and_is_idempotent(tmp_path):
    """A database that already holds two Tier-1 rows for a run keeps the
    finished one, then gets the index; a second pass changes nothing."""
    import sqlite3
    from db.migrations import _migrate_tier1_one_per_run
    conn = sqlite3.connect(tmp_path / "t.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE agent_evaluations (id TEXT PRIMARY KEY, evaluator TEXT, "
                "target_kind TEXT, target_id TEXT, judge_json TEXT, created_at TEXT, updated_at TEXT)")
    rows = [
        ("a", "platform:tier1:m1", "execution", "e1", '{"state": "claimed"}', "2026-11-01T00:00:03Z"),
        ("b", "platform:tier1:m2", "execution", "e1", '{"state": "judged"}', "2026-11-01T00:00:01Z"),
        ("c", "platform:tier1:m1", "execution", "e2", '{"state": "skipped"}', "2026-11-01T00:00:01Z"),
        ("d", "platform:tier1:m2", "execution", "e2", '{"state": "judged"}', "2026-11-01T00:00:02Z"),
        ("e", "platform:tier0", "execution", "e1", None, "2026-11-01T00:00:00Z"),
    ]
    cur.executemany("INSERT INTO agent_evaluations VALUES (?,?,?,?,?,?,?)",
                    [r + (r[-1],) for r in rows])
    conn.commit()
    _migrate_tier1_one_per_run(cur, conn)
    _migrate_tier1_one_per_run(cur, conn)
    left = sorted(r[0] for r in cur.execute("SELECT id FROM agent_evaluations"))
    assert left == ["b", "d", "e"]
    names = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_agent_evaluations_tier1_target" in names


def test_the_sqlite_migration_leaves_a_database_without_the_table_alone(tmp_path):
    import sqlite3
    from db.migrations import _migrate_tier1_one_per_run
    conn = sqlite3.connect(tmp_path / "t.db")
    _migrate_tier1_one_per_run(conn.cursor(), conn)


def test_a_runs_tier1_read_is_deterministic():
    """`_tier1_row_for` orders before LIMIT 1 (newest first), so a read never
    depends on the backend's row order."""
    import inspect
    from db.evaluations import EvaluationOperations
    src = inspect.getsource(EvaluationOperations._tier1_row_for)
    assert "order_by" in src


# --- 2. the guard checks the whole document ------------------------------------------

def _findings(**over):
    f = [{"dimension": "scope_fidelity", "verdict": "pass", "reason": None},
         {"dimension": "helpfulness", "verdict": "fail", "reason": None}]
    return f


def test_a_well_formed_findings_list_passes():
    from db.evaluations import validate_judge_json
    out = validate_judge_json(judged(findings=_findings()))
    assert out["findings"] == _findings()


@pytest.mark.parametrize("bad, code", [
    ("not a list", "judge_findings_invalid"),
    ([{"dimension": "made_up", "verdict": "pass", "reason": None}], "judge_finding_invalid"),
    ([{"dimension": "scope_fidelity", "verdict": "great", "reason": None}], "judge_finding_invalid"),
    ([{"dimension": "scope_fidelity", "verdict": "pass", "reason": "x" * 5000}], "judge_finding_invalid"),
    ([{"dimension": "scope_fidelity", "verdict": "pass", "reason": None, "quote": "q"}],
     "judge_finding_invalid"),
    (["scope_fidelity"], "judge_finding_invalid"),
])
def test_a_finding_carries_closed_tokens_and_a_null_reason(bad, code):
    from db.evaluations import JudgeJsonError, validate_judge_json
    with pytest.raises(JudgeJsonError) as e:
        validate_judge_json(judged(findings=bad))
    assert e.value.code == code


def test_findings_are_capped():
    from db.evaluations import JUDGE_FINDINGS_CAP, JudgeJsonError, validate_judge_json
    many = [{"dimension": "scope_fidelity", "verdict": "pass", "reason": None}] * (JUDGE_FINDINGS_CAP + 1)
    with pytest.raises(JudgeJsonError) as e:
        validate_judge_json(judged(findings=many))
    assert e.value.code == "judge_findings_too_many"


def test_the_read_projection_shares_the_cap():
    from db.evaluations import JUDGE_FINDINGS_CAP
    from services import evaluation_read_service
    assert evaluation_read_service.FINDINGS_CAP == JUDGE_FINDINGS_CAP


@pytest.mark.parametrize("doc", [
    judged(transcript_excerpt="the agent said ..."),
    skipped() | {"transcript_excerpt": "the agent said ..."},
])
def test_an_unknown_top_level_key_is_refused(doc):
    from db.evaluations import JudgeJsonError, validate_judge_json
    with pytest.raises(JudgeJsonError) as e:
        validate_judge_json(doc)
    assert e.value.code == "judge_key_unknown"


@pytest.mark.parametrize("doc", [
    judged(sampling={"lane": "x" * 5000}),
    judged(context={"trajectory_source": "y" * 5000}),
    judged(model="m" * 5000),
    skipped() | {"sampling": {"nested": ["z" * 5000]}},
])
def test_every_other_string_leaf_is_capped(doc):
    from db.evaluations import JudgeJsonError, validate_judge_json
    with pytest.raises(JudgeJsonError) as e:
        validate_judge_json(doc)
    assert e.value.code == "judge_string_too_long"


def test_the_judges_own_documents_pass():
    """Every key the enterprise judge writes today is on the allowlist."""
    from db.evaluations import validate_judge_json
    validate_judge_json(judged(
        credential="subscription", sampled_because="tier0_fail",
        sampling={"lane": "fail", "run_cost": 0.12, "family": "sonnet"},
        findings=_findings(), tier0_updated_at="2026-11-01T00:00:00Z",
        context={"trajectory_source": "transcript", "truncated": False, "input_tokens": 1200},
        judged_at="2026-11-01T00:00:00Z", call_id="call_1", evaluator="platform:tier1:m",
    ))
    validate_judge_json(skipped("provider_error") | {
        "decided_at": "2026-11-01T00:00:00Z", "sampled_because": "tier0_fail", "call_id": "call_1"})


# --- 3. a finished document never carries the claim marker ---------------------------

@pytest.mark.parametrize("doc", [
    judged(sampling={"state": "claimed"}),
    skipped() | {"sampling": {"claim": {"state": "claimed"}}},
])
def test_a_document_carrying_the_claim_marker_is_refused(doc):
    from db.evaluations import JudgeJsonError, validate_judge_json
    with pytest.raises(JudgeJsonError) as e:
        validate_judge_json(doc)
    assert e.value.code == "judge_claim_marker"


def test_a_finished_verdict_survives_a_stale_reclaim(db_backend):
    """The reported path: a verdict whose document names the marker was
    deleted by the reclaim. It can no longer be written at all."""
    from database import db
    # The class the facade's own module raises (see above).
    JudgeJsonError = type(db._evaluation_ops).finish_tier1_row.__globals__["JudgeJsonError"]
    seed_exec("e-mark")
    tier0("e-mark")
    claim = db.claim_tier1_row("e-mark", model="claude-sonnet-5")
    with pytest.raises(JudgeJsonError):
        db.finish_tier1_row(claim["id"], judge=judged(sampling={"claim": {"state": "claimed"}}))
    assert db.finish_tier1_row(claim["id"], judge=judged())["judge"]["state"] == "judged"
    _age_row(claim["id"], 900)
    assert db.reclaim_stale_tier1(stale_seconds=600) == 0
    assert _tier1_rows("e-mark") == 1


def test_both_migration_tracks_run_the_same_dedupe(monkeypatch):
    import importlib.util
    import types
    from db.migrations import TIER1_DEDUPE_SQL
    path = _BACKEND / "migrations" / "versions" / "0115_tier1_one_per_run.py"
    monkeypatch.setitem(sys.modules, "alembic", types.SimpleNamespace(op=None))
    spec = importlib.util.spec_from_file_location("rev_0115", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.TIER1_DEDUPE_SQL == TIER1_DEDUPE_SQL
    assert mod.down_revision == "0114_execution_parent_index"
