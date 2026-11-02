"""Evaluation reads hold a share holder to their own runs (trinity-enterprise#875
rule, found by /cso on the ent#936 range).

ent#875 narrowed a person an agent is merely SHARED with to the executions they
started (`viewer_own_rows` / `viewer_row_scope`). The evaluation reads fold the
same executions — the Tier-0, judge and pair rows this epic writes one per run —
and went through `accessible_agent_names` alone, so a share holder read every
user's run ids, schedule ids, failure reasons and costs through them while
`GET /executions/{id}` 404ed the same ids.

Driven through the real router, dependencies and db (the `test_937_routes`
harness); only the principal, the owner map and the roster are supplied. The
owner reads both runs on every route — the control that the narrowing is the
share tier's and not a broken filter.
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

OWNER, OWNER_ID = "owner", 1
SHARER, SHARER_ID = "client@example.com", 7
AGENT = "agent-s"
MINE, THEIRS = "run-mine", "run-theirs"


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _exec(exec_id, user_id):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id="sched-1", agent_name=AGENT, status="success",
            triggered_by="manual", message="/x", source_user_id=user_id, cost=0.5,
            started_at=_ago(11), completed_at=_ago(10)))


def _grade(exec_id):
    from database import db
    checks = [{"id": "claimed_artifact", "tier": 0, "source": "x", "status": "fail",
               "reason": "missing", "evidence": "quote"}]
    db.upsert_platform_evaluation(
        AGENT, execution_id=exec_id, completion=True,
        checks={"schema": 1, "headline": {"passed": False, "gate_pass": 0,
                                          "gate_fail": 1, "gate_skip": 0},
                "checks": checks})


def _user(username, uid, role="creator"):
    from models import User
    return User(id=uid, username=username, role=role)


@pytest.fixture
def client(db_backend, monkeypatch):
    from database import db
    from routers import evaluations
    from services.agent_service import helpers

    principal = {"user": _user(OWNER, OWNER_ID)}
    app = FastAPI()
    app.include_router(evaluations.router)
    app.dependency_overrides[evaluations.get_current_user] = lambda: principal["user"]
    reach = {OWNER, SHARER}
    monkeypatch.setattr(db, "get_agent_owner", lambda name: OWNER if name == AGENT else None)
    monkeypatch.setattr(db, "can_user_access_agent", lambda u, n: n == AGENT and u in reach)
    monkeypatch.setattr(db, "can_user_share_agent", lambda u, n: n == AGENT and u == OWNER)
    monkeypatch.setattr(helpers, "accessible_agent_names",
                        lambda u: [AGENT] if u.username in reach else [])
    monkeypatch.setattr(helpers, "get_accessible_agents", lambda u: (
        [{"name": AGENT, "is_owner": u.username == OWNER}] if u.username in reach else []))
    for exec_id, uid in ((MINE, SHARER_ID), (THEIRS, OWNER_ID)):
        _exec(exec_id, uid)
        _grade(exec_id)
    tc = TestClient(app)
    tc.as_user = lambda u: principal.__setitem__("user", u)
    return tc


def _run_ids(path, body):
    """Every execution id a route body names, wherever the route puts it."""
    found = set()

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ("execution_id", "id") and x in (MINE, THEIRS):
                    found.add(x)
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(body)
    return found


LIST_ROUTES = [
    f"/api/agents/{AGENT}/evaluations",
    "/api/evaluations",
    f"/api/agents/{AGENT}/evaluations/summary",
    "/api/evaluations/failed",
    f"/api/agents/{AGENT}/evaluations/indicators/checks_passed/runs",
]


@pytest.mark.parametrize("path", LIST_ROUTES)
def test_the_owner_reads_every_run(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert _run_ids(path, r.json()) == {MINE, THEIRS}


@pytest.mark.parametrize("path", LIST_ROUTES)
def test_a_share_holder_reads_only_the_runs_they_started(client, path):
    client.as_user(_user(SHARER, SHARER_ID))
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert _run_ids(path, r.json()) == {MINE}


def test_a_share_holders_counts_fold_only_their_runs(client):
    client.as_user(_user(SHARER, SHARER_ID))
    agg = client.get("/api/evaluations/aggregate?by=agent").json()
    assert agg["total"]["evaluated"] == 1
    ind = client.get(f"/api/agents/{AGENT}/evaluations/indicators").json()
    client.as_user(_user(OWNER, OWNER_ID))
    owner_ind = client.get(f"/api/agents/{AGENT}/evaluations/indicators").json()
    assert ind != owner_ind


@pytest.mark.parametrize("suffix", ["evaluation", "evaluations"])
def test_another_users_run_reads_as_not_evaluated(client, suffix):
    client.as_user(_user(SHARER, SHARER_ID))
    theirs = client.get(f"/api/agents/{AGENT}/executions/{THEIRS}/{suffix}")
    assert (theirs.status_code, theirs.json()["detail"]) == (404, "not_evaluated")
    assert client.get(f"/api/agents/{AGENT}/executions/{MINE}/{suffix}").status_code == 200


def test_another_users_evaluation_by_id_and_chain_are_not_found(client):
    from database import db
    theirs = db.latest_tier0_for_execution(THEIRS)["id"]
    mine = db.latest_tier0_for_execution(MINE)["id"]
    client.as_user(_user(SHARER, SHARER_ID))
    assert client.get(f"/api/evaluations/{theirs}").status_code == 404
    assert client.get(f"/api/evaluations/{mine}").status_code == 200
    assert client.get(f"/api/evaluations/chain/{THEIRS}").status_code == 404
    assert client.get(f"/api/evaluations/chain/{MINE}").status_code == 200


def test_a_share_holder_gets_no_stored_trend_for_the_agent(client, monkeypatch):
    """The daily points fold every user's runs and cannot be split by user, so
    a share holder reads the empty series an out-of-scope agent reads."""
    from database import db
    monkeypatch.setattr(db, "metric_series_points_for_dims",
                        lambda *a, **k: [{"ts": _ago(60), "value_numeric": 0.5}])
    path = f"/api/evaluations/trend?metric=eval.completion_rate&agent={AGENT}"
    assert client.get(path).json()["points"] != []
    client.as_user(_user(SHARER, SHARER_ID))
    assert client.get(path).json()["points"] == []
