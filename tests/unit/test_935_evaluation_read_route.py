"""The Tier-0 read route (trinity-enterprise#935, checkpoint B).

`GET /api/agents/{agent_name}/executions/{execution_id}/evaluation` answers
"how did this run grade" for the run's own agent, its owner and an admin —
under the SAME access rule as the agent's other evaluations
(`get_authorized_agent_by_name`, uniform 404 — Invariant #8/#15), and with
the same projection every other evaluation read goes through (ent#366). The
route is driven through a real FastAPI app with the real dependency; only the
principal and the two agent lookups the dependency makes are supplied.
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
OTHER = "someone-else"
ADMIN = "root"
AGENTS = {"agent-a": OWNER, "agent-b": OWNER, "agent-c": OTHER}


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _seed(exec_id, agent="agent-a"):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id="sched-1", agent_name=agent, status="success",
            triggered_by="schedule", message="/report-weekly", response="Done",
            started_at=_ago(300), completed_at=_ago(120)))


def _grade(exec_id, agent="agent-a"):
    from database import db
    return db.upsert_platform_evaluation(
        agent, execution_id=exec_id, completion=True, archetype="playbook",
        checks={"schema": 1, "headline": {"passed": True}, "checks": []})


def _user(username, *, role="user", agent_name=None, mcp_scope=None):
    from models import User
    return User(id=1, username=username, role=role, agent_name=agent_name, mcp_scope=mcp_scope)


@pytest.fixture
def client(db_backend, monkeypatch):
    """The evaluations router on a real app, the real agent-access dependency,
    with the principal injectable and the two agent lookups it makes answered
    from AGENTS (owner map). Everything else is the real db on the harness."""
    from database import db
    from routers import evaluations

    principal = {"user": _user(OWNER)}
    app = FastAPI()
    app.include_router(evaluations.router)
    # Keyed on the ROUTER's binding, not `dependencies.get_current_user`: the
    # unit conftest's sys.modules restore re-imports `dependencies` between
    # tests, so the module attribute is a fresh object while the router
    # (cached) still depends on the one it imported.
    app.dependency_overrides[evaluations.get_current_user] = lambda: principal["user"]
    monkeypatch.setattr(db, "get_agent_owner", lambda name: AGENTS.get(name))
    monkeypatch.setattr(db, "can_user_access_agent",
                        lambda user, name: name in AGENTS and (AGENTS[name] == user or user == ADMIN))
    tc = TestClient(app)
    tc.as_user = lambda u: principal.__setitem__("user", u)
    return tc


URL = "/api/agents/{agent}/executions/{ex}/evaluation"


def test_the_owner_reads_the_runs_grade(client):
    _seed("ex-1")
    row = _grade("ex-1")
    r = client.get(URL.format(agent="agent-a", ex="ex-1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == row["id"]
    assert body["evaluator"] == "platform:tier0"
    assert body["execution_id"] == "ex-1" and body["target_id"] == "ex-1"
    assert body["target_kind"] == "execution"
    assert body["checks"]["headline"] == {"passed": True}
    assert body["completion"] is True and body["quality"] is None


def test_an_unevaluated_run_is_a_named_404(client):
    _seed("ex-1")
    r = client.get(URL.format(agent="agent-a", ex="ex-1"))
    assert r.status_code == 404
    assert r.json()["detail"] == "not_evaluated"


def test_an_unknown_execution_is_the_same_404_as_an_unevaluated_one(client):
    r = client.get(URL.format(agent="agent-a", ex="never-ran"))
    assert r.status_code == 404 and r.json()["detail"] == "not_evaluated"


def test_another_agents_run_is_not_readable_under_this_agents_path(client):
    """The caller owns both agents, so the agent gate passes for either path;
    the row still answers only under the agent it grades. Same 404 as
    "not evaluated" — the path must not become an execution-id oracle."""
    _seed("ex-b", agent="agent-b")
    _grade("ex-b", agent="agent-b")
    r = client.get(URL.format(agent="agent-a", ex="ex-b"))
    assert r.status_code == 404 and r.json()["detail"] == "not_evaluated"
    assert client.get(URL.format(agent="agent-b", ex="ex-b")).status_code == 200


def test_another_tenant_gets_the_uniform_agent_404(client):
    _seed("ex-1")
    _grade("ex-1")
    client.as_user(_user(OTHER))
    r = client.get(URL.format(agent="agent-a", ex="ex-1"))
    assert r.status_code == 404
    assert r.json()["detail"] == "Agent not found"
    # ...identical to an agent that does not exist (Invariant #8).
    missing = client.get(URL.format(agent="agent-zzz", ex="ex-1"))
    assert (missing.status_code, missing.json()["detail"]) == (404, "Agent not found")


def test_an_admin_reads_any_agents_grade(client):
    _seed("ex-c", agent="agent-c")
    _grade("ex-c", agent="agent-c")
    client.as_user(_user(ADMIN, role="admin"))
    assert client.get(URL.format(agent="agent-c", ex="ex-c")).status_code == 200


def test_the_graded_agent_reads_its_own_grade_and_no_siblings(client):
    """An agent-scoped key resolves to its owner; ent#629 narrows it to the
    agent's own reach. Reading its own grade is allowed (feedback is fine —
    writing it is not); a sibling's row under the sibling's path is the
    uniform 404."""
    _seed("ex-1")
    _grade("ex-1")
    _seed("ex-b", agent="agent-b")
    _grade("ex-b", agent="agent-b")
    client.as_user(_user(OWNER, role="admin", agent_name="agent-a", mcp_scope="agent"))
    own = client.get(URL.format(agent="agent-a", ex="ex-1"))
    assert own.status_code == 200 and own.json()["evaluator"] == "platform:tier0"
    sibling = client.get(URL.format(agent="agent-b", ex="ex-b"))
    assert sibling.status_code == 404 and sibling.json()["detail"] == "Agent not found"


def test_the_route_is_read_only_and_gated_like_the_other_evaluation_reads():
    """Structural pin on top of the behaviour above: the route is a GET and
    takes the agent through the shared uniform-404 dependency, not an inline
    check that could drift into a 404-then-403 split."""
    import inspect
    from routers import evaluations
    sig = inspect.signature(evaluations.get_execution_evaluation)
    dep = sig.parameters["agent_name"].default
    assert getattr(dep.dependency, "__name__", "") == "get_authorized_agent_by_name"
    methods = {m for r in evaluations.router.routes
               if getattr(r, "endpoint", None) is evaluations.get_execution_evaluation
               for m in r.methods}
    assert methods == {"GET"}
