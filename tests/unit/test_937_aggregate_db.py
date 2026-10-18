"""Evaluation read layer — SQL half (trinity-enterprise#937, C1).

`db/evaluation_aggregates.py` is the read side of the referee surface: the
rows every aggregate, failed list and chain view folds over. Pinned here, on
the real schema (`tests/db_harness.py`, both dialects):

  * the window is an INNER JOIN to `schedule_executions` — a Tier-0 row whose
    execution row is gone (retention) never counts, so aggregates age out with
    the runs they describe;
  * the window is bucketed on the execution's `completed_at`;
  * `by=pair` reads only delegated rows (`caller_agent` NOT NULL), and the two
    pair scopes — both sides visible (a person) / self on either side (an
    agent key) — filter in SQL;
  * the window is capped newest-first and says `truncated`;
  * a Tier-1 verdict is its OWN row (`platform:tier1:<model>`, orchestrator
    ruling T2) and is LEFT JOINed one-per-execution, newest wins;
  * human labels for an execution are `workspace:`/`operator:` rows only;
  * the chain walk is bounded (depth, node cap, visited set) and stops at an
    inaccessible or missing node without returning it;
  * `tier0_gate_outcome` is shared by the analytics rollup (moved, not copied);
  * the dead `latest_for_execution` helper is gone (N3);
  * `0101_execution_parent_index` indexes `schedule_executions.parent_execution_id`
    on both tracks.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
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


def _ago(minutes: float) -> str:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def seed_exec(exec_id, *, agent="callee", source=None, parent=None,
              minutes_ago=10.0, status="success", schedule_id="s1",
              triggered_by="agent"):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id=schedule_id, agent_name=agent, status=status,
            triggered_by=triggered_by, message="/x", source_agent_name=source,
            parent_execution_id=parent,
            started_at=_ago(minutes_ago + 1), completed_at=_ago(minutes_ago),
            duration_ms=1000,
        ))
    return exec_id


def grade(exec_id, *, agent="callee", gate_pass=1, gate_fail=0, completion=True):
    from database import db
    db.upsert_platform_evaluation(
        agent, execution_id=exec_id, completion=completion,
        checks={"schema": 1, "headline": {
            "passed": gate_fail == 0, "gate_pass": gate_pass,
            "gate_fail": gate_fail, "gate_skip": 0}, "checks": []})


def tier1(exec_id, *, agent="callee", model="m1", quality=0.8, minutes_ago=5.0,
          judge=None):
    """A fixture Tier-1 row in the ruling-T2 shape: its own row per run."""
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_evaluations
    with get_engine().begin() as conn:
        conn.execute(insert(agent_evaluations).values(
            id=f"t1-{exec_id}-{model}", agent_name=agent, execution_id=exec_id,
            evaluator=f"platform:tier1:{model}", target_kind="execution",
            target_id=exec_id, quality=quality,
            judge_json=json.dumps(judge or {"findings": []}),
            created_at=_ago(minutes_ago), updated_at=_ago(minutes_ago),
        ))


def human(exec_id, *, who="workspace:p@example.com", quality=1.0, agent="callee"):
    from database import db
    db.upsert_workspace_rating(agent, evaluator=who, target_kind="message",
                               target_id=f"msg-{exec_id}-{who}", quality=quality,
                               execution_id=exec_id)


def window(**kw):
    from database import db
    kw.setdefault("since", _ago(24 * 60))
    kw.setdefault("until", _ago(-1))
    return db.evaluation_window_rows(**kw)


# --- the window ---------------------------------------------------------------

class TestWindow:
    def test_orphan_tier0_row_is_excluded(self, db_backend):
        """A Tier-0 row whose execution row was pruned never counts."""
        from sqlalchemy import delete
        from db.engine import get_engine
        from db.tables import schedule_executions
        seed_exec("e1"); grade("e1")
        seed_exec("e2"); grade("e2")
        with get_engine().begin() as conn:
            conn.execute(delete(schedule_executions).where(schedule_executions.c.id == "e2"))
        rows, truncated = window()
        assert [r["execution_id"] for r in rows] == ["e1"]
        assert truncated is False

    def test_unevaluated_run_is_absent(self, db_backend):
        seed_exec("e1"); grade("e1")
        seed_exec("e2")
        rows, _ = window()
        assert [r["execution_id"] for r in rows] == ["e1"]

    def test_bucketed_on_completed_at(self, db_backend):
        seed_exec("old", minutes_ago=3 * 24 * 60); grade("old")
        seed_exec("new", minutes_ago=10); grade("new")
        rows, _ = window(since=_ago(24 * 60))
        assert [r["execution_id"] for r in rows] == ["new"]

    def test_row_carries_the_fold_inputs(self, db_backend):
        seed_exec("e1", source="caller", status="failed"); grade("e1", gate_fail=1, completion=False)
        (row,), _ = window()
        assert row["agent_name"] == "callee"
        assert row["caller_agent"] == "caller"
        assert row["status"] == "failed"
        assert row["completion"] is False
        assert row["checks"]["headline"]["gate_fail"] == 1
        assert row["schedule_id"] == "s1"

    def test_agent_and_schedule_filters(self, db_backend):
        seed_exec("e1", agent="a"); grade("e1", agent="a")
        seed_exec("e2", agent="b", schedule_id="s2"); grade("e2", agent="b")
        assert [r["execution_id"] for r in window(agents=["a"])[0]] == ["e1"]
        assert window(agents=[])[0] == []
        assert [r["execution_id"] for r in window(schedule_id="s2")[0]] == ["e2"]

    def test_cap_is_newest_first_and_truncated(self, db_backend):
        for i in range(5):
            seed_exec(f"e{i}", minutes_ago=50 - i); grade(f"e{i}")
        rows, truncated = window(cap=3)
        assert [r["execution_id"] for r in rows] == ["e4", "e3", "e2"]
        assert truncated is True
        assert window(cap=5)[1] is False


class TestPairs:
    @pytest.fixture
    def pairs(self, db_backend):
        seed_exec("ab", agent="b", source="a"); grade("ab", agent="b")
        seed_exec("cb", agent="b", source="c"); grade("cb", agent="b")
        seed_exec("ba", agent="a", source="b"); grade("ba", agent="a")
        seed_exec("solo", agent="b"); grade("solo", agent="b")   # not delegated

    def test_delegated_only_excludes_null_caller(self, pairs):
        ids = {r["execution_id"] for r in window(delegated_only=True)[0]}
        assert ids == {"ab", "cb", "ba"}

    def test_person_scope_needs_both_sides_visible(self, pairs):
        ids = {r["execution_id"] for r in window(delegated_only=True, agents=["a", "b"],
                                                 callers=["a", "b"])[0]}
        assert ids == {"ab", "ba"}   # c -> b hidden: c is not visible

    def test_agent_scope_is_self_on_either_side(self, pairs):
        ids = {r["execution_id"] for r in window(delegated_only=True, either="a")[0]}
        assert ids == {"ab", "ba"}
        ids = {r["execution_id"] for r in window(either="c")[0]}
        assert ids == {"cb"}

    def test_caller_and_callee_filters(self, pairs):
        assert {r["execution_id"] for r in window(caller="a")[0]} == {"ab"}
        assert {r["execution_id"] for r in window(callee="a")[0]} == {"ba"}


class TestTier1Join:
    def test_tier1_row_is_joined_one_per_execution_newest_wins(self, db_backend):
        seed_exec("e1"); grade("e1")
        tier1("e1", model="old", quality=0.2, minutes_ago=8)
        tier1("e1", model="new", quality=0.9, minutes_ago=2)
        seed_exec("e2"); grade("e2")
        rows, _ = window()
        by_id = {r["execution_id"]: r for r in rows}
        assert len(rows) == 2
        assert by_id["e1"]["judge_quality"] == 0.9
        assert by_id["e1"]["judge_evaluator"] == "platform:tier1:new"
        assert by_id["e1"]["judge"] == {"findings": []}
        assert by_id["e2"]["judge_quality"] is None

    def test_tier1_row_alone_is_not_a_tier0_row(self, db_backend):
        """A judged run with no Tier-0 row is not in the window."""
        seed_exec("e1"); tier1("e1")
        assert window()[0] == []


class TestHumanLabels:
    def test_only_person_rows_with_that_execution(self, db_backend):
        seed_exec("e1"); grade("e1"); tier1("e1")
        human("e1", quality=1.0)
        human("e1", who="operator:o@example.com", quality=0.0)
        from database import db
        got = db.evaluation_human_labels(["e1", "e-none"])
        assert sorted(got["e1"]) == [0.0, 1.0]
        assert "e-none" not in got
        assert db.evaluation_human_labels([]) == {}


class TestChain:
    def _line(self):
        seed_exec("root", agent="a", minutes_ago=30)
        seed_exec("mid", agent="b", source="a", parent="root", minutes_ago=20)
        seed_exec("leaf", agent="c", source="b", parent="mid", minutes_ago=10)
        seed_exec("leaf2", agent="d", source="b", parent="mid", minutes_ago=9)

    def test_ancestors_and_descendants(self, db_backend):
        from database import db
        self._line()
        up, stop = db.execution_chain_ancestors("leaf", max_depth=8)
        assert [n["id"] for n in up] == ["mid", "root"]
        assert stop is None
        down, stop = db.execution_chain_descendants("root", max_depth=8)
        assert {n["id"] for n in down} == {"mid", "leaf", "leaf2"}
        assert stop is None

    def test_depth_bound(self, db_backend):
        from database import db
        self._line()
        up, stop = db.execution_chain_ancestors("leaf", max_depth=1)
        assert [n["id"] for n in up] == ["mid"] and stop == "depth"
        down, stop = db.execution_chain_descendants("root", max_depth=1)
        assert [n["id"] for n in down] == ["mid"] and stop == "depth"

    def test_node_cap(self, db_backend):
        from database import db
        self._line()
        down, stop = db.execution_chain_descendants("root", max_depth=8, node_cap=2)
        assert len(down) == 2 and stop == "nodes"

    def test_cycle_is_cut_by_visited_set(self, db_backend):
        from database import db
        seed_exec("x", agent="a", parent="y")
        seed_exec("y", agent="b", parent="x")
        up, _ = db.execution_chain_ancestors("x", max_depth=32)
        assert [n["id"] for n in up] == ["y"]
        down, _ = db.execution_chain_descendants("x", max_depth=32)
        assert [n["id"] for n in down] == ["y"]

    def test_stops_at_inaccessible_node_without_returning_it(self, db_backend):
        from database import db
        self._line()
        up, stop = db.execution_chain_ancestors("leaf", max_depth=8, visible={"c", "b"})
        assert [n["id"] for n in up] == ["mid"] and stop == "access"
        down, stop = db.execution_chain_descendants("root", max_depth=8, visible={"a", "b", "c"})
        assert {n["id"] for n in down} == {"mid", "leaf"} and stop == "access"

    def test_missing_parent_stops_without_echoing_its_id(self, db_backend):
        from database import db
        seed_exec("orphan", agent="a", parent="gone")
        up, stop = db.execution_chain_ancestors("orphan", max_depth=8)
        assert up == [] and stop == "missing"


# --- shared helpers / dead code -------------------------------------------------

def test_gate_outcome_is_shared_with_analytics():
    from db import evaluations
    from db.schedules import analytics
    assert analytics.tier0_gate_outcome is evaluations.tier0_gate_outcome
    assert not hasattr(analytics, "_tier0_gate_outcome")
    f = evaluations.tier0_gate_outcome
    assert f(json.dumps({"headline": {"passed": True, "gate_pass": 1, "gate_fail": 0}})) is True
    assert f({"headline": {"passed": False, "gate_pass": 1, "gate_fail": 1}}) is False
    assert f({"headline": {"gate_pass": 0, "gate_fail": 0}}) is None
    for junk in (None, "", "{not json", "[]", {"headline": "x"}, {"headline": {"gate_pass": "x"}}):
        assert f(junk) is None


def test_latest_for_execution_is_deleted():
    from db.evaluations import EvaluationOperations
    from database import DatabaseManager
    assert not hasattr(EvaluationOperations, "latest_for_execution")
    assert not hasattr(DatabaseManager, "latest_evaluation_for_execution")


def test_series_points_for_dims_accepts_until(db_backend):
    from database import db
    rows = [{"metric": "eval.evaluated", "ts": _ago(60 * 24 * 2), "value": 1,
             "dims": {"agent": "a"}},
            {"metric": "eval.evaluated", "ts": _ago(60), "value": 2,
             "dims": {"agent": "a"}}]
    from services import metric_points_service as mps
    db.insert_metric_points("platform:eval", [
        dict(metric=r["metric"], ts=r["ts"], value_numeric=r["value"], value_text=None,
             dims=r["dims"], execution_id=None, created_at=_ago(0),
             idempotency_key=mps.point_identity(r["metric"], r["ts"], r["dims"]))
        for r in rows])
    got = db.metric_series_points_for_dims("platform:eval", "eval.evaluated", {"agent": "a"},
                                           [], _ago(60 * 24 * 3), 10, until_iso=_ago(60 * 24))
    assert [r["value_numeric"] for r in got] == [1]


# --- T3 migration: both tracks ------------------------------------------------------

def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, _BACKEND / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sqlite_migration_adds_parent_index(tmp_path):
    migrations = _load("db/migrations.py", "ent937_migrations")
    names = [n for n, _ in migrations.MIGRATIONS]
    assert names.index("execution_parent_index") == names.index("delegation_link") + 1
    conn = sqlite3.connect(tmp_path / "pre.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE schedule_executions (id TEXT PRIMARY KEY, parent_execution_id TEXT)")
    fn = dict(migrations.MIGRATIONS)["execution_parent_index"]
    fn(cur, conn); fn(cur, conn)   # idempotent
    cur.execute("PRAGMA index_info(idx_executions_parent)")
    assert [r[2] for r in cur.fetchall()] == ["parent_execution_id"]


def test_sqlite_migration_tolerates_missing_table(tmp_path):
    migrations = _load("db/migrations.py", "ent937_migrations_b")
    conn = sqlite3.connect(tmp_path / "empty.db")
    dict(migrations.MIGRATIONS)["execution_parent_index"](conn.cursor(), conn)


def test_fresh_schema_declares_the_index():
    schema = _load("db/schema.py", "ent937_schema")
    assert any("idx_executions_parent ON schedule_executions(parent_execution_id)" in s
               for s in schema.INDEXES)


def test_alembic_revision_chains_after_0100():
    versions = _BACKEND / "migrations" / "versions"
    hits = [p for p in versions.glob("*.py")
            if 'down_revision = "0100_delegation_link"' in p.read_text()]
    assert [p.name for p in hits] == ["0101_execution_parent_index.py"]
    rev = _load(str(hits[0].relative_to(_BACKEND)), "ent937_rev")
    assert rev.revision == "0101_execution_parent_index"
    executed = []

    class _Op:
        def execute(self, sql):
            executed.append(" ".join(str(sql).split()))

    rev.op = _Op()
    rev.upgrade()
    assert executed == ["CREATE INDEX IF NOT EXISTS idx_executions_parent "
                        "ON schedule_executions(parent_execution_id)"]
    executed.clear()
    rev.downgrade()
    assert executed == ["DROP INDEX IF EXISTS idx_executions_parent"]
