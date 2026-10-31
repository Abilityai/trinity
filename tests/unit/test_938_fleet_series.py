"""Fleet evaluation series (trinity-enterprise#938, C7 — ruling T3).

The scorecard writes one more daily series, `dims={"scope": "fleet"}`, and the
ent#937 trend route serves it when neither `agent=` nor a pair is named:

  * the fleet point is the fold of EVERY agent's rows for the day, written by
    the nightly pass's unscoped read of a COMPLETED day only (never today,
    which is still filling) — never by the per-row restate or
    the over-cap per-agent retry, which see one agent and would store a
    partial number as a whole one;
  * its dims are non-empty (the dims reader selects nothing for `{}`) and its
    identity is distinct from every per-agent / per-pair point;
  * the route reads it with every per-agent/pair key absent, and hands it only
    to a principal whose scope is already every agent and that is not an
    agent key: an admin person, and the system key (counts only — a judge
    score is still a 422 for a machine). A person scoped to fewer agents and
    an agent key get the empty series, never a 403.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_937_rollup import (  # noqa: E402,F401 — fixtures register on import
    DAY0, NOW, _capped, _iso, grade, points, seed, svc,
)
from test_937_routes import (  # noqa: E402,F401
    ADMIN, OWNER, _agent_key, _point, _user, client, db_backend,
)

pytestmark = pytest.mark.unit

FLEET = {"scope": "fleet"}


def _rows():
    return [
        {"execution_id": "e1", "agent_name": "a", "caller_agent": None, "completion": True,
         "checks": {"headline": {"passed": True, "gate_pass": 1, "gate_fail": 0}}},
        {"execution_id": "e2", "agent_name": "b", "caller_agent": "a", "completion": False,
         "checks": {"headline": {"passed": False, "gate_pass": 0, "gate_fail": 1}}},
        {"execution_id": "e3", "agent_name": "b", "caller_agent": None, "completion": True,
         "checks": {"headline": {"passed": True, "gate_pass": 1, "gate_fail": 0}}},
    ]


# --- the writer ----------------------------------------------------------------

def test_a_fleet_day_folds_every_agents_rows_under_non_empty_dims(svc):
    out = svc.points_for_day(_iso(DAY0), _rows(), {}, fleet=True)
    fleet = {p["metric"]: p for p in out if p["dims"] == FLEET}
    assert fleet["eval.evaluated"]["value_numeric"] == 3
    assert fleet["eval.passed"]["value_numeric"] == 2
    assert fleet["eval.checks_passed_rate"]["value_numeric"] == 0.6667     # fold rounds rates to 4 places
    # No judge verdict → no quality point for the fleet either (absence, not 0).
    assert "eval.quality_mean" not in fleet
    # The per-agent and per-pair points are unchanged beside it.
    agent_b = {p["metric"]: p["value_numeric"] for p in out if p["dims"] == {"agent": "b"}}
    assert agent_b["eval.evaluated"] == 2
    assert any(p["dims"] == {"caller": "a", "callee": "b"} for p in out)
    # One identity per (metric, day, dims): the fleet point never collides.
    keys = [p["idempotency_key"] for p in out]
    assert len(keys) == len(set(keys))


def test_a_scoped_day_writes_no_fleet_point(svc):
    out = svc.points_for_day(_iso(DAY0), _rows(), {}, fleet=False)
    assert out and not any(p["dims"] == FLEET for p in out)


def test_the_per_row_restate_never_writes_a_fleet_point(db_backend, svc):
    seed("e1", agent="a")
    grade("e1", agent="a")
    assert svc.on_tier0_row("e1").recorded > 0
    assert points(agent="a")["eval.evaluated"][0] == 1
    assert points(**FLEET) == {}


def _fleet_days(metric="eval.evaluated"):
    """Every stored fleet point of one metric, as {day_ts: value}."""
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import metric_points
    import json
    with get_engine().connect() as conn:
        rows = conn.execute(select(metric_points).where(
            metric_points.c.metric == metric)).mappings().all()
    return {r["ts"]: r["value_numeric"] for r in rows
            if (r["dims"] if isinstance(r["dims"], dict) else json.loads(r["dims"])) == FLEET}


def test_the_nightly_pass_writes_the_whole_fleets_completed_day_and_is_idempotent(db_backend, svc):
    yesterday = DAY0 - timedelta(days=1)
    seed("a1", agent="a", completed=yesterday + timedelta(minutes=5))
    grade("a1", agent="a")
    seed("b1", agent="b", completed=yesterday + timedelta(minutes=7))
    grade("b1", agent="b", passed=False)
    assert svc.nightly_pass(NOW, days=1)["failed"] == 0
    got = points(**FLEET)
    assert got["eval.evaluated"] == (2.0, _iso(yesterday))
    assert got["eval.passed"][0] == 1
    assert got["eval.checks_passed_rate"][0] == 0.5
    assert svc.nightly_pass(NOW, days=1)["written"] == 0


def test_the_nightly_pass_never_writes_todays_fleet_point(db_backend, svc):
    """Today is still filling: a fleet point folded at 00:10 from ten minutes of
    runs would be a partial number stored as the day's whole one, and nothing
    restates the fleet until the next nightly (review C1). Today's per-agent
    point is still written — the per-row restate keeps that one current."""
    yesterday = DAY0 - timedelta(days=1)
    seed("y1", agent="a", completed=yesterday + timedelta(minutes=5))
    grade("y1", agent="a")
    seed("t1", agent="a")                                     # today
    grade("t1", agent="a")
    assert svc.nightly_pass(NOW, days=1)["failed"] == 0
    assert _fleet_days() == {_iso(yesterday): 1.0}
    assert _iso(DAY0) in _agent_days("a")


def _agent_days(agent, metric="eval.evaluated"):
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import metric_points
    import json
    with get_engine().connect() as conn:
        rows = conn.execute(select(metric_points).where(
            metric_points.c.metric == metric)).mappings().all()
    return {r["ts"] for r in rows
            if (r["dims"] if isinstance(r["dims"], dict) else json.loads(r["dims"])) == {"agent": agent}}


def test_an_over_cap_day_retries_per_agent_and_writes_no_fleet_point(db_backend, svc, monkeypatch):
    yesterday = DAY0 - timedelta(days=1)     # a completed day, so only the cap withholds the fleet point
    for i, agent in enumerate(("a", "b")):
        seed(f"e-{agent}", agent=agent, completed=yesterday + timedelta(minutes=5 + i))
        grade(f"e-{agent}", agent=agent)
    _capped(monkeypatch, 1)
    assert svc.nightly_pass(NOW, days=1)["failed"] == 0
    assert points(agent="a")["eval.evaluated"][0] == 1      # the per-agent retry ran
    assert points(**FLEET) == {}                              # …and wrote no partial fleet total


# --- the route ------------------------------------------------------------------

def _fleet_read(client, metric="eval.evaluated"):
    return client.get("/api/evaluations/trend", params={"metric": metric})


@pytest.fixture
def stored(client):
    _point("eval.evaluated", FLEET, 9, 2)
    _point("eval.evaluated", FLEET, 12, 1)
    _point("eval.evaluated", {"agent": "agent-a"}, 4, 1)      # never part of the fleet read
    return client


def test_an_admin_person_reads_the_fleet_series(stored):
    stored.as_user(_user(ADMIN, role="admin"))
    r = _fleet_read(stored)
    assert r.status_code == 200, r.text
    assert r.json()["dims"] == FLEET
    assert [p["value"] for p in r.json()["points"]] == [9, 12]


def test_the_system_key_reads_fleet_counts_but_never_a_judge_score(stored):
    stored.as_user(_user(ADMIN, role="admin", mcp_scope="system"))
    assert [p["value"] for p in _fleet_read(stored).json()["points"]] == [9, 12]
    r = _fleet_read(stored, "eval.quality_mean")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_metric"


@pytest.mark.parametrize("principal", [
    pytest.param(lambda: _user(OWNER, role="operator"), id="operator-person"),
    pytest.param(lambda: _user(OWNER, role="creator"), id="creator-person"),
    pytest.param(lambda: _agent_key("agent-a"), id="agent-key-of-an-admin-owner"),
    # Machine keys owned by an admin whose scope is every agent: the fleet
    # branch names the admin PERSON and the system key, not "whoever's scope is
    # every agent" (review /cso residual) — so a scope fenced off elsewhere, or
    # one invented tomorrow, still reads nothing here.
    pytest.param(lambda: _user(ADMIN, role="admin", mcp_scope="connector"), id="connector-key-of-an-admin"),
    pytest.param(lambda: _user(ADMIN, role="admin", mcp_scope="portal_delegate"), id="portal-delegate-of-an-admin"),
    pytest.param(lambda: _user(ADMIN, role="admin", mcp_scope="ops"), id="unknown-scope-of-an-admin"),
])
def test_a_reader_scoped_to_fewer_agents_gets_the_empty_series_not_a_403(stored, principal):
    stored.as_user(principal())
    r = _fleet_read(stored)
    assert r.status_code == 200, r.text
    assert r.json() == {"metric": "eval.evaluated", "dims": FLEET, "points": []}
