"""Tier-0 writer (trinity-enterprise#935, checkpoint A1).

`upsert_platform_evaluation` is the one write path of the deterministic
evaluator. Three things are pinned here, all on the real schema
(`tests/db_harness.py`, so the partial unique index the rule lives in exists):

1. **One row per execution.** The row is keyed `target_kind='execution',
   target_id=<execution_id>` under `evaluator='platform:tier0'`, which is what
   `idx_agent_evaluations_rating_target` (ent#366) makes unique — so a second
   evaluation of the same run updates in place, and a racing duplicate INSERT
   becomes the UPDATE it should have been (no migration, TD-2).
2. **The delegation columns (addendum A) are derived from the execution row on
   every write**, never passed by the caller and never guessed: fan_out_id →
   fan_out, loop_id → loop, triggered_by='reminder' → reminder, otherwise
   NULL — including an agent-sourced run, because sync vs async is not recorded
   on the row.
3. **A Tier-0 row never enters a rating read** (TD-2 rider a): every reader of
   ratings keys on a `workspace:`/`operator:` evaluator, so the new
   `target_kind='execution'` rows stay out of tallies and per-person reads.
"""
from __future__ import annotations

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

TIER0 = "platform:tier0"


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
        completed_at=(_ago(completed_ago) if completed_ago is not None else None),
    )
    values.update(cols)
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(**values))
    return exec_id


def _write(exec_id, agent="agent-a", **kw):
    from database import db
    kw.setdefault("completion", True)
    kw.setdefault("checks", {"schema": 1, "checks": []})
    return db.upsert_platform_evaluation(agent, execution_id=exec_id, **kw)


# --- one row per execution -----------------------------------------------------

def test_the_row_is_keyed_on_the_execution_it_grades(db_backend):
    seed_exec("ex-1")
    row = _write("ex-1", archetype="playbook")
    assert row["evaluator"] == TIER0
    assert row["target_kind"] == "execution" and row["target_id"] == "ex-1"
    assert row["execution_id"] == "ex-1"
    assert row["completion"] is True and row["quality"] is None
    assert row["archetype"] == "playbook"
    assert row["checks"] == {"schema": 1, "checks": []}


def test_a_second_evaluation_regrades_in_place(db_backend):
    """A FAILED terminal later overwritten by SUCCESS must flip the grade, not
    leave two rows that disagree about one run."""
    seed_exec("ex-2", status="failed")
    first = _write("ex-2", completion=False, checks={"schema": 1, "checks": [{"id": "a"}]})
    second = _write("ex-2", completion=True, checks={"schema": 1, "checks": [{"id": "b"}]})
    assert count("agent_evaluations", "target_id = :t", t="ex-2") == 1
    assert second["id"] == first["id"]
    assert second["completion"] is True
    assert second["checks"]["checks"] == [{"id": "b"}]
    assert second["updated_at"] and second["updated_at"] >= first["updated_at"]


def test_a_racing_duplicate_insert_becomes_the_update(db_backend, monkeypatch):
    """Two sweep workers evaluating the same run: both UPDATE zero rows, both
    INSERT. The unique index refuses the loser, and the loser must apply its
    write to the winner's row instead of raising — on the SAME connection
    (a second one deadlocks SQLite against itself, the ent#366 finding)."""
    from sqlalchemy import insert
    from db.tables import agent_evaluations
    from db.evaluations import EvaluationOperations

    seed_exec("ex-3")
    real = EvaluationOperations._tier0_update
    calls = {"n": 0}

    def racing_update(self, conn, execution_id, values):
        calls["n"] += 1
        if calls["n"] == 1:
            # The winner commits between our UPDATE and our INSERT.
            conn.execute(insert(agent_evaluations).values(
                id="eval_winner", agent_name="agent-a", execution_id=execution_id,
                evaluator=TIER0, target_kind="execution", target_id=execution_id,
                completion=0, created_at=_ago(1), updated_at=_ago(1)))
            return None
        return real(self, conn, execution_id, values)

    monkeypatch.setattr(EvaluationOperations, "_tier0_update", racing_update)
    row = _write("ex-3", completion=True, checks={"schema": 1, "checks": [{"id": "loser"}]})
    assert calls["n"] == 2
    assert row["id"] == "eval_winner"
    assert row["completion"] is True
    assert row["checks"]["checks"] == [{"id": "loser"}]
    assert count("agent_evaluations", "target_id = :t", t="ex-3") == 1


def test_latest_tier0_ignores_every_other_evaluator(db_backend):
    from database import db
    seed_exec("ex-4")
    assert db.latest_tier0_for_execution("ex-4") is None
    db.create_agent_evaluation("agent-a", evaluator="admin:alice", execution_id="ex-4",
                               quality=0.9)
    assert db.latest_tier0_for_execution("ex-4") is None
    written = _write("ex-4")
    db.create_agent_evaluation("agent-a", evaluator="admin:bob", execution_id="ex-4",
                               quality=0.1)
    assert db.latest_tier0_for_execution("ex-4")["id"] == written["id"]


# --- delegation columns (addendum A) ---------------------------------------------

@pytest.mark.parametrize("cols, expected", [
    pytest.param(dict(fan_out_id="fo-1", source_agent_name="boss",
                      parent_execution_id="ex-parent", triggered_by="fan_out"),
                 ("boss", "ex-parent", "fan_out"), id="fan_out"),
    pytest.param(dict(loop_id="loop-1", source_agent_name="boss", triggered_by="loop"),
                 ("boss", None, "loop"), id="loop"),
    pytest.param(dict(triggered_by="reminder", source_agent_name="boss"),
                 ("boss", None, "reminder"), id="reminder"),
    # Sync vs async is not recorded on the row: NULL, never a guess.
    pytest.param(dict(triggered_by="agent", source_agent_name="boss",
                      parent_execution_id="ex-parent"),
                 ("boss", "ex-parent", None), id="agent-sourced-mode-unknown"),
    pytest.param(dict(triggered_by="schedule"), (None, None, None), id="not-delegated"),
])
def test_delegation_columns_are_derived_from_the_execution_row(db_backend, cols, expected):
    seed_exec("ex-d", **cols)
    row = _write("ex-d")
    assert (row["caller_agent"], row["parent_execution_id"], row["delegation_mode"]) == expected


def test_the_precedence_is_fan_out_then_loop_then_reminder():
    from db.evaluations import derive_delegation
    row = {"fan_out_id": "f", "loop_id": "l", "triggered_by": "reminder",
           "source_agent_name": "a", "parent_execution_id": "p"}
    assert derive_delegation(row) == ("a", "p", "fan_out")
    assert derive_delegation({**row, "fan_out_id": None}) == ("a", "p", "loop")
    assert derive_delegation({**row, "fan_out_id": None, "loop_id": None}) == ("a", "p", "reminder")
    assert derive_delegation(None) == (None, None, None)


def test_a_second_write_refreshes_the_delegation_columns(db_backend):
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import schedule_executions
    seed_exec("ex-r", triggered_by="agent", source_agent_name="boss")
    assert _write("ex-r")["caller_agent"] == "boss"
    with get_engine().begin() as conn:
        conn.execute(update(schedule_executions).where(schedule_executions.c.id == "ex-r")
                     .values(source_agent_name="boss-2", parent_execution_id="ex-p2",
                             fan_out_id="fo-9"))
    again = _write("ex-r")
    assert (again["caller_agent"], again["parent_execution_id"], again["delegation_mode"]) == \
        ("boss-2", "ex-p2", "fan_out")
    # ...and back to NULL when the row stops saying so: a stale link is a lie.
    with get_engine().begin() as conn:
        conn.execute(update(schedule_executions).where(schedule_executions.c.id == "ex-r")
                     .values(source_agent_name=None, parent_execution_id=None, fan_out_id=None))
    cleared = _write("ex-r")
    assert (cleared["caller_agent"], cleared["parent_execution_id"], cleared["delegation_mode"]) == \
        (None, None, None)


# --- the sweep's candidate read ----------------------------------------------------

def test_unevaluated_ids_are_settled_bounded_terminal_schedule_or_agent_runs(db_backend):
    from database import db
    seed_exec("pick-sched", triggered_by="schedule", completed_ago=300)
    seed_exec("pick-agent", triggered_by="agent", status="failed", completed_ago=200)
    seed_exec("pick-cancel", triggered_by="schedule", status="cancelled", completed_ago=100)
    seed_exec("no-manual", triggered_by="manual", completed_ago=300)
    seed_exec("no-running", status="running", completed_ago=None)
    seed_exec("no-skipped", status="skipped", completed_ago=300)
    seed_exec("no-fresh", completed_ago=5)                    # inside the settle delay
    seed_exec("no-old", completed_ago=25 * 3600)              # past the lookback
    seed_exec("no-done", completed_ago=300)
    _write("no-done")

    ids = db.unevaluated_execution_ids(limit=25, settle_seconds=30, lookback_hours=24)
    assert ids == ["pick-sched", "pick-agent", "pick-cancel"]   # oldest completion first
    assert db.unevaluated_execution_ids(limit=1, settle_seconds=30, lookback_hours=24) == ["pick-sched"]


def test_another_evaluators_row_does_not_count_as_evaluated(db_backend):
    from database import db
    seed_exec("ex-h", completed_ago=300)
    db.create_agent_evaluation("agent-a", evaluator="admin:alice", execution_id="ex-h")
    assert db.unevaluated_execution_ids(limit=5, settle_seconds=30, lookback_hours=24) == ["ex-h"]


# --- TD-2 rider (a): rating reads never see a Tier-0 row ---------------------------

def test_a_tier0_row_never_enters_a_rating_read(db_backend):
    """Every reader of ratings, pinned against a Tier-0 row deliberately given a
    rating-shaped `quality`. The ids collide on purpose: a message id equal to an
    execution id must still only ever return the person's own thumb."""
    from database import db
    seed_exec("same-id")
    _write("same-id", quality=1.0)
    db.upsert_workspace_rating("agent-a", evaluator="workspace:c@example.com",
                               target_kind="message", target_id="same-id", quality=0.0)

    # client_portal/agent_page.py — the tally every client of the agent sees
    assert db.workspace_rating_tally("agent-a") == {"up": 0, "down": 1, "total": 1}
    # client_portal/service.py + role_card.py — one person's own thumbs
    assert db.list_workspace_ratings_for_targets(
        "workspace:c@example.com", "message", ["same-id"]) == {"same-id": 0.0}
    assert db.list_workspace_ratings_for_targets(
        "workspace:c@example.com", "execution", ["same-id"]) == {}
    assert db.list_workspace_ratings_for_targets(
        "operator:c@example.com", "message", ["same-id"]) == {}
    # client_portal/service.py submit path — the existing-rating read
    assert db.get_workspace_rating("workspace:c@example.com", "execution", "same-id") is None
    assert db.get_workspace_rating("workspace:c@example.com", "message", "same-id")["quality"] == 0.0


def test_the_rating_index_comment_no_longer_says_tier0_rows_are_null_target():
    """TD-2 rider (b): the model comment is the documentation of the rule the
    Tier-0 writer now depends on. (Source pin on top of the executed tests
    above — its consumer is the next person editing the index.)"""
    src = (_BACKEND / "db" / "tables.py").read_text()
    block = src[src.index("agent_evaluations = Table("):src.index('"idx_agent_evaluations_rating_target"')]
    assert "NULL-target rows a Tier-0" not in block
    assert "target_kind='execution'" in block


def test_deleting_the_caller_keeps_the_callee_grade(db_backend):
    """Review I3: `caller_agent` is initiator provenance (like
    `schedule_executions.source_agent_name`), not ownership. Purging the agent
    that delegated must not delete another agent's Tier-0 history — the
    callee's Checks passed rate and verdicts stay as they were. The callee's
    own purge still removes it (`agent_name` stays CASCADE)."""
    from db.agent_cleanup import cascade_delete
    from db.engine import get_engine
    seed_exec("ex-c", triggered_by="agent", source_agent_name="boss")
    assert _write("ex-c")["caller_agent"] == "boss"
    with get_engine().begin() as conn:
        cascade_delete(conn, "boss")
    assert count("agent_evaluations") == 1
    with get_engine().begin() as conn:
        cascade_delete(conn, "agent-a")
    assert count("agent_evaluations") == 0
