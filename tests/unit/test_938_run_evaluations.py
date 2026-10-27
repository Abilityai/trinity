"""The run's evaluation rows, together (trinity-enterprise#938, execution page).

`GET /api/agents/{agent_name}/executions/{execution_id}/evaluations` answers
"how did this run grade, under every grader" in one read: the Tier-0 row, the
judge's row when the run was sampled, and the pair row when another agent
delegated it. Same gate, same uniform 404 and the same per-caller projection
as the singular route beside it — an agent key reads its judge's findings and
never its score (ent#936 TD-8), and a pair row's ids and reason codes only
(ent#940). A run with no Tier-0 row is `not_evaluated`, whatever else exists.
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
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

OWNER = "owner"
ADMIN = "root"
AGENTS = {"agent-a": OWNER, "agent-b": OWNER}
URL = "/api/agents/{agent}/executions/{ex}/evaluations"


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _seed(exec_id, agent="agent-a", *, source=None):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id="sched-1", agent_name=agent, status="success",
            triggered_by="agent" if source else "schedule", message="/report-weekly",
            response="Done", source_agent_name=source,
            started_at=_ago(300), completed_at=_ago(120)))


def _tier0(exec_id, agent="agent-a"):
    from database import db
    return db.upsert_platform_evaluation(
        agent, execution_id=exec_id, completion=True, archetype="playbook",
        checks={"schema": 1, "headline": {"passed": True, "gate_pass": 1, "gate_fail": 0,
                                          "gate_skip": 0},
                "checks": [{"id": "credential_echo", "tier": "gate", "source": "platform",
                            "status": "pass", "evidence": {}}]})


def _judge(exec_id):
    from database import db
    row = db.claim_tier1_row(exec_id, model="claude-sonnet-5")
    return db.finish_tier1_row(row["id"], judge={
        "schema": 1, "state": "judged", "model": "claude-sonnet-5", "verdict": "graded",
        "quality": 0.4,
        "dimensions": {"scope_fidelity": {"verdict": "fail", "reason": "edited a file outside the brief"},
                       "helpfulness": {"verdict": "pass"}},
        "findings": [{"dimension": "scope_fidelity", "verdict": "fail", "reason": "out_of_scope_edit"}],
    })


def _pair(exec_id, agent="agent-a"):
    from database import db
    from db.evaluations import TIER0_PAIR_EVALUATOR
    return db.upsert_platform_evaluation(
        agent, execution_id=exec_id, completion=True, evaluator=TIER0_PAIR_EVALUATOR,
        checks={"schema": 1,
                "headline": {"passed": False, "gate_pass": 1, "gate_fail": 1, "gate_skip": 0},
                "checks": [
                    {"id": "dispatch_exists", "tier": "gate", "source": "platform",
                     "status": "pass", "reason": None, "evidence": {}},
                    {"id": "relay_unverified", "tier": "gate", "source": "platform",
                     "status": "fail", "reason": "no_readback",
                     "evidence": {"quote": "SECRET pair"}}],
                "metrics": {"latency_ms": {"start_to_end": 2000}, "cost": 0.5, "verified": False}})


def _user(username, *, role="user", agent_name=None, mcp_scope=None):
    from models import User
    return User(id=1, username=username, role=role, agent_name=agent_name, mcp_scope=mcp_scope)


@pytest.fixture
def client(db_backend, monkeypatch):
    from database import db
    from routers import evaluations

    principal = {"user": _user(OWNER)}
    app = FastAPI()
    app.include_router(evaluations.router)
    app.dependency_overrides[evaluations.get_current_user] = lambda: principal["user"]
    monkeypatch.setattr(db, "get_agent_owner", lambda name: AGENTS.get(name))
    monkeypatch.setattr(db, "can_user_access_agent",
                        lambda user, name: name in AGENTS and (AGENTS[name] == user or user == ADMIN))
    # The operator test behind the projection (`_operator_for`): owner or admin.
    monkeypatch.setattr(db, "can_user_share_agent",
                        lambda user, name: name in AGENTS and (AGENTS[name] == user or user == ADMIN))
    tc = TestClient(app)
    tc.as_user = lambda u: principal.__setitem__("user", u)
    return tc


def test_the_owner_reads_all_three_rows(client):
    _seed("ex-1", source="agent-b")
    t0 = _tier0("ex-1")
    _judge("ex-1")
    _pair("ex-1")
    r = client.get(URL.format(agent="agent-a", ex="ex-1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tier0"]["id"] == t0["id"] and body["tier0"]["evaluator"] == "platform:tier0"
    assert body["judge"]["evaluator"] == "platform:tier1:claude-sonnet-5"
    assert body["judge"]["quality"] == 0.4
    assert body["judge"]["judge"]["dimensions"]["scope_fidelity"]["verdict"] == "fail"
    assert body["pair"]["evaluator"] == "platform:tier0:pair"
    assert body["pair"]["checks"]["checks"][1]["evidence"] == {"quote": "SECRET pair"}


def test_a_run_graded_by_tier0_alone_has_null_siblings(client):
    _seed("ex-1")
    _tier0("ex-1")
    body = client.get(URL.format(agent="agent-a", ex="ex-1")).json()
    assert body["tier0"]["evaluator"] == "platform:tier0"
    assert body["judge"] is None and body["pair"] is None


def test_no_tier0_row_is_the_same_named_404_whatever_else_exists(client):
    """The judge grades on top of Tier-0; a pair row alone is the caller's
    record. Neither makes a run "evaluated" to this route."""
    _seed("ex-1")
    _pair("ex-1")
    r = client.get(URL.format(agent="agent-a", ex="ex-1"))
    assert r.status_code == 404 and r.json()["detail"] == "not_evaluated"
    r = client.get(URL.format(agent="agent-a", ex="never-ran"))
    assert r.status_code == 404 and r.json()["detail"] == "not_evaluated"


def test_another_agents_run_is_not_readable_under_this_agents_path(client):
    _seed("ex-b", agent="agent-b")
    _tier0("ex-b", agent="agent-b")
    r = client.get(URL.format(agent="agent-a", ex="ex-b"))
    assert r.status_code == 404 and r.json()["detail"] == "not_evaluated"


def test_the_graded_agent_reads_findings_never_the_score_and_no_pair_evidence(client):
    """ent#936 TD-8 and ent#940 deviation C, applied to every row this route
    returns: the projection is the shared one, so the sibling route cannot
    leak what the singular one withholds."""
    _seed("ex-1", source="agent-b")
    _tier0("ex-1")
    _judge("ex-1")
    _pair("ex-1")
    client.as_user(_user(OWNER, agent_name="agent-a", mcp_scope="agent"))
    r = client.get(URL.format(agent="agent-a", ex="ex-1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["judge"]["quality"] is None and body["judge"]["judge"] is None
    assert body["judge"]["judge_findings"] == [
        {"dimension": "scope_fidelity", "verdict": "fail", "reason": "out_of_scope_edit"}]
    pair_checks = body["pair"]["checks"]
    assert "metrics" not in pair_checks
    assert all("evidence" not in c for c in pair_checks["checks"])
    assert pair_checks["checks"][1]["reason"] == "no_readback"


def test_the_route_is_read_only_and_gated_like_the_singular_one():
    """No POST/PUT/DELETE sibling, and the same dependency pair."""
    from routers import evaluations
    routes = [r for r in evaluations.router.routes
              if r.path == "/api/agents/{agent_name}/executions/{execution_id}/evaluations"]
    assert len(routes) == 1 and routes[0].methods == {"GET"}
    singular = next(r for r in evaluations.router.routes
                    if r.path == "/api/agents/{agent_name}/executions/{execution_id}/evaluation")
    names = lambda r: sorted(d.call.__name__ for d in r.dependant.dependencies)  # noqa: E731
    assert names(routes[0]) == names(singular)
