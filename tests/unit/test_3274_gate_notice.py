"""
Gated skills — what the requester is told (trinity#3274 items 8 and 10, trinity#3233).

* **Where the outcome goes.** `outcome_delivery` is the ONE rule for whether the
  backend tells a requester the outcome: a task to a requesting agent, an Inbox
  notice to a person, nothing to anyone else (a system key, a connector, a
  schedule, a channel user, a public visitor, a paid caller). The pending answer
  carries it, and its message says so when nothing will come back — it used to
  promise nothing while the MCP layer promised delivery to everyone (#3233).
* **Never the approver's email.** The requester's notice named the decider by
  email, including to an external Workspace client. Now: the decider's display
  name for a request a platform session or key made, "the agent's approver" for
  everyone else and whenever no usable name exists.

Driven through `enforce`, the `/task` seam, the real ask sink and `resolve`.
"""
import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parent))   # `_gate_world`, as `_route_census`

import _gate_world as W  # noqa: E402
import services.ask_service as _ASK  # noqa: E402
import services.skill_gate_service as _GATE  # noqa: E402
from config import PORTAL_SOURCE_CHANNEL  # noqa: E402
from database import db  # noqa: E402
from services.skill_gate_errors import SkillApprovalRequired  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def world(db_backend, monkeypatch):
    return W.build(monkeypatch, owner_name="Dana Lee")


# ---------------------------------------------------------------------------
# #3233 — the pending answer says whether anything will come back
# ---------------------------------------------------------------------------

def test_a_system_key_is_told_where_the_outcome_will_be_and_promised_nothing(world):
    with pytest.raises(SkillApprovalRequired) as info:
        W.task(W.system_key(world))
    body = info.value.detail()
    assert body["outcome_delivery"] == "none"
    assert body["message"].endswith(
        f"Nothing will be sent back when it is decided: if it is approved, it runs as a new "
        f"execution on {W.FIN}; if not, nothing runs.")
    assert info.value.request_id in body["message"]


@pytest.mark.parametrize("principal, delivery", [
    (W.agent_key, "agent_task"),
    (W.other_person, "inbox"),
])
def test_a_requester_the_backend_will_tell_is_not_told_otherwise(world, principal, delivery):
    with pytest.raises(SkillApprovalRequired) as info:
        W.task(principal(world))
    assert info.value.detail()["outcome_delivery"] == delivery
    assert "Nothing will be sent back" not in info.value.message


def test_a_retry_of_the_same_request_keeps_its_delivery(world):
    for _ in range(2):
        with pytest.raises(SkillApprovalRequired) as info:
            W.task(W.system_key(world), idem="idem-retry")
    assert info.value.detail()["outcome_delivery"] == "none"


def test_a_schedule_rows_skipped_text_is_the_notice_itself(world):
    from db.write_params import ExecutionSource
    row = db.create_schedule_execution("s1", W.FIN, "/pay-invoice", "schedule", ExecutionSource())
    with pytest.raises(SkillApprovalRequired) as info:
        W.execute(message="/pay-invoice", execution_id=row.id)
    assert db.get_execution(row.id).error == info.value.message


_KINDS = [
    dict(requester_kind="agent", source_agent=W.REQ),
    dict(requester_kind="person", requester_email="asker-3274@example.com"),
    dict(requester_kind="schedule"), dict(requester_kind="channel", requester_email="c@example.com"),
    dict(requester_kind="public", requester_email="v@example.com"), dict(requester_kind="paid"),
    dict(requester_kind="connector"), dict(requester_kind="other"),
]


@pytest.mark.parametrize("kind", _KINDS, ids=[k["requester_kind"] for k in _KINDS])
def test_notify_tells_exactly_the_requesters_the_rule_names(world, kind):
    rid = f"gate-kind-{kind['requester_kind']}"
    rec, _ = db.create_gate_request(request_id=rid, agent_name=W.FIN, skills=["pay-invoice"],
                                    request_text="/pay-invoice", requester_key="k:" + rid,
                                    dispatch={"triggered_by": "agent"}, **kind)
    asyncio.run(_GATE._notify(rec, "denied", decider=W.OWNER_EMAIL))
    told = bool(world.woken) or bool(_notes())
    rule = _GATE.outcome_delivery(rec["requester_kind"], agent_name=rec.get("source_agent"),
                                  email=rec.get("requester_email"))
    assert told == (rule != "none"), (rule, world.woken, _notes())


# ---------------------------------------------------------------------------
# Item 8 — the decider is never named by email
# ---------------------------------------------------------------------------

def _notes():
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(
            text("SELECT addressed_to_email, question FROM operator_queue "
                 "WHERE agent_name = :a AND request_id LIKE :p"),
            {"a": W.FIN, "p": _GATE.NOTE_PREFIX + "%"})]


def _decide(rid, response=_GATE.REJECT, before_resolve=None):
    row = db.get_operator_queue_item_for_agent_by_request_id(W.FIN, rid)
    _ASK.answer(row, response=response, response_text=None, actor=_ASK.Actor(email=W.OWNER_EMAIL))
    if before_resolve:
        before_resolve()
    asyncio.run(_GATE.resolve(rid))
    (note,) = _notes()
    return note["question"]


def test_a_platform_requester_reads_the_deciders_display_name(world):
    with pytest.raises(SkillApprovalRequired) as info:
        W.task(W.other_person(world))
    text = _decide(info.value.request_id)
    assert "rejected by Dana Lee;" in text
    assert "@" not in text


def test_a_decider_with_no_usable_name_is_the_agents_approver(world):
    db.update_user(W.OWNER, {"name": "dana@example.com"})       # an address is not a name
    with pytest.raises(SkillApprovalRequired) as info:
        W.task(W.other_person(world))
    text = _decide(info.value.request_id)
    assert "rejected by the agent's approver;" in text and "@" not in text


def test_a_workspace_client_reads_the_agents_approver_even_when_a_name_exists(world):
    requester = _GATE.Requester(kind="person", key="person:client@example.com",
                                email="client@example.com")

    async def _go():
        await _GATE.enforce(W.FIN, request_text="/pay-invoice 2 EUR", requester=requester,
                            triggered_by="public", gates=world.gates,
                            dispatch={"triggered_by": "public", "source_channel": PORTAL_SOURCE_CHANNEL,
                                      "source_user_email": "client@example.com"})
    with pytest.raises(SkillApprovalRequired) as info:
        asyncio.run(_go())
    text = _decide(info.value.request_id)
    assert "rejected by the agent's approver;" in text
    assert "Dana Lee" not in text and W.OWNER_EMAIL not in text


def test_an_unreadable_account_falls_back_to_the_agents_approver(world, monkeypatch):
    with pytest.raises(SkillApprovalRequired) as info:
        W.task(W.other_person(world))

    def _boom(email):
        raise RuntimeError("db down")

    text = _decide(info.value.request_id,
                   before_resolve=lambda: monkeypatch.setattr(db, "get_user_by_email", _boom))
    assert "rejected by the agent's approver;" in text
