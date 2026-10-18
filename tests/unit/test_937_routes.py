"""Evaluation read API — the routes (trinity-enterprise#937, C3).

Driven through a real FastAPI app carrying the real `routers.evaluations`
router, the real agent-access dependency and the real db on the harness; only
the principal and the owner map are supplied. Pinned:

  * the static `/evaluations/<name>` routes resolve and `/evaluations/{eval_id}`
    still does (Invariant #4 — checked on the route table, not reading order);
  * the access matrix: admin all, owner its own, another tenant zeros/404, an
    agent key ITS OWN rows only — also on the existing `GET /evaluations` and
    `GET /evaluations/{id}` (ruling T1, the ent#629 class: an agent key resolves
    to its owner, whose accessible set holds the sibling);
  * pairs: a person needs both sides visible; an agent key reads the pairs it
    is caller or callee of;
  * a machine never reads a judge's score (summary, aggregate, trend);
  * the chain stops at an inaccessible node and never echoes its id;
  * `caller=` / `callee=` on the list reads;
  * the per-agent reads (`/agents/{name}/evaluations`, `.../executions/{id}/
    evaluation`) hold an agent key to the same self/pair rule even where a
    permission edge lets it past the path gate (09:20Z addendum);
  * a Workspace-only principal is refused by the operator floor (F12).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

OWNER = "owner"
OTHER = "someone-else"
ADMIN = "root"
AGENTS = {"agent-a": OWNER, "agent-b": OWNER, "agent-c": OTHER}


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _exec(exec_id, agent="agent-a", *, source=None, parent=None, status="success",
          schedule_id="sched-1", minutes_ago=10):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id=schedule_id, agent_name=agent, status=status,
            triggered_by="agent" if source else "schedule", message="/x",
            source_agent_name=source, parent_execution_id=parent,
            started_at=_ago(minutes_ago + 1), completed_at=_ago(minutes_ago)))


def _grade(exec_id, agent="agent-a", *, gate_fail=0, completion=True):
    from database import db
    checks = [{"id": "claimed_artifact", "tier": 0, "source": "x", "status": "fail",
               "reason": "missing", "evidence": "SECRET quote"}] if gate_fail else []
    return db.upsert_platform_evaluation(
        agent, execution_id=exec_id, completion=completion,
        checks={"schema": 1, "headline": {"passed": gate_fail == 0, "gate_pass": 1,
                                          "gate_fail": gate_fail, "gate_skip": 0},
                "checks": checks})


def _tier1(exec_id, agent="agent-a", quality=0.9):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_evaluations
    with get_engine().begin() as conn:
        conn.execute(insert(agent_evaluations).values(
            id=f"t1-{exec_id}", agent_name=agent, execution_id=exec_id,
            evaluator="platform:tier1:model-x", target_kind="execution",
            target_id=exec_id, quality=quality,
            judge_json=json.dumps({"findings": [], "prose": "SECRET"}),
            created_at=_ago(1), updated_at=_ago(1)))


def _user(username, *, role="user", agent_name=None, mcp_scope=None):
    from models import User
    return User(id=1, username=username, role=role, agent_name=agent_name, mcp_scope=mcp_scope)


def _agent_key(agent):
    # owner is an admin: the default install, where an agent key's owner-carried
    # role would otherwise read the whole fleet
    return _user(OWNER, role="admin", agent_name=agent, mcp_scope="agent")


@pytest.fixture
def client(db_backend, monkeypatch):
    from database import db
    from routers import evaluations
    from services.agent_service import helpers

    principal = {"user": _user(OWNER)}
    app = FastAPI()
    app.include_router(evaluations.router)
    app.dependency_overrides[evaluations.get_current_user] = lambda: principal["user"]
    monkeypatch.setattr(db, "get_agent_owner", lambda name: AGENTS.get(name))
    monkeypatch.setattr(db, "can_user_access_agent",
                        lambda user, name: name in AGENTS and (AGENTS[name] == user or user == ADMIN))
    monkeypatch.setattr(db, "can_user_share_agent",
                        lambda user, name: name in AGENTS and (AGENTS[name] == user or user == ADMIN))
    monkeypatch.setattr(helpers, "accessible_agent_names", lambda u: None if u.role == "admin"
                        else [n for n, o in AGENTS.items() if o == u.username])
    tc = TestClient(app)
    tc.as_user = lambda u: principal.__setitem__("user", u)
    return tc


@pytest.fixture
def fleet(client):
    """a: two own runs (one failed a gate); b: one run delegated by a; c: another
    tenant's run delegated by a's sibling b."""
    _exec("a1"); _grade("a1"); _tier1("a1")
    _exec("a2"); _grade("a2", gate_fail=1)
    _exec("b1", "agent-b", source="agent-a", parent="a1"); _grade("b1", "agent-b")
    _exec("c1", "agent-c", source="agent-b", parent="b1"); _grade("c1", "agent-c")
    return client


# --- route order ------------------------------------------------------------

def test_static_routes_are_declared_before_the_eval_id_catch_all():
    from routers import evaluations
    paths = [r.path for r in evaluations.router.routes]
    catch_all = paths.index("/api/evaluations/{eval_id}")
    for p in ("/api/evaluations/aggregate", "/api/evaluations/failed",
              "/api/evaluations/trend", "/api/evaluations/chain/{execution_id}"):
        assert paths.index(p) < catch_all, p


def test_static_routes_resolve_and_eval_id_still_works(fleet):
    for p in ("aggregate", "failed"):
        r = fleet.get(f"/api/evaluations/{p}")
        assert r.status_code == 200, (p, r.text)
    row = _grade("a1")
    r = fleet.get(f"/api/evaluations/{row['id']}")
    assert r.status_code == 200 and r.json()["execution_id"] == "a1"


# --- aggregate --------------------------------------------------------------

def test_owner_aggregate_by_agent(fleet):
    r = fleet.get("/api/evaluations/aggregate", params={"by": "agent"})
    assert r.status_code == 200, r.text
    body = r.json()
    groups = {g["key"]["agent"]: g for g in body["groups"]}
    assert set(groups) == {"agent-a", "agent-b"}           # never agent-c
    a = groups["agent-a"]
    assert (a["evaluated"], a["graded"], a["passed"]) == (2, 2, 1)
    assert a["checks_passed_rate"] == 0.5
    assert a["per_check_fail"] == {"claimed_artifact": 1}
    assert a["judged"] == 1 and a["quality_mean"] == 0.9    # a person reads the score
    assert body["total"]["evaluated"] == 3 and body["truncated"] is False


def test_admin_sees_every_agent(fleet):
    fleet.as_user(_user(ADMIN, role="admin"))
    body = fleet.get("/api/evaluations/aggregate").json()
    assert {g["key"]["agent"] for g in body["groups"]} == {"agent-a", "agent-b", "agent-c"}


def test_an_agent_outside_scope_reads_as_zeros_not_403(fleet):
    r = fleet.get("/api/evaluations/aggregate", params={"agent": "agent-c"})
    assert r.status_code == 200
    assert r.json()["total"]["evaluated"] == 0 and r.json()["groups"] == []


def test_agent_key_aggregate_is_its_own_and_has_no_score(fleet):
    fleet.as_user(_agent_key("agent-a"))
    body = fleet.get("/api/evaluations/aggregate").json()
    assert [g["key"]["agent"] for g in body["groups"]] == ["agent-a"]
    for s in (body["total"], body["groups"][0]):
        assert "quality_mean" not in s and "judge_human_agreement" not in s
    # asking for the sibling explicitly is zeros, not its rows
    sib = fleet.get("/api/evaluations/aggregate", params={"agent": "agent-b"}).json()
    assert sib["total"]["evaluated"] == 0


def test_pair_needs_both_sides_visible_for_a_person(fleet):
    body = fleet.get("/api/evaluations/aggregate", params={"by": "pair"}).json()
    # a→b both owned; b→c has c outside the owner's scope
    assert [g["key"] for g in body["groups"]] == [{"caller": "agent-a", "callee": "agent-b"}]


def test_agent_key_reads_pairs_it_is_a_side_of(fleet):
    fleet.as_user(_agent_key("agent-b"))
    body = fleet.get("/api/evaluations/aggregate", params={"by": "pair"}).json()
    assert {(g["key"]["caller"], g["key"]["callee"]) for g in body["groups"]} == {
        ("agent-a", "agent-b"), ("agent-b", "agent-c")}
    fleet.as_user(_agent_key("agent-c"))
    body = fleet.get("/api/evaluations/aggregate",
                     params={"by": "pair", "caller": "agent-a"}).json()
    assert body["groups"] == []                               # a third agent's pair


def test_invalid_period_is_named(fleet):
    r = fleet.get("/api/evaluations/aggregate", params={"period": "custom", "since": "x"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_period"
    assert fleet.get("/api/evaluations/aggregate", params={"by": "nope"}).status_code == 422


# --- failed -----------------------------------------------------------------

def test_failed_lists_check_ids_never_evidence(fleet):
    body = fleet.get("/api/evaluations/failed").json()
    assert [i["execution_id"] for i in body["items"]] == ["a2"]
    assert body["items"][0]["failed_checks"] == [{"id": "claimed_artifact", "reason": "missing"}]
    assert "SECRET" not in json.dumps(body)
    fleet.as_user(_user(OTHER))
    assert fleet.get("/api/evaluations/failed").json()["items"] == []


def test_failed_pairs_carry_both_ids(client):
    _exec("ab", "agent-b", source="agent-a", status="failed")
    _grade("ab", "agent-b", completion=False)
    item = client.get("/api/evaluations/failed", params={"by": "pair"}).json()["items"][0]
    assert (item["caller_agent"], item["agent_name"]) == ("agent-a", "agent-b")


# --- agent summary ----------------------------------------------------------

def test_agent_summary_for_owner_and_self(fleet):
    r = fleet.get("/api/agents/agent-a/evaluations/summary")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["summary"]["evaluated"] == 2 and s["summary"]["quality_mean"] == 0.9
    assert [f["execution_id"] for f in s["failed"]] == ["a2"]
    fleet.as_user(_agent_key("agent-a"))
    s = fleet.get("/api/agents/agent-a/evaluations/summary").json()
    assert s["summary"]["evaluated"] == 2
    assert "quality_mean" not in s["summary"] and "SECRET" not in json.dumps(s)


def test_agent_summary_of_a_sibling_via_agent_key_is_zeros(fleet, monkeypatch):
    """Even where the agent gate lets an agent key reach a sibling (a
    permission edge), the summary is its own rows only (T1)."""
    from routers import evaluations
    # through the router's binding: the unit conftest re-imports `dependencies`
    monkeypatch.setitem(evaluations.get_authorized_agent_by_name.__globals__,
                        "agent_may_reach", lambda u, n, **k: True)
    fleet.as_user(_agent_key("agent-a"))
    r = fleet.get("/api/agents/agent-b/evaluations/summary")
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["evaluated"] == 0


def test_agent_summary_other_tenant_is_uniform_404(fleet):
    fleet.as_user(_user(OTHER))
    r = fleet.get("/api/agents/agent-a/evaluations/summary")
    assert (r.status_code, r.json()["detail"]) == (404, "Agent not found")


# --- the existing list + by-id reads (T1) ------------------------------------

def test_fleet_list_for_agent_key_is_its_own_rows(fleet):
    fleet.as_user(_agent_key("agent-a"))
    rows = fleet.get("/api/evaluations").json()
    assert rows and {r["agent_name"] for r in rows} == {"agent-a"}
    # the Tier-1 row is projected for a machine: no score
    t1 = [r for r in rows if r["evaluator"] == "platform:tier1"]
    assert t1 and t1[0]["quality"] is None and t1[0]["judge"] is None


def test_by_id_for_agent_key_refuses_a_sibling_row(fleet):
    from database import db
    sibling = db.latest_tier0_for_execution("b1")
    fleet.as_user(_agent_key("agent-a"))
    r = fleet.get(f"/api/evaluations/{sibling['id']}")
    assert (r.status_code, r.json()["detail"]) == (404, "Evaluation not found")
    own = db.latest_tier0_for_execution("a1")
    assert fleet.get(f"/api/evaluations/{own['id']}").status_code == 200
    fleet.as_user(_user(OWNER))
    assert fleet.get(f"/api/evaluations/{sibling['id']}").status_code == 200


def test_list_reads_take_caller_and_callee(fleet):
    rows = fleet.get("/api/evaluations", params={"caller": "agent-a"}).json()
    assert {r["execution_id"] for r in rows} == {"b1"}
    rows = fleet.get("/api/evaluations", params={"callee": "agent-b"}).json()
    assert {r["agent_name"] for r in rows} == {"agent-b"}
    assert fleet.get("/api/evaluations", params={"callee": "agent-c"}).json() == []
    rows = fleet.get("/api/agents/agent-b/evaluations", params={"caller": "agent-a"}).json()
    assert [r["execution_id"] for r in rows] == ["b1"]
    assert fleet.get("/api/agents/agent-a/evaluations",
                     params={"caller": "agent-b"}).json() == []


# --- chain ------------------------------------------------------------------

def test_chain_for_owner_stops_at_another_tenant_without_its_id(fleet):
    r = fleet.get("/api/evaluations/chain/b1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [n["execution_id"] for n in body["ancestors"]] == ["a1"]
    assert body["descendants"] == [] and body["descendants_truncated"] == "access"
    assert "c1" not in json.dumps(body) and "agent-c" not in json.dumps(body)


def test_chain_root_outside_scope_is_uniform_404(fleet):
    assert fleet.get("/api/evaluations/chain/c1").json()["detail"] == "not_found"
    assert fleet.get("/api/evaluations/chain/nope").status_code == 404


def test_chain_for_agent_key_is_itself_only(fleet):
    fleet.as_user(_agent_key("agent-b"))
    body = fleet.get("/api/evaluations/chain/b1").json()
    assert body["ancestors"] == [] and body["ancestors_truncated"] == "access"
    assert body["execution"]["parent_execution_id"] is None   # a1 not served
    assert fleet.get("/api/evaluations/chain/a1").status_code == 404


def test_admin_chain_walks_the_whole_tree(fleet):
    fleet.as_user(_user(ADMIN, role="admin"))
    body = fleet.get("/api/evaluations/chain/a1").json()
    assert [n["execution_id"] for n in body["descendants"]] == ["b1", "c1"]
    assert body["descendants"][0]["parent_execution_id"] == "a1"


# --- trend ------------------------------------------------------------------

def _point(metric, dims, value, day_ago):
    from database import db
    from services import evaluation_read_service as ers
    from services.metric_points_service import point_identity
    ts = (datetime.now(timezone.utc) - timedelta(days=day_ago)).strftime("%Y-%m-%dT00:00:00.000000Z")
    db.insert_metric_points(ers.PLATFORM_EVAL_OWNER, [{
        "metric": metric, "ts": ts, "value_numeric": float(value), "value_text": None,
        "dims": dims, "execution_id": None, "created_at": _ago(0),
        "idempotency_key": point_identity(metric, ts, dims)}])


def test_trend_reads_stored_points_for_owner(client):
    _point("eval.checks_passed_rate", {"agent": "agent-a"}, 0.5, 2)
    _point("eval.checks_passed_rate", {"agent": "agent-a"}, 0.75, 1)
    r = client.get("/api/evaluations/trend",
                   params={"metric": "eval.checks_passed_rate", "agent": "agent-a"})
    assert r.status_code == 200, r.text
    assert [p["value"] for p in r.json()["points"]] == [0.5, 0.75]
    client.as_user(_user(OTHER))
    r = client.get("/api/evaluations/trend",
                   params={"metric": "eval.checks_passed_rate", "agent": "agent-a"})
    assert r.json()["points"] == []


def test_trend_refuses_a_score_metric_to_a_machine_and_unknown_metrics(client):
    client.as_user(_agent_key("agent-a"))
    r = client.get("/api/evaluations/trend",
                   params={"metric": "eval.quality_mean", "agent": "agent-a"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_metric"
    r = client.get("/api/evaluations/trend", params={"metric": "cpu", "agent": "agent-a"})
    assert r.status_code == 422 and "cpu" not in r.text
    r = client.get("/api/evaluations/trend", params={"metric": "eval.passed"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_series"


def test_trend_pair_for_agent_key_needs_self_on_a_side(client):
    _point("eval.passed", {"caller": "agent-a", "callee": "agent-b"}, 3, 1)
    client.as_user(_agent_key("agent-b"))
    r = client.get("/api/evaluations/trend", params={
        "metric": "eval.passed", "caller": "agent-a", "callee": "agent-b"})
    assert [p["value"] for p in r.json()["points"]] == [3]
    client.as_user(_agent_key("agent-c"))
    r = client.get("/api/evaluations/trend", params={
        "metric": "eval.passed", "caller": "agent-a", "callee": "agent-b"})
    assert r.json()["points"] == []


# --- workspace floor (F12) ----------------------------------------------------

def test_workspace_only_principal_is_refused_on_every_new_route():
    import dependencies
    from routers import evaluations
    new = {"aggregate_evaluations", "failed_evaluations", "evaluation_trend",
           "evaluation_chain", "agent_evaluation_summary"}
    routes = [r for r in evaluations.router.routes if r.endpoint.__name__ in new]
    assert len(routes) == len(new)
    for route in routes:
        with pytest.raises(HTTPException) as exc:
            dependencies.enforce_operator_floor(SimpleNamespace(scope={"route": route}),
                                                _user("client", role="user"))
        assert exc.value.status_code == 403


# --- per-agent reads narrowed for an agent key (09:20Z addendum) ---------------

def _edge(monkeypatch):
    """A permission edge: the path gate lets agent-a's key reach its siblings."""
    from routers import evaluations
    monkeypatch.setitem(evaluations.get_authorized_agent_by_name.__globals__,
                        "agent_may_reach", lambda u, n, **k: True)


def test_agent_key_lists_a_siblings_rows_only_for_pairs_it_is_a_side_of(fleet, monkeypatch):
    _edge(monkeypatch)
    _exec("b2", "agent-b"); _grade("b2", "agent-b")            # b's own run, no caller
    fleet.as_user(_agent_key("agent-a"))
    rows = fleet.get("/api/agents/agent-b/evaluations").json()
    assert [r["execution_id"] for r in rows] == ["b1"]          # a delegated b1 only
    # naming another caller does not widen it.
    assert fleet.get("/api/agents/agent-b/evaluations",
                     params={"caller": "agent-b"}).json() == []
    # b delegated nothing to a: b's key reads none of a's rows.
    fleet.as_user(_agent_key("agent-b"))
    assert fleet.get("/api/agents/agent-a/evaluations").json() == []
    # its own rows are unchanged.
    own = fleet.get("/api/agents/agent-b/evaluations").json()
    assert {r["execution_id"] for r in own} == {"b1", "b2"}


def test_agent_key_reads_a_siblings_run_grade_only_as_its_caller(fleet, monkeypatch):
    _edge(monkeypatch)
    _exec("b2", "agent-b"); _grade("b2", "agent-b")
    fleet.as_user(_agent_key("agent-a"))
    assert fleet.get("/api/agents/agent-b/executions/b1/evaluation").status_code == 200
    r = fleet.get("/api/agents/agent-b/executions/b2/evaluation")
    assert (r.status_code, r.json()["detail"]) == (404, "not_evaluated")
    fleet.as_user(_agent_key("agent-b"))
    r = fleet.get("/api/agents/agent-a/executions/a1/evaluation")
    assert (r.status_code, r.json()["detail"]) == (404, "not_evaluated")
    assert fleet.get("/api/agents/agent-b/executions/b2/evaluation").status_code == 200


def test_a_person_still_reads_every_row_of_an_agent_it_can_access(fleet):
    _exec("b2", "agent-b"); _grade("b2", "agent-b")
    fleet.as_user(_user(OWNER))
    rows = fleet.get("/api/agents/agent-b/evaluations").json()
    assert {r["execution_id"] for r in rows} == {"b1", "b2"}
    assert fleet.get("/api/agents/agent-b/executions/b2/evaluation").status_code == 200
