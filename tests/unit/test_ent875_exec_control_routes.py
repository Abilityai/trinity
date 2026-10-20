"""Execution reads and control verbs, route by route (trinity-enterprise#875).

Every route here is called directly with real tier resolution
(`dependencies.agent_viewer_tier`) and an owner, an admin, a share holder (by
JWT and by their own user-scope key) and an agent key, in BOTH directions: the
share holder is refused or narrowed, and everyone else still gets what they
had. "Passed the gate" is observed by a sentinel the first call after the gate
raises, so none of this depends on a response model.

A route whose handler calls a db read helper must be listed in `READ_SITES`
(the guard at the bottom). A new read added to a router without a viewer
decision fails there; the route census catches a new route, not a new read
inside an existing one.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import dependencies as dep
from routers import activities, agents, chat, executions, fan_out, git, schedules

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
AGENT = "shared-agent"
ME, OTHER = 7, 8


class Passed(Exception):
    """Raised by the first call after a gate: the caller got past it."""


def _p(username, uid, scope=None, role="user", **extra):
    return SimpleNamespace(
        id=uid, username=username, role=role, mcp_scope=scope,
        email=f"{username}@example.com",
        agent_name=extra.pop("agent_name", None),
        connector_agent=extra.pop("connector_agent", None),
        portal_delegate=False, is_event_loopback=False, vouched_source_agent=None,
        mcp_key_id=None, mcp_key_name=None, **extra,
    )


PRINCIPALS = {
    "owner": lambda: _p("owner", 1),
    "admin": lambda: _p("admin", 2, role="admin"),
    "share": lambda: _p("teammate", ME),
    "share_user_key": lambda: _p("teammate", ME, scope="user"),
    "agent_key": lambda: _p("owner", 1, scope="agent", agent_name="sibling"),
}
FULL_VIEW = ["owner", "admin", "agent_key"]
SHARE = ["share", "share_user_key"]


@pytest.fixture(autouse=True)
def _tiers(monkeypatch):
    monkeypatch.setattr(dep.db, "can_user_share_agent", lambda u, a: u in ("owner", "admin"))
    monkeypatch.setattr(dep, "agent_may_reach", lambda *a, **k: True)


def run(coro):
    return asyncio.run(coro)


def _row(rid, uid, agent=AGENT, **extra):
    return SimpleNamespace(id=rid, agent_name=agent, source_user_id=uid,
                           execution_log=extra.pop("execution_log", None),
                           started_at=None, completed_at=None, status="success", **extra)


# ---- list / detail / log ---------------------------------------------------------------------

@pytest.fixture
def list_db(monkeypatch):
    calls = []

    def summary(name, limit=50, **kw):
        calls.append((name, limit, kw))
        raise Passed
    monkeypatch.setattr(schedules.db, "get_agent_executions_summary", summary)
    return calls


@pytest.mark.parametrize("who", SHARE)
def test_a_share_holders_list_is_narrowed_to_their_id_in_sql(list_db, who):
    with pytest.raises(Passed):
        run(schedules.get_agent_executions(AGENT, 50, PRINCIPALS[who]()))
    assert list_db == [(AGENT, 50, {"source_user_id": ME})]


@pytest.mark.parametrize("who", FULL_VIEW)
def test_everyone_else_calls_the_list_exactly_as_before(list_db, who):
    with pytest.raises(Passed):
        run(schedules.get_agent_executions(AGENT, 50, PRINCIPALS[who]()))
    assert list_db == [(AGENT, 50, {})], "no new keyword for the owner, an admin or an agent key"


def _detail_db(monkeypatch, row):
    monkeypatch.setattr(schedules.db, "get_execution", lambda eid: row)
    # the first call after the gate: observing that the caller got through
    monkeypatch.setattr(schedules.skill_gate_map_service, "self_approved_flags",
                        MagicMock(side_effect=Passed))


@pytest.mark.parametrize("who", SHARE)
def test_a_share_holder_reads_their_own_execution_detail(monkeypatch, who):
    _detail_db(monkeypatch, _row("e1", ME))
    with pytest.raises(Passed):
        run(schedules.get_execution(AGENT, "e1", PRINCIPALS[who]()))


@pytest.mark.parametrize("who", SHARE)
@pytest.mark.parametrize("row", [
    _row("e1", OTHER),                       # another person's turn
    _row("e1", None),                        # a scheduled / channel / legacy row
    _row("e1", ME, agent="another-agent"),   # mine, but another agent's
    None,                                    # no such execution
])
def test_a_share_holder_gets_one_uniform_404_for_a_row_that_is_not_theirs(monkeypatch, who, row):
    _detail_db(monkeypatch, row)
    with pytest.raises(HTTPException) as exc:
        run(schedules.get_execution(AGENT, "e1", PRINCIPALS[who]()))
    assert (exc.value.status_code, exc.value.detail) == (404, "Execution not found"), (
        "a row that is not yours must be indistinguishable from one that does not exist"
    )


@pytest.mark.parametrize("who", FULL_VIEW)
def test_the_owner_an_admin_and_an_agent_key_still_read_anyones_detail(monkeypatch, who):
    _detail_db(monkeypatch, _row("e1", OTHER))
    with pytest.raises(Passed):
        run(schedules.get_execution(AGENT, "e1", PRINCIPALS[who]()))


@pytest.mark.parametrize("who,visible", [("share", False), ("share_user_key", False),
                                         ("owner", True), ("admin", True), ("agent_key", True)])
def test_the_log_follows_the_same_rule_as_the_detail(monkeypatch, who, visible):
    monkeypatch.setattr(schedules.db, "get_execution", lambda eid: _row("e1", OTHER))
    if visible:
        assert run(schedules.get_execution_log(AGENT, "e1", PRINCIPALS[who]()))["has_log"] is False
    else:
        with pytest.raises(HTTPException) as exc:
            run(schedules.get_execution_log(AGENT, "e1", PRINCIPALS[who]()))
        assert exc.value.status_code == 404


@pytest.mark.parametrize("who,kw", [("share", {"source_user_id": ME}), ("owner", {}), ("agent_key", {})])
def test_a_schedules_history_is_narrowed_for_a_share_holder_only(monkeypatch, who, kw):
    seen = []
    monkeypatch.setattr(schedules.db, "get_schedule",
                        lambda sid: SimpleNamespace(agent_name=AGENT, id=sid, owner_id=1))
    monkeypatch.setattr(schedules.db, "get_schedule_executions",
                        lambda sid, limit=50, **k: seen.append(k) or [])
    assert run(schedules.get_schedule_executions(AGENT, "s1", 50, PRINCIPALS[who]())) == []
    assert seen == [kw]


# ---- fan-out ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("who,kw", [("share", {"source_user_id": ME}), ("share_user_key", {"source_user_id": ME}),
                                    ("owner", {}), ("agent_key", {})])
def test_a_fan_out_batch_is_read_back_narrowed_for_a_share_holder(monkeypatch, who, kw):
    seen = []
    monkeypatch.setattr(fan_out.db, "get_fan_out_executions",
                        lambda a, f, *args, **k: seen.append(k) or [])
    with pytest.raises(HTTPException) as exc:           # no rows -> the uniform 404
        run(fan_out.get_fan_out_status("fo_abc", AGENT, PRINCIPALS[who]()))
    assert exc.value.status_code == 404
    assert seen == [kw]


# ---- the live stream --------------------------------------------------------------------------------

def _stream(monkeypatch, row):
    monkeypatch.setattr(chat.db, "get_execution", lambda eid: row)
    monkeypatch.setattr(chat, "get_agent_container", lambda n: None)   # the step after the gate


@pytest.mark.parametrize("who", SHARE)
@pytest.mark.parametrize("row", [_row("e1", OTHER), _row("e1", None), _row("e1", ME, agent="x"), None])
def test_a_share_holder_cannot_open_a_stream_that_is_not_theirs(monkeypatch, who, row):
    _stream(monkeypatch, row)
    with pytest.raises(HTTPException) as exc:
        run(chat.stream_execution_log("e1", AGENT, PRINCIPALS[who]()))
    assert (exc.value.status_code, exc.value.detail) == (404, "Execution not found")


@pytest.mark.parametrize("who", SHARE)
def test_a_share_holder_reaches_the_container_for_their_own_stream(monkeypatch, who):
    _stream(monkeypatch, _row("e1", ME))
    with pytest.raises(HTTPException) as exc:
        run(chat.stream_execution_log("e1", AGENT, PRINCIPALS[who]()))
    assert exc.value.detail == "Agent not found", "they passed the row gate and met the container lookup"


@pytest.mark.parametrize("who", FULL_VIEW)
def test_the_owner_an_admin_and_an_agent_key_stream_without_a_row_check(monkeypatch, who):
    _stream(monkeypatch, _row("e1", OTHER))
    with pytest.raises(HTTPException) as exc:
        run(chat.stream_execution_log("e1", AGENT, PRINCIPALS[who]()))
    assert exc.value.detail == "Agent not found"


# ---- the running list -----------------------------------------------------------------------------------

class _FakeAgentClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: self._payload)


def _running(monkeypatch, rows):
    payload = {"executions": [{"execution_id": k, "pid": 1, "started_at": "t", "metadata": {}}
                              for k in ("mine", "theirs", "scheduled", "ghost")]}
    monkeypatch.setattr(chat, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(chat, "agent_httpx_client", lambda n, timeout=0: _FakeAgentClient(payload))
    monkeypatch.setattr(chat.db, "get_execution", lambda eid: rows.get(eid))


ROWS = {"mine": _row("mine", ME), "theirs": _row("theirs", OTHER), "scheduled": _row("scheduled", None)}


@pytest.mark.parametrize("who", SHARE)
def test_a_share_holder_sees_only_their_running_turns(monkeypatch, who):
    _running(monkeypatch, ROWS)
    out = run(chat.get_agent_running_executions(AGENT, PRINCIPALS[who]()))
    assert [e["execution_id"] for e in out["executions"]] == ["mine"], (
        "another person's, a scheduled and a row-less (ghost) entry are all dropped")


@pytest.mark.parametrize("who", FULL_VIEW)
def test_the_running_list_is_unfiltered_for_everyone_else(monkeypatch, who):
    _running(monkeypatch, ROWS)
    out = run(chat.get_agent_running_executions(AGENT, PRINCIPALS[who]()))
    assert len(out["executions"]) == 4


# ---- terminate ---------------------------------------------------------------------------------------------

@pytest.fixture
def terminate(monkeypatch):
    called = []

    async def fake_terminate(**kw):
        called.append(kw)
        return {"terminated": True}
    monkeypatch.setattr(chat.chat_execution_service, "terminate_execution", fake_terminate)
    return called


def _rows(monkeypatch, mapping):
    monkeypatch.setattr(chat.db, "get_execution", lambda eid: mapping.get(eid))


@pytest.mark.parametrize("who", SHARE)
def test_a_share_holder_ends_their_own_turn(monkeypatch, terminate, who):
    _rows(monkeypatch, {"e1": _row("e1", ME)})
    assert run(chat.terminate_agent_execution("e1", None, AGENT, PRINCIPALS[who]())) == {"terminated": True}
    assert len(terminate) == 1


@pytest.mark.parametrize("mapping,args", [
    ({"e1": _row("e1", OTHER)}, ("e1", None)),                                       # someone else's
    ({}, ("e1", None)),                                                              # no row at all: not a pass
    ({"e1": _row("e1", ME), "e2": _row("e2", OTHER)}, ("e1", "e2")),                 # mine + theirs: BOTH ids must match
    ({"e1": _row("e1", OTHER), "e2": _row("e2", ME)}, ("e1", "e2")),
    ({"e1": _row("e1", ME)}, ("e1", "missing")),                                     # the second id has no row
    ({"e1": _row("e1", ME, agent="another-agent")}, ("e1", None)),                   # mine, but another agent's
    ({"e1": _row("e1", None)}, ("e1", None)),                                        # a scheduled run
])
def test_a_share_holder_cannot_end_a_turn_that_is_not_wholly_theirs(monkeypatch, terminate, mapping, args):
    _rows(monkeypatch, mapping)
    with pytest.raises(HTTPException) as exc:
        run(chat.terminate_agent_execution(args[0], args[1], AGENT, PRINCIPALS["share"]()))
    assert (exc.value.status_code, exc.value.detail) == (404, "Execution not found in agent")
    assert terminate == [], "the service must never be reached"


@pytest.mark.parametrize("who", FULL_VIEW)
def test_the_owner_an_admin_and_an_agent_key_still_end_any_turn(monkeypatch, terminate, who):
    _rows(monkeypatch, {})        # no lookup is made, and none is needed
    assert run(chat.terminate_agent_execution("e1", None, AGENT, PRINCIPALS[who]())) == {"terminated": True}
    assert terminate[0]["task_execution_id"] == "e1"


# ---- the container's shared state --------------------------------------------------------------------------------

SHARED_STATE = [
    ("get_agent_chat_history", {}),
    ("get_agent_activity", {}),
    ("get_agent_activity_detail", {"tool_id": "t1"}),
    ("clear_agent_activity", {}),
]


@pytest.mark.parametrize("who", SHARE)
@pytest.mark.parametrize("fn,kw", SHARED_STATE)
def test_a_share_holder_is_refused_the_containers_shared_state(monkeypatch, fn, kw, who):
    monkeypatch.setattr(chat, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(chat, "is_pull_pilot_agent", lambda n: False)
    with pytest.raises(HTTPException) as exc:
        run(getattr(chat, fn)(name=AGENT, current_user=PRINCIPALS[who](), **kw))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "owner_tier_path"


@pytest.mark.parametrize("who", FULL_VIEW)
@pytest.mark.parametrize("fn,kw", SHARED_STATE)
def test_everyone_else_still_reaches_the_container(monkeypatch, fn, kw, who):
    monkeypatch.setattr(chat, "get_agent_container", lambda n: None)     # the step after the gate
    monkeypatch.setattr(chat, "is_pull_pilot_agent", lambda n: False)
    with pytest.raises(HTTPException) as exc:
        run(getattr(chat, fn)(name=AGENT, current_user=PRINCIPALS[who](), **kw))
    assert exc.value.status_code == 404 and exc.value.detail == "Agent not found"


def test_a_pull_pilots_per_user_history_stays_open_to_a_share_holder(monkeypatch):
    """On a pull pilot /chat memory is already per user (#3127): that path never
    touches the container's shared state, so it is not refused."""
    monkeypatch.setattr(chat, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(chat, "is_pull_pilot_agent", lambda n: True)
    seen = {}
    monkeypatch.setattr(chat.db, "get_agent_chat_sessions",
                        lambda name, user_id=None, status=None: seen.update(uid=user_id) or [])
    assert run(chat.get_agent_chat_history(name=AGENT, current_user=PRINCIPALS["share"]())) == []
    assert seen == {"uid": ME}


@pytest.mark.parametrize("who", SHARE)
def test_the_container_log_is_refused_to_a_share_holder(monkeypatch, who):
    monkeypatch.setattr(agents, "get_agent_container", lambda n: SimpleNamespace(logs=lambda tail: b"x"))
    with pytest.raises(HTTPException) as exc:
        run(agents.get_agent_logs_endpoint(AGENT, MagicMock(), PRINCIPALS[who](), 10))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("who", FULL_VIEW)
def test_the_container_log_is_unchanged_for_everyone_else(monkeypatch, who):
    monkeypatch.setattr(agents, "get_agent_container", lambda n: SimpleNamespace(logs=lambda tail: b"line"))
    assert run(agents.get_agent_logs_endpoint(AGENT, MagicMock(), PRINCIPALS[who](), 10)) == {"logs": "line"}


# ---- model + force reset ----------------------------------------------------------------------------------------------

def test_changing_the_model_is_owner_tier():
    """FastAPI runs the route's `Depends`; this pins which dependency that is.
    (The owner dependency's own behaviour is `test_186_enumeration_uniformity`.)"""
    dependency = inspect.signature(chat.set_agent_model).parameters["name"].default.dependency
    assert dependency is chat.get_owned_agent
    assert inspect.signature(chat.get_agent_model).parameters["name"].default.dependency is chat.get_authorized_agent, (
        "reading the model stays open to anyone with access"
    )


@pytest.fixture
def pull(monkeypatch):
    seen = []

    async def fake_pull(agent, strategy="clean"):
        seen.append(strategy)
        return {"success": True}
    monkeypatch.setattr(git.git_service, "pull_from_github", fake_pull)
    import services.docker_service as ds
    monkeypatch.setattr(ds, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    return seen


def _pull(who, strategy):
    return run(git.pull_from_github(AGENT, MagicMock(), git.GitPullRequest(strategy=strategy), PRINCIPALS[who]()))


@pytest.mark.parametrize("who", SHARE)
def test_a_share_holder_cannot_force_reset_the_workspace(pull, who):
    with pytest.raises(HTTPException) as exc:
        _pull(who, "force_reset")
    assert exc.value.status_code == 403
    assert pull == [], "the pull must never start"


@pytest.mark.parametrize("who", SHARE)
@pytest.mark.parametrize("strategy", ["clean", "stash_reapply"])
def test_a_share_holder_still_runs_a_clean_or_stash_pull(pull, who, strategy):
    _pull(who, strategy)
    assert pull == [strategy]


@pytest.mark.parametrize("who", ["owner", "admin", "agent_key"])
def test_the_owner_an_admin_and_an_agent_key_still_force_reset(pull, who):
    _pull(who, "force_reset")
    assert pull == ["force_reset"]


# ---- the queue ----------------------------------------------------------------------------------------------------------------

def _queue_status():
    return {
        "agent_name": AGENT, "is_busy": True, "queue_length": 3,
        "current_execution": {"id": "running-1", "message": "their running prompt"},
        "queued_executions": [
            {"id": "q1", "source_user_id": str(ME), "message": "mine"},
            {"id": "q2", "source_user_id": str(OTHER), "message": "theirs"},
            {"id": "q3", "source_user_id": None, "message": "no user"},
        ],
    }


def test_a_share_holder_sees_only_their_queued_prompts_and_not_a_running_one_thats_not_theirs(monkeypatch):
    from services.agent_service import queue
    monkeypatch.setattr(queue.db, "get_execution", lambda eid: _row(eid, OTHER))
    out = queue._narrow_queue_status(_queue_status(), AGENT, PRINCIPALS["share"]())
    assert [q["id"] for q in out["queued_executions"]] == ["q1"], "the id arrives as a string"
    assert out["current_execution"] is None
    assert out["is_busy"] is True and out["queue_length"] == 3, "counts are not text"


def test_a_share_holder_keeps_a_running_turn_that_is_theirs(monkeypatch):
    from services.agent_service import queue
    monkeypatch.setattr(queue.db, "get_execution", lambda eid: _row(eid, ME))
    out = queue._narrow_queue_status(_queue_status(), AGENT, PRINCIPALS["share"]())
    assert out["current_execution"]["id"] == "running-1"


@pytest.mark.parametrize("who", FULL_VIEW)
def test_the_queue_is_untouched_for_everyone_else(who):
    from services.agent_service import queue
    assert queue._narrow_queue_status(_queue_status(), AGENT, PRINCIPALS[who]()) == _queue_status()


# ---- the fleet list and the activity timeline -----------------------------------------------------------------------------------------

def _own_rows(monkeypatch, owned=("a-owned",), shared=("a-shared",)):
    from services.agent_service import helpers
    fake = lambda u: (
        [{"name": n, "is_owner": True, "is_shared": False} for n in owned]
        + [{"name": n, "is_owner": False, "is_shared": True} for n in shared])
    # The fleet router binds `viewer_own_rows` at import. When another test in the
    # run reloads the helpers module, `helpers` here and the function the route
    # holds are different module objects, so patch the globals of every copy the
    # code under test can reach (CI orders tests differently from a local run).
    # Each dict is patched once, with setitem only: monkeypatch undoes every
    # setattr before any setitem, so mixing the two on one dict restored the fake
    # after the test and leaked it into later files (ent#872's accessor tests).
    scopes = {id(helpers.__dict__): helpers.__dict__}
    for fn in (helpers.viewer_own_rows, getattr(executions, "viewer_own_rows", None)):
        if fn is not None:
            scopes.setdefault(id(fn.__globals__), fn.__globals__)
    for scope in scopes.values():
        monkeypatch.setitem(scope, "get_accessible_agents", fake)
    return helpers


def test_the_fleet_helper_exempts_only_the_agents_the_caller_owns(monkeypatch):
    helpers = _own_rows(monkeypatch)
    assert helpers.viewer_own_rows(PRINCIPALS["share"]()) == (ME, ["a-owned"])
    assert helpers.viewer_own_rows(PRINCIPALS["share_user_key"]()) == (ME, ["a-owned"])


@pytest.mark.parametrize("who", ["admin", "agent_key"])
def test_the_fleet_helper_does_not_narrow_an_admin_or_a_key(monkeypatch, who):
    assert _own_rows(monkeypatch).viewer_own_rows(PRINCIPALS[who]()) == (None, None)


def test_a_person_who_owns_everything_they_reach_is_still_narrowed_with_every_agent_exempt(monkeypatch):
    """Narrowing is the default; an owner is just exempt on their own agents."""
    assert _own_rows(monkeypatch, shared=()).viewer_own_rows(PRINCIPALS["owner"]()) == (1, ["a-owned"])


def test_a_gap_in_the_owned_list_narrows_the_agent_instead_of_showing_it_in_full(monkeypatch):
    """Review finding, fail-open direction. If the roster the exemptions are
    built from misses an agent the caller can reach (Docker did not list it, a
    metadata gap), that agent must be NARROWED. An empty exemption list means
    "narrow everywhere", never "narrow nowhere"."""
    helpers = _own_rows(monkeypatch, owned=(), shared=())
    assert helpers.viewer_own_rows(PRINCIPALS["share"]()) == (ME, [])
    from services.agent_service import helpers as h
    monkeypatch.setattr(h, "get_accessible_agents", lambda u: [])
    assert h.viewer_own_rows(PRINCIPALS["share"]()) == (ME, [])


def test_only_an_agent_flagged_as_owned_is_exempt_never_one_that_is_merely_not_shared(monkeypatch):
    """An accessible agent that is neither flagged owned nor shared (a metadata
    gap) must be narrowed. Exempting "everything not flagged shared" would hand
    the caller every row of it."""
    from services.agent_service import helpers
    monkeypatch.setattr(helpers, "get_accessible_agents", lambda u: [
        {"name": "a-owned", "is_owner": True, "is_shared": False},
        {"name": "a-shared", "is_owner": False, "is_shared": True},
        {"name": "a-gap", "is_owner": False, "is_shared": False},
    ])
    assert helpers.viewer_own_rows(PRINCIPALS["share"]()) == (ME, ["a-owned"])


@pytest.mark.parametrize("scope", ["portal_delegate", "robot"])
def test_an_unlisted_principal_is_narrowed_on_every_agent(monkeypatch, scope):
    who = _p("owner", 1, scope=scope, role="admin")
    assert _own_rows(monkeypatch).viewer_own_rows(who) == (1, [])


def test_a_principal_with_no_usable_id_is_narrowed_to_nothing(monkeypatch):
    who = _p("owner", None, scope="robot")
    assert _own_rows(monkeypatch).viewer_own_rows(who) == (dep.NO_ROWS_USER_ID, [])


def test_the_fleet_route_passes_the_narrowing_to_the_query(monkeypatch):
    _own_rows(monkeypatch)
    monkeypatch.setattr(executions, "accessible_agent_names", lambda u: ["a-shared", "a-owned"])
    seen = []
    monkeypatch.setattr(executions.db, "get_fleet_executions", lambda names, **kw: seen.append(kw) or [])
    run(executions.list_fleet_executions(None, None, 24, "secret", None, 50, 0, PRINCIPALS["share"]()))
    assert (seen[0]["own_rows_user_id"], seen[0]["own_rows_exempt_agents"]) == (ME, ["a-owned"])
    seen.clear()
    run(executions.list_fleet_executions(None, None, 24, None, None, 50, 0, PRINCIPALS["admin"]()))
    assert "own_rows_user_id" not in seen[0], "an admin's call is the old call"


def test_the_activity_timeline_passes_the_narrowing_to_the_query(monkeypatch):
    _own_rows(monkeypatch)
    from services.agent_service import helpers
    monkeypatch.setattr(helpers, "accessible_agent_names", lambda u: ["a-shared", "a-owned"])
    seen = []
    monkeypatch.setattr(activities.db, "get_activities_in_range", lambda **kw: seen.append(kw) or [])
    run(activities.get_activity_timeline(None, None, None, 100, PRINCIPALS["share"]()))
    assert (seen[0]["own_rows_user_id"], seen[0]["own_rows_exempt_agents"]) == (ME, ["a-owned"])


# ---- the capacity panel's slots (a prompt preview per running turn) -------------------------------------------------------------------

def _capacity(monkeypatch, who):
    from routers import agent_config
    slots = [SimpleNamespace(slot_number=i, execution_id=eid, started_at="t", duration_seconds=1.0,
                             message_preview=f"preview of {eid}")
             for i, eid in enumerate(("mine", "theirs", "scheduled", "ghost"), 1)]
    state = SimpleNamespace(slots=slots, active_slots=4, available_slots=0)
    monkeypatch.setattr(agent_config, "assert_agent_access", lambda u, a: None)
    monkeypatch.setattr(agent_config, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(agent_config.db, "get_max_parallel_tasks", lambda n: 4)
    monkeypatch.setattr(agent_config.db, "get_execution", lambda eid: ROWS.get(eid))
    import services.capacity_manager as cm
    monkeypatch.setattr(cm, "get_capacity_manager",
                        lambda: SimpleNamespace(get_slot_state=AsyncMock(return_value=state)))
    import services.settings_service as ss
    monkeypatch.setattr(ss, "get_max_parallel_tasks_ceiling", lambda: 8)
    monkeypatch.setattr(ss, "clamp_to_ceiling", lambda v: v)
    return run(agent_config.get_agent_capacity(AGENT, PRINCIPALS[who]()))


@pytest.mark.parametrize("who", SHARE)
def test_a_share_holder_sees_only_the_slots_their_own_turns_hold(monkeypatch, who):
    out = _capacity(monkeypatch, who)
    assert [s.execution_id for s in out.slots] == ["mine"]
    assert "preview of theirs" not in str(out.model_dump())
    assert out.active_slots == 4 and out.available_slots == 0, "counts are numbers, not text"


@pytest.mark.parametrize("who", FULL_VIEW)
def test_the_capacity_panel_is_unchanged_for_everyone_else(monkeypatch, who):
    assert len(_capacity(monkeypatch, who).slots) == 4


# ---- webhooks ----------------------------------------------------------------------------------------------------------------------------

class _GateDb:
    """A db whose first call after the schedule lookup raises `Passed`."""

    def __init__(self, owner_id):
        self._schedule = SimpleNamespace(agent_name=AGENT, id="s1", owner_id=owner_id, webhook_token="tok")

    def get_schedule(self, sid):
        return self._schedule

    def is_agent_live(self, name):
        return True

    def __getattr__(self, name):
        def _after_gate(*a, **k):
            raise Passed
        return _after_gate


WEBHOOK_ROUTES = [
    ("generate_webhook", lambda p: (AGENT, "s1", SimpleNamespace(base_url="http://x/"), p)),
    ("get_webhook_status", lambda p: (AGENT, "s1", SimpleNamespace(base_url="http://x/"), p)),
    ("generate_webhook_secret", lambda p: (AGENT, "s1", SimpleNamespace(base_url="http://x/"), p)),
    ("disable_webhook_secret", lambda p: (AGENT, "s1", SimpleNamespace(base_url="http://x/"), p)),
    ("revoke_webhook", lambda p: (AGENT, "s1", p)),
]


def _webhook(monkeypatch, route, args_of, who, creator_id):
    monkeypatch.setattr(schedules, "db", _GateDb(creator_id))
    return run(getattr(schedules, route)(*args_of(PRINCIPALS[who]())))


@pytest.mark.parametrize("route,args_of", WEBHOOK_ROUTES)
@pytest.mark.parametrize("who", SHARE)
def test_a_share_holder_who_did_not_create_the_schedule_cannot_touch_its_webhook(monkeypatch, route, args_of, who):
    with pytest.raises(HTTPException) as exc:
        _webhook(monkeypatch, route, args_of, who, creator_id=1)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "webhook_owner_or_creator"


@pytest.mark.parametrize("route,args_of", WEBHOOK_ROUTES)
@pytest.mark.parametrize("who", SHARE)
def test_the_schedules_creator_may_even_as_a_share_holder(monkeypatch, route, args_of, who):
    with pytest.raises(Passed):
        _webhook(monkeypatch, route, args_of, who, creator_id=ME)


@pytest.mark.parametrize("route,args_of", WEBHOOK_ROUTES)
@pytest.mark.parametrize("who", ["owner", "admin", "agent_key"])
def test_the_owner_an_admin_and_an_agent_key_keep_every_webhook_route(monkeypatch, route, args_of, who):
    with pytest.raises(Passed):
        _webhook(monkeypatch, route, args_of, who, creator_id=OTHER)


@pytest.mark.parametrize("creator", [None, 0])
def test_a_schedule_with_no_recorded_creator_is_the_owners_alone(monkeypatch, creator):
    with pytest.raises(HTTPException) as exc:
        _webhook(monkeypatch, "get_webhook_status", WEBHOOK_ROUTES[1][1], "share", creator_id=creator)
    assert exc.value.status_code == 403


def test_the_webhook_status_read_is_gated_because_it_returns_the_live_token_url(monkeypatch):
    """The read returns `webhook_url` with the bearer token in it."""
    with pytest.raises(HTTPException) as exc:
        _webhook(monkeypatch, "get_webhook_status", WEBHOOK_ROUTES[1][1], "share", creator_id=1)
    assert exc.value.status_code == 403


def test_the_schedule_responses_say_whether_this_viewer_may_manage_the_webhook():
    sched = SimpleNamespace(owner_id=ME, model_dump=lambda: _schedule_payload())
    mine = schedules._schedule_response(sched, AGENT, PRINCIPALS["share"]())
    sched_other = SimpleNamespace(owner_id=1, model_dump=lambda: _schedule_payload())
    theirs = schedules._schedule_response(sched_other, AGENT, PRINCIPALS["share"]())
    owners = schedules._schedule_response(sched_other, AGENT, PRINCIPALS["owner"]())
    assert (mine.can_manage_webhook, theirs.can_manage_webhook, owners.can_manage_webhook) == (True, False, True)
    assert not hasattr(mine, "owner_id"), "the creator's id is not exposed"


def _schedule_payload():
    from datetime import datetime
    now = datetime(2026, 10, 16)
    return dict(id="s1", agent_name=AGENT, name="n", cron_expression="* * * * *", message="m",
                enabled=True, timezone="UTC", description=None, created_at=now, updated_at=now,
                last_run_at=None, next_run_at=None)


# ---- start and stop stay open, by ruling ---------------------------------------------------------------------------------------------------

def test_start_and_stop_stay_open_to_anyone_with_access_by_ruling():
    for fn in (agents.start_agent_endpoint, agents.stop_agent_endpoint):
        annotation = str(inspect.signature(fn).parameters["agent_name"].annotation)
        assert "AuthorizedAgentByName" in annotation or "get_authorized_agent_by_name" in annotation, fn.__name__


# ---- the guard: every router read of an execution row decides on a viewer ----------------------------------------------------------

READ_HELPERS = {"get_agent_executions_summary", "get_fleet_executions", "get_schedule_executions",
                "get_execution", "get_fan_out_executions", "get_activities_in_range"}

# (file, function) -> why the read is allowed to be unfiltered, or "viewer" when it
# decides on `viewer_row_scope` / `viewer_own_rows` / `row_is_viewers` itself.
READ_SITES = {
    ("routers/schedules.py", "get_agent_executions"): "viewer",
    ("routers/schedules.py", "get_schedule_executions"): "viewer",
    ("routers/schedules.py", "get_execution"): "viewer",
    ("routers/schedules.py", "get_execution_log"): "viewer",
    ("routers/executions.py", "list_fleet_executions"): "viewer",
    ("routers/activities.py", "get_activity_timeline"): "viewer",
    ("routers/fan_out.py", "get_fan_out_status"): "viewer",
    ("routers/chat.py", "terminate_agent_execution"): "viewer",
    ("routers/chat.py", "get_agent_running_executions"): "viewer",
    ("routers/chat.py", "stream_execution_log"): "viewer",
    ("routers/agent_config.py", "get_agent_capacity"): "viewer",
    ("routers/agent_config.py", "_visible"): "the closure get_agent_capacity filters its slots with, under the scope it asked for",
    # Not a person reading another person's turn:
    ("routers/agents.py", "agent_execution_result"): "the agent's own key reports its own turn's result (heartbeat pattern)",
    ("routers/agents.py", "send_voice_reply_endpoint"): "agent-driven; returns delivery flags, never the turn's text",
    ("routers/a2a.py", "_send_paid_task"): "A2A tasks are bound to the caller's own task id",
    ("routers/a2a.py", "_bound_task_rpc"): "A2A tasks are bound to the caller's own task id",
    ("routers/a2a.py", "_dispatch_jsonrpc"): "A2A tasks are bound to the caller's own task id",
    ("routers/internal.py", "internal_task_result"): "internal shared-secret router (agent -> backend)",
    ("routers/internal.py", "_fail_execution_row"): "internal shared-secret router (agent -> backend)",
    ("routers/internal.py", "_execute_task_internal_background"): "internal shared-secret router (agent -> backend)",
    ("routers/public.py", "public_stream_execution"): "public link token binds the session",
    ("routers/public.py", "public_execution_status"): "public link token binds the session",
    ("routers/public.py", "public_terminate_execution"): "public link token binds the session",
    ("routers/public_memory.py", "write_user_memory"): "reads the row to find the seat it writes memory for",
    ("routers/seat_decisions.py", "_seat_for"): "reads the row to find the seat a decision belongs to",
}


def _read_sites():
    found = set()
    for path in sorted((_BACKEND / "routers").rglob("*.py")):
        tree = ast.parse(path.read_text())
        rel = path.relative_to(_BACKEND).as_posix()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr in READ_HELPERS):
                        found.add((rel, fn.name))
    return found


def test_every_router_read_of_an_execution_row_has_a_viewer_decision():
    found = _read_sites()
    assert found, "the scanner found no read sites: it is blind"
    unlisted = found - set(READ_SITES)
    assert not unlisted, (
        "a router reads schedule_executions / agent_activities with no viewer decision "
        f"(use viewer_row_scope + row_is_viewers, or list it with a reason): {sorted(unlisted)}"
    )
    stale = set(READ_SITES) - found
    assert not stale, f"READ_SITES lists a function that no longer reads: {sorted(stale)}"


@pytest.mark.parametrize("site", sorted(k for k, v in READ_SITES.items() if v == "viewer"))
def test_a_site_marked_viewer_really_asks_the_viewer_helpers(site):
    rel, fname = site
    tree = ast.parse((_BACKEND / rel).read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fname)
    called = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert called & {"viewer_row_scope", "viewer_own_rows"}, f"{rel}:{fname} does not ask the viewer helpers"


# ---- the route census -------------------------------------------------------------------------------------------------------------------------

# (file, function) -> the tier a route resolves to, with the reason. A route in these
# files that is not in the table fails the census: the default is not accessor.
ROUTE_TIERS = {
    # executions: accessor gate, then the viewer tier narrows the rows
    ("routers/schedules.py", "get_agent_executions"): "accessor+own-rows",
    ("routers/schedules.py", "get_execution"): "accessor+own-rows",
    ("routers/schedules.py", "get_execution_log"): "accessor+own-rows",
    ("routers/schedules.py", "get_schedule_executions"): "accessor+own-rows",
    ("routers/executions.py", "list_fleet_executions"): "accessor+own-rows",
    ("routers/chat.py", "stream_execution_log"): "accessor+own-rows",
    ("routers/chat.py", "get_agent_running_executions"): "accessor+own-rows",
    ("routers/chat.py", "terminate_agent_execution"): "accessor+own-rows",
    ("routers/fan_out.py", "get_fan_out_status"): "accessor+own-rows",
    ("routers/activities.py", "get_activity_timeline"): "accessor+own-rows",
    ("routers/agents.py", "get_agent_queue_status"): "accessor+own-rows",
    ("routers/agent_config.py", "get_agent_capacity"): "accessor+own-slots (a slot carries the prompt preview)",
    # the container's shared state: owner tier for a share holder
    ("routers/chat.py", "get_agent_chat_history"): "shared-state",
    ("routers/chat.py", "get_agent_activity"): "shared-state",
    ("routers/chat.py", "get_agent_activity_detail"): "shared-state",
    ("routers/chat.py", "clear_agent_activity"): "shared-state",
    ("routers/agents.py", "get_agent_logs_endpoint"): "shared-state",
    # control verbs
    ("routers/chat.py", "set_agent_model"): "owner",
    ("routers/git.py", "pull_from_github"): "owner-for-force_reset",
    ("routers/schedules.py", "generate_webhook"): "creator-or-owner",
    ("routers/schedules.py", "get_webhook_status"): "creator-or-owner",
    ("routers/schedules.py", "generate_webhook_secret"): "creator-or-owner",
    ("routers/schedules.py", "disable_webhook_secret"): "creator-or-owner",
    ("routers/schedules.py", "revoke_webhook"): "creator-or-owner",
    # ruled open, on purpose
    ("routers/agents.py", "start_agent_endpoint"): "accessor (ruling: chat does not auto-start a stopped agent)",
    ("routers/agents.py", "stop_agent_endpoint"): "accessor (ruling: stays open to share holders)",
    ("routers/chat.py", "get_agent_model"): "accessor (the model is not a secret)",
    ("routers/agents.py", "reset_circuit_breaker_endpoint"): "admin (require_admin — already ruled)",
    ("routers/agents.py", "clear_agent_queue"): "owner (OwnedAgentByName — already ruled)",
    ("routers/agents.py", "force_release_agent"): "owner (OwnedAgentByName — already ruled)",
    # reviewed for ent#875 and left as they were, each for its reason
    ("routers/chat.py", "reset_agent_chat_history"): "owner (get_owned_agent — already ruled)",
    ("routers/chat.py", "get_persistent_chat_history"): "accessor, already own-rows (user_id filter before the limit, #629)",
    ("routers/agents.py", "get_agents_execution_stats"): "accessor: counts and cost per agent, no text",
    ("routers/executions.py", "get_fleet_execution_timeline"): "accessor: bucket counts, no text",
    ("routers/executions.py", "get_fleet_execution_stats"): "accessor: counts and cost, no text",
    ("routers/agent_config.py", "set_agent_capacity"): "owner (assert_agent_owner, already ruled)",
    ("routers/agent_config.py", "get_public_channel_model"): "accessor: which model public channels use, a setting, no text",
    ("routers/agent_config.py", "set_public_channel_model"): "owner or agents.manage capability (already ruled)",
    ("routers/agents.py", "get_circuit_breaker_endpoint"): "accessor: breaker state, no text",
    ("routers/agents.py", "set_circuit_breaker_endpoint"): "owner (OwnedAgentByName — already ruled)",
    ("routers/agents.py", "agent_execution_result"): "the agent's own key reports its own turn (heartbeat pattern)",
    ("routers/git.py", "get_pull_sync_config"): "accessor: a config flag, no text",
    ("routers/git.py", "set_pull_sync_config"): "owner + person (already ruled)",
    ("routers/fan_out.py", "fan_out"): "dispatch with the caller's own key; the rows it makes carry their id",
    ("routers/chat.py", "arm_execution_report_back"): "accessor + dispatcher-only (#3295): the service 404s unless the caller dispatched the row, and it reads nothing back",
}

_CENSUS_FILES = ("routers/schedules.py", "routers/chat.py", "routers/agents.py", "routers/git.py",
                 "routers/executions.py", "routers/fan_out.py", "routers/activities.py", "routers/agent_config.py")
# Files that are wholly about executions or activity: every route in them counts
# (the fleet list's own path is "", which no word would match).
_ALL_ROUTES_FILES = ("routers/executions.py", "routers/activities.py")
_EXECUTION_CONTROL_WORDS = ("execution", "webhook", "activity", "model", "queue", "logs", "capacity", "chat/history",
                            "git/pull", "start", "stop", "circuit-breaker", "release", "fan-out", "timeline")


def _routes_matching():
    out = {}
    for rel in _CENSUS_FILES:
        tree = ast.parse((_BACKEND / rel).read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in fn.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in ("get", "post", "put", "delete", "patch")
                        and dec.args and isinstance(dec.args[0], ast.Constant)
                        and isinstance(dec.args[0].value, str)):
                    path = dec.args[0].value
                    if rel in _ALL_ROUTES_FILES or any(w in path for w in _EXECUTION_CONTROL_WORDS):
                        out[(rel, fn.name)] = f"{dec.func.attr.upper()} {path}"
    return out


def test_every_execution_or_control_route_has_a_ruled_tier():
    routes = _routes_matching()
    assert routes, "the census found no routes: it is blind"
    unruled = {k: v for k, v in routes.items() if k not in ROUTE_TIERS}
    # Routes in these files that are about something else (schedules CRUD, agent
    # create/delete, ...) carry no execution or control word and are not listed.
    assert not unruled, (
        "a route that reads executions or steers the agent has no ruled tier; add it to ROUTE_TIERS "
        f"with its reason (the default is not accessor): {unruled}"
    )
    missing = set(ROUTE_TIERS) - set(routes)
    assert not missing, f"ROUTE_TIERS lists a route that no longer exists: {sorted(missing)}"
