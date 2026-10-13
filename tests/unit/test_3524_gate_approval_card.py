"""The gated-skill approval card and its ending (trinity#3524, #3525, #3527).

- #3525: an approver's rejection note reaches the requester — the requesting
  agent's task and a person's Inbox notice — sanitised, one line and bounded.
  A rejection without a note reads as before.
- #3527: a person with no email (the built-in admin) is named on the card,
  never "A caller"; a request that carries chat history leads with the message
  typed this turn and marks the history as context.
- #3524: the Workspace projection carries the viewer's OWN answer and note;
  a recent answer to a gate ask names its request.

Driven over the real per-test database and the real ask sink (the shared gate
world of `test_ent751_gate_dispatch`).
"""
import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_harness import db_backend  # noqa: E402,F401
from test_ent751_gate_dispatch import (  # noqa: E402,F401 — the shared world
    FIN, OWNER_EMAIL, _ask_row, _raise_request, _resolve, gate,
)

import services.ask_service as _ASK  # noqa: E402
import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402

pytestmark = pytest.mark.unit


def _answer(rid, response, note=None):
    async def _go():
        return _ASK.answer(_ask_row(rid), response=response, response_text=note,
                           actor=_ASK.Actor(email=OWNER_EMAIL))
    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# #3525 — the rejection note reaches the requester
# ---------------------------------------------------------------------------

def test_a_rejection_note_reaches_the_requesting_agent(gate):
    rid = _raise_request()
    _answer(rid, _GATE.REJECT, note="Wrong customer — use the EU account")
    assert _resolve(rid) == "denied"
    text = gate.woken[0][1]
    assert "was rejected; nothing was run." in text
    # Quoted by the trinity-enterprise#755 reason path, which #3525 now rides.
    assert 'Reason given: "Wrong customer — use the EU account"' in text
    # #715: still never the approver's identity.
    assert OWNER_EMAIL not in text


def test_a_rejection_without_a_note_reads_as_before(gate):
    rid = _raise_request()
    _answer(rid, _GATE.REJECT)
    _resolve(rid)
    text = gate.woken[0][1]
    assert "note" not in text
    assert text.startswith("[Trinity] Rejected — your request")
    assert "was rejected; nothing was run. (Your request came from execution" in text


def test_the_note_is_sanitised_one_line_and_bounded():
    secret = "ghp_" + "a" * 36
    record = {"request_id": "gate-x", "agent_name": "fin", "skills": ["pay"]}
    text = _GATE.outcome_text(record, "denied",
                              reason=f"line one\n\nline two {secret} " + "x" * 2000)
    quoted = text[text.index("Reason given:"):]
    assert "\n" not in quoted
    assert secret not in quoted
    assert "line one line two" in quoted
    assert len(quoted) < _GATE.REJECT_REASON_MAX_CHARS + 80
    for empty in ("   ", None):
        assert "Reason given" not in _GATE.outcome_text(record, "denied", reason=empty)


def test_an_approval_never_carries_the_note(gate):
    rid = _raise_request()
    _answer(rid, _GATE.APPROVE, note="go ahead")
    assert _resolve(rid) == "dispatched"
    assert "go ahead" not in gate.woken[0][1]


def test_a_person_requesters_inbox_notice_carries_the_note(gate):
    person = _GATE.Requester(kind="person", key="person:asker@example.com", email="asker@example.com")
    rid = _raise_request(requester=person)
    _answer(rid, _GATE.REJECT, note="not this quarter")
    _resolve(rid)
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(
            text("SELECT question, addressed_to_email FROM operator_queue "
                 "WHERE agent_name = :a AND request_id LIKE :p"),
            {"a": FIN, "p": _GATE.NOTE_PREFIX + "%"})]
    assert len(rows) == 1
    assert rows[0]["addressed_to_email"] == "asker@example.com"
    assert '"not this quarter"' in rows[0]["question"]


# ---------------------------------------------------------------------------
# #3527 — who asked, and what
# ---------------------------------------------------------------------------

def _person(**over):
    from models import User
    fields = dict(id=1, username="admin", email=None, role="admin")
    fields.update(over)
    return _GATE.requester_from_principal(User(**fields))


def test_a_person_with_no_email_is_named_by_username():
    requester = _person()
    assert requester.is_person
    assert requester.display() == "admin"
    body = _GATE._ask_body(FIN, "gate-x", ["pay-invoice"], "/pay-invoice 100 EUR", requester,
                           {"pay-invoice": "fp"}, "owner", None)
    assert body["question"].startswith("admin asked ")
    assert body["proposal"]["requester"] == "admin"
    assert "A caller" not in body["question"]


def test_a_person_with_an_email_is_still_named_by_it():
    assert _person(email="ops@example.com").display() == "ops@example.com"


def test_a_chat_panel_request_leads_with_the_new_message():
    text = ("### Previous conversation:\n\nUser: hello\n\nAssistant: " + "long reply " * 80 +
            "\n\n### Current message:\n\nUser: /pay-invoice 100 EUR")
    assert _GATE.split_chat_history(text) == ("/pay-invoice 100 EUR", True)
    body = _GATE._ask_body(FIN, "gate-x", ["pay-invoice"], text, _person(),
                           {"pay-invoice": "fp"}, "owner", None)
    question = body["question"]
    assert "Request:\n/pay-invoice 100 EUR\n\n" in question
    assert "Previous conversation" not in question
    assert "earlier messages of the conversation as context" in question
    # What Approve runs is still the whole text, shown in full.
    assert body["proposal"]["input"] == text


def _raise_text(text):
    requester = _GATE.Requester(kind="agent", key="agent:chat-req", agent_name="chat-req",
                                execution_id="exec-chat")

    async def _go():
        try:
            await _GATE.enforce(FIN, request_text=text, requester=requester, triggered_by="agent",
                                dispatch={"triggered_by": "agent", "model": "sonnet"})
        except _GATE.SkillApprovalRequired as e:
            return e.request_id
        raise AssertionError("expected an approval")
    return asyncio.run(_go())


def test_the_card_leads_with_the_typed_request_when_history_came_with_it(gate):
    """The approval card (trinity-enterprise#755) leaves the question out, so the
    split has to reach `card_view` or the request-first fix is invisible there."""
    text = ("### Previous conversation:\n\nUser: hello\n\nAssistant: hi there"
            "\n\n### Current message:\n\nUser: /pay-invoice 100 EUR")
    view = _GATE.card_view(_ask_row(_raise_text(text)))
    assert view["request"] == "/pay-invoice 100 EUR"
    assert view["also_sent"] == ""


def test_a_plain_request_puts_nothing_in_the_card_request_row(gate):
    assert _GATE.card_view(_ask_row(_raise_request()))["request"] is None


def test_also_sent_still_reads_the_context_after_a_history_split():
    text = "Previous conversation:\nUser: hi\nAssistant: hello\n\nUser: /pay-invoice 5 EUR"
    body = _GATE._ask_body(FIN, "gate-x", ["pay-invoice"], text, _person(),
                           {"pay-invoice": "fp"}, "owner", None, card_context="Ticket 42 is open")
    assert _GATE._also_sent(body["question"], FIN, text) == "Ticket 42 is open"


def test_a_mobile_request_leads_with_the_new_message():
    text = "Previous conversation:\nUser: hi\nAssistant: hello\n\nUser: /pay-invoice 5 EUR"
    assert _GATE.split_chat_history(text) == ("/pay-invoice 5 EUR", True)


def test_a_plain_request_is_unchanged():
    assert _GATE.split_chat_history("/pay-invoice 100 EUR") == ("/pay-invoice 100 EUR", False)
    body = _GATE._ask_body(FIN, "gate-x", ["pay-invoice"], "/pay-invoice 100 EUR", _person(),
                           {"pay-invoice": "fp"}, "owner", None)
    assert "as context" not in body["question"]


def test_an_ambiguous_split_shows_more_as_the_request_never_less():
    # A message that repeats the marker cannot move its own words into the
    # part labelled context: the FIRST marker splits.
    text = ("### Previous conversation:\n\nUser: hi\n\n### Current message:\n\nUser: benign\n\n"
            "### Current message:\n\nUser: tail")
    message, _ = _GATE.split_chat_history(text)
    assert message.startswith("benign")
    assert message.endswith("tail")


# ---------------------------------------------------------------------------
# #3524 — the viewer's own answer on the Workspace projection
# ---------------------------------------------------------------------------

def test_the_projection_carries_the_viewers_own_answer_and_note(gate):
    from client_portal.asks import service as _WS
    rid = _raise_request()
    _answer(rid, _GATE.REJECT, note="wrong customer")
    ask = _WS._project(_ask_row(rid), viewer_email=OWNER_EMAIL)
    assert ask.ended_by == "you"
    assert ask.answer == "Reject"
    assert ask.answer_note == "wrong customer"


def test_someone_elses_answer_is_never_projected(gate):
    from client_portal.asks import service as _WS
    rid = _raise_request()
    _answer(rid, _GATE.REJECT, note="wrong customer")
    ask = _WS._project(_ask_row(rid), viewer_email="someone-else@example.com")
    assert ask.ended_by != "you"
    assert ask.answer is None and ask.answer_note is None


def test_a_pending_ask_has_no_answer(gate):
    from client_portal.asks import service as _WS
    ask = _WS._project(_ask_row(_raise_request()), viewer_email=OWNER_EMAIL)
    assert ask.answer is None and ask.answer_note is None


def test_a_recent_gate_answer_names_its_request(gate):
    from client_portal.asks import service as _WS
    from client_portal.chat_previews import _arrival_excerpt
    row = _ask_row(_raise_request())
    assert _WS._request_line(row, _arrival_excerpt) == "/pay-invoice 100 EUR"
    assert _WS._request_line({"proposal": {"kind": "other", "input": "x"}}, _arrival_excerpt) is None
    assert _WS._request_line({"proposal": None}, _arrival_excerpt) is None
    history = ("### Previous conversation:\n\nUser: hi\n\n### Current message:\n\nUser: deploy "
               + "staging " * 40)
    line = _WS._request_line({"proposal": {"kind": "gated_skill", "input": history}}, _arrival_excerpt)
    assert line.startswith("deploy staging")
    assert len(line) <= _WS.RECENT_ANSWER_REQUEST_MAX
