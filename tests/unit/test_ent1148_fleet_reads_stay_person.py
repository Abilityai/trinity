"""AC3 on the fleet reads: an agent key in a shared person's turn sees nothing
of an agent that person cannot open (abilityai/trinity-enterprise#1148,
review finding F2).

`viewer_row_scope` fences the per-agent routes. The fleet routes —
`GET /api/executions` and `GET /api/activities/timeline` — narrow through
`viewer_own_rows` instead, to `(person, [])` over the KEY's reach. A callee the
key's agent delegated to records the person, so in P's turn A's key read B's
rows carrying P (message text, `search`, the prompt preview) on an agent P
cannot open. The agent set is now cut to the agents the person may open,
before the query; an unreadable lookup drops the agent.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

import dependencies as dep  # noqa: E402
import routers.activities as _ACT  # noqa: E402
import routers.executions as _EXE  # noqa: E402
from services.agent_service import helpers  # noqa: E402

pytestmark = pytest.mark.unit

CALLER, CALLEE = "agent-a", "agent-b"
OWNER_ID, MATE_ID = 1, 9
USERS = {OWNER_ID: {"id": OWNER_ID, "username": "owner", "role": "user"},
         MATE_ID: {"id": MATE_ID, "username": "mate", "role": "user"}}
ACCESS = {("owner", CALLER), ("owner", CALLEE), ("mate", CALLER)}
EXECUTIONS = {"e-mate": SimpleNamespace(agent_name=CALLER, source_user_id=MATE_ID, triggered_by="user",
                                        source_agent_name=None)}


def _key(run):
    return SimpleNamespace(id=OWNER_ID, username="owner", email=None, role="user", mcp_scope="agent",
                           agent_name=CALLER, connector_agent=None, portal_delegate=False,
                           is_event_loopback=False, vouched_source_agent=None, _acting_execution_id=run)


@pytest.fixture(autouse=True)
def world(monkeypatch):
    seen = {}
    monkeypatch.setattr(dep.db, "get_execution", lambda run: EXECUTIONS.get(run))
    monkeypatch.setattr(dep.db, "get_user_by_id", lambda uid: USERS.get(uid))
    monkeypatch.setattr(dep.db, "can_user_share_agent", lambda u, a: u == "owner")
    monkeypatch.setattr(dep.db, "can_user_access_agent", lambda u, a: (u, a) in ACCESS)
    monkeypatch.setattr(dep.db, "get_running_turn_attribution",
                        lambda agent: [dict(vars(EXECUTIONS["e-mate"]), id="e-mate")] if agent == CALLER else [])
    # The key's reach: its own agent and the callee it delegates to.
    monkeypatch.setattr(helpers, "principal_reach", lambda u: {CALLER, CALLEE})

    def fleet(agent_names, **kw):
        seen["executions"] = (agent_names, kw)
        return []

    def timeline(**kw):
        seen["activities"] = kw
        return []

    monkeypatch.setattr(_EXE.db, "get_fleet_executions", fleet)
    monkeypatch.setattr(_ACT.db, "get_activities_in_range", timeline)
    return seen


def _executions(user, agent=None):
    return asyncio.run(_EXE.list_fleet_executions(
        status=None, triggered_by=None, hours=24, search=None, agent=agent, limit=50, offset=0,
        current_user=user))


def _timeline(user):
    return asyncio.run(_ACT.get_activity_timeline(
        start_time=None, end_time=None, activity_types=None, limit=100, current_user=user))


def test_fleet_executions_in_the_persons_turn_omit_the_agent_they_cannot_open(world):
    _executions(_key("e-mate"))
    names, kw = world["executions"]
    assert names == [CALLER]
    assert kw["own_rows_user_id"] == MATE_ID


def test_asking_for_that_agent_by_name_reads_nothing(world):
    _executions(_key("e-mate"), agent=CALLEE)
    assert world["executions"][0] == []


def test_the_activity_timeline_omits_it_too(world):
    _timeline(_key("e-mate"))
    assert world["activities"]["agent_names"] == [CALLER]
    assert world["activities"]["own_rows_user_id"] == MATE_ID


def test_an_agent_the_person_can_open_stays(world, monkeypatch):
    monkeypatch.setattr(dep.db, "can_user_access_agent", lambda u, a: (u, a) in ACCESS | {("mate", CALLEE)})
    _executions(_key("e-mate"))
    assert world["executions"][0] == [CALLER, CALLEE]


def test_an_unreadable_lookup_drops_the_agent(world, monkeypatch):
    def boom(u, a):
        raise RuntimeError("db down")
    monkeypatch.setattr(dep.db, "can_user_access_agent", boom)
    _executions(_key("e-mate"))
    _timeline(_key("e-mate"))
    assert world["executions"][0] == []
    assert world["activities"]["agent_names"] == []


def test_a_key_in_no_persons_turn_keeps_its_reach_without_a_lookup(world, monkeypatch):
    asked = []
    monkeypatch.setattr(dep.db, "can_user_access_agent", lambda u, a: asked.append((u, a)) or True)
    monkeypatch.setattr(dep.db, "get_running_turn_attribution", lambda agent: [])
    _executions(_key(None))
    _timeline(_key(None))
    assert world["executions"][0] == [CALLER, CALLEE]
    assert world["activities"]["agent_names"] == [CALLER, CALLEE]
    assert asked == []
