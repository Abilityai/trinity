"""Pair evaluation — the read side (trinity-enterprise#940, checkpoint C).

The pair row (`platform:tier0:pair`, one per delegated callee run) is read
beside the Tier-0 row it hangs off. Pinned here, on the real schema
(`tests/db_harness.py`) and through the real `routers.evaluations` router:

  * `window_rows` LEFT JOINs the pair row as `pair_checks`; a run with both
    rows is still one run (Tier-0 counters unchanged);
  * `fold` keeps `graded`/`passed`/`per_check_fail` Tier-0-only and adds a
    disjoint `pair` block only when asked (`by=pair`); a failed entry carries
    `pair_failed_checks` (ids + reason codes, never evidence);
  * `by=agent` carries no pair rates (TD-6); the agent summary lists the
    agent's own failed pair checks as callee (`failed`) and as caller
    (`pair_failed`);
  * an agent key reads its own pairs' block without the latency/cost/
    round-trip metrics, never another agent's pair, never a judge score;
  * the pair evaluator is a known non-person grader name;
  * the scorecard writes `eval.pair_*` on pair series only.
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
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

OWNER = "owner"
OTHER = "someone-else"
AGENTS = {"agent-a": OWNER, "agent-b": OWNER, "agent-c": OWNER, "agent-x": OTHER}
METRIC_KEYS = ("latency_p50_ms", "cost_per_verified_outcome", "round_trips_mean")


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _exec(exec_id, agent, *, source=None, parent=None, status="success", minutes_ago=10):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id="sched-1", agent_name=agent, status=status,
            triggered_by="agent" if source else "schedule", message="/x",
            source_agent_name=source, parent_execution_id=parent,
            started_at=_ago(minutes_ago + 1), completed_at=_ago(minutes_ago)))


def _grade(exec_id, agent, *, gate_fail=0):
    from database import db
    checks = [{"id": "claimed_artifact", "tier": 0, "source": "x", "status": "fail",
               "reason": "missing", "evidence": "SECRET quote"}] if gate_fail else []
    db.upsert_platform_evaluation(
        agent, execution_id=exec_id, completion=True,
        checks={"schema": 1, "headline": {"passed": gate_fail == 0, "gate_pass": 1,
                                          "gate_fail": gate_fail, "gate_skip": 0},
                "checks": checks})


def pair_checks(*, fail=None, latency=(100, 2000, 500), cost=0.5, verified=True,
                round_trips=None):
    checks = [{"id": "dispatch_exists", "tier": "gate", "source": "platform",
               "status": "pass", "reason": None, "evidence": {}}]
    if fail:
        checks.append({"id": fail, "tier": "gate", "source": "platform", "status": "fail",
                       "reason": "no_readback", "evidence": {"quote": "SECRET pair"}})
    if round_trips is not None:
        checks.append({"id": "rework_bounds", "tier": "signal", "source": "platform",
                       "status": "pass", "reason": None,
                       "evidence": {"round_trips": round_trips, "p95": 9, "n": 5}})
    return {"schema": 1,
            "headline": {"passed": not fail, "gate_pass": 1, "gate_fail": 1 if fail else 0,
                         "gate_skip": 0},
            "checks": checks,
            "metrics": {"latency_ms": dict(zip(
                ("dispatch_to_start", "start_to_end", "end_to_first_read"), latency)),
                "cost": cost, "verified": verified}}


def _pair(exec_id, agent, **kw):
    from database import db
    from db.evaluations import TIER0_PAIR_EVALUATOR
    db.upsert_platform_evaluation(agent, execution_id=exec_id, completion=True,
                                  checks=pair_checks(**kw), evaluator=TIER0_PAIR_EVALUATOR)


# --- the SQL join ---------------------------------------------------------------

class TestWindowRows:
    def test_pair_row_is_joined_and_counts_once(self, db_backend):
        from database import db
        _exec("p0", "agent-a")
        _exec("p1", "agent-b", source="agent-a", parent="p0"); _grade("p1", "agent-b")
        _pair("p1", "agent-b", fail="relay_unverified")
        _exec("p2", "agent-b", source="agent-a", parent="p0"); _grade("p2", "agent-b")
        rows, _ = db.evaluation_window_rows(since=_ago(60), until=_ago(-1))
        by = {r["execution_id"]: r for r in rows}
        assert set(by) == {"p1", "p2"}                         # one row per run
        assert by["p1"]["pair_checks"]["headline"]["gate_fail"] == 1
        assert by["p2"]["pair_checks"] is None
        assert by["p1"]["checks"]["headline"]["gate_fail"] == 0   # Tier-0 untouched

    def test_pair_row_alone_is_not_a_tier0_row(self, db_backend):
        from database import db
        _exec("q1", "agent-b", source="agent-a")
        _pair("q1", "agent-b")
        rows, _ = db.evaluation_window_rows(since=_ago(60), until=_ago(-1))
        assert rows == []


# --- the fold (pure) ------------------------------------------------------------

def _row(eid, *, pair=None, caller="a", t0_fail=0):
    return {"execution_id": eid, "agent_name": "b", "caller_agent": caller,
            "completion": True, "status": "success", "schedule_id": "s1",
            "completed_at": "2026-10-18T10:00:00.000000Z", "judge_quality": None,
            "checks": {"headline": {"passed": t0_fail == 0, "gate_pass": 1,
                                    "gate_fail": t0_fail, "gate_skip": 0}, "checks": []},
            "pair_checks": pair}


class TestFold:
    @pytest.fixture
    def svc(self):
        from services import evaluation_read_service
        return evaluation_read_service

    def test_pair_block_is_disjoint_from_tier0_counters(self, svc):
        rows = [_row("1", pair=pair_checks(fail="relay_unverified", round_trips=1)),
                _row("2", pair=pair_checks(latency=(300, 4000, None), cost=1.5, round_trips=3)),
                _row("3", pair=None)]
        out = svc.fold(rows, {}, pair=True)
        assert (out["evaluated"], out["graded"], out["passed"]) == (3, 3, 3)
        assert out["per_check_fail"] == {}
        p = out["pair"]
        assert (p["pair_graded"], p["pair_passed"], p["pair_checks_passed_rate"]) == (2, 1, 0.5)
        assert p["pair_per_check_fail"] == {"relay_unverified": 1}
        assert p["latency_p50_ms"] == {"dispatch_to_start": 200, "start_to_end": 3000,
                                       "end_to_first_read": 500}
        assert p["cost_per_verified_outcome"] == 1.0          # (0.5 + 1.5) / 2 verified
        assert p["round_trips_mean"] == 2.0

    def test_no_pair_block_unless_asked(self, svc):
        out = svc.fold([_row("1", pair=pair_checks(fail="wrong_route"))], {})
        assert out.get("pair") is None

    def test_failed_entry_carries_pair_failed_checks_without_evidence(self, svc):
        out = svc.fold([_row("1", pair=pair_checks(fail="relay_unverified")),
                        _row("2", pair=pair_checks())], {})
        assert [f["execution_id"] for f in out["failed"]] == ["1"]
        assert out["failed"][0]["pair_failed_checks"] == [
            {"id": "relay_unverified", "reason": "no_readback"}]
        assert out["failed"][0]["failed_checks"] == []
        assert "SECRET" not in json.dumps(out)

    def test_malformed_pair_checks_are_not_graded(self, svc):
        out = svc.fold([_row("1", pair={"headline": "x"}), _row("2", pair="garbage")],
                       {}, pair=True)
        assert out["pair"]["pair_graded"] == 0 and out["pair"]["pair_checks_passed_rate"] is None
        assert out["failed"] == []

    def test_machine_pair_block_keeps_counts_only(self, svc):
        out = svc.fold([_row("1", pair=pair_checks(fail="wrong_route"))], {}, pair=True)
        machine = svc.project_summary(out, machine=True)
        assert machine["pair"]["pair_per_check_fail"] == {"wrong_route": 1}
        assert not set(METRIC_KEYS) & set(machine["pair"])
        assert set(METRIC_KEYS) <= set(svc.project_summary(out, machine=False)["pair"])

    def test_pair_evaluator_is_a_known_grader(self, svc):
        from db.evaluations import TIER0_PAIR_EVALUATOR
        assert svc.redact_evaluator(TIER0_PAIR_EVALUATOR) == TIER0_PAIR_EVALUATOR


# --- the scorecard ----------------------------------------------------------------

def test_scorecard_writes_pair_metrics_on_pair_series_only():
    from services import eval_scorecard_service as svc
    from services.evaluation_read_service import EVAL_METRICS
    pair_metrics = {"eval.pair_graded", "eval.pair_passed", "eval.pair_checks_passed_rate"}
    assert pair_metrics <= set(EVAL_METRICS)
    rows = [_row("1", pair=pair_checks(fail="relay_unverified")), _row("2", pair=pair_checks())]
    for r in rows:
        r["agent_name"] = "b"
    out = svc.points_for_day("2026-10-18T00:00:00.000000Z", rows, {}, fleet=True)
    pair_dims = {(p["metric"], p["value_numeric"]) for p in out
                 if p["dims"] == {"caller": "a", "callee": "b"}}
    assert {("eval.pair_graded", 2.0), ("eval.pair_passed", 1.0),
            ("eval.pair_checks_passed_rate", 0.5)} <= pair_dims
    assert not [p for p in out if p["metric"] in pair_metrics
                and p["dims"] != {"caller": "a", "callee": "b"}]


# --- the routes ---------------------------------------------------------------------

def _user(username, *, role="user", agent_name=None, mcp_scope=None):
    from models import User
    return User(id=1, username=username, role=role, agent_name=agent_name, mcp_scope=mcp_scope)


def _agent_key(agent):
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
                        lambda user, name: name in AGENTS and AGENTS[name] == user)
    monkeypatch.setattr(db, "can_user_share_agent",
                        lambda user, name: name in AGENTS and AGENTS[name] == user)
    monkeypatch.setattr(helpers, "accessible_agent_names", lambda u: None if u.role == "admin"
                        else [n for n, o in AGENTS.items() if o == u.username])
    tc = TestClient(app)
    tc.as_user = lambda u: principal.__setitem__("user", u)
    return tc


@pytest.fixture
def edges(client):
    """a → b (edge failed relay_unverified), a → c (edge passed),
    b → x (x is another tenant's agent; edge failed wrong_route)."""
    _exec("a0", "agent-a")
    _exec("ab", "agent-b", source="agent-a", parent="a0"); _grade("ab", "agent-b")
    _pair("ab", "agent-b", fail="relay_unverified", round_trips=2)
    _exec("ac", "agent-c", source="agent-a", parent="a0"); _grade("ac", "agent-c")
    _pair("ac", "agent-c")
    _exec("bx", "agent-x", source="agent-b"); _grade("bx", "agent-x")
    _pair("bx", "agent-x", fail="wrong_route")
    return client


def _groups(body):
    return {(g["key"]["caller"], g["key"]["callee"]): g for g in body["groups"]}


def test_by_pair_carries_the_pair_block_for_a_person(edges):
    body = edges.get("/api/evaluations/aggregate", params={"by": "pair"}).json()
    g = _groups(body)
    assert set(g) == {("agent-a", "agent-b"), ("agent-a", "agent-c")}
    assert g[("agent-a", "agent-b")]["pair"]["pair_per_check_fail"] == {"relay_unverified": 1}
    assert g[("agent-a", "agent-b")]["pair"]["round_trips_mean"] == 2.0
    assert g[("agent-a", "agent-c")]["pair"]["pair_checks_passed_rate"] == 1.0
    assert body["total"]["pair"]["pair_graded"] == 2


def test_by_agent_carries_no_pair_rates(edges):
    body = edges.get("/api/evaluations/aggregate").json()
    assert body["total"].get("pair") is None
    assert all(g.get("pair") is None for g in body["groups"])


def test_agent_key_reads_its_own_pairs_without_metrics_or_quality(edges):
    edges.as_user(_agent_key("agent-b"))
    body = edges.get("/api/evaluations/aggregate", params={"by": "pair"}).json()
    g = _groups(body)
    assert set(g) == {("agent-a", "agent-b"), ("agent-b", "agent-x")}   # never a→c
    for s in (body["total"], *g.values()):
        assert not set(METRIC_KEYS) & set(s["pair"])
        assert "quality_mean" not in s
    assert g[("agent-b", "agent-x")]["pair"]["pair_per_check_fail"] == {"wrong_route": 1}
    assert "SECRET" not in json.dumps(body)


def test_failed_route_carries_pair_failed_checks(edges):
    body = edges.get("/api/evaluations/failed", params={"by": "pair"}).json()
    items = {i["execution_id"]: i for i in body["items"]}
    assert set(items) == {"ab"}                               # bx: x is not visible
    assert items["ab"]["pair_failed_checks"] == [{"id": "relay_unverified",
                                                  "reason": "no_readback"}]
    assert "SECRET" not in json.dumps(body)


def test_agent_summary_lists_own_failed_pair_checks_as_callee_and_caller(edges):
    edges.as_user(_agent_key("agent-b"))
    s = edges.get("/api/agents/agent-b/evaluations/summary").json()
    assert [f["execution_id"] for f in s["failed"]] == ["ab"]           # as callee
    assert s["failed"][0]["pair_failed_checks"][0]["id"] == "relay_unverified"
    assert [f["execution_id"] for f in s["pair_failed"]] == ["bx"]      # as caller
    assert s["pair_failed"][0]["pair_failed_checks"][0]["id"] == "wrong_route"
    assert s["summary"].get("pair") is None and "quality_mean" not in s["summary"]
    assert "SECRET" not in json.dumps(s)


def test_agent_key_never_reads_another_agents_pair_failures(edges, monkeypatch):
    from routers import evaluations
    monkeypatch.setitem(evaluations.get_authorized_agent_by_name.__globals__,
                        "agent_may_reach", lambda u, n, **k: True)
    edges.as_user(_agent_key("agent-c"))
    s = edges.get("/api/agents/agent-a/evaluations/summary").json()
    assert s["failed"] == [] and s["pair_failed"] == []


def test_person_caller_side_hides_a_callee_outside_scope(edges):
    s = edges.get("/api/agents/agent-b/evaluations/summary").json()
    assert [f["execution_id"] for f in s["failed"]] == ["ab"]
    assert s["pair_failed"] == []                             # b → x: x is another tenant's


# --- the row reads (C1: deviation C binds row reads too) ----------------------------

def _pair_rows(rows):
    from db.evaluations import TIER0_PAIR_EVALUATOR
    from services.evaluation_read_service import redact_evaluator
    return [r for r in rows if r["evaluator"] == redact_evaluator(TIER0_PAIR_EVALUATOR)]


def _assert_machine_pair_row(row):
    c = row["checks"]
    assert set(c) <= {"schema", "headline", "edge", "checks"}
    assert "metrics" not in c
    assert c["checks"] and all(set(k) == {"id", "tier", "source", "status", "reason"}
                               for k in c["checks"])
    ids = {k["id"] for k in c["checks"]}
    assert "relay_unverified" in ids and "rework_bounds" in ids     # ids + reasons kept
    assert "SECRET" not in json.dumps(row)


def test_agent_key_list_route_reads_pair_rows_without_metrics_or_evidence(edges):
    edges.as_user(_agent_key("agent-b"))
    rows = edges.get("/api/agents/agent-b/evaluations").json()
    pair = _pair_rows(rows)
    assert [r["execution_id"] for r in pair] == ["ab"]
    _assert_machine_pair_row(pair[0])


def test_agent_key_fleet_list_and_by_id_read_pair_rows_without_metrics(edges):
    edges.as_user(_agent_key("agent-b"))
    pair = _pair_rows(edges.get("/api/evaluations").json())
    assert pair and all(r["agent_name"] == "agent-b" for r in pair)
    for r in pair:
        _assert_machine_pair_row(r)
    one = edges.get(f"/api/evaluations/{pair[0]['id']}")
    assert one.status_code == 200
    _assert_machine_pair_row(one.json())


def test_a_person_reads_the_pair_row_with_metrics_and_evidence(edges):
    from db.evaluations import TIER0_PAIR_EVALUATOR
    rows = edges.get("/api/agents/agent-b/evaluations").json()
    pair = [r for r in rows if r["evaluator"] == TIER0_PAIR_EVALUATOR]
    assert len(pair) == 1
    assert pair[0]["checks"]["metrics"]["cost"] == 0.5
    assert any("evidence" in k for k in pair[0]["checks"]["checks"])
    one = edges.get(f"/api/evaluations/{pair[0]['id']}").json()
    assert one["checks"]["metrics"]["latency_ms"]["start_to_end"] == 2000


def test_agent_key_tier0_row_is_not_reduced(edges):
    """The reduction is the pair row's only: a Tier-0 row reads as ent#937 left it."""
    from db.evaluations import TIER0_EVALUATOR
    from services.evaluation_read_service import redact_evaluator
    edges.as_user(_agent_key("agent-b"))
    r = edges.get("/api/agents/agent-b/executions/ab/evaluation").json()
    assert r["evaluator"] == redact_evaluator(TIER0_EVALUATOR)
    assert r["checks"]["headline"]["gate_fail"] == 0
