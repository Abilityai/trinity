"""Per-agent behavioral indicators — the routes (trinity-enterprise#939).

A real FastAPI app over the real `routers.evaluations` router and the harness
db; only the principal and the owner map are supplied (the ent#937 route-test
shape). Pinned: the owner reads the indicators with samples; another tenant is
the uniform 404; an agent key is refused (no agent-visible scalar); the window
is one of the declared lengths; the drill-down groups by source with schedule
names resolved; the cost baseline comes from the agent's prior successful runs.
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
AGENTS = {"agent-a": OWNER, "agent-c": OTHER}


def _ago(days=0, minutes=10):
    return (datetime.now(timezone.utc) - timedelta(days=days, minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _exec(exec_id, agent="agent-a", *, schedule_id=None, message="/lead-research go",
          status="success", days=0, cost=None):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id=schedule_id or "__manual__", agent_name=agent, status=status,
            triggered_by="schedule" if schedule_id else "manual", message=message,
            cost=cost, started_at=_ago(days, 11), completed_at=_ago(days)))


def _grade(exec_id, agent="agent-a", *, fail=False, playbook="lead-research"):
    from database import db
    checks = ([{"id": "deliverable_landed", "tier": "gate", "source": "platform",
                "status": "fail", "reason": "no_receipt", "evidence": {"q": "SECRET"}}]
              if fail else [])
    return db.upsert_platform_evaluation(
        agent, execution_id=exec_id, completion=True,
        checks={"schema": 1,
                "intent_ref": {"kind": "playbook" if playbook else "message", "ref": playbook},
                "headline": {"passed": not fail, "gate_pass": 1, "gate_fail": int(fail),
                             "gate_skip": 0},
                "checks": checks})


def _judge(exec_id, agent="agent-a", *, scope="pass", quality=0.8):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_evaluations
    with get_engine().begin() as conn:
        conn.execute(insert(agent_evaluations).values(
            id=f"t1-{exec_id}", agent_name=agent, execution_id=exec_id,
            evaluator="platform:tier1:model-x", target_kind="execution",
            target_id=exec_id, quality=quality,
            judge_json=json.dumps({"schema": 1, "state": "judged", "model": "m",
                                   "verdict": "graded", "quality": quality,
                                   "dimensions": {"scope_fidelity": {"verdict": scope}}}),
            created_at=_ago(), updated_at=_ago()))


def _user(username, *, role="user", agent_name=None, mcp_scope=None):
    from models import User
    return User(id=1, username=username, role=role, agent_name=agent_name, mcp_scope=mcp_scope)


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
                        lambda user, name: AGENTS.get(name) == user)
    monkeypatch.setattr(db, "can_user_share_agent",
                        lambda user, name: AGENTS.get(name) == user)
    monkeypatch.setattr(helpers, "accessible_agent_names",
                        lambda u: [n for n, o in AGENTS.items() if o == u.username])
    tc = TestClient(app)
    tc.as_user = lambda u: principal.__setitem__("user", u)
    return tc


@pytest.fixture
def runs(client):
    """agent-a: six playbook runs (two fail deliverable_landed), all judged;
    two schedule runs that fail; prior-month successful runs for the cost
    baseline; agent-c: another tenant's run."""
    for i in range(6):
        _exec(f"p{i}", cost=0.4)
        _grade(f"p{i}", fail=i < 2)
        _judge(f"p{i}", scope="fail" if i == 0 else "pass")
    for i in range(2):
        _exec(f"s{i}", schedule_id="sched-digest", message="send the digest", cost=0.4)
        _grade(f"s{i}", fail=True, playbook=None)
    for i in range(5):
        _exec(f"old{i}", days=20, cost=0.2)
    _exec("c1", "agent-c"); _grade("c1", "agent-c")
    return client


def _ind(body, iid):
    return next(i for i in body["indicators"] if i["id"] == iid)


def test_owner_reads_indicators_with_samples(runs, monkeypatch):
    from database import db
    from types import SimpleNamespace
    monkeypatch.setattr(db, "get_schedule", lambda sid: SimpleNamespace(
        name="Weekly pipeline digest", agent_name="agent-a") if sid == "sched-digest" else None)
    r = runs.get("/api/agents/agent-a/evaluations/indicators")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 14 and body["runs"] == 8 and body["min_runs"] == 5
    cp = _ind(body, "checks_passed")
    assert (cp["num"], cp["n"], cp["state"]) == (4, 8, "ok") and cp["value"] == 0.5
    sf = _ind(body, "scope_fidelity")
    assert (sf["num"], sf["n"]) == (5, 6)
    assert _ind(body, "escalation_correctness")["state"] == "insufficient"
    cost = _ind(body, "cost_vs_baseline")
    assert cost["median"] == 0.4 and cost["baseline"] == 0.2 and cost["value"] == 2.0
    sched = next(p for p in body["fix_paths"] if p["source"]["kind"] == "schedule")
    assert sched["source"]["label"] == "Weekly pipeline digest" and sched["failed"] == 2
    assert "SECRET" not in r.text


def test_other_tenant_is_uniform_404(runs):
    assert runs.get("/api/agents/agent-c/evaluations/indicators").status_code == 404
    assert runs.get("/api/agents/agent-c/evaluations/indicators/completion/runs").status_code == 404


def test_agent_key_is_refused(runs):
    runs.as_user(_user(OWNER, role="admin", agent_name="agent-a", mcp_scope="agent"))
    r = runs.get("/api/agents/agent-a/evaluations/indicators")
    assert r.status_code == 403
    r = runs.get("/api/agents/agent-a/evaluations/indicators/checks_passed/runs")
    assert r.status_code == 403


def test_window_must_be_declared(runs):
    r = runs.get("/api/agents/agent-a/evaluations/indicators", params={"days": 9})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_window"


def test_drilldown_groups_by_source_and_hides_evidence(runs):
    r = runs.get("/api/agents/agent-a/evaluations/indicators/checks_passed/runs",
                 params={"check": "deliverable_landed"})
    assert r.status_code == 200, r.text
    groups = r.json()["groups"]
    pb = next(g for g in groups if g["source"]["kind"] == "playbook")
    assert pb["source"]["ref"] == "lead-research" and (pb["failed"], pb["runs"]) == (2, 6)
    assert pb["items"][0]["reasons"] == ["deliverable_landed:no_receipt"]
    assert "SECRET" not in r.text


def test_drilldown_refuses_cost_and_unknown(runs):
    for ind in ("cost_vs_baseline", "nonsense"):
        r = runs.get(f"/api/agents/agent-a/evaluations/indicators/{ind}/runs")
        assert r.status_code == 422, ind


def test_no_runs_window(client):
    body = client.get("/api/agents/agent-a/evaluations/indicators").json()
    assert body["runs"] == 0 and body["latest_run_at"] is None
    assert all(i["state"] == "no_runs" and i["value"] is None for i in body["indicators"])


def test_min_runs_override(runs, monkeypatch):
    from database import db
    monkeypatch.setattr(db, "get_setting_value",
                        lambda k, d=None: "10" if k == "agent_indicators_min_runs" else d)
    body = runs.get("/api/agents/agent-a/evaluations/indicators").json()
    assert body["min_runs"] == 10
    assert _ind(body, "completion")["state"] == "insufficient"
