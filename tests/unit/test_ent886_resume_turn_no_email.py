"""trinity-enterprise#886 — the resume turn stops handing the agent the responder.

The ask-resume turn (ent#329) and the ending wake (ent#611) stamped the person
who answered or ended the ask as `source_user_email`; the prompt then rendered
them on the Triggered-by line (`user: '<email>'`). Every other return path
withholds the person (ent#715). These tests pin the effect, not the kwarg:

* the prompt the REAL composer builds from the kwargs the producer sends carries
  neither the responder's address nor its local part, and the Triggered-by line
  is the bare trigger;
* the Workspace destination and the audit row still name who answered;
* the served seat (ent#814) of a discussed-ask resume is read off the row's
  Workspace stamp by ONE helper both composers (push and pull) use;
* a gate request frozen with the old field never re-stamps it on approval.
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

from db_harness import db_backend  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parent))   # `_gate_world`

# Real modules, bound BEFORE any test stubs `database` / `services.*` in
# sys.modules — a first import under those stubs would bind the stubs for good.
import services.docker_utils  # noqa: E402,F401
import services.operator_queue_service  # noqa: E402,F401
import client_portal.db as portal_db  # noqa: E402
import services.operator_resume_service as ors  # noqa: E402
import services.task_execution_service as tes  # noqa: E402
import services.pull_coordination_service as pcs  # noqa: E402
from services import assignment_provider as ap  # noqa: E402
from services import platform_prompt_service as pps  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "zq886-companion"
# Sentinels owned by this file: an absence assertion must own its names.
RESPONDER = "zq886-responder@example.test"
RESPONDER_LOCAL = "zq886-responder"
CHAT = "zq886-chat-1"
SEAT = "zq886-head-of-sales"


@pytest.fixture(autouse=True)
def _clean_provider():
    ap.clear_provider()
    yield
    ap.clear_provider()


class _Roster:
    """The primary holds `cfo`; RESPONDER holds SEAT."""

    def assignment_for(self, agent_name, triggered_by):
        return {"primary_user_display": "Ann", "role_id": "cfo", "proactive_consent": False}

    def seat_for(self, agent_name):
        return {"case": "serves", "role_id": "cfo", "seats": ["cfo", SEAT]}

    def served_seat_for(self, agent_name, person_email):
        if person_email == RESPONDER:
            return {"role_id": SEAT, "source": "person"}
        return {"role_id": "cfo", "source": "primary"}


def _install(monkeypatch):
    """Stub the resume service's lazily imported collaborators (as test_ent611)."""
    import services as _services_pkg

    calls, audit = [], []

    def _mod(dotted, module):
        monkeypatch.setitem(sys.modules, dotted, module)
        if dotted.startswith("services."):
            monkeypatch.setattr(_services_pkg, dotted.split(".", 1)[1], module, raising=False)

    monkeypatch.setitem(sys.modules, "database", SimpleNamespace(
        db=SimpleNamespace(get_operator_resume_enabled=lambda name: True)))
    _mod("services.idempotency_service", SimpleNamespace(
        begin=lambda scope, key: SimpleNamespace(replay=False, in_flight=False),
        complete=lambda *a, **k: None, fail=lambda *a, **k: None,
        make_agent_scope=lambda name: f"agent:{name}"))

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)

    _mod("services.platform_audit_service", SimpleNamespace(
        platform_audit_service=_Audit(), AuditEventType=SimpleNamespace(EXECUTION="execution")))

    async def _adapter(**kw):
        calls.append(kw)
        return SimpleNamespace(execution_id=f"exec-{len(calls)}", status="success", error=None)

    _mod("services.task_execution_service", SimpleNamespace(
        dispatch_and_await_terminal=_adapter, get_task_execution_service=lambda: None))
    _mod("services.docker_service", SimpleNamespace(agent_container_state=lambda name: "running"))
    monkeypatch.setattr(portal_db, "get_portal_session",
                        lambda chat, agent, email: chat == CHAT and email == RESPONDER)
    return calls, audit


def _item(**over):
    item = {"id": "zq886-item", "agent_name": AGENT, "title": "Approve the invoice?",
            "question": "Send invoice #42?", "addressed_to_email": RESPONDER,
            "context": {"workspace_discussion_id": CHAT}}
    item.update(over)
    return item


def _row_from(kwargs):
    """What `execute_task` writes for these kwargs (the columns the composer reads)."""
    return SimpleNamespace(
        triggered_by=kwargs["triggered_by"], schedule_id=None,
        source_user_email=kwargs.get("source_user_email"),
        source_channel=kwargs.get("source_channel"),
        source_channel_client=kwargs.get("source_channel_client"),
    )


def _render_push(monkeypatch, kwargs):
    """The REAL push composer over the kwargs the producer sent."""
    fake = MagicMock()
    fake.get_setting_value = MagicMock(return_value=None)
    fake.get_permitted_agents = MagicMock(return_value=[])
    fake.list_recent_operator_queue_endings = MagicMock(return_value=[])
    monkeypatch.setattr(pps, "db", fake)
    monkeypatch.setattr(tes.db, "get_execution", lambda eid: _row_from(kwargs))
    monkeypatch.setattr(tes, "is_execution_context_enabled", lambda: True)
    monkeypatch.setattr(tes, "_resolve_agent_runtime", lambda name: "claude-code")
    return tes.TaskExecutionService()._compose_effective_system_prompt(
        agent_name=kwargs["agent_name"], triggered_by=kwargs["triggered_by"],
        source_user_email=kwargs.get("source_user_email"), source_agent_name=None,
        source_mcp_key_name=None, model=None, timeout_seconds=None, attempt=None,
        schedule_context=None, execution_id="zq886-exec", system_prompt=None)


def _triggered_line(prompt):
    return next(l for l in prompt.splitlines() if l.startswith("- **Triggered by**:"))


# ---------------------------------------------------------------------------
# The producers — what reaches the agent
# ---------------------------------------------------------------------------

def test_the_resume_prompt_names_neither_the_responder_nor_their_local_part(monkeypatch):
    calls, _ = _install(monkeypatch)
    asyncio.run(ors.maybe_dispatch_resume(_item(), response="approve", responded_by_email=RESPONDER))
    assert len(calls) == 1
    prompt = _render_push(monkeypatch, calls[0])
    assert RESPONDER not in prompt
    assert RESPONDER_LOCAL not in prompt
    assert _triggered_line(prompt) == "- **Triggered by**: operator_response"


def test_the_ending_prompt_names_neither_the_canceller_nor_their_local_part(monkeypatch):
    calls, audit = _install(monkeypatch)
    row = {"id": "uuid-zq886", "request_id": "zq886-ask", "agent_name": AGENT, "type": "approval",
           "status": "cancelled", "title": "Pay invoice", "question": "Release 500?",
           "sync_state": "confirmed"}
    asyncio.run(ors.maybe_dispatch_ending([row], disposition="cancelled",
                                          disposed_by_email=RESPONDER, reason="other vendor"))
    assert len(calls) == 1
    prompt = _render_push(monkeypatch, calls[0])
    assert RESPONDER not in prompt and RESPONDER_LOCAL not in prompt
    assert _triggered_line(prompt) == "- **Triggered by**: operator_ending"
    assert any(a.get("actor_email") == RESPONDER for a in audit), "the audit row keeps the canceller"


def test_the_destination_and_the_audit_still_name_who_answered(monkeypatch):
    calls, audit = _install(monkeypatch)
    asyncio.run(ors.maybe_dispatch_resume(_item(), response="approve", responded_by_email=RESPONDER))
    call = calls[0]
    assert "source_user_email" not in call
    assert (call["source_channel"], call["source_channel_chat_id"], call["source_channel_client"]) == (
        "portal", CHAT, RESPONDER)
    assert any(a.get("actor_email") == RESPONDER for a in audit)


def test_a_discussed_resume_still_serves_the_responders_seat_without_naming_them(monkeypatch):
    ap.register_provider(_Roster())
    calls, _ = _install(monkeypatch)
    asyncio.run(ors.maybe_dispatch_resume(_item(), response="approve", responded_by_email=RESPONDER))
    prompt = _render_push(monkeypatch, calls[0])
    assert f"- **Seat this run serves**: {SEAT}" in prompt
    assert RESPONDER not in prompt and RESPONDER_LOCAL not in prompt


def test_an_undiscussed_resume_serves_nobody_in_particular(monkeypatch):
    ap.register_provider(_Roster())
    calls, _ = _install(monkeypatch)
    asyncio.run(ors.maybe_dispatch_resume(_item(context={}), response="approve",
                                          responded_by_email=RESPONDER))
    prompt = _render_push(monkeypatch, calls[0])
    assert "Seat this run serves" not in prompt
    assert RESPONDER not in prompt and RESPONDER_LOCAL not in prompt


# ---------------------------------------------------------------------------
# The shared row helper (push + pull)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("make", [lambda **kw: kw, lambda **kw: SimpleNamespace(**kw)],
                         ids=["pull-dict", "push-row"])
def test_served_person_for_row_reads_the_workspace_stamp(make):
    stamped = dict(triggered_by="operator_response", schedule_id=None,
                   source_channel="portal", source_channel_client=" Bob@Example.com ")
    assert tes.served_person_for_row(make(**stamped)) == "bob@example.com"
    assert tes.served_person_for_row(make(**{**stamped, "source_channel": None})) is None
    assert tes.served_person_for_row(make(**{**stamped, "triggered_by": "chat"})) is None
    assert tes.served_person_for_row(make(**{**stamped, "triggered_by": "operator_ending"})) is None
    seat_run = {**stamped, "triggered_by": "schedule", "schedule_id": "sch-1"}
    assert tes.served_person_for_row(make(**seat_run)) == "bob@example.com"
    assert tes.served_person_for_row(make(**{**seat_run, "schedule_id": None})) is None
    assert tes.served_person_for_row(None) is None


def test_the_push_lookup_reads_the_row_once_and_fails_soft(monkeypatch):
    row = SimpleNamespace(triggered_by="operator_response", schedule_id=None,
                          source_channel="portal", source_channel_client=RESPONDER)
    reads = []
    monkeypatch.setattr(tes.db, "get_execution", lambda eid: reads.append(eid) or row)
    assert tes._served_person_for("e1", "operator_response") == RESPONDER
    assert tes._served_person_for("e1", "chat") is None
    assert reads == ["e1"]

    def _boom(eid):
        raise RuntimeError("db down")

    monkeypatch.setattr(tes.db, "get_execution", _boom)
    assert tes._served_person_for("e1", "operator_response") is None


def _claim_payload(monkeypatch, row):
    fake = MagicMock()
    fake.get_setting_value = MagicMock(return_value=None)
    fake.get_permitted_agents = MagicMock(return_value=[])
    fake.list_recent_operator_queue_endings = MagicMock(return_value=[])
    monkeypatch.setattr(pps, "db", fake)
    monkeypatch.setattr(pcs, "is_execution_context_enabled", lambda: True)
    monkeypatch.setattr(pcs, "_resolve_agent_runtime", lambda name: "claude-code")
    return pcs._build_claim_response(row)["envelope"]["payload"]


def _claimed_row(**over):
    row = {"id": "zq886-pull", "agent_name": AGENT, "message": "framed answer",
           "backlog_metadata": None, "source_user_id": None, "source_user_email": None,
           "source_agent_name": None, "source_mcp_key_id": None, "source_mcp_key_name": None,
           "subscription_id": None, "triggered_by": "operator_response", "claude_session_id": None,
           "model_used": None, "started_at": None, "claim_token": "tok", "lease_expires_at": None,
           "claimed_by_worker": "w1", "redelivery_count": 0, "schedule_id": "__manual__",
           "source_channel": "portal", "source_channel_client": RESPONDER}
    row.update(over)
    return row


def test_a_pull_claimed_discussed_resume_names_the_seat_not_the_person(monkeypatch):
    ap.register_provider(_Roster())
    payload = _claim_payload(monkeypatch, _claimed_row())
    prompt = payload["task_overrides"]["system_prompt"]
    assert f"- **Seat this run serves**: {SEAT}" in prompt
    assert RESPONDER not in prompt and RESPONDER_LOCAL not in prompt


def test_a_pull_claimed_undiscussed_resume_has_no_seat_line(monkeypatch):
    ap.register_provider(_Roster())
    payload = _claim_payload(monkeypatch, _claimed_row(source_channel=None, source_channel_client=None))
    assert "Seat this run serves" not in payload["task_overrides"]["system_prompt"]


def test_the_real_claim_hands_the_helper_the_columns_it_reads(db_backend):
    """The helper is only as good as the row it is handed: a claim whose
    RETURNING lacks the stamp reads None for every pull-claimed run."""
    from db_harness import run as _hrun
    from db.schedules import ScheduleOperations

    _hrun(
        "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, "
        " queued_at, message, triggered_by, source_channel, source_channel_client) "
        "VALUES ('zq886-q', '__manual__', :a, 'queued', '2026-10-13T00:00:00Z', "
        " '2026-10-13T00:00:00Z', 'm', 'operator_response', 'portal', :c)",
        a=AGENT, c=RESPONDER,
    )
    ops = ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())
    row = ops.claim_next_queued(AGENT, worker_id="w1", lease_seconds=900)
    assert row["id"] == "zq886-q"
    assert tes.served_person_for_row(row) == RESPONDER


# ---------------------------------------------------------------------------
# T2 — a gate request frozen before the fix
# ---------------------------------------------------------------------------

@pytest.fixture
def gate_world(db_backend, monkeypatch):
    import _gate_world as W
    import services.capacity_manager as _CM
    import services.chat_execution_service as _CE
    world = W.build(monkeypatch)
    world.W = W
    world.spawned = []
    cap = SimpleNamespace(calls=[])

    async def _acquire(**kw):
        cap.calls.append(kw)
        return SimpleNamespace(state="admitted", queue_position=None)

    cap.acquire = _acquire
    monkeypatch.setattr(_CM, "get_capacity_manager", lambda: cap)
    monkeypatch.setattr(tes, "dispatch_breaker_active", lambda n: False)

    async def _run_async_task(**kw):
        world.spawned.append(kw)

    monkeypatch.setattr(_CE, "run_async_task", _run_async_task)
    return world


def _approve(request_id, execution_id):
    import services.skill_gate_service as gate
    from database import db

    async def _go():
        await gate._dispatch_approved(db.get_gate_request(request_id), execution_id)
        await asyncio.sleep(0)
    asyncio.run(_go())


@pytest.mark.parametrize("trigger", ["operator_response", "operator_ending"])
def test_a_frozen_resume_request_never_restamps_the_responder(gate_world, trigger):
    from database import db
    W = gate_world.W
    db.create_gate_request(
        request_id=f"zq886-gate-{trigger}", agent_name=W.FIN, skills=["pay-invoice"],
        request_text="/pay-invoice 1", requester_kind="person",
        requester_key=f"person:{RESPONDER}",
        dispatch={"triggered_by": trigger, "source_user_email": RESPONDER})
    _approve(f"zq886-gate-{trigger}", f"zq886-exec-{trigger}")
    assert db.get_execution(f"zq886-exec-{trigger}").source_user_email is None
    assert gate_world.spawned[0]["user_email"] is None


def test_a_frozen_chat_request_keeps_its_person(gate_world):
    from database import db
    W = gate_world.W
    db.create_gate_request(
        request_id="zq886-gate-chat", agent_name=W.FIN, skills=["pay-invoice"],
        request_text="/pay-invoice 1", requester_kind="person",
        requester_key=f"person:{RESPONDER}",
        dispatch={"triggered_by": "chat", "source_user_email": RESPONDER})
    _approve("zq886-gate-chat", "zq886-exec-chat")
    assert db.get_execution("zq886-exec-chat").source_user_email == RESPONDER
