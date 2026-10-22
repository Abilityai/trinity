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


# ---- standing work: a restricted turn cannot set up what later runs as the owner -----------
# (ruling 2026-10-19). Fired schedule/loop/reminder/event turns rate as owner when the
# agent's key created them, because that key resolves to the owner.

EXECUTIONS["e-visitor"] = SimpleNamespace(agent_name=AGENT, source_user_id=None, triggered_by="slack")


class _Req:
    def __init__(self, method, path):
        self.method, self.scope = method, {"path": path}


STANDING = [
    ("POST", "/api/agents/agent-a/schedules"),
    ("PUT", "/api/agents/agent-a/schedules/s1"),
    ("POST", "/api/agents/agent-a/schedules/s1/enable"),
    ("POST", "/api/agents/agent-a/schedules/s1/trigger"),
    ("POST", "/api/agents/agent-a/schedules/s1/webhook"),
    ("POST", "/api/agents/agent-a/schedules/s1/webhook/secret"),
    ("POST", "/api/agents/agent-a/loops"),
    ("POST", "/api/agents/agent-a/reminders"),
    ("POST", "/api/events"),
    ("POST", "/api/agents/agent-a/emit-event"),
    ("POST", "/api/agents/agent-a/event-subscriptions"),
    ("PUT", "/api/event-subscriptions/sub-1"),
]


@pytest.fixture
def visitor_shell(monkeypatch):
    setting = {}
    monkeypatch.setattr(dep.db, "get_guardrails_config", lambda agent: setting)
    return setting


@pytest.mark.parametrize("method,path", STANDING)
def test_a_shared_persons_turn_cannot_set_up_standing_work(method, path, visitor_shell):
    with pytest.raises(dep.HTTPException) as exc:
        dep._enforce_standing_work_fence(_Req(method, path), _key("e-mate"))
    assert exc.value.status_code == 403 and "owner" in exc.value.detail


@pytest.mark.parametrize("method,path", STANDING)
def test_the_owners_turn_and_no_turn_may(method, path, visitor_shell):
    for run in ("e-owner", "e-sched", "manual", None):
        dep._enforce_standing_work_fence(_Req(method, path), _key(run))


def test_a_visitor_may_while_the_owner_allows_it_and_not_after(visitor_shell):
    req = _Req("POST", "/api/agents/agent-a/reminders")
    dep._enforce_standing_work_fence(req, _key("e-visitor"))          # default on
    visitor_shell["visitor_shell"] = False
    with pytest.raises(dep.HTTPException):
        dep._enforce_standing_work_fence(req, _key("e-visitor"))


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/agents/agent-a/schedules"),            # reading is not setting up
    ("DELETE", "/api/agents/agent-a/schedules/s1"),       # removing standing work is fine
    ("POST", "/api/agents/agent-a/schedules/s1/disable"),
    ("POST", "/api/agents/agent-a/reminders/r1/cancel"),
    ("POST", "/api/agents/agent-a/task"),                 # dispatch is scoped, not refused
])
def test_other_routes_are_not_fenced(method, path, visitor_shell):
    dep._enforce_standing_work_fence(_Req(method, path), _key("e-mate"))


def test_a_foreign_execution_id_is_refused_not_trusted(visitor_shell):
    with pytest.raises(dep.HTTPException):
        dep._enforce_standing_work_fence(_Req("POST", "/api/agents/agent-a/loops"), _key("e-other"))


# ---- a turn the agent dispatches is no higher than its parent --------------------------------

def test_a_turn_dispatched_for_a_shared_person_carries_that_person():
    from services import turn_tier_service as t
    principal = _key("e-mate")
    USERS[MATE_ID]["email"] = "mate@example.com"
    assert t.turn_origin_user_id(dep.db, principal) == MATE_ID
    assert t.turn_origin_email(dep.db, principal, "owner@example.com") == "mate@example.com"


def test_a_turn_dispatched_for_a_visitor_carries_no_user():
    from services import turn_tier_service as t
    principal = _key("e-visitor")
    assert t.turn_origin_user_id(dep.db, principal) is None
    assert t.turn_origin_email(dep.db, principal, "owner@example.com") is None


@pytest.mark.parametrize("run", ["e-owner", "e-sched", "manual", None])
def test_an_owner_turn_or_no_turn_dispatches_as_before(run):
    from services import turn_tier_service as t
    principal = _key(run)
    assert t.turn_origin_user_id(dep.db, principal) == OWNER_ID
    assert t.turn_origin_email(dep.db, principal, "owner@example.com") == "owner@example.com"


def test_a_child_of_a_visitor_turn_rates_as_a_visitor_not_the_owner():
    """`self_task` left the owner-configured list: a no-user self-task is a visitor's."""
    from services import turn_tier_service as t
    EXECUTIONS["e-child"] = SimpleNamespace(agent_name=AGENT, source_user_id=None, triggered_by="self_task")
    assert t.turn_actor(dep.db, AGENT, "e-child") == ("share", "visitor", None)


def test_no_row_is_stamped_with_the_bare_caller_id():
    """Every site that stamps a new turn goes through turn_origin_user_id; a new
    `source_user_id=current_user.id` would let an agent key dispatch as the owner."""
    import ast
    from pathlib import Path
    root = Path(dep.__file__).resolve().parent
    offenders = []
    for path in list((root / "services").rglob("*.py")) + list((root / "routers").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.keyword) and node.arg == "source_user_id"
                    and isinstance(node.value, ast.Attribute) and node.value.attr == "id"
                    and isinstance(node.value.value, ast.Name) and node.value.value.id == "current_user"):
                offenders.append(f"{path.relative_to(root)}:{node.value.lineno}")
    assert offenders == [], f"use turn_tier_service.turn_origin_user_id: {offenders}"


# ---- owner-tier routes: refused by default in a restricted turn (PR #3778 review) ---------
# The standing-work fence was nine regexes, so every other owner-tier route an agent's key
# could reach (`POST /git/sync`, `/git/reset-to-main-preserve-state`, a force-reset pull)
# still ran at owner tier in a shared person's turn: the key resolves to the owner. The
# fence now reads the route census at runtime: an `owner` row, or an agent-naming route
# with no row at all, is refused for an agent key in a restricted turn unless its row says
# why it may pass (`restricted_turn`).

import json as _json
import re as _re
from pathlib import Path as _Path

_TABLE = _json.loads((_Path(dep.__file__).resolve().parent / "services" / "agent_route_tiers.json").read_text())


def _concrete(template):
    """A request path for a route template: the agent is agent-a, every other param x."""
    path = _re.sub(r"\{(agent_name|name)\}", AGENT, template)
    return _re.sub(r"\{[^}]+\}", "x", path)


class _RoutedReq:
    """A request as FastAPI hands it to a dependency: the matched route is in the scope."""

    def __init__(self, method, template):
        self.method = method
        self.scope = {"path": _concrete(template), "route": SimpleNamespace(path=template)}


OWNER_ROWS = sorted(k for k, r in _TABLE.items()
                    if r["tier"] == "owner" and r["method"] != "WEBSOCKET" and not r.get("restricted_turn"))
OPEN_ROWS = sorted(k for k, r in _TABLE.items()
                   if (r["tier"] != "owner" or r.get("restricted_turn")) and not r.get("standing_work")
                   and r["method"] != "WEBSOCKET")


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/agents/agent-a/git/sync"),
    ("POST", "/api/agents/agent-a/git/reset-to-main-preserve-state"),
    ("PUT", "/api/agents/agent-a/git/auto-sync"),
    ("POST", "/api/agents/agent-a/credentials/inject"),
])
def test_the_reviewers_owner_tier_routes_are_refused_in_a_shared_persons_turn(method, path, visitor_shell):
    with pytest.raises(dep.HTTPException) as exc:
        dep._enforce_standing_work_fence(_Req(method, path), _key("e-mate"))
    assert exc.value.status_code == 403
    for run in ("e-owner", "e-sched", "manual", None):
        dep._enforce_standing_work_fence(_Req(method, path), _key(run))


@pytest.mark.parametrize("key", OWNER_ROWS)
def test_every_owner_tier_route_is_refused_in_a_shared_persons_turn(key, visitor_shell):
    row = _TABLE[key]
    with pytest.raises(dep.HTTPException) as exc:
        dep._enforce_standing_work_fence(_RoutedReq(row["method"], row["path"]), _key("e-mate"))
    assert exc.value.status_code == 403, key
    dep._enforce_standing_work_fence(_RoutedReq(row["method"], row["path"]), _key("e-owner"))


@pytest.mark.parametrize("key", OPEN_ROWS)
def test_a_route_a_shared_person_may_use_stays_open_to_the_agent_in_their_turn(key, visitor_shell):
    row = _TABLE[key]
    dep._enforce_standing_work_fence(_RoutedReq(row["method"], row["path"]), _key("e-mate"))


def test_an_agent_route_the_census_has_never_seen_is_owner_tier(visitor_shell):
    req = _RoutedReq("POST", "/api/agents/{agent_name}/brand-new-power")
    with pytest.raises(dep.HTTPException):
        dep._enforce_standing_work_fence(req, _key("e-mate"))
    dep._enforce_standing_work_fence(req, _key("e-owner"))


def test_a_route_that_names_no_agent_is_not_the_census_s_question(visitor_shell):
    dep._enforce_standing_work_fence(_RoutedReq("GET", "/api/agents"), _key("e-mate"))


def test_a_visitor_reaches_owner_tier_routes_only_while_the_owner_allows_the_shell(visitor_shell):
    req = _Req("POST", "/api/agents/agent-a/git/sync")
    dep._enforce_standing_work_fence(req, _key("e-visitor"))
    visitor_shell["visitor_shell"] = False
    with pytest.raises(dep.HTTPException):
        dep._enforce_standing_work_fence(req, _key("e-visitor"))


def test_an_owner_check_inside_a_handler_refuses_a_restricted_turn(visitor_shell):
    """`POST /git/pull` is accessor tier; its `force_reset` is owner tier inside the
    handler (`assert_agent_owner`), which the agent's key passed as the owner."""
    with pytest.raises(dep.HTTPException) as exc:
        dep.assert_agent_owner(_key("e-mate"), AGENT)
    assert exc.value.status_code == 403
    for run in ("e-owner", "manual", None):
        dep.assert_agent_owner(_key(run), AGENT)


def test_the_census_ships_with_the_backend():
    """Read at runtime, so it lives in a directory the backend image copies."""
    backend = _Path(dep.__file__).resolve().parent
    dockerfile = (backend.parents[1] / "docker/backend/Dockerfile").read_text()
    assert "COPY ../../src/backend/services /app/services/" in dockerfile
    from services import route_tier_service
    assert route_tier_service.TABLE == _TABLE


def test_the_files_surface_rates_an_agent_key_in_a_shared_turn_as_that_share(visitor_shell):
    """AC3: an agent key is KEY tier on the Files routes, where an `instructions.manage`
    grant lets it rewrite CLAUDE.md. Acting for a shared person it is that person's tier."""
    from services.agent_service import files
    from services import file_path_policy as policy
    assert files._caller_tier(_key("e-mate"), AGENT) == policy.SHARE
    assert not policy.decide(files._caller_tier(_key("e-mate"), AGENT), "write", "CLAUDE.md").allowed
    for run in ("e-owner", "manual", None):
        assert files._caller_tier(_key(run), AGENT) == policy.KEY


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
