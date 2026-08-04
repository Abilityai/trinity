"""The behavioral-eval referee surface + write-fence (ent#206).

The load-bearing rule of the eval epic: **the graded agent must never write its
own grade.** `agent_reports` (#918) was rejected as the surface precisely because
its create is self-gated (an agent writes its own report). This surface inverts
that — the write route is human-admin-only (`require_admin` + a
`reject_agent_principal` in the handler), and the graded agent has no write path.

These cover the db layer + the write-fence wiring. The end-to-end fence was also
verified live: an admin write returns 200, the graded agent's own key returns 403
on write but 200 on read.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def eval_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-eval.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.engine import get_engine
    from db.tables import metadata as m, agent_evaluations, schedule_executions
    m.create_all(get_engine(), tables=[agent_evaluations, schedule_executions])
    yield str(db_file)


# --- db layer ----------------------------------------------------------------

def test_create_and_read_evaluation(eval_db):
    from database import db
    row = db.create_agent_evaluation(
        "agent-a", evaluator="admin:alice", execution_id="ex1",
        archetype="scheduled", completion=True, quality=0.8,
        checks={"report_landed": True}, judge={"score": 0.8})
    assert row["agent_name"] == "agent-a"
    assert row["completion"] is True and row["quality"] == 0.8
    assert row["checks"] == {"report_landed": True}      # JSON round-trips
    assert row["evaluator"] == "admin:alice"
    # readable back by id
    again = db.get_agent_evaluation(row["id"])
    assert again["id"] == row["id"]


def test_completion_and_quality_are_independent_axes(eval_db):
    """The whole point of ent#206: a run can COMPLETE cleanly yet score low on
    quality. The surface must represent both, separately."""
    from database import db
    row = db.create_agent_evaluation(
        "agent-a", evaluator="tier0", completion=True, quality=0.2)
    assert row["completion"] is True     # finished without erroring
    assert row["quality"] == 0.2         # ...but did a poor job


def test_quality_may_be_null_when_not_evaluated(eval_db):
    from database import db
    row = db.create_agent_evaluation("agent-a", evaluator="tier0", completion=True)
    assert row["completion"] is True and row["quality"] is None


def test_list_is_agent_scoped_and_newest_first(eval_db):
    from database import db
    db.create_agent_evaluation("agent-a", evaluator="t", quality=0.1)
    db.create_agent_evaluation("agent-a", evaluator="t", quality=0.9)
    db.create_agent_evaluation("agent-b", evaluator="t", quality=0.5)
    a = db.list_agent_evaluations("agent-a")
    assert len(a) == 2 and all(r["agent_name"] == "agent-a" for r in a)
    # newest first
    assert a[0]["created_at"] >= a[1]["created_at"]


def test_fleet_list_respects_the_accessible_set(eval_db):
    from database import db
    db.create_agent_evaluation("agent-a", evaluator="t", quality=0.1)
    db.create_agent_evaluation("agent-b", evaluator="t", quality=0.2)
    assert len(db.list_fleet_evaluations(None)) == 2          # admin → all
    assert [r["agent_name"] for r in db.list_fleet_evaluations(["agent-a"])] == ["agent-a"]
    assert db.list_fleet_evaluations([]) == []               # empty set → none


# --- the write-fence (the load-bearing rule) ---------------------------------

def test_write_route_rejects_an_agent_principal():
    """The referee route MUST reject an agent-scoped principal even though it
    inherits its owner's admin role — else a graded agent grades itself. This is
    the trinity-ops-agent#232 trap the skill runner also had to close.
    """
    import inspect
    from routers import evaluations

    src = inspect.getsource(evaluations.create_evaluation)
    assert "reject_agent_principal(current_user)" in src, (
        "the write route must reject agent principals — require_admin alone lets "
        "an agent-scoped key through (it carries its owner's role)"
    )


def test_write_route_is_admin_gated():
    import inspect
    from routers import evaluations
    sig = inspect.signature(evaluations.create_evaluation)
    dep = sig.parameters["current_user"].default
    assert getattr(dep.dependency, "__name__", "") == "require_admin"


def test_there_is_no_agent_writable_evaluation_route():
    """Belt-and-suspenders: no route in the module writes evaluations behind a
    plain `get_current_user` / `AuthorizedAgent` (which an agent key satisfies).
    Every POST must be admin-gated."""
    import inspect
    from routers import evaluations

    for name, fn in inspect.getmembers(evaluations, inspect.isfunction):
        src = inspect.getsource(fn)
        if "create_agent_evaluation" in src and "db." in src:
            # the only writer must be admin-gated + agent-rejecting
            assert "require_admin" in src and "reject_agent_principal" in src, (
                f"{name} writes an evaluation without the full write-fence"
            )


def test_reports_surface_is_not_reused_as_the_referee():
    """A guard against regressing to `agent_reports` — its create is self-gated
    (an agent writes its own), so it can never be the referee. The eval router
    must use the dedicated `agent_evaluations` db path, not reports.
    """
    import inspect
    from routers import evaluations
    src = inspect.getsource(evaluations)
    assert "create_agent_evaluation" in src
    assert "create_agent_report" not in src


# --- cascade registration ----------------------------------------------------

def test_evaluations_cascade_on_agent_delete():
    """Keyed on agent_name, so it must be in AGENT_REFS (CI parity guard)."""
    from db.agent_cleanup import AGENT_REFS
    assert any(r.table == "agent_evaluations" for r in AGENT_REFS)
