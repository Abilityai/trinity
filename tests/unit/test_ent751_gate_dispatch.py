"""
Gated skills — from an ending to the run, exactly once (trinity-enterprise#751).

Targets: ``services/skill_gate_service`` (``on_ending``, ``resolve``,
``_approve``, ``_dispatch_approved``, ``sweep``, the notices) and the ask sink's
``may_end`` (``services/ask_service``), over the real per-test database and the
real ask sink. A request is raised through ``enforce`` and decided through
``ask_service.answer`` / ``cancel`` / ``expire`` exactly as the routes do it.

Stubbed only: the gate map (ent#753), the in-container fingerprint read, the
capacity manager, the background run and the requester wake.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import count as _count, db_backend, run as _hrun  # noqa: E402,F401

import services.ask_service as _ASK  # noqa: E402
import services.operator_queue_service as _OQS  # noqa: E402
import services.role_addressing as _ROLES  # noqa: E402
import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from db_models import UserCreate  # noqa: E402
from services.rate_limiter import RateLimitResult  # noqa: E402

pytestmark = pytest.mark.unit

OWNER, OWNER_EMAIL = "disp-owner", "owner-disp@example.com"
FIN, REQ = "disp-fin", "disp-mkt"


@pytest.fixture
def gate(db_backend, monkeypatch):
    db.create_user(UserCreate(username=OWNER, role="user", email=OWNER_EMAIL))
    db.register_agent_owner(FIN, OWNER)
    db.register_agent_owner(REQ, OWNER)

    class _Audit:
        async def log(self, **kw):
            return "evt"

    class _WS:
        async def broadcast(self, message):
            json.loads(message)

    monkeypatch.setattr(_ASK, "platform_audit_service", _Audit())
    monkeypatch.setattr(_ASK, "_websocket_manager", _WS())
    monkeypatch.setattr(_ASK, "_observers", [])
    monkeypatch.setattr(_ROLES, "owner_email", lambda agent: OWNER_EMAIL)
    monkeypatch.setattr(_OQS, "_workspace_attachment", lambda agent, email, **_: (None, False))
    monkeypatch.setattr(_OQS.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    monkeypatch.setattr(_GATE.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    monkeypatch.setattr(db, "get_operator_resume_enabled", lambda agent: False, raising=False)

    state = SimpleNamespace(fingerprint="fp-1", fp_reads=0, dispatched=[], woken=[], hold=None)

    async def _read(agent, names):
        state.fp_reads += 1
        if state.hold is not None:
            await state.hold.wait()
        if state.fingerprint is None:
            return None
        return {n: {"fingerprint": state.fingerprint, "kind": "own"} for n in names}

    async def _dispatch(record, execution_id):
        state.dispatched.append((record["request_id"], execution_id))
        return "admitted"

    async def _wake(record, text):
        state.woken.append((record["source_agent"], text))

    monkeypatch.setattr(_GATE, "list_skill_gates",
                        lambda agent: {"pay-invoice": _GATE.SkillGate()} if agent == FIN else {})
    monkeypatch.setattr(_GATE, "read_skill_fingerprints", _read)
    monkeypatch.setattr(_GATE, "_dispatch_approved", _dispatch)
    monkeypatch.setattr(_GATE, "_wake_requester_agent", _wake)
    return state


def _raise_request(requester=None, occurrence=None):
    requester = requester or _GATE.Requester(kind="agent", key=f"agent:{REQ}", agent_name=REQ,
                                             execution_id="exec-origin")

    async def _go():
        try:
            await _GATE.enforce(FIN, request_text="/pay-invoice 100 EUR", requester=requester,
                                triggered_by="agent", occurrence_key=occurrence,
                                dispatch={"triggered_by": "agent", "model": "sonnet"})
        except _GATE.SkillApprovalRequired as e:
            return e.request_id
        raise AssertionError("expected an approval")
    return asyncio.run(_go())


def _ask_row(rid):
    return db.get_operator_queue_item_for_agent_by_request_id(FIN, rid)


def _answer(rid, response, email=OWNER_EMAIL):
    async def _go():
        return _ASK.answer(_ask_row(rid), response=response, response_text=None,
                           actor=_ASK.Actor(email=email))
    return asyncio.run(_go())


def _resolve(rid):
    return asyncio.run(_GATE.resolve(rid))


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def test_an_approval_by_the_addressee_dispatches_once_and_tells_the_requester(gate):
    rid = _raise_request()
    _answer(rid, _GATE.APPROVE)
    assert _resolve(rid) == "dispatched"
    record = db.get_gate_request(rid)
    assert record["state"] == "dispatched"
    assert gate.dispatched == [(rid, record["dispatched_execution_id"])]
    assert len(gate.woken) == 1
    requester, text = gate.woken[0]
    assert requester == REQ
    assert rid in text and record["dispatched_execution_id"] in text
    assert "exec-origin" in text
    assert "/" not in text.split(" on ")[0].split("skill ")[1]       # no slash in the notice
    assert _resolve(rid) is None                                    # consumed: nothing twice
    assert len(gate.dispatched) == 1


def test_a_rejection_runs_nothing_and_says_so(gate):
    rid = _raise_request()
    _answer(rid, _GATE.REJECT)
    assert _resolve(rid) == "denied"
    assert gate.dispatched == []
    assert "rejected" in gate.woken[0][1]


def test_a_changed_skill_is_stale_and_runs_nothing(gate):
    rid = _raise_request()
    gate.fingerprint = "fp-2"
    _answer(rid, _GATE.APPROVE)
    assert _resolve(rid) == "stale"
    assert db.get_gate_request(rid)["state_detail"] == "pay-invoice"
    assert gate.dispatched == []
    assert "changed" in gate.woken[0][1]


def test_an_unreachable_executor_at_approval_is_not_run_and_not_retried(gate):
    rid = _raise_request()
    gate.fingerprint = None
    _answer(rid, _GATE.APPROVE)
    assert _resolve(rid) == "not_run"
    assert gate.dispatched == []
    assert _resolve(rid) is None


def test_expiry_is_a_recorded_ending_that_runs_nothing(gate):
    rid = _raise_request()
    _hrun("UPDATE operator_queue SET expires_at = '2000-01-01T00:00:00Z' WHERE request_id = :r", r=rid)
    _ASK.expire()
    assert _resolve(rid) == "expired"
    assert gate.dispatched == []


def test_an_approval_from_someone_not_addressed_is_never_dispatched(gate):
    """Belt and braces over the sink: a row approved by a person outside
    `resolved_to` (written around the sink) is denied, not run."""
    rid = _raise_request()
    _hrun("UPDATE operator_queue SET status='responded', disposition='answered', response=:a, "
          "disposed_by='person', responded_by_email='intruder@example.com' WHERE request_id=:r",
          a=_GATE.APPROVE, r=rid)
    assert _resolve(rid) == "denied"
    assert db.get_gate_request(rid)["state_detail"] == "approver_not_addressed"
    assert gate.dispatched == []


def test_two_resolvers_that_both_read_pending_dispatch_once(gate, monkeypatch):
    """Two workers can both read the record as `pending` before either claims
    it; the claim's compare-and-set is the only thing that may decide."""
    rid = _raise_request()
    _answer(rid, _GATE.APPROVE)
    snapshot = dict(db.get_gate_request(rid))
    monkeypatch.setattr(db, "get_gate_request", lambda r: dict(snapshot))   # both see `pending`
    first, second = _resolve(rid), _resolve(rid)
    assert (first, second) == ("dispatched", None)
    assert len(gate.dispatched) == 1


# ---------------------------------------------------------------------------
# The observer and the sweep
# ---------------------------------------------------------------------------

def test_the_observer_picks_gate_approvals_only(gate, monkeypatch):
    seen = []
    import services.operator_resume_service as _ORS
    monkeypatch.setattr(_ORS, "spawn_on_loop", lambda factory: seen.append(factory))
    event = _ASK.EndingEvent("answered", (
        {"request_id": "gate-abc", "raised_by": "gate"},
        {"request_id": f"{_GATE.NOTE_PREFIX}xyz", "raised_by": "gate"},
        {"request_id": "own-ask", "raised_by": "agent"},
    ), OWNER_EMAIL)
    _GATE.on_ending(event)
    assert len(seen) == 1


def test_the_sweep_consumes_an_ending_no_observer_saw_and_writes_nothing_twice(gate):
    rid = _raise_request()
    _answer(rid, _GATE.APPROVE)            # observers are off in this fixture
    asyncio.run(_GATE.sweep())
    assert db.get_gate_request(rid)["state"] == "dispatched"
    asyncio.run(_GATE.sweep())
    assert len(gate.dispatched) == 1


def test_a_run_lost_between_claim_and_start_becomes_unknown_and_is_never_rerun(gate):
    rid = _raise_request()
    assert db.claim_gate_request_for_dispatch(rid, "exec-never-created")
    _hrun("UPDATE skill_gate_requests SET decided_at = '2000-01-01T00:00:00Z' WHERE request_id = :r", r=rid)
    asyncio.run(_GATE.sweep())
    record = db.get_gate_request(rid)
    assert record["state"] == "unknown"
    assert gate.dispatched == []
    assert "not retried" in gate.woken[0][1]
    asyncio.run(_GATE.sweep())
    assert len(gate.woken) == 1


def _age(rid, column):
    _hrun(f"UPDATE skill_gate_requests SET {column} = '2000-01-01T00:00:00Z' WHERE request_id = :r", r=rid)


def test_an_unknown_run_is_also_reported_to_the_person_who_approved_it(gate):
    """They approved a business action and nothing says whether it happened:
    the approver hears it as well as the requester."""
    rid = _raise_request()
    _answer(rid, _GATE.APPROVE)                 # observers are off: the record stays pending
    assert db.claim_gate_request_for_dispatch(rid, "exec-never-created")
    _age(rid, "decided_at")
    asyncio.run(_GATE.sweep())
    assert db.get_gate_request(rid)["state"] == "unknown"
    notes = _rows_like(_GATE.NOTE_PREFIX)
    assert [n["addressed_to_email"] for n in notes] == [OWNER_EMAIL]


def test_a_run_that_started_but_was_never_recorded_is_recorded_and_reported_never_rerun(gate):
    """The process died after the run row was written and before the record
    said so: the run is real, so the sweep records it and sends the notice."""
    rid = _raise_request()
    assert db.claim_gate_request_for_dispatch(rid, "exec-started")
    db.create_task_execution(agent_name=FIN, message="/pay-invoice 100 EUR",
                             triggered_by="agent", execution_id="exec-started")
    _age(rid, "decided_at")
    asyncio.run(_GATE.sweep())
    record = db.get_gate_request(rid)
    assert (record["state"], record["dispatched_execution_id"]) == ("dispatched", "exec-started")
    assert gate.dispatched == []
    assert len(gate.woken) == 1 and "exec-started" in gate.woken[0][1]
    asyncio.run(_GATE.sweep())
    assert len(gate.woken) == 1


def test_a_record_whose_ask_was_never_attached_is_attached_not_cancelled(gate):
    rid = _raise_request()
    item_id = _ask_row(rid)["id"]
    _hrun("UPDATE skill_gate_requests SET ask_item_id = NULL WHERE request_id = :r", r=rid)
    _age(rid, "created_at")
    asyncio.run(_GATE.sweep())
    record = db.get_gate_request(rid)
    assert (record["state"], record["ask_item_id"]) == ("pending", item_id)
    assert gate.woken == []
    _answer(rid, _GATE.APPROVE)                 # and its ending is consumed as usual
    asyncio.run(_GATE.sweep())
    assert db.get_gate_request(rid)["state"] == "dispatched"


def test_a_record_whose_ask_is_gone_is_cancelled_and_frees_its_slot(gate):
    rid = _raise_request()
    _hrun("DELETE FROM operator_queue WHERE request_id = :r", r=rid)
    _age(rid, "created_at")
    assert db.count_pending_gate_requests(FIN) == 1
    asyncio.run(_GATE.sweep())
    record = db.get_gate_request(rid)
    assert (record["state"], record["state_detail"]) == ("cancelled", "approval_ask_missing")
    assert db.count_pending_gate_requests(FIN) == 0
    assert len(gate.woken) == 1 and "cancelled" in gate.woken[0][1]


def test_a_fresh_record_is_left_alone_by_the_missing_ask_pass(gate):
    """The cutoff is what keeps the sweep off a request being raised right now."""
    rid = _raise_request()
    _hrun("UPDATE skill_gate_requests SET ask_item_id = NULL WHERE request_id = :r", r=rid)
    asyncio.run(_GATE.sweep())
    assert db.get_gate_request(rid)["ask_item_id"] is None
    assert db.get_gate_request(rid)["state"] == "pending"


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------

def test_a_person_requester_gets_one_inbox_notice_addressed_to_them(gate):
    person = _GATE.Requester(kind="person", key="person:asker@example.com", email="asker@example.com")
    rid = _raise_request(requester=person)
    _answer(rid, _GATE.REJECT)
    _resolve(rid)
    notes = [r for r in _rows_like(_GATE.NOTE_PREFIX)]
    assert len(notes) == 1
    assert notes[0]["addressed_to_email"] == "asker@example.com"
    assert notes[0]["type"] == "alert"
    assert db.get_gate_request(rid)["notified_at"]


def _rows_like(prefix):
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(
            text("SELECT request_id, type, addressed_to_email FROM operator_queue "
                 "WHERE agent_name = :a AND request_id LIKE :p"), {"a": FIN, "p": prefix + "%"})]


# ---------------------------------------------------------------------------
# may_end — who decides a gated-skill approval (the ask sink)
# ---------------------------------------------------------------------------

def test_only_the_addressee_may_answer_a_gate_approval(gate):
    rid = _raise_request()
    with pytest.raises(_ASK.AskNotAddressee):
        _answer(rid, _GATE.APPROVE, email="someone-else@example.com")
    assert _ask_row(rid)["status"] == "pending"


def test_an_admin_may_cancel_but_not_approve(gate):
    rid = _raise_request()
    admin = _ASK.Actor(email="admin@example.com", user=SimpleNamespace(role="admin"))
    with pytest.raises(_ASK.AskNotAddressee):
        asyncio.run(_answer_as(rid, admin))
    ending = asyncio.run(_cancel_as(rid, admin))
    assert ending.rows[0]["status"] == "cancelled"


async def _answer_as(rid, actor):
    return _ASK.answer(_ask_row(rid), response=_GATE.APPROVE, response_text=None, actor=actor)


async def _cancel_as(rid, actor):
    return _ASK.cancel(_ask_row(rid)["id"], actor=actor)


def test_a_non_admin_who_is_not_addressed_cannot_cancel(gate):
    rid = _raise_request()
    with pytest.raises(_ASK.AskNotAddressee):
        asyncio.run(_cancel_as(rid, _ASK.Actor(email="someone-else@example.com")))


def test_a_bulk_cancel_skips_gate_approvals_the_actor_may_not_end(gate):
    rid = _raise_request()
    item_id = _ask_row(rid)["id"]

    async def _go():
        return _ASK.bulk_cancel([item_id], None, actor=_ASK.Actor(email="someone-else@example.com"))
    assert asyncio.run(_go()).rows == []
    assert _ask_row(rid)["status"] == "pending"


def test_other_asks_keep_their_rule(gate):
    row = {"raised_by": "agent", "type": "approval", "resolved_to": [OWNER_EMAIL]}
    assert _ASK.may_end(row, _ASK.Actor(email="anyone@example.com")) is True


# ---------------------------------------------------------------------------
# _dispatch_approved — the run starts like an async /task, or queues
# ---------------------------------------------------------------------------

_ORIGINAL_DISPATCH = _GATE._dispatch_approved


def _record_for_dispatch(rid):
    rec, _ = db.create_gate_request(
        request_id=rid, agent_name=FIN, skills=["pay-invoice"],
        request_text="/pay-invoice 100 EUR", requester_kind="agent",
        requester_key=f"agent:{REQ}", source_agent=REQ,
        dispatch={"triggered_by": "agent", "model": "sonnet", "source_agent_name": REQ,
                  "chain_depth": 2})
    return rec


class _Cap:
    def __init__(self, state="admitted", exc=None):
        self.state, self.exc, self.calls = state, exc, []

    async def acquire(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return SimpleNamespace(state=self.state, queue_position=None)


@pytest.fixture
def real_dispatch(gate, monkeypatch):
    """The real `_dispatch_approved`, over the real row writer; capacity and
    the background run recorded."""
    import services.capacity_manager as _CM
    import services.chat_execution_service as _CE
    import services.task_execution_service as _TES
    world = SimpleNamespace(cap=_Cap("admitted"), spawned=[])
    monkeypatch.setattr(_CM, "get_capacity_manager", lambda: world.cap)
    monkeypatch.setattr(_TES, "dispatch_breaker_active", lambda n: False)

    async def _run_async_task(**kw):
        world.spawned.append(kw)

    monkeypatch.setattr(_CE, "run_async_task", _run_async_task)
    monkeypatch.setattr(_GATE, "_dispatch_approved", _ORIGINAL_DISPATCH)
    return world


def _status(execution_id):
    status = db.get_execution(execution_id).status
    return getattr(status, "value", status)


def test_an_admitted_approved_run_starts_under_the_claimed_id(real_dispatch):
    async def _go():
        out = await _GATE._dispatch_approved(_record_for_dispatch("gate-d1"), "exec-approved-1")
        await asyncio.sleep(0)       # let the background run start
        return out
    assert asyncio.run(_go()) == "admitted"
    row = db.get_execution("exec-approved-1")
    assert (row.message, row.triggered_by) == ("/pay-invoice 100 EUR", "agent")
    assert real_dispatch.cap.calls[0]["overflow_policy"] == "queue_persistent"
    spawned = real_dispatch.spawned[0]
    assert (spawned["execution_id"], spawned["triggered_by_override"]) == ("exec-approved-1", "agent")
    assert spawned["request"].message == "/pay-invoice 100 EUR"


def test_a_busy_executor_queues_the_approved_run_instead_of_losing_it(real_dispatch):
    real_dispatch.cap.state = "queued"
    assert asyncio.run(_GATE._dispatch_approved(_record_for_dispatch("gate-d2"), "exec-approved-2")) == "queued"
    assert real_dispatch.spawned == []
    assert real_dispatch.cap.calls[0]["overflow_payload"].triggered_by == "agent"
    assert _status("exec-approved-2") == "running"


def test_a_full_backlog_fails_the_row_and_raises(real_dispatch):
    from services.capacity_manager import CapacityFull
    real_dispatch.cap.exc = CapacityFull(FIN, 1, "persistent_full")
    with pytest.raises(RuntimeError):
        asyncio.run(_GATE._dispatch_approved(_record_for_dispatch("gate-d3"), "exec-approved-3"))
    assert _status("exec-approved-3") == "failed"
    assert real_dispatch.spawned == []


@pytest.mark.parametrize("trigger, prompt", [("public", "CALLER-PROMPT"), ("slack", "CALLER-PROMPT"),
                                             ("agent", None)])
def test_an_approved_public_request_keeps_the_owners_caller_prompt(real_dispatch, monkeypatch,
                                                                   trigger, prompt):
    """A request from outside the operator's team runs under the owner's
    public-channel instructions, approved or not."""
    import services.platform_prompt_service as _PP
    monkeypatch.setattr(_PP, "build_public_channel_caller_prompt", lambda agent: "CALLER-PROMPT")
    rec, _ = db.create_gate_request(
        request_id=f"gate-pp-{trigger}", agent_name=FIN, skills=["pay-invoice"],
        request_text="/pay-invoice 1", requester_kind="public", requester_key="public:x",
        dispatch={"triggered_by": trigger})

    async def _go():
        await _GATE._dispatch_approved(rec, f"exec-pp-{trigger}")
        await asyncio.sleep(0)
    asyncio.run(_go())
    assert real_dispatch.spawned[0]["request"].system_prompt == prompt


# ---------------------------------------------------------------------------
# Agent deleted / renamed while a request waits
# ---------------------------------------------------------------------------

def test_deleting_the_executor_cancels_its_waiting_requests_and_tells_the_requester(gate):
    rid = _raise_request()
    n = asyncio.run(_GATE.cancel_pending_for_agent(FIN, actor_email=OWNER_EMAIL, reason="agent_deleted"))
    assert n == 1
    assert db.get_gate_request(rid)["state"] == "cancelled"
    assert _ask_row(rid)["status"] == "cancelled"
    assert "cancelled" in gate.woken[0][1]


def test_an_actor_who_may_not_end_the_ask_still_makes_its_approval_inert(gate):
    rid = _raise_request()
    asyncio.run(_GATE.cancel_pending_for_agent(FIN, actor_email="stranger@example.com",
                                               reason="agent_renamed"))
    assert db.get_gate_request(rid)["state"] == "cancelled"
    assert _ask_row(rid)["status"] == "pending"          # left open, but inert
    _answer(rid, _GATE.APPROVE)
    assert _resolve(rid) is None
    assert gate.dispatched == []


def test_an_agent_key_deleting_the_executor_cancels_the_record_but_never_ends_the_ask_as_its_owner(gate):
    """An agent key carries its owner's email; ending the ask under it would
    record a person's ending no person made."""
    from models import User
    rid = _raise_request()
    agent_key = User(id=1, username=OWNER, email=OWNER_EMAIL, role="user",
                     agent_name=REQ, mcp_scope="agent", mcp_key_id="k1")
    asyncio.run(_GATE.cancel_pending_for_agent(FIN, actor_email=OWNER_EMAIL, actor_user=agent_key,
                                               reason="agent_deleted"))
    assert db.get_gate_request(rid)["state"] == "cancelled"
    assert _ask_row(rid)["status"] == "pending"


def test_every_outcome_is_audited_once_with_ids_only(gate):
    rid = _raise_request()
    _answer(rid, _GATE.APPROVE)
    _resolve(rid)
    asyncio.run(_GATE.sweep())                      # a second pass writes nothing
    rows = _audit_rows(rid)
    assert len(rows) == 1
    details = json.loads(rows[0]["details"]) if isinstance(rows[0]["details"], str) else rows[0]["details"]
    assert (details["outcome"], details["execution_id"]) == (
        "approved", db.get_gate_request(rid)["dispatched_execution_id"])
    assert "request_text" not in details and "/pay-invoice" not in json.dumps(details)


def _audit_rows(rid):
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(
            text("SELECT event_action, details FROM audit_log WHERE event_action = 'skill_gate_outcome' "
                 "AND details LIKE :p"), {"p": f"%{rid}%"})]


# ---------------------------------------------------------------------------
# #715: a person's identity never reaches an agent (/cso finding 2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response", [_GATE.APPROVE, _GATE.REJECT])
def test_the_requesting_agent_is_told_the_outcome_never_who_decided(gate, response):
    rid = _raise_request()
    _answer(rid, response)
    _resolve(rid)
    (_requester, text) = gate.woken[0]
    assert OWNER_EMAIL not in text
    assert rid in text


def _agent_principal():
    from models import User
    return User(id=1, username=OWNER, email=OWNER_EMAIL, role="user",
                agent_name=REQ, mcp_scope="agent", mcp_key_id="k1")


def _gate_rows():
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(
            text("SELECT * FROM operator_queue WHERE agent_name = :a"), {"a": FIN})]


def test_a_machine_key_reads_no_gate_row_a_person_reads_them_all(gate):
    """The card names the person who asked and the notice the person who
    decided: the operator's, not a machine's (`_for_principal`)."""
    from models import User
    from routers.operator_queue import _for_principal
    person = _GATE.Requester(kind="person", key="person:asker@example.com",
                             email="asker@example.com")
    rid = _raise_request(requester=person)
    _answer(rid, _GATE.REJECT)
    _resolve(rid)                                    # adds the person's notice
    rows = _gate_rows()
    assert len(rows) == 2 and "asker@example.com" in json.dumps(rows, default=str)
    assert _for_principal(rows, _agent_principal()) == []
    human = User(id=1, username=OWNER, email=OWNER_EMAIL, role="user")
    assert len(_for_principal(rows, human)) == 2


def test_an_agent_cannot_read_a_gate_row_back_by_its_id(gate):
    """The executor can learn a request id from its own skipped run's error."""
    from fastapi import HTTPException
    from routers.operator_queue import get_my_ask
    rid = _raise_request()
    with pytest.raises(HTTPException) as info:
        asyncio.run(get_my_ask(request_id=rid, name=FIN))
    assert info.value.status_code == 404


# ---------------------------------------------------------------------------
# A person's notice (eyeball findings)
# ---------------------------------------------------------------------------

def _notes():
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(
            text("SELECT addressed_to_email, question FROM operator_queue "
                 "WHERE agent_name = :a AND request_id LIKE :p"), {"a": FIN, "p": _GATE.NOTE_PREFIX + "%"})]


def _owner_asks():
    return _GATE.Requester(kind="person", key=f"person:{OWNER_EMAIL}", email=OWNER_EMAIL)


@pytest.mark.parametrize("response", [_GATE.APPROVE, _GATE.REJECT])
def test_a_person_is_not_told_of_a_decision_they_made_themselves(gate, response):
    """They pressed Run now and decided it themselves: a notice is noise."""
    rid = _raise_request(requester=_owner_asks())
    _answer(rid, response)
    _resolve(rid)
    assert _notes() == []
    assert db.get_gate_request(rid)["notified_at"]          # still consumed once


def test_a_person_is_told_once_when_their_own_approval_could_not_run(gate):
    """Approved by themselves, but the skill changed: that outcome is news."""
    rid = _raise_request(requester=_owner_asks())
    gate.fingerprint = "fp-2"
    _answer(rid, _GATE.APPROVE)
    assert _resolve(rid) == "stale"
    assert [n["addressed_to_email"] for n in _notes()] == [OWNER_EMAIL]


def test_an_unknown_run_reaches_a_requester_who_also_decided_once(gate):
    rid = _raise_request(requester=_owner_asks())
    _answer(rid, _GATE.APPROVE)
    assert db.claim_gate_request_for_dispatch(rid, "exec-never-created")
    _age(rid, "decided_at")
    asyncio.run(_GATE.sweep())
    assert [n["addressed_to_email"] for n in _notes()] == [OWNER_EMAIL]


def test_a_persons_notice_reads_as_text_for_a_person(gate):
    person = _GATE.Requester(kind="person", key="person:asker@example.com", email="asker@example.com")
    rid = _raise_request(requester=person)
    _answer(rid, _GATE.REJECT)
    _resolve(rid)
    (note,) = _notes()
    assert note["question"].startswith("Your request ")       # no "[Trinity]" machine prefix


def test_an_agents_notice_keeps_its_platform_prefix(gate):
    rid = _raise_request()
    _answer(rid, _GATE.REJECT)
    _resolve(rid)
    assert gate.woken[0][1].startswith("[Trinity] Your request ")
