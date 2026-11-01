"""An agent's own key reads executions for the person whose turn it is (ent#926).

ent#875 narrowed a person's view of an agent's executions to their own rows and
left an agent's key at the full view ("the fleet-wide default is ent#926"). But
an agent acts for whoever chats with it: in a turn for a person the agent is
merely shared with, the agent's MCP tools must not read the owner's runs back
to them.

The turn is the MCP request's `X-Trinity-Execution-Id` (#2392), recorded on the
principal by the auth dependency. But the header is not proof: a running turn
can call the API with the agent's key and omit it, send `manual`, or name
another turn. So the decision starts from the LEDGER (the people of the agent's
RUNNING share-tier turns) and the header can only narrow, never widen:

  * no share turn running, and no header / `manual` / an owner-tier turn
    -> the full view (the ent#875 ruling);
  * a share turn running -> its person's rows, whatever the header says;
    several people's, or one with no platform user -> no rows;
  * an id that is not one of this agent's executions -> no rows (fail closed).

A schedule run carries no user: its person is the schedule's creator, so a
share holder's own schedule (and its webhook) runs at share tier.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import dependencies as dep
from services.agent_service import helpers

pytestmark = pytest.mark.unit

AGENT = "agent-a"
OWNER_ID = 1
MATE_ID = 9
MATE2_ID = 10


def _key(run, agent=AGENT, scope="agent"):
    """An agent-scoped key: it resolves to the owner (id 1) carrying their role."""
    return SimpleNamespace(
        id=OWNER_ID, username="owner", role="user", mcp_scope=scope, agent_name=agent,
        connector_agent=None, portal_delegate=False, is_event_loopback=False,
        vouched_source_agent=None, _acting_execution_id=run,
    )


EXECUTIONS = {
    "e-mate": SimpleNamespace(agent_name=AGENT, source_user_id=MATE_ID, triggered_by="user"),
    "e-owner": SimpleNamespace(agent_name=AGENT, source_user_id=OWNER_ID, triggered_by="user"),
    "e-sched": SimpleNamespace(agent_name=AGENT, source_user_id=None, triggered_by="schedule"),
    "e-public": SimpleNamespace(agent_name=AGENT, source_user_id=None, triggered_by="public"),
    "e-other": SimpleNamespace(agent_name="agent-b", source_user_id=MATE_ID, triggered_by="user"),
    "e-mate-other": SimpleNamespace(agent_name=AGENT, source_user_id=MATE2_ID, triggered_by="user"),
    # Runs of real schedules: no user on the row; the creator is the person.
    "e-mate-cron": SimpleNamespace(agent_name=AGENT, source_user_id=None, triggered_by="schedule",
                                   schedule_id="s-mate"),
    "e-mate-hook": SimpleNamespace(agent_name=AGENT, source_user_id=None, triggered_by="webhook",
                                   schedule_id="s-mate"),
    "e-owner-cron": SimpleNamespace(agent_name=AGENT, source_user_id=None, triggered_by="schedule",
                                    schedule_id="s-owner"),
    "e-gone-cron": SimpleNamespace(agent_name=AGENT, source_user_id=None, triggered_by="schedule",
                                   schedule_id="s-deleted"),
}
SCHEDULES = {
    "s-mate": SimpleNamespace(agent_name=AGENT, owner_id=MATE_ID),
    "s-owner": SimpleNamespace(agent_name=AGENT, owner_id=OWNER_ID),
}
#: The agent's RUNNING rows, as `db.get_running_turn_attribution` returns them.
RUNNING: list = []


def _running(*ids):
    RUNNING[:] = [dict(vars(EXECUTIONS[i]), id=i) for i in ids]
USERS = {OWNER_ID: {"username": "owner", "role": "user"}, MATE_ID: {"username": "mate", "role": "user"},
         MATE2_ID: {"username": "mate2", "role": "user"}}


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setattr(dep.db, "get_execution", lambda run: EXECUTIONS.get(run))
    monkeypatch.setattr(dep.db, "get_user_by_id", lambda uid: USERS.get(uid))
    monkeypatch.setattr(dep.db, "can_user_share_agent", lambda u, a: u == "owner")
    monkeypatch.setattr(dep, "agent_may_reach", lambda *a, **k: True)
    monkeypatch.setattr(dep.db, "get_schedule", lambda sid: SCHEDULES.get(sid))
    RUNNING.clear()
    monkeypatch.setattr(dep.db, "get_running_turn_attribution",
                        lambda agent: [r for r in RUNNING if r["agent_name"] == agent])


@pytest.mark.parametrize("run", [None, "", "manual", "e-owner", "e-sched"])
def test_an_owner_turn_or_no_turn_keeps_the_full_view(run):
    principal = _key(run)
    assert dep.acting_turn_user_id(principal) is None
    assert dep.agent_viewer_tier(principal, AGENT) == dep.VIEWER_KEY
    assert dep.viewer_row_scope(principal, AGENT) is None
    assert helpers.viewer_own_rows(principal) == (None, None)


def test_a_shared_persons_turn_sees_that_persons_rows_only():
    _running("e-mate")
    principal = _key("e-mate")
    assert dep.acting_turn_user_id(principal) == MATE_ID
    assert dep.agent_viewer_tier(principal, AGENT) == dep.VIEWER_SHARE
    assert dep.viewer_row_scope(principal, AGENT) == MATE_ID
    # Fleet reads: narrowed on every agent, never exempted by the key owner's agents.
    assert helpers.viewer_own_rows(principal) == (MATE_ID, [])


def test_the_turn_scopes_reads_of_another_agent_too():
    """The turn is the KEY's agent's; a read of agent-b during it is still for the mate."""
    assert dep.viewer_row_scope(_key("e-mate"), "agent-b") == MATE_ID


@pytest.mark.parametrize("run", ["e-public", "e-other", "no-such-run"])
def test_a_share_turn_with_no_person_or_a_foreign_id_sees_nothing(run):
    principal = _key(run)
    assert dep.viewer_row_scope(principal, AGENT) == dep.NO_ROWS_USER_ID
    assert dep.agent_viewer_tier(principal, AGENT) == dep.VIEWER_SHARE


def test_shared_state_routes_refuse_a_shared_persons_turn():
    with pytest.raises(dep.HTTPException) as exc:
        dep.assert_not_share_viewer(_key("e-mate"), AGENT)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("scope", ["system", "ops", "connector"])
def test_only_an_agent_scoped_key_is_read_this_way(scope):
    """Other keys carry no turn of an agent; the header changes nothing for them."""
    principal = _key("e-mate", agent=None, scope=scope)
    assert dep.acting_turn_user_id(principal) is None


def test_a_person_is_untouched_by_the_header():
    person = SimpleNamespace(
        id=MATE_ID, username="mate", role="user", mcp_scope=None, agent_name=None,
        connector_agent=None, portal_delegate=False, is_event_loopback=False,
        vouched_source_agent=None, _acting_execution_id="e-owner",
    )
    assert dep.acting_turn_user_id(person) is None
    assert dep.viewer_row_scope(person, AGENT) == MATE_ID


def test_the_auth_dependency_records_the_header_for_an_agent_key_only():
    from models import User
    req = SimpleNamespace(headers={"X-Trinity-Execution-Id": "e-mate"})
    key = User(id=1, username="owner", mcp_scope="agent", agent_name=AGENT)
    person = User(id=9, username="mate")
    dep.record_acting_turn(req, key)
    dep.record_acting_turn(req, person)
    assert key._acting_execution_id == "e-mate"
    assert person._acting_execution_id is None
    # Private: never serialized, so no token or response can carry it.
    assert "_acting_execution_id" not in key.model_dump()


# ---- the header is not proof: the ledger decides (PR #3777 review, point 3) ----------------


@pytest.mark.parametrize("run", [None, "", "manual", "e-owner", "e-sched", "e-owner-cron"])
def test_while_a_shared_persons_turn_runs_no_header_widens_the_view(run):
    """The shared person's turn could call the API itself with the agent's key and
    send no header, `manual`, or an owner turn's id: none of them is the full view."""
    _running("e-mate", "e-owner")
    principal = _key(run)
    assert dep.acting_turn_user_id(principal) == MATE_ID
    assert dep.viewer_row_scope(principal, AGENT) == MATE_ID
    assert dep.agent_viewer_tier(principal, AGENT) == dep.VIEWER_SHARE
    assert helpers.viewer_own_rows(principal) == (MATE_ID, [])


@pytest.mark.parametrize("run", [None, "manual", "e-owner", "e-mate"])
def test_two_peoples_share_turns_at_once_read_nothing(run):
    """Which turn a request comes from is unprovable, so naming one person's turn
    while another person's runs cannot pick that person's rows."""
    _running("e-mate", "e-mate-other")
    assert dep.viewer_row_scope(_key(run), AGENT) == dep.NO_ROWS_USER_ID


@pytest.mark.parametrize("run", [None, "manual", "e-owner"])
def test_a_running_share_turn_with_no_person_reads_nothing(run):
    _running("e-public")
    assert dep.viewer_row_scope(_key(run), AGENT) == dep.NO_ROWS_USER_ID


def test_only_running_turns_of_the_keys_own_agent_count():
    _running("e-other")
    assert dep.acting_turn_user_id(_key(None)) is None


def test_an_unreadable_ledger_fails_closed(monkeypatch):
    def boom(agent):
        raise RuntimeError("db down")
    monkeypatch.setattr(dep.db, "get_running_turn_attribution", boom)
    assert dep.acting_turn_user_id(_key(None)) == dep.NO_ROWS_USER_ID


# ---- a schedule run is its creator's (PR #3777 review, point 2) --------------------------------


@pytest.mark.parametrize("run", ["e-mate-cron", "e-mate-hook"])
def test_a_share_holders_own_schedule_runs_at_share_tier(run):
    from services import turn_tier_service as tts
    assert tts.turn_tier(dep.db, AGENT, run) == tts.SHARE
    assert tts.turn_person(dep.db, AGENT, EXECUTIONS[run]) == MATE_ID
    _running(run)
    principal = _key(run)
    assert dep.viewer_row_scope(principal, AGENT) == MATE_ID
    assert helpers.viewer_own_rows(principal) == (MATE_ID, [])


def test_the_owners_schedule_still_runs_at_owner_tier():
    from services import turn_tier_service as tts
    assert tts.turn_tier(dep.db, AGENT, "e-owner-cron") == tts.OWNER
    _running("e-owner-cron")
    assert dep.viewer_row_scope(_key("e-owner-cron"), AGENT) is None


def test_a_schedule_that_cannot_be_found_fails_closed_to_share():
    from services import turn_tier_service as tts
    assert tts.turn_tier(dep.db, AGENT, "e-gone-cron") == tts.SHARE
    _running("e-gone-cron")
    assert dep.viewer_row_scope(_key(None), AGENT) == dep.NO_ROWS_USER_ID


def test_a_schedule_of_another_agent_is_not_this_turns_creator(monkeypatch):
    from services import turn_tier_service as tts
    monkeypatch.setitem(SCHEDULES, "s-mate", SimpleNamespace(agent_name="agent-b", owner_id=OWNER_ID))
    assert tts.turn_tier(dep.db, AGENT, "e-mate-cron") == tts.SHARE
