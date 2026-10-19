"""An agent's own key reads executions for the person whose turn it is (ent#926).

ent#875 narrowed a person's view of an agent's executions to their own rows and
left an agent's key at the full view ("the fleet-wide default is ent#926"). But
an agent acts for whoever chats with it: in a turn for a person the agent is
merely shared with, the agent's MCP tools must not read the owner's runs back
to them.

The turn is the MCP request's `X-Trinity-Execution-Id` (#2392), recorded on the
principal by the auth dependency. Read for an agent-scoped key only:

  * a turn the platform rates `owner` (the owner, an admin, or something the
    owner configured), or no platform turn (`manual` / absent)  -> full view;
  * a `share` turn -> the rows of the person it is for; a share turn with no
    platform user (a public link, a channel) -> no rows;
  * an id that is not one of this agent's executions -> no rows (fail closed).

The header is honest in a share turn because that turn has no shell and cannot
read the agent's key (the turn-tier hook, ent#926), so only the platform's MCP
config sends it.
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
}
USERS = {OWNER_ID: {"username": "owner", "role": "user"}, MATE_ID: {"username": "mate", "role": "user"}}


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setattr(dep.db, "get_execution", lambda run: EXECUTIONS.get(run))
    monkeypatch.setattr(dep.db, "get_user_by_id", lambda uid: USERS.get(uid))
    monkeypatch.setattr(dep.db, "can_user_share_agent", lambda u, a: u == "owner")
    monkeypatch.setattr(dep, "agent_may_reach", lambda *a, **k: True)


@pytest.mark.parametrize("run", [None, "", "manual", "e-owner", "e-sched"])
def test_an_owner_turn_or_no_turn_keeps_the_full_view(run):
    principal = _key(run)
    assert dep.acting_turn_user_id(principal) is None
    assert dep.agent_viewer_tier(principal, AGENT) == dep.VIEWER_KEY
    assert dep.viewer_row_scope(principal, AGENT) is None
    assert helpers.viewer_own_rows(principal) == (None, None)


def test_a_shared_persons_turn_sees_that_persons_rows_only():
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
