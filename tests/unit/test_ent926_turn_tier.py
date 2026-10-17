"""Which tier a running turn acts for, and who may ask (ent#926).

`services/turn_tier_service.turn_tier` decides from the execution row, never from
the agent. `GET /api/agents/{name}/executions/{id}/turn-tier` answers only to the
agent's own key. The in-container hook asks it before a file tool reaches an
owner-tier path; the hook's own tests live in test_ent926_turn_tier_hook.py.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

AGENT = "agent-a"


def _db(*, execution=None, user=None, can_share=False):
    db = MagicMock()
    db.get_execution.return_value = execution
    db.get_user_by_id.return_value = user
    db.can_user_share_agent.return_value = can_share
    return db


def _exec(*, agent=AGENT, user_id=None, triggered_by="user"):
    return SimpleNamespace(id="e1", agent_name=agent, source_user_id=user_id, triggered_by=triggered_by)


def _tier(db):
    from services import turn_tier_service
    return turn_tier_service.turn_tier(db, AGENT, "e1")


# ---- the decision ---------------------------------------------------------------------

def test_a_missing_execution_is_unknown():
    assert _tier(_db(execution=None)) == "unknown"


def test_an_execution_of_another_agent_is_unknown():
    assert _tier(_db(execution=_exec(agent="agent-b", user_id=1))) == "unknown"


def test_the_owner_acts_at_owner_tier():
    db = _db(execution=_exec(user_id=7), user={"username": "o", "role": "user"}, can_share=True)
    assert _tier(db) == "owner"
    db.can_user_share_agent.assert_called_once_with("o", AGENT)


def test_an_admin_acts_at_owner_tier():
    db = _db(execution=_exec(user_id=1), user={"username": "admin", "role": "admin"})
    assert _tier(db) == "owner"


def test_a_shared_person_acts_at_share_tier():
    db = _db(execution=_exec(user_id=9), user={"username": "mate", "role": "operator"}, can_share=False)
    assert _tier(db) == "share"


def test_a_user_id_with_no_user_row_fails_closed_to_share():
    assert _tier(_db(execution=_exec(user_id=404), user=None)) == "share"


def test_a_garbage_user_id_fails_closed_to_share():
    assert _tier(_db(execution=_exec(user_id="x"), user=None)) == "share"


@pytest.mark.parametrize("trigger", ["schedule", "system", "reminder", "loop", "self_task",
                                     "validation", "webhook", "manual", "event"])
def test_a_turn_the_owner_configured_acts_at_owner_tier(trigger):
    assert _tier(_db(execution=_exec(user_id=None, triggered_by=trigger))) == "owner"


@pytest.mark.parametrize("trigger", ["public", "paid", "voip", "voice", "agent", "room",
                                     "fan_out", "session", "mcp", "slack", "telegram", "", None])
def test_an_outside_caller_with_no_user_acts_at_share_tier(trigger):
    assert _tier(_db(execution=_exec(user_id=None, triggered_by=trigger))) == "share"


def test_a_shared_person_is_not_promoted_by_a_schedule_trigger_label():
    """A user id decides; the trigger label only matters when there is no user."""
    db = _db(execution=_exec(user_id=9, triggered_by="schedule"),
             user={"username": "mate", "role": "operator"}, can_share=False)
    assert _tier(db) == "share"


# ---- the route ------------------------------------------------------------------------

class _Request:
    def __init__(self, auth=None):
        self.headers = {} if auth is None else {"Authorization": auth}


def _call(*, auth="Bearer trinity_mcp_k", key=None, tier_db=None, agent=AGENT):
    from routers import agents
    mdb = tier_db or _db(execution=_exec(user_id=9), user={"username": "m", "role": "operator"})
    mdb.validate_mcp_api_key.return_value = key if key is not None else {"scope": "agent", "agent_name": agent}
    with patch.object(agents, "db", mdb):
        try:
            return asyncio.run(agents.agent_execution_turn_tier(agent, "e1", _Request(auth)))
        except Exception as exc:  # HTTPException
            return exc


def _code(r):
    return getattr(r, "status_code", None)


def test_the_route_answers_the_agents_own_key():
    assert _call() == {"tier": "share"}


def test_the_route_needs_a_bearer_token():
    assert _code(_call(auth=None)) == 403
    assert _code(_call(auth="Basic abc")) == 403


@pytest.mark.parametrize("key", [{"scope": "user", "agent_name": None},
                                 {"scope": "system", "agent_name": None},
                                 {"scope": "agent", "agent_name": "agent-b"},
                                 {}])
def test_the_route_refuses_every_key_but_the_agents_own(key):
    mdb = _db(execution=_exec(user_id=9), user={"username": "m", "role": "operator"})
    r = _call(key=key, tier_db=mdb) if key else _call(key={"scope": None}, tier_db=mdb)
    assert _code(r) == 403
    mdb.get_execution.assert_not_called()


def test_the_route_404s_an_unknown_execution():
    assert _code(_call(tier_db=_db(execution=None))) == 404
