"""
Peer skill grants, enforced in-container (abilityai/trinity-enterprise#925).

ent#662 refuses a peer request that NAMES a library-assigned skill
(`/<name>` in the text). A peer that asks in prose ("please pay the invoice")
passed that check, and the callee's model could then load the library skill
under its own identity. The ent#752 hook already asks
`POST /api/skill-gate/check` before every skill load, with the run's execution
id, so the platform now answers there too: on a run whose requester is a peer,
a library skill that peer holds no grant for is refused at load, named or not.

Target: the REAL route (`routers/skill_gate.py` ->
`skill_gate_service.check_invocation` -> `peer_skill_policy`) over the real
per-test database (`db_backend`), and `skill_gate_service.enforce` for the
gateway half of the "key's agent first" rule. Stubbed: the gate map, the audit
sink, the rate limiter and the marker sync.

What each block pins:
1. the refusal on a peer run, exact model-facing text (one, several, subagent);
2. a grant, a declared skill and a #2914 conflict name pass; grants match the
   library name exactly while the hook's names match case-insensitively;
3. the requester is the agent of an agent-scoped key FIRST (a fan-out sent with
   no X-Source-Agent is stamped with the callee itself; A2A records no source
   agent), then `source_agent_name` — at the hook AND at the gateway;
4. AC3: the agent itself, its schedules, people, a caller wake, a room turn, no
   run, `trinity-system` and a parent on an agent it spawned are unaffected;
5. which run: the row must be this agent's, any status; an unknown id and
   another agent's run answer alike, at the same read cost;
6. fail open on an unreadable read; a programming error is not swallowed;
7. an unresolvable call on a peer run is refused; a named one gets its name;
8. the audit row, its throttle, and its own rate-limit keys;
9. the drift report lists in-container refusals.
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_harness import db_backend  # noqa: E402,F401

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

import services.peer_skill_policy as _POLICY  # noqa: E402
import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from db.write_params import ExecutionResult, TaskExecutionFields  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402
from error_handlers import skill_gate_error  # noqa: E402
from models import TaskExecutionStatus, User  # noqa: E402
from routers import skill_gate as _SKILL_GATE_ROUTER  # noqa: E402
from services.rate_limiter import RateLimitResult  # noqa: E402
from services.skill_gate_errors import SkillGateError, SkillGateRefused  # noqa: E402

pytestmark = pytest.mark.unit

OWNER, OWNER_EMAIL = "owner-925", "owner-925@example.com"
ADMIN = "admin-925"
FIN, BILL, OPS = "fin-925", "bill-925", "ops-925"
URL = "/api/skill-gate/check"
ALLOWED = {"allowed": True, "gated": False, "message": None}

ONE = (
    f"Not run: the skill pay-invoice was assigned to {FIN} from the skills library, and this run "
    f"is a request from the agent {BILL}, which holds no grant for it. Do not carry out its steps "
    f"another way. Tell {BILL} that pay-invoice is not available to it on {FIN}."
)
SUBAGENT_TWO = (
    f"Not run: the subagent closer loads the skills pay-invoice and refund-invoice, which were "
    f"assigned to {FIN} from the skills library, and this run is a request from the agent {BILL}, "
    f"which holds no grant for them. Do not carry out their steps another way. Tell {BILL} that "
    f"pay-invoice and refund-invoice are not available to it on {FIN}."
)
SUBAGENT_ONE = (
    f"Not run: the subagent closer loads the skill pay-invoice, which was assigned to {FIN} from "
    f"the skills library, and this run is a request from the agent {BILL}, which holds no grant "
    f"for it. Do not carry out its steps another way. Tell {BILL} that pay-invoice is not "
    f"available to it on {FIN}."
)
COULD_NOT_TELL = (
    f"Not run: this run is a request from the agent {BILL}, which may run {FIN}'s library skills "
    f"only with a grant, and Trinity could not tell which skill this call loads. Do not carry out "
    f"its steps another way."
)


def _app():
    """One app over the real router; `get_current_user` overridden by walking
    the route's own dependant tree. The router is imported at module level, so
    the override key is the function object the route holds (learning
    2026-11-04)."""
    app = FastAPI()
    app.include_router(_SKILL_GATE_ROUTER.router)
    app.add_exception_handler(SkillGateError, skill_gate_error)
    found = set()

    def walk(dependant):
        for sub in dependant.dependencies:
            if getattr(sub.call, "__name__", "") == "get_current_user":
                found.add(sub.call)
            walk(sub)

    for route in _SKILL_GATE_ROUTER.router.routes:
        if getattr(route, "dependant", None) is not None:
            walk(route.dependant)
    assert found, "no get_current_user dependency on the skill-gate route"
    for call in found:
        app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
    return app


_PRINCIPAL = {"user": None}


@pytest.fixture
def world(db_backend, monkeypatch):
    db.create_user(UserCreate(username=OWNER, role="user", email=OWNER_EMAIL))
    db.create_user(UserCreate(username=ADMIN, role="admin", email="admin-925@example.com"))
    for agent in (FIN, BILL, OPS):
        db.register_agent_owner(agent, OWNER)
    db.register_agent_owner("trinity-system", ADMIN)
    owner = db.get_user_by_username(OWNER)
    keys = {agent: db.create_agent_mcp_api_key(agent, OWNER).id for agent in (FIN, BILL, OPS)}
    user_key = db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner-925-key")).id

    state = {"gates": {}}
    monkeypatch.setattr(_GATE, "list_skill_gates", lambda agent: state["gates"].get(agent, {}))

    audits = []

    class _Audit:
        async def log(self, **kw):
            audits.append(kw)
            return "evt"

    import services.platform_audit_service as _PAS
    monkeypatch.setattr(_PAS, "platform_audit_service", _Audit())

    limits = {"allow": True, "calls": []}

    def _check(key, limit, window):
        limits["calls"].append((key, limit, window))
        return RateLimitResult(limits["allow"], 0, 0 if limits["allow"] else window, limit)

    monkeypatch.setattr(_GATE.rate_limiter, "check", _check)
    monkeypatch.setattr(_GATE, "spawn_gate_marker_sync", lambda agent, **kw: True)

    def as_agent(agent=FIN):
        _PRINCIPAL["user"] = User(id=owner["id"], username=OWNER, email=OWNER_EMAIL, role="user",
                                  mcp_scope="agent", agent_name=agent, mcp_key_id=keys[agent])

    as_agent()
    db.assign_skill(FIN, "pay-invoice", "owner")
    return SimpleNamespace(client=TestClient(_app(), raise_server_exceptions=True), state=state,
                           audits=audits, limits=limits, keys=keys, user_key=user_key,
                           owner_id=owner["id"], as_agent=as_agent)


def _post(world, **body):
    payload = {"via": "skill_tool", "invoked": "pay-invoice", "names": ["pay-invoice"],
               "resolved": True, "execution_id": None, "marker": False}
    payload.update(body)
    res = world.client.post(URL, json=payload)
    assert res.status_code == 200, res.text
    return res.json()


def _row(agent=FIN, triggered_by="agent", **fields):
    return db.create_task_execution(agent_name=agent, message="please pay the invoice",
                                    triggered_by=triggered_by,
                                    fields=TaskExecutionFields(**fields)).id


def _peer_run(world, caller=BILL, agent=FIN):
    """What MCP `chat_with_agent` writes: the resolved X-Source-Agent and the
    caller's own agent key."""
    return _row(agent=agent, source_agent_name=caller, source_mcp_key_id=world.keys[caller])


def _finish(run_id):
    assert db.update_execution_status(execution_id=run_id, status=TaskExecutionStatus.SUCCESS,
                                      result=ExecutionResult(response="done"))


# ---------------------------------------------------------------------------
# 1. The refusal on a peer run
# ---------------------------------------------------------------------------

def test_a_peer_run_cannot_load_an_ungranted_library_skill_it_never_named(world):
    """The prose case: nothing in the request named the skill; the load does."""
    assert _post(world, execution_id=_peer_run(world)) == {
        "allowed": False, "gated": False, "message": ONE}


def test_a_subagent_preloading_two_library_skills_is_refused_by_both_names(world):
    db.assign_skill(FIN, "refund-invoice", "owner")
    res = _post(world, via="subagent_preload", invoked=None, subagent="closer",
                names=["refund-invoice", "pay-invoice"], execution_id=_peer_run(world))
    assert res == {"allowed": False, "gated": False, "message": SUBAGENT_TWO}


def test_a_subagent_preloading_one_library_skill_uses_the_singular(world):
    res = _post(world, via="subagent_preload", invoked=None, subagent="closer",
                names=["pay-invoice"], execution_id=_peer_run(world))
    assert res["message"] == SUBAGENT_ONE


def test_the_refusal_never_puts_a_slash_before_a_name(world):
    db.assign_skill(FIN, "refund-invoice", "owner")
    for body in ({}, {"via": "subagent_preload", "invoked": None, "subagent": "closer",
                      "names": ["refund-invoice", "pay-invoice"]}):
        msg = _post(world, execution_id=_peer_run(world), **body)["message"]
        assert "/pay-invoice" not in msg and "/refund-invoice" not in msg


# ---------------------------------------------------------------------------
# 2. What passes on a peer run
# ---------------------------------------------------------------------------

def test_a_grant_opens_the_skill_for_that_caller_only(world):
    db.grant_peer_skill(BILL, FIN, "pay-invoice", ADMIN)
    assert _post(world, execution_id=_peer_run(world)) == ALLOWED
    assert _post(world, execution_id=_peer_run(world, caller=OPS))["allowed"] is False


def test_a_declared_skill_loads_on_a_peer_run(world):
    assert _post(world, invoked="weekly-report", names=["weekly-report"],
                 execution_id=_peer_run(world)) == ALLOWED


def test_a_conflict_name_is_the_agents_own_skill(world):
    """#2914: the platform kept the agent's own directory of that name."""
    db.assign_skill(FIN, "research", "owner")
    db.set_skill_delivery_status(FIN, ["research"], [])
    assert _post(world, invoked="research", names=["research"],
                 execution_id=_peer_run(world)) == ALLOWED


def test_names_match_case_insensitively_and_grants_by_the_library_name(world):
    db.assign_skill(FIN, "Quarter-Close", "owner")
    res = _post(world, invoked="quarter-close", names=["quarter-close"],
                execution_id=_peer_run(world))
    assert res["allowed"] is False and "the skill Quarter-Close was" in res["message"]
    db.grant_peer_skill(BILL, FIN, "Quarter-Close", ADMIN)
    assert _post(world, invoked="quarter-close", names=["quarter-close"],
                 execution_id=_peer_run(world)) == ALLOWED


def test_an_agent_with_no_library_skill_reads_nothing_more(world):
    db.delete_agent_skills(FIN)
    assert _post(world, execution_id=_peer_run(world)) == ALLOWED


# ---------------------------------------------------------------------------
# 3. Who asked: the agent of an agent-scoped key first
# ---------------------------------------------------------------------------

def test_a_fan_out_sent_without_the_header_is_still_the_peers(world):
    """`routers/fan_out.py` stamps the TARGET as the source when the caller sent
    no X-Source-Agent; the key that made the request is the caller's."""
    run = _row(triggered_by="fan_out", source_agent_name=FIN, source_mcp_key_id=world.keys[BILL])
    assert _post(world, execution_id=run)["message"] == ONE


def test_an_a2a_call_made_with_an_agent_key_is_the_peers(world):
    """A2A records no source agent; the key is the caller's agent key."""
    run = _row(triggered_by="a2a", source_user_email=OWNER_EMAIL, source_mcp_key_id=world.keys[BILL])
    assert _post(world, execution_id=run)["message"] == ONE


def test_a_user_scoped_key_is_a_person_not_a_peer(world):
    run = _row(triggered_by="mcp", source_user_email=OWNER_EMAIL, source_mcp_key_id=world.user_key)
    assert _post(world, execution_id=run) == ALLOWED


def test_a_deactivated_key_still_names_its_agent(world):
    run = _peer_run(world)
    db.set_agent_keys_active(BILL, False)
    assert _post(world, execution_id=run)["allowed"] is False


def test_a_deleted_key_falls_back_to_the_recorded_source_agent(world):
    run = _peer_run(world)
    db.delete_agent_mcp_api_key(BILL)
    assert _post(world, execution_id=run)["message"] == ONE


def _enforce(requester, text="/pay-invoice 100 EUR"):
    return asyncio.run(_GATE.enforce(FIN, request_text=text, requester=requester,
                                     triggered_by=requester.kind))


def test_the_gateway_reads_the_key_first_for_a_fan_out_without_the_header(world):
    requester = _GATE.requester_for_dispatch(
        triggered_by="fan_out", source_agent_name=FIN, source_mcp_key_id=world.keys[BILL])
    with pytest.raises(SkillGateRefused) as info:
        _enforce(requester)
    assert info.value.code == "skill_not_granted"
    assert BILL in str(info.value)


def test_the_gateway_reads_the_key_first_for_an_a2a_call(world):
    requester = _GATE.requester_for_dispatch(
        triggered_by="a2a", source_user_email=OWNER_EMAIL, source_mcp_key_id=world.keys[BILL])
    with pytest.raises(SkillGateRefused) as info:
        _enforce(requester)
    assert info.value.code == "skill_not_granted"


def test_the_gateway_refuses_a_known_peer_but_passes_a_person_when_it_cannot_read(world, monkeypatch):
    """Reading the key is new for a person's request; an outage must not turn
    a request nobody identified as a peer's into a refusal. A stamped peer's
    still refuses (ent#662: never "nothing is restricted")."""
    monkeypatch.setattr(db, "get_agent_skills", _db_down)
    person = _GATE.requester_for_dispatch(
        triggered_by="mcp", source_user_email=OWNER_EMAIL, source_mcp_key_id=world.user_key)
    assert _enforce(person).ungated
    peer = _GATE.requester_for_dispatch(triggered_by="agent", source_agent_name=BILL,
                                        source_mcp_key_id=world.keys[BILL])
    with pytest.raises(SkillGateRefused) as info:
        _enforce(peer)
    assert info.value.code == "peer_surface_unavailable"


def test_the_gateway_still_passes_a_person_and_the_agent_itself(world):
    person = _GATE.requester_for_dispatch(
        triggered_by="mcp", source_user_email=OWNER_EMAIL, source_mcp_key_id=world.user_key)
    own = _GATE.requester_for_dispatch(
        triggered_by="fan_out", source_agent_name=FIN, source_mcp_key_id=world.keys[FIN])
    for requester in (person, own):
        assert _enforce(requester).ungated


# ---------------------------------------------------------------------------
# 4. AC3 — not a peer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("triggered_by,fields", [
    pytest.param("self_task", {"source_agent_name": FIN, "source_mcp_key_id": "own"}, id="the-agent-itself"),
    pytest.param("schedule", {}, id="its-schedule"),
    pytest.param("chat", {"source_user_email": OWNER_EMAIL, "source_user_id": 1}, id="a-person"),
    pytest.param("caller_wake", {}, id="a-caller-wake"),
    pytest.param("room", {"source_user_email": OWNER_EMAIL, "source_channel": "room",
                          "source_channel_chat_id": "room-1"}, id="a-room-turn"),
    pytest.param("agent", {"source_agent_name": "trinity-system"}, id="trinity-system"),
])
def test_runs_that_are_not_a_peers_are_unaffected(world, triggered_by, fields):
    if fields.get("source_mcp_key_id") == "own":
        fields = {**fields, "source_mcp_key_id": world.keys[FIN]}
    assert _post(world, execution_id=_row(triggered_by=triggered_by, **fields)) == ALLOWED


@pytest.mark.parametrize("execution_id", [None, "manual"])
def test_a_session_that_is_not_a_run_is_the_agents_own(world, execution_id):
    assert _post(world, execution_id=execution_id) == ALLOWED


@pytest.fixture
def spawned(world, monkeypatch):
    """FIN was spawned by BILL with the key BILL holds now (ent#629)."""
    def _info(agent):
        return {"spawned_by_agent": BILL, "spawned_by_key_id": world.keys[BILL]} if agent == FIN else {}
    monkeypatch.setattr(db, "get_agent_ephemeral_info", _info)
    return world


def test_a_parent_may_load_a_library_skill_on_an_agent_it_spawned(spawned):
    assert _post(spawned, execution_id=_peer_run(spawned)) == ALLOWED


def test_a_child_running_on_its_parent_is_a_peer(spawned):
    db.assign_skill(BILL, "pay-invoice", "owner")
    spawned.as_agent(BILL)
    res = _post(spawned, execution_id=_peer_run(spawned, caller=FIN, agent=BILL))
    assert res["allowed"] is False


def test_an_unreadable_spawn_record_fails_open_not_closed(world, monkeypatch):
    """`_spawned_by_caller` fails CLOSED (a parent reads as a stranger); inside
    the peer step that would refuse a parent's turn during a blip."""
    def _boom(agent):
        raise OperationalError("select", {}, Exception("db down"))
    monkeypatch.setattr(db, "get_agent_ephemeral_info", _boom)
    assert _post(world, execution_id=_peer_run(world)) == ALLOWED


# ---------------------------------------------------------------------------
# 5. Which run
# ---------------------------------------------------------------------------

def test_a_finished_peer_run_keeps_its_requester(world):
    """A restriction outlives the run (a turn the reaper marked failed may
    still be running); only a clearance is held to a live row."""
    run = _peer_run(world)
    _finish(run)
    assert _post(world, execution_id=run)["message"] == ONE


def test_an_unknown_id_and_another_agents_run_answer_alike_at_the_same_cost(world, monkeypatch):
    reads = []
    real_row, real_key = db.get_execution_requester, db.get_agent_name_for_agent_key
    monkeypatch.setattr(db, "get_execution_requester", lambda e: reads.append("row") or real_row(e))
    monkeypatch.setattr(db, "get_agent_name_for_agent_key",
                        lambda k: reads.append("key") or real_key(k))
    answers, costs = [], []
    for run in ("no-such-run", _peer_run(world, agent=OPS)):
        reads.clear()
        answers.append(_post(world, execution_id=run))
        costs.append(list(reads))
    assert answers == [ALLOWED, ALLOWED]
    assert costs == [["row"], ["row"]]


# ---------------------------------------------------------------------------
# 6. Failure direction
# ---------------------------------------------------------------------------

def _db_down(*_a, **_k):
    raise OperationalError("select", {}, Exception("db down"))


@pytest.mark.parametrize("read", ["get_execution_requester", "get_agent_name_for_agent_key",
                                  "get_agent_skills", "granted_peer_skills"])
def test_an_unreadable_read_fails_open(world, monkeypatch, read):
    run = _peer_run(world)
    monkeypatch.setattr(db, read, _db_down)
    assert _post(world, execution_id=run) == ALLOWED


def test_a_gated_skill_keeps_the_gates_answer_when_the_peer_step_fails_open(world, monkeypatch):
    world.state["gates"] = {FIN: {"pay-invoice": _GATE.SkillGate(approver="primary")}}
    run = _peer_run(world)
    monkeypatch.setattr(db, "get_agent_skills", _db_down)
    res = _post(world, execution_id=run)
    assert res["allowed"] is False and res["gated"] is True


def test_a_programming_error_in_the_peer_step_is_not_swallowed(world, monkeypatch):
    """Only an unreadable READ fails open (learning 2026-08-12): a bug must
    surface, not silently lift the rule."""
    run = _peer_run(world)
    monkeypatch.setattr(_POLICY, "library_assigned", lambda agent: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        world.client.post(URL, json={"via": "skill_tool", "invoked": "pay-invoice",
                                     "names": ["pay-invoice"], "execution_id": run})


def test_on_a_gated_agent_the_peer_refusal_comes_first(world):
    """The gateway's order: the peer rule, then the gate — even a run the gate
    cleared for the skill."""
    world.state["gates"] = {FIN: {"pay-invoice": _GATE.SkillGate(approver="primary")}}
    run = _peer_run(world)
    db.record_self_approved_run(request_id=f"gate-self-{run}", agent_name=FIN,
                                skills=["pay-invoice"], request_text="/pay-invoice",
                                requester_email=OWNER_EMAIL, triggered_by="chat",
                                dispatched_execution_id=run)
    assert _post(world, execution_id=run) == {"allowed": False, "gated": False, "message": ONE}


# ---------------------------------------------------------------------------
# 7. A call the hook could not identify
# ---------------------------------------------------------------------------

def test_an_unresolvable_call_on_a_peer_run_is_refused(world):
    res = _post(world, invoked="mystery", names=["mystery"], resolved=False,
                execution_id=_peer_run(world))
    assert res == {"allowed": False, "gated": False, "message": COULD_NOT_TELL}


def test_an_unresolvable_call_that_names_a_restricted_skill_gets_the_named_refusal(world):
    res = _post(world, resolved=False, execution_id=_peer_run(world))
    assert res["message"] == ONE


def test_an_unresolvable_call_passes_when_the_caller_holds_every_grant(world):
    db.grant_peer_skill(BILL, FIN, "pay-invoice", ADMIN)
    assert _post(world, invoked="mystery", names=[], resolved=False,
                 execution_id=_peer_run(world)) == ALLOWED


def test_an_unresolvable_call_on_the_agents_own_run_passes(world):
    run = _row(triggered_by="schedule")
    assert _post(world, invoked="mystery", names=[], resolved=False, execution_id=run) == ALLOWED


# ---------------------------------------------------------------------------
# 8. The audit row
# ---------------------------------------------------------------------------

def test_a_refusal_is_audited_like_the_gateways_with_its_lane(world):
    run = _peer_run(world)
    _post(world, via="subagent_preload", invoked=None, subagent="closer",
          names=["pay-invoice"], execution_id=run)
    rows = [a for a in world.audits if a.get("event_action") == "peer_skill_refused"]
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"].value == "authorization"
    assert row["actor_agent_name"] == BILL and row["target_id"] == FIN
    assert row["mcp_key_id"] == world.keys[FIN]          # the key that made the call
    assert row["endpoint"] == URL
    assert row["details"] == {"skills": ["pay-invoice"], "reason": "skill_not_granted",
                              "execution_id": run, "via": "subagent_preload",
                              "subagent": "closer", "requester_key_id": world.keys[BILL]}


def test_the_audit_is_throttled_on_its_own_keys_and_the_refusal_stands(world):
    run = _peer_run(world)
    world.limits["allow"] = False
    assert _post(world, execution_id=run)["allowed"] is False
    assert not [a for a in world.audits if a.get("event_action") == "peer_skill_refused"]
    keys = [k for k, _l, _w in world.limits["calls"]]
    assert keys and all(k.startswith("peer_skill_refused") for k in keys)
    assert any(BILL in k for k in keys)                 # budgeted per caller, not the gate's


def test_a_peer_spends_its_own_audit_budget_never_the_gates(world, monkeypatch):
    """The per-run key admits the row; the per-(callee, caller) budget is what
    a peer that opens many runs on the callee exhausts — on its own key, so it
    can never use up the 20 rows the gate keeps for bypass attempts."""
    spent = []

    def _check(key, limit, window):
        spent.append((key, limit))
        return RateLimitResult(key.startswith("peer_skill_refused:"), 0, 0, limit)

    monkeypatch.setattr(_GATE.rate_limiter, "check", _check)
    assert _post(world, execution_id=_peer_run(world))["allowed"] is False
    assert spent[-1] == (f"peer_skill_refused_caller:{FIN}:{BILL}", 20)
    assert not [a for a in world.audits if a.get("event_action") == "peer_skill_refused"]


# ---------------------------------------------------------------------------
# 9. The drift report sees prose use
# ---------------------------------------------------------------------------

def _refusal_row(caller, target, skills, *, via="skill_tool", when=None):
    import uuid
    from utils.helpers import utc_now_iso
    db.create_audit_entry({
        "event_id": str(uuid.uuid4()),
        "event_type": "authorization", "event_action": "peer_skill_refused",
        "actor_type": "agent", "actor_id": caller, "target_type": "agent", "target_id": target,
        "timestamp": when or utc_now_iso(), "source": "api" if via else "gateway",
        "endpoint": "/api/skill-gate/check" if via else None,
        "details": json.dumps({"skills": skills, "reason": "skill_not_granted",
                               **({"via": via} if via else {})}),
    })


def test_the_drift_report_lists_in_container_refusals(world):
    db.add_agent_permission(BILL, FIN, OWNER)
    _refusal_row(BILL, FIN, ["pay-invoice"])
    _refusal_row(BILL, FIN, ["pay-invoice"])
    pairs = _POLICY.drift_report()["pairs"]
    assert [(p["caller"], p["target"], p["skill"], p["uses"]) for p in pairs] == [
        (BILL, FIN, "pay-invoice", 2)]


def test_the_drift_report_drops_a_refusal_once_granted_or_unassigned_or_stale(world):
    db.add_agent_permission(BILL, FIN, OWNER)
    db.add_agent_permission(OPS, FIN, OWNER)
    _refusal_row(BILL, FIN, ["pay-invoice"])
    _refusal_row(OPS, FIN, ["gone-skill"])
    _refusal_row(OPS, FIN, ["pay-invoice"], when="2020-01-01T00:00:00.000000Z")
    _refusal_row(OPS, FIN, ["pay-invoice"], via=None)     # a gateway refusal: its message is the ledger
    db.grant_peer_skill(BILL, FIN, "pay-invoice", ADMIN)
    assert _POLICY.drift_report()["pairs"] == []


def test_the_drift_report_drops_a_caller_that_can_no_longer_reach_the_agent(world):
    """A grant would not restore reach once the edge is gone: no pair to offer."""
    _refusal_row(BILL, FIN, ["pay-invoice"])
    assert _POLICY.drift_report()["pairs"] == []


def test_gateway_refusals_never_crowd_in_container_ones_out_of_the_ledger(world):
    """The ledger read is bounded; gateway refusals (same event, no `via`, not
    throttled) must not fill that bound before the in-container rows are read."""
    from utils.helpers import iso_cutoff
    _refusal_row(BILL, FIN, ["pay-invoice"], when="2026-11-05T00:00:00.000000Z")
    for _ in range(3):
        _refusal_row(BILL, FIN, ["pay-invoice"], via=None)
    rows = db.recent_in_container_peer_refusals(iso_cutoff(24 * 365 * 5), limit=2)
    assert [(r["caller"], r["target"], r["skills"]) for r in rows] == [(BILL, FIN, ["pay-invoice"])]


def test_an_agent_with_no_library_skill_reads_no_run(world, monkeypatch):
    """The hot path: every skill load fleet-wide asks. An agent holding no
    library skill (the common case) must not pay a run or key read."""
    db.delete_agent_skills(FIN)
    reads = []
    monkeypatch.setattr(db, "get_execution_requester", lambda e: reads.append(e))
    assert _post(world, execution_id="exec-anything") == ALLOWED
    assert reads == []
