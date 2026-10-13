"""
Gated skills, the in-container hook — a self-approved run is CLEARED, durably,
on the execution the agent receives (trinity-enterprise#752).

#751's only record of a self-approval was a best-effort audit row, which cannot
back a fail-closed hook: the hook asks "is execution E cleared for skill S?",
and the answer has to come from a record that exists whenever the run does.

Targets, each driven through its own layer with the #751 harness (real per-test
database, real ask sink; the gate map, capacity and the dispatch bodies stubbed):
  * ``/task``  → ``chat_execution_service.dispatch_parallel_task``
  * ``/chat``  → ``admit_chat_request`` + ``prepare_chat_execution`` (the record
    moved out of admission: the agent receives the ROW's id, not the slot's)
  * the backstop inside ``execute_task`` (the Workspace's ``gate_requester``)
  * ``db/skill_gate_requests.record_self_approved_run`` (the record itself)
  * ``channel_completion_report._is_approved_gate_run`` (must not read a
    self-approved inline run as an approved background one)
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_harness import count, db_backend  # noqa: E402,F401 — `db_backend` backs `world`
from test_ent751_gate_entries import (  # noqa: E402,F401 — `world` is a fixture
    FIN,
    OWNER_EMAIL,
    REQ,
    _agent,
    _human,
    _stop_at_admission,
    _task,
    _workspace,
    world,
)

import services.chat_execution_service as _CE  # noqa: E402
import services.dispatch_admission_service as _DISPATCH  # noqa: E402
import services.skill_gate_service as _GATE  # noqa: E402
import services.task_execution_service as _TES  # noqa: E402
from database import db  # noqa: E402
from models import ChatMessageRequest  # noqa: E402

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _self_approvals(world):
    return [a for a in world.audits if a.get("event_action") == "skill_gate_self_approved"]


def _clearance(execution_id):
    record = db.get_gate_request_by_dispatched_execution(execution_id)
    assert record is not None, f"no clearance record for the run {execution_id}"
    return record


# ---------------------------------------------------------------------------
# /task — the id the agent receives is the one `_dispatch_*` is handed
# ---------------------------------------------------------------------------

def test_a_self_approved_task_is_cleared_on_the_run_the_agent_receives(world):
    _task(world, _human(world))
    world.sync_dispatch.assert_called_once()
    run_id = world.sync_dispatch.call_args.kwargs["execution_id"]
    record = _clearance(run_id)
    assert record["state"] == "self_approved"
    assert record["agent_name"] == FIN
    assert record["skills"] == ["pay-invoice"]
    assert (record["requester_kind"], record["requester_email"]) == ("person", OWNER_EMAIL)
    assert record["request_id"].startswith("gate-self-")
    [audit] = _self_approvals(world)
    assert audit["details"]["execution_id"] == run_id


def test_an_ungated_task_writes_no_clearance(world):
    _task(world, _human(world), message="/weekly-report")
    run_id = world.sync_dispatch.call_args.kwargs["execution_id"]
    assert db.get_gate_request_by_dispatched_execution(run_id) is None
    assert _self_approvals(world) == []


def test_an_agents_task_is_held_and_cleared_for_nothing(world):
    with pytest.raises(_GATE.SkillApprovalRequired):
        _task(world, _agent(world))
    assert count("skill_gate_requests", "state = 'self_approved'") == 0


# ---------------------------------------------------------------------------
# /chat — admission decides, the row's setup records
# ---------------------------------------------------------------------------

def _chat_turn(principal, message="/pay-invoice 100 EUR", idem="idem-chat-752"):
    admission = asyncio.run(_DISPATCH.admit_chat_request(
        name=FIN, request=ChatMessageRequest(message=message), current_user=principal,
        x_source_agent=None, x_via_mcp="true", idempotency_key=idem))
    ctx = asyncio.run(_CE.prepare_chat_execution(
        name=FIN, request=ChatMessageRequest(message=message), current_user=principal,
        x_source_agent=None, x_via_mcp="true", idem=admission.idem,
        chat_execution_id=admission.execution_id,
        capacity_result=SimpleNamespace(state="admitted"), queue_result="running",
        chain_depth=admission.chain_depth, gate=admission.gate))
    return admission, ctx


@pytest.fixture
def quiet_activities(monkeypatch):
    monkeypatch.setattr(_CE.activity_service, "track_activity", AsyncMock(return_value="act-752"))


def test_chat_admission_carries_the_decision_and_records_nothing_itself(world):
    admission = asyncio.run(_DISPATCH.admit_chat_request(
        name=FIN, request=ChatMessageRequest(message="/pay-invoice 100 EUR"),
        current_user=_human(world), x_source_agent=None, x_via_mcp="true",
        idempotency_key="idem-adm-752"))
    assert admission.gate.self_approved_by == OWNER_EMAIL
    assert admission.gate.skills == ("pay-invoice",)
    # The capacity slot's id is not a run the agent will ever see.
    assert _self_approvals(world) == []
    assert db.get_gate_request_by_dispatched_execution(admission.execution_id) is None


def test_a_self_approved_chat_is_cleared_on_the_row_not_the_slot(world, quiet_activities):
    admission, ctx = _chat_turn(_human(world))
    assert ctx.task_execution_id and ctx.task_execution_id != admission.execution_id
    record = _clearance(ctx.task_execution_id)
    assert (record["state"], record["agent_name"], record["skills"]) == (
        "self_approved", FIN, ["pay-invoice"])
    [audit] = _self_approvals(world)
    assert audit["details"]["execution_id"] == ctx.task_execution_id
    assert ctx.isolated_session is True


def test_an_ungated_chat_is_neither_recorded_nor_isolated(world, quiet_activities):
    _admission, ctx = _chat_turn(_human(world), message="hello there", idem="idem-plain-752")
    assert db.get_gate_request_by_dispatched_execution(ctx.task_execution_id) is None
    assert ctx.isolated_session is False


def test_prepare_without_a_decision_behaves_as_before(world, quiet_activities):
    """The positional/default contract: an old caller passing no `gate` gets no
    record and no isolation."""
    from services import idempotency_service
    idem = idempotency_service.begin(idempotency_service.make_agent_scope(FIN), None)
    ctx = asyncio.run(_CE.prepare_chat_execution(
        name=FIN, request=ChatMessageRequest(message="/pay-invoice 1"), current_user=_human(world),
        x_source_agent=None, x_via_mcp="true", idem=idem, chat_execution_id="slot-752",
        capacity_result=SimpleNamespace(state="admitted"), queue_result="running"))
    assert ctx.isolated_session is False
    assert db.get_gate_request_by_dispatched_execution(ctx.task_execution_id) is None


# ---------------------------------------------------------------------------
# The execute_task backstop — the Workspace's proven person
# ---------------------------------------------------------------------------

def test_a_self_approved_workspace_turn_is_cleared_on_the_row_execute_task_made(world, monkeypatch):
    seen = _stop_at_admission(monkeypatch)
    _workspace(message="/pay-invoice 1", source_user_email=OWNER_EMAIL,
               gate_requester=_GATE.Requester(kind="person", key=f"person:{OWNER_EMAIL}",
                                              email=OWNER_EMAIL, is_person=True))
    run_id = seen[0]["execution_id"]
    record = _clearance(run_id)
    assert (record["state"], record["agent_name"]) == ("self_approved", FIN)
    [audit] = _self_approvals(world)
    assert audit["details"]["execution_id"] == run_id


def test_a_self_approved_record_never_passes_as_an_approved_run_in_the_backstop(world, monkeypatch):
    """The backstop's own read of the same lookup must keep skipping only a run
    the gate DISPATCHED. A request reusing a self-approved run's id is gated."""
    run = db.create_task_execution(agent_name=FIN, message="/pay-invoice", triggered_by="agent")
    db.record_self_approved_run(
        request_id="gate-self-backstop", agent_name=FIN, skills=["pay-invoice"],
        request_text="/pay-invoice", requester_email=OWNER_EMAIL, triggered_by="chat",
        dispatched_execution_id=run.id)
    seen = _stop_at_admission(monkeypatch)
    with pytest.raises(_GATE.SkillApprovalRequired):
        asyncio.run(_TES.TaskExecutionService().execute_task(
            agent_name=FIN, message="/pay-invoice", triggered_by="agent", execution_id=run.id,
            source_agent_name=REQ))
    assert seen == []


# ---------------------------------------------------------------------------
# The service seam itself
# ---------------------------------------------------------------------------

def _decision():
    return _GATE.GateDecision(("pay-invoice",), self_approved_by=OWNER_EMAIL)


def test_recording_twice_for_one_run_keeps_one_record(world):
    run = db.create_task_execution(agent_name=FIN, message="/pay-invoice", triggered_by="chat")
    for _ in range(2):
        asyncio.run(_GATE.record_self_approval(
            FIN, _decision(), execution_id=run.id, current_user=_human(world),
            endpoint="/api/agents/x/chat", request_text="/pay-invoice", triggered_by="chat"))
    assert count("skill_gate_requests", "dispatched_execution_id = :e", e=run.id) == 1


def test_a_failed_record_write_is_logged_and_never_fails_the_dispatch(world, monkeypatch, caplog):
    def _boom(**_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "record_self_approved_run", _boom)
    asyncio.run(_GATE.record_self_approval(
        FIN, _decision(), execution_id="exec-752-fail", current_user=_human(world),
        endpoint="/api/agents/x/task", request_text="/pay-invoice", triggered_by="mcp"))
    assert "self-approval record" in caplog.text
    assert len(_self_approvals(world)) == 1      # the audit still says it happened


def test_an_ungated_or_unapproved_decision_records_nothing(world):
    for decision in (_GATE.GateDecision(), _GATE.GateDecision(("pay-invoice",))):
        asyncio.run(_GATE.record_self_approval(
            FIN, decision, execution_id="exec-752-none", current_user=_human(world),
            endpoint="e", request_text="t", triggered_by="chat"))
    assert db.get_gate_request_by_dispatched_execution("exec-752-none") is None
    assert _self_approvals(world) == []


def test_a_run_with_no_row_is_audited_but_cannot_be_cleared(world):
    asyncio.run(_GATE.record_self_approval(
        FIN, _decision(), execution_id=None, current_user=_human(world),
        endpoint="e", request_text="t", triggered_by="chat"))
    assert count("skill_gate_requests", "state = 'self_approved'") == 0
    assert len(_self_approvals(world)) == 1


def test_a_long_request_is_kept_bounded_on_the_record(world):
    run = db.create_task_execution(agent_name=FIN, message="x", triggered_by="chat")
    asyncio.run(_GATE.record_self_approval(
        FIN, _decision(), execution_id=run.id, current_user=_human(world),
        endpoint="e", request_text="/pay-invoice " + "x" * 50_000, triggered_by="chat"))
    assert len(_clearance(run.id)["request_text"]) < 7000


# ---------------------------------------------------------------------------
# The record (db layer)
# ---------------------------------------------------------------------------

def test_the_record_is_a_state_no_transition_enters_or_leaves(world):
    from db import skill_gate_requests as sgr
    assert "self_approved" in sgr.STATES
    assert "self_approved" not in sgr.TERMINAL_STATES
    run = db.create_task_execution(agent_name=FIN, message="x", triggered_by="chat")
    created = db.record_self_approved_run(
        request_id="gate-self-lattice", agent_name=FIN, skills=["pay-invoice"],
        request_text="/pay-invoice", requester_email=OWNER_EMAIL, triggered_by="chat",
        dispatched_execution_id=run.id)
    assert created is True
    for target in ("cancelled", "denied", "dispatched", "unknown"):
        assert db.transition_gate_request("gate-self-lattice", target) is False
    assert db.claim_gate_request_for_dispatch("gate-self-lattice", "exec-other") is False
    assert db.get_gate_request("gate-self-lattice")["state"] == "self_approved"
    # It is never pending work: no cap, no sweep, no ask.
    assert db.count_pending_gate_requests(FIN) == 0
    assert db.list_pending_gate_requests(FIN) == []


def test_a_second_insert_for_the_same_run_changes_nothing(world):
    run = db.create_task_execution(agent_name=FIN, message="x", triggered_by="chat")
    kw = dict(agent_name=FIN, skills=["pay-invoice"], request_text="/pay-invoice",
              requester_email=OWNER_EMAIL, triggered_by="chat", dispatched_execution_id=run.id)
    assert db.record_self_approved_run(request_id="gate-self-twice", **kw) is True
    assert db.record_self_approved_run(request_id="gate-self-twice", **kw) is False
    # A different id for the SAME run is refused by the unique run column, not raised.
    assert db.record_self_approved_run(request_id="gate-self-other", **kw) is False


# ---------------------------------------------------------------------------
# The completion report must not treat a self-approved inline run as approved
# ---------------------------------------------------------------------------

def test_only_a_dispatched_approval_is_reported_in_the_background(world):
    from services.channel_completion_report import _is_approved_gate_run
    run = db.create_task_execution(agent_name=FIN, message="x", triggered_by="public")
    db.record_self_approved_run(
        request_id="gate-self-report", agent_name=FIN, skills=["pay-invoice"],
        request_text="/pay-invoice", requester_email=OWNER_EMAIL, triggered_by="public",
        dispatched_execution_id=run.id)
    assert _is_approved_gate_run(run.id) is False

    db.create_gate_request(request_id="gate-approved-report", agent_name=FIN, skills=["pay-invoice"],
                           request_text="/pay-invoice", requester_kind="agent",
                           requester_key=f"agent:{REQ}", dispatch={})
    assert db.claim_gate_request_for_dispatch("gate-approved-report", "exec-approved-report")
    assert _is_approved_gate_run("exec-approved-report") is True


# ---------------------------------------------------------------------------
# Structure — one way to self-approve, so a fourth producer cannot forget the record
# ---------------------------------------------------------------------------

def test_the_self_approval_audit_is_only_written_with_the_record():
    """A producer that audits a self-approval without recording it would leave
    a run the platform let through that the hook then refuses — or, worse, a
    record-less audit read as a clearance by someone later. Every call of
    `audit_self_approved` under src/backend sits in `record_self_approval`."""
    offenders = []
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith(("enterprise/", "tests/")):
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name == "audit_self_approved" and not (
                        rel == "services/skill_gate_service.py" and fn.name == "record_self_approval"):
                    offenders.append(f"{rel}::{fn.name}")
    assert offenders == [], f"audit_self_approved called outside record_self_approval: {offenders}"
    src = (_BACKEND / "services" / "skill_gate_service.py").read_text()
    assert "await audit_self_approved(" in src, "the guard must still see the one real call"
