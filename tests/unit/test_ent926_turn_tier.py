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


@pytest.mark.parametrize("trigger", ["schedule", "system", "reminder", "loop",
                                     "validation", "webhook", "manual", "event"])
def test_a_turn_the_owner_configured_acts_at_owner_tier(trigger):
    assert _tier(_db(execution=_exec(user_id=None, triggered_by=trigger))) == "owner"


@pytest.mark.parametrize("trigger", ["public", "paid", "voip", "voice", "agent", "room",
                                     "fan_out", "session", "mcp", "slack", "telegram", "", None,
                                     # a self-task row always carries its caller; one that does
                                     # not was dispatched in a visitor's turn (ent#926 2026-10-19)
                                     "self_task"])
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
    # A shared person: share tier, no shell. The answer names no person.
    assert _call() == {"tier": "share", "kind": "person", "shell": False}


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


# ---- ent#926 2026-10-19: a schedule rates as its creator; visitors vs persons ----------

def _sched_db(*, creator, creator_user, can_share, schedule_id="s1", trigger="schedule"):
    db = _db(execution=_exec(user_id=None, triggered_by=trigger), user=creator_user, can_share=can_share)
    db.get_execution.return_value.schedule_id = schedule_id
    db.get_schedule.return_value = SimpleNamespace(owner_id=creator) if creator is not False else None
    return db


@pytest.mark.parametrize("trigger", ["schedule", "webhook", "retry", "validation", "manual"])
def test_a_schedule_a_shared_person_created_runs_at_their_tier(trigger):
    """They may create schedules (accessor route); the runs carry no user, and
    used to read as the owner's by trigger label alone."""
    from services import turn_tier_service as t
    db = _sched_db(creator=9, creator_user={"username": "mate", "role": "operator"}, can_share=False,
                   trigger=trigger)
    assert t.turn_actor(db, AGENT, "e1") == ("share", "person", 9)


def test_a_schedule_the_owner_created_runs_at_owner_tier():
    from services import turn_tier_service as t
    db = _sched_db(creator=1, creator_user={"username": "owner", "role": "user"}, can_share=True)
    assert t.turn_actor(db, AGENT, "e1") == ("owner", None, None)


def test_a_schedule_that_is_gone_fails_closed():
    from services import turn_tier_service as t
    db = _sched_db(creator=False, creator_user=None, can_share=False)
    assert t.turn_actor(db, AGENT, "e1") == ("share", "visitor", None)


def test_a_reminder_row_is_not_looked_up_as_a_schedule():
    from services import turn_tier_service as t
    db = _sched_db(creator=9, creator_user=None, can_share=False, schedule_id="__manual__", trigger="reminder")
    assert t.turn_actor(db, AGENT, "e1") == ("owner", None, None)
    db.get_schedule.assert_not_called()


def test_a_channel_turn_is_a_visitor():
    from services import turn_tier_service as t
    db = _db(execution=_exec(user_id=None, triggered_by="slack"))
    assert t.turn_actor(db, AGENT, "e1") == ("share", "visitor", None)


@pytest.mark.parametrize("tier,kind,setting,expected", [
    ("owner", None, {"visitor_shell": False}, True),       # the owner's turns always
    ("share", "person", {}, False),                        # a shared person never
    ("share", "person", {"visitor_shell": True}, False),
    ("share", "visitor", {}, True),                        # default on: channels keep working
    ("share", "visitor", {"visitor_shell": True}, True),
    ("share", "visitor", {"visitor_shell": False}, False), # the owner turned it off
    ("unknown", None, {}, False),
])
def test_who_gets_the_shell(tier, kind, setting, expected):
    from services import turn_tier_service as t
    db = MagicMock()
    db.get_guardrails_config.return_value = setting
    assert t.shell_allowed(db, AGENT, tier, kind) is expected
    assert t.restricts_standing_work(db, AGENT, tier, kind) is (tier != "owner" and not expected)


def test_an_unreadable_setting_keeps_the_default():
    from services import turn_tier_service as t
    db = MagicMock()
    db.get_guardrails_config.side_effect = RuntimeError("db down")
    assert t.shell_allowed(db, AGENT, "share", "visitor") is True


def test_the_route_tells_a_visitor_its_shell_answer():
    db = _db(execution=_exec(user_id=None, triggered_by="telegram"))
    db.get_guardrails_config.return_value = {"visitor_shell": False}
    assert _call(tier_db=db) == {"tier": "share", "kind": "visitor", "shell": False}


# ---- a refused turn is a 403, never a retryable 503 (found live 2026-10-19) -------------

def test_an_agent_403_classifies_as_turn_refused():
    from services import chat_execution_service as ces
    from services.execution_envelope import TaskExecutionErrorCode as C
    assert ces._classify_agent_http_failure(403, "this agent runs on Gemini CLI") == C.TURN_REFUSED
    assert ces._classify_agent_http_failure(503, "x") == C.AUTH          # unchanged


def test_the_chat_path_answers_403_with_the_agents_sentence():
    from services import chat_execution_service as ces
    with pytest.raises(ces.ChatDispatchError) as exc:
        asyncio.run(ces._apply_sub003_autoswitch("agent-a", "runs on Gemini CLI, owner only", 403, "e1"))
    assert exc.value.status_code == 403
    assert "Gemini CLI" in str(exc.value.detail)


def test_the_task_path_answers_403_for_a_refused_turn():
    from services import chat_execution_service as ces
    from services.execution_envelope import TaskExecutionErrorCode as C
    result = SimpleNamespace(status="failed", error="runs on Codex, owner only", error_code=C.TURN_REFUSED)
    with patch.object(ces.idempotency_service, "fail"), pytest.raises(ces.ChatDispatchError) as exc:
        ces._map_task_failure("agent-a", result, idem=object())
    assert exc.value.status_code == 403


# ---- emit-event: owner tier for PEOPLE; agent keys keep the #2973 sibling path ------------
# CI 2026-10-19: making the route `OwnedAgent` also cut an agent's key off from
# emitting "for" a sibling (#2973 / ent#614), which is supported. The restriction
# ent#926 needs is on people the agent is merely shared with; an agent key acting
# in a restricted turn is already refused by the standing-work fence on this route.

class _Stop(Exception):
    pass


def _emit(principal, can_share):
    from routers import event_subscriptions as ev
    from models import EmitEventRequest
    with patch.object(ev.db, "can_user_share_agent", lambda u, a: can_share), \
            patch.object(ev.db, "find_matching_event_subscriptions", MagicMock(side_effect=_Stop)):
        return asyncio.run(ev.emit_event_for_agent(AGENT, EmitEventRequest(event_type="work.done"), principal))


def _person(username="mate"):
    return SimpleNamespace(id=9, username=username, role="user", mcp_scope=None, agent_name=None,
                           connector_agent=None, portal_delegate=False, is_event_loopback=False,
                           vouched_source_agent=None)


def test_a_person_the_agent_is_shared_with_cannot_emit_in_its_name():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _emit(_person(), can_share=False)
    assert exc.value.status_code == 403


def test_the_owner_and_an_agent_key_reach_the_emit():
    with pytest.raises(_Stop):                                   # past the gate, into the emit
        _emit(_person("owner"), can_share=True)
    key = SimpleNamespace(id=1, username="owner", role="user", mcp_scope="agent", agent_name="sibling",
                          connector_agent=None, portal_delegate=False, is_event_loopback=False,
                          vouched_source_agent=None)
    with pytest.raises(_Stop):
        _emit(key, can_share=True)
