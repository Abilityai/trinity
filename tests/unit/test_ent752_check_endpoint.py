"""
Gated skills, the in-container hook — the platform's answer
(trinity-enterprise#752).

Target: ``POST /api/skill-gate/check`` (``routers/skill_gate.py`` →
``skill_gate_service.check_invocation``), driven through a TestClient over the
REAL route and dependencies on the real per-test database (``db_backend``).
``get_current_user`` is overridden by walking the route's own dependant tree, so
every principal below goes through ``get_self_agent`` as it would in production.

The agent is derived from the KEY, never named in the request: the hook sends
no agent name (a renamed agent's container keeps its old ``AGENT_NAME``).

Stubbed: the gate map (ent#753's storage), the audit sink, the rate limiter,
and the marker sync's docker exec.
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import json
import logging
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
import _ent752_hook_harness as H  # noqa: E402

import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from db.write_params import ExecutionResult  # noqa: E402
from db_models import UserCreate  # noqa: E402
from models import TaskExecutionStatus, User  # noqa: E402
from services.rate_limiter import RateLimitResult  # noqa: E402

pytestmark = pytest.mark.unit

OWNER, OWNER_EMAIL = "gate-owner-752", "owner-752@example.com"
ADMIN = "admin-752"
FIN, SIB = "fin-752", "sib-752"
URL = "/api/skill-gate/check"

RUN_REFUSAL = (
    "The skill pay-invoice needs approval before it can run, and this run was not approved "
    "for it, so it was not run. Do not carry out its steps another way. Tell whoever asked for "
    "it that it needs approval, and that they can request it through Trinity with a message "
    "that includes /pay-invoice and what they want done. It will then wait for a decision, or "
    "run at once if they are the one who approves it."
)
NO_RUN_REFUSAL = (
    "The skill pay-invoice needs approval before it can run, and this session is not a Trinity "
    "run, so it cannot carry an approval. It was not run. Do not carry out its steps another "
    "way. To run it, ask for it in the agent's chat with a message that includes /pay-invoice "
    "and what you want done. It will then wait for a decision, or run at once if you are the "
    "one who approves it."
)
COULD_NOT_TELL = (
    "This agent has skills that need approval, and Trinity could not tell which skill this "
    "call loads, so it was not run. Do not carry out its steps another way."
)

_APP = None
_PRINCIPAL = {"user": None}


def _client():
    """ONE app over the real router; `get_current_user` overridden by walking
    the route's own dependant tree (never a fresh import)."""
    global _APP
    from fastapi.testclient import TestClient
    if _APP is None:
        from fastapi import FastAPI
        from error_handlers import skill_gate_error
        from routers import skill_gate as r
        from services.skill_gate_errors import SkillGateError
        app = FastAPI()
        app.include_router(r.router)
        app.add_exception_handler(SkillGateError, skill_gate_error)
        found = set()

        def walk(dependant):
            for sub in dependant.dependencies:
                if getattr(sub.call, "__name__", "") == "get_current_user":
                    found.add(sub.call)
                walk(sub)

        for route in r.router.routes:
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
        assert found, "no get_current_user dependency on the skill-gate route"
        for call in found:
            app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
        _APP = app
    return TestClient(_APP, raise_server_exceptions=True)


@pytest.fixture
def gate(db_backend, monkeypatch):
    db.create_user(UserCreate(username=OWNER, role="user", email=OWNER_EMAIL))
    db.create_user(UserCreate(username=ADMIN, role="admin", email="admin-752@example.com"))
    for agent in (FIN, SIB):
        db.register_agent_owner(agent, OWNER)
    db.register_agent_owner("trinity-system", ADMIN)
    owner = db.get_user_by_username(OWNER)

    state = {"gates": {FIN: {"pay-invoice": _GATE.SkillGate(approver="primary"),
                             "refund-invoice": _GATE.SkillGate(approver="primary")}},
             "fail": None}

    def _list(agent):
        if state["fail"] == "raise":
            raise RuntimeError("gate store down")
        if state["fail"] == "none":
            return None
        return state["gates"].get(agent, {})

    monkeypatch.setattr(_GATE, "list_skill_gates", _list)

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
    heals = []
    monkeypatch.setattr(_GATE, "spawn_gate_marker_sync",
                        lambda agent, **kw: heals.append((agent, kw)) or True)

    def as_(**principal):
        base = {"id": owner["id"], "username": OWNER, "email": OWNER_EMAIL, "role": "user"}
        base.update(principal)
        _PRINCIPAL["user"] = User(**base)

    as_(mcp_scope="agent", agent_name=FIN, mcp_key_id="key-fin")
    return SimpleNamespace(client=_client(), as_=as_, state=state, audits=audits, limits=limits,
                           heals=heals, owner_id=owner["id"])


def _post(gate, **body):
    payload = {"via": "skill_tool", "invoked": "pay-invoice", "names": ["pay-invoice"],
               "resolved": True, "execution_id": None, "marker": True}
    payload.update(body)
    return gate.client.post(URL, json=payload)


def _run(agent=FIN):
    return db.create_task_execution(agent_name=agent, message="do it", triggered_by="chat").id


def _approved(run_id, agent=FIN, skills=("pay-invoice",), dispatched=False, rid=None):
    rid = rid or f"gate-ok-{run_id}"
    db.create_gate_request(request_id=rid, agent_name=agent, skills=list(skills),
                           request_text="/pay-invoice", requester_kind="agent",
                           requester_key="agent:someone", dispatch={})
    assert db.claim_gate_request_for_dispatch(rid, run_id)
    if dispatched:
        assert db.transition_gate_request(rid, "dispatched")


def _self_approved(run_id, agent=FIN, skills=("pay-invoice",)):
    assert db.record_self_approved_run(
        request_id=f"gate-self-{run_id}", agent_name=agent, skills=list(skills),
        request_text="/pay-invoice", requester_email=OWNER_EMAIL, triggered_by="chat",
        dispatched_execution_id=run_id)


def _finish(run_id, status=TaskExecutionStatus.SUCCESS):
    assert db.update_execution_status(execution_id=run_id, status=status,
                                      result=ExecutionResult(response="done"))


# ---------------------------------------------------------------------------
# Who may ask — the agent itself, derived from its key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("principal", [
    pytest.param({}, id="a-signed-in-person"),
    pytest.param({"mcp_scope": "user"}, id="a-user-scoped-key"),
    pytest.param({"mcp_scope": "connector", "connector_agent": FIN}, id="a-connector-key"),
    pytest.param({"mcp_scope": "ops"}, id="an-ops-key"),
    pytest.param({"mcp_scope": "portal_delegate", "portal_delegate": True}, id="a-portal-delegate"),
    pytest.param({"mcp_scope": "system", "agent_name": FIN}, id="a-system-key-claiming-an-agent"),
])
def test_only_an_agent_acting_as_itself_may_ask(gate, principal):
    gate.as_(**principal)
    res = _post(gate)
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == "agent_identity_required"


def test_every_refused_principal_gets_the_same_answer(gate):
    bodies = set()
    for principal in ({}, {"mcp_scope": "user"}, {"mcp_scope": "connector", "connector_agent": FIN}):
        gate.as_(**principal)
        res = _post(gate)
        bodies.add((res.status_code, res.text))
    assert len(bodies) == 1


def test_the_answer_is_about_the_callers_own_agent_whatever_the_body_says(gate):
    """A sibling's key asks about the sibling — the body cannot name FIN."""
    gate.as_(mcp_scope="agent", agent_name=SIB, mcp_key_id="key-sib")
    res = _post(gate, names=["pay-invoice"], execution_id=_run(SIB))
    assert res.status_code == 200
    assert res.json() == {"allowed": True, "gated": False, "message": None}
    res = _post(gate, agent=FIN)
    assert res.status_code == 422          # no field may name an agent


def test_the_system_key_asks_as_trinity_system(gate):
    gate.as_(mcp_scope="system", agent_name=None, username=ADMIN, role="admin")
    res = _post(gate)
    assert res.status_code == 200, res.text
    assert res.json()["allowed"] is True


def test_a_key_whose_agent_is_gone_is_a_uniform_404(gate):
    gate.as_(mcp_scope="agent", agent_name="gone-752")
    res = _post(gate)
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

def test_an_agent_with_no_gates_is_always_allowed(gate):
    gate.state["gates"] = {}
    for body in ({}, {"resolved": False, "names": []}, {"execution_id": "anything"}):
        res = _post(gate, **body)
        assert res.json() == {"allowed": True, "gated": False, "message": None}


def test_a_skill_that_is_not_gated_is_allowed(gate):
    res = _post(gate, invoked="weekly-report", names=["weekly-report"])
    assert res.json() == {"allowed": True, "gated": False, "message": None}


def test_any_name_the_skill_answers_to_is_matched_case_insensitively(gate):
    res = _post(gate, invoked="pay", names=["pay", "PAY-Invoice"], execution_id=_run())
    assert res.json()["allowed"] is False
    assert "/pay-invoice" in res.json()["message"]


def test_a_gated_skill_with_no_run_is_refused_with_the_session_copy(gate):
    for execution_id in (None, "manual"):
        res = _post(gate, execution_id=execution_id)
        assert res.status_code == 200
        assert res.json() == {"allowed": False, "gated": True, "message": NO_RUN_REFUSAL}


def test_a_gated_skill_in_an_unapproved_run_is_refused_with_the_hand_back(gate):
    res = _post(gate, execution_id=_run())
    assert res.json() == {"allowed": False, "gated": True, "message": RUN_REFUSAL}


@pytest.mark.parametrize("dispatched", [False, True], ids=["dispatching", "dispatched"])
def test_the_approved_run_is_cleared_for_its_skill(gate, dispatched):
    run_id = _run()
    _approved(run_id, dispatched=dispatched)
    res = _post(gate, execution_id=run_id)
    assert res.json() == {"allowed": True, "gated": True, "message": None}


def test_a_self_approved_run_is_cleared_for_its_skill(gate):
    run_id = _run()
    _self_approved(run_id)
    assert _post(gate, execution_id=run_id).json()["allowed"] is True


def test_a_clearance_covers_its_skills_only(gate):
    """D3: execution + skill. Inside a run approved for refund-invoice, the
    agent may not also load pay-invoice."""
    run_id = _run()
    _approved(run_id, skills=("refund-invoice",))
    assert _post(gate, invoked="refund-invoice", names=["refund-invoice"],
                 execution_id=run_id).json()["allowed"] is True
    res = _post(gate, execution_id=run_id).json()
    assert res == {"allowed": False, "gated": True, "message": RUN_REFUSAL}


def test_a_preload_needs_every_gated_skill_cleared_and_names_only_the_rest(gate):
    run_id = _run()
    _approved(run_id, skills=("refund-invoice",))
    res = _post(gate, via="subagent_preload", invoked=None, subagent="finance",
                names=["refund-invoice", "pay-invoice", "notes"], execution_id=run_id).json()
    assert res["allowed"] is False
    assert res["message"].startswith(
        "The subagent finance loads the skill pay-invoice, which needs approval before it can run")
    assert "refund-invoice" not in res["message"]


def test_two_uncleared_skills_are_both_named(gate):
    res = _post(gate, via="subagent_preload", invoked=None, subagent="finance",
                names=["refund-invoice", "pay-invoice"], execution_id=_run()).json()
    assert res["allowed"] is False
    for name in ("pay-invoice", "refund-invoice", "/pay-invoice", "/refund-invoice"):
        assert name in res["message"]


def test_a_call_the_hook_could_not_resolve_is_refused_on_a_gated_agent(gate):
    res = _post(gate, invoked=None, names=[], resolved=False, execution_id=_run())
    assert res.json() == {"allowed": False, "gated": True, "message": COULD_NOT_TELL}


def test_a_preload_the_hook_could_not_resolve_names_the_subagent(gate):
    res = _post(gate, via="subagent_preload", invoked=None, subagent="acme:finance",
                names=[], resolved=False, execution_id=_run()).json()
    assert res["allowed"] is False
    assert "the subagent acme:finance" in res["message"]
    assert "Do not carry out its steps another way." in res["message"]


def test_the_refusal_names_no_person_and_no_role(gate):
    texts = [
        _post(gate, execution_id=_run()).json()["message"],
        _post(gate).json()["message"],
        _post(gate, names=[], resolved=False).json()["message"],
    ]
    for text in texts:
        for word in (OWNER_EMAIL, OWNER, "primary", "approver", "owner", "admin"):
            assert word not in text.lower(), (word, text)


# ---------------------------------------------------------------------------
# Uniform answers — the run must be live, this agent's, and cleared
# ---------------------------------------------------------------------------

def _other_agents_run():
    run_id = _run(SIB)
    _approved(run_id, agent=FIN, rid="gate-mismatch-row")
    return run_id


def _record_on_another_agent():
    run_id = _run(FIN)
    _approved(run_id, agent=SIB, rid="gate-mismatch-record")
    return run_id


def _finished_run():
    run_id = _run(FIN)
    _approved(run_id)
    _finish(run_id)
    return run_id


def _failed_run():
    run_id = _run(FIN)
    _self_approved(run_id)
    _finish(run_id, TaskExecutionStatus.FAILED)
    return run_id


@pytest.mark.parametrize("make", [
    pytest.param(lambda: "exec-nobody-752", id="unknown-id"),
    pytest.param(_other_agents_run, id="another-agents-run"),
    pytest.param(_record_on_another_agent, id="a-record-for-another-agent"),
    pytest.param(_finished_run, id="a-finished-run"),
    pytest.param(_failed_run, id="a-failed-self-approved-run"),
])
def test_a_run_that_is_not_this_agents_live_cleared_run_is_refused_identically(gate, make):
    res = _post(gate, execution_id=make())
    assert res.status_code == 200
    assert res.json() == {"allowed": False, "gated": True, "message": RUN_REFUSAL}


@pytest.mark.parametrize("ended", ["unknown", "stale", "not_run"])
def test_a_gate_record_that_ended_without_dispatching_clears_nothing(gate, ended):
    """Only a record that DISPATCHED the run (or recorded a self-approval)
    clears it. A claim the sweep marked `unknown` just before its row
    appeared names a live run that no approval ever started."""
    run_id = _run()
    _approved(run_id, rid=f"gate-ended-{ended}")
    assert db.transition_gate_request(f"gate-ended-{ended}", ended)
    res = _post(gate, execution_id=run_id).json()
    assert res == {"allowed": False, "gated": True, "message": RUN_REFUSAL}


def test_every_run_id_costs_the_same_two_reads(gate, monkeypatch):
    reads = []
    real_state, real_record = db.get_execution_gate_state, db.get_gate_request_by_dispatched_execution
    monkeypatch.setattr(db, "get_execution_gate_state",
                        lambda eid: reads.append(("row", eid)) or real_state(eid))
    monkeypatch.setattr(db, "get_gate_request_by_dispatched_execution",
                        lambda eid: reads.append(("record", eid)) or real_record(eid))
    counts = []
    for make in (lambda: "exec-nobody-752", _other_agents_run, _finished_run):
        run_id = make()
        reads.clear()
        _post(gate, execution_id=run_id)
        counts.append(sorted(kind for kind, _ in reads))
    assert counts == [["record", "row"]] * 3


# ---------------------------------------------------------------------------
# Failure: an unreadable map is the only non-200
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fail", ["raise", "none"])
def test_an_unreadable_gate_map_is_a_503_never_nothing_gated(gate, fail):
    gate.state["fail"] = fail
    res = _post(gate, execution_id=_run())
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "gate_unavailable"
    assert gate.heals == []                 # an unreadable map never moves the marker


@pytest.mark.parametrize("body", [
    pytest.param({"agent": FIN}, id="an-unknown-field"),
    pytest.param({"via": "slash_command"}, id="an-unknown-via"),
    pytest.param({"names": ["x"] * 65}, id="too-many-names"),
    pytest.param({"names": ["x" * 257]}, id="a-name-too-long"),
    pytest.param({"invoked": "x" * 257}, id="invoked-too-long"),
    pytest.param({"subagent": "x" * 257}, id="subagent-too-long"),
    pytest.param({"execution_id": "x" * 129}, id="execution-id-too-long"),
    pytest.param({"resolved": "maybe"}, id="resolved-not-a-bool"),
])
def test_a_malformed_body_is_a_named_422(gate, body):
    assert _post(gate, **body).status_code == 422


def test_a_missing_via_is_a_422(gate):
    res = gate.client.post(URL, json={"names": ["pay-invoice"], "resolved": True})
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Every refusal leaves an operator-visible trace, throttled
# ---------------------------------------------------------------------------

def _refusals(gate):
    return [a for a in gate.audits if a.get("event_action") == "skill_gate_refused"]


def test_a_refusal_is_audited_with_its_reason(gate, caplog):
    caplog.set_level(logging.INFO)
    run_id = _run()
    _post(gate, execution_id=run_id)
    [row] = _refusals(gate)
    assert row["target_id"] == FIN
    assert row["actor_agent_name"] == FIN
    assert row["details"]["skills"] == ["pay-invoice"]
    assert row["details"]["execution_id"] == run_id
    assert row["details"]["reason"] == "not_cleared"
    assert row["details"]["via"] == "skill_tool"
    assert "not_cleared" in caplog.text


@pytest.mark.parametrize("body, reason", [
    pytest.param({"execution_id": None}, "no_run", id="no-run"),
    pytest.param({"execution_id": "exec-nobody-752"}, "run_not_live", id="unknown-run"),
    pytest.param({"names": [], "resolved": False}, "could_not_tell", id="unresolved"),
])
def test_each_refusal_reason_is_distinct(gate, body, reason):
    _post(gate, **body)
    assert [r["details"]["reason"] for r in _refusals(gate)] == [reason]


def test_a_repeated_refusal_is_audited_once_per_run_and_skill(gate):
    run_id = _run()
    _post(gate, execution_id=run_id)
    keys = [call for call in gate.limits["calls"] if call[0].startswith("skill_gate_refused:")]
    assert keys and all(limit == 1 and window == 600 for _key, limit, window in keys)
    assert run_id in keys[0][0] and "pay-invoice" in keys[0][0]
    gate.limits["allow"] = False
    res = _post(gate, execution_id=run_id)
    assert res.json()["allowed"] is False      # still refused
    assert len(_refusals(gate)) == 1           # but not audited again


def test_an_agent_cannot_fill_the_audit_log_by_varying_the_run_id(gate):
    """The per-run key is the caller's own `execution_id`; a per-agent budget
    bounds what it can write to the append-only log."""
    counts = {}

    def _counting(key, limit, window):
        counts[key] = counts.get(key, 0) + 1
        return RateLimitResult(counts[key] <= limit, 0, 0, limit)

    _GATE.rate_limiter.check = _counting
    for i in range(_GATE.REFUSAL_AUDITS_PER_AGENT + 7):
        assert _post(gate, execution_id=f"exec-forged-{i}").json()["allowed"] is False
    assert len(_refusals(gate)) == _GATE.REFUSAL_AUDITS_PER_AGENT


def test_an_allowed_call_is_not_audited(gate):
    run_id = _run()
    _self_approved(run_id)
    _post(gate, execution_id=run_id)
    _post(gate, invoked="weekly-report", names=["weekly-report"])
    assert _refusals(gate) == []


# ---------------------------------------------------------------------------
# The marker self-heals from the hook's report, rate-limited
# ---------------------------------------------------------------------------

def test_a_gated_agent_without_its_marker_is_healed(gate):
    _post(gate, marker=False)
    assert gate.heals == [(FIN, {"only_if_gated": False})]
    assert (f"skill_gate_marker:{FIN}", 1, 300) in gate.limits["calls"]


def test_an_ungated_agent_reporting_a_marker_is_healed(gate):
    gate.as_(mcp_scope="agent", agent_name=SIB)
    _post(gate, marker=True)
    assert gate.heals == [(SIB, {"only_if_gated": False})]


@pytest.mark.parametrize("agent, marker", [(FIN, True), (SIB, False), (FIN, None)],
                         ids=["gated-with-marker", "ungated-without", "not-reported"])
def test_an_agreeing_or_absent_report_heals_nothing(gate, agent, marker):
    gate.as_(mcp_scope="agent", agent_name=agent)
    _post(gate, marker=marker)
    assert gate.heals == []


def test_the_heal_is_rate_limited_per_agent(gate):
    gate.limits["allow"] = False
    _post(gate, marker=False)
    assert gate.heals == []


# ---------------------------------------------------------------------------
# Contract — what the real hook sends, the real route accepts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload, setup", [
    pytest.param(H.skill_payload("/pay-invoice"), None, id="skill-tool"),
    pytest.param(H.agent_payload("finance"), "preload", id="subagent-preload"),
    pytest.param(H.skill_payload(None), None, id="unresolvable-skill"),
])
def test_the_body_the_hook_sends_is_accepted_by_the_route(gate, tmp_path, payload, setup):
    world = H.HookWorld(tmp_path)
    try:
        if setup == "preload":
            world.subagent("finance.md", "name: finance\nskills:\n  - pay-invoice")
        world.skill("pay-invoice")
        world.backend.answer(200, {"allowed": True, "gated": False, "message": None})
        out = world.run(payload)
        assert out.returncode == 0, out.stderr
        [request] = world.backend.requests
    finally:
        world.close()
    res = gate.client.post(URL, content=request.body,
                           headers={"Content-Type": request.headers.get("content-type", "")})
    assert res.status_code == 200, res.text
    assert set(res.json()) == {"allowed", "gated", "message"}
    assert json.loads(request.body).get("execution_id") == H.EXECUTION_ID
