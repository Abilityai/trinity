"""
Tests for the reserved off-menu approval answer "(something else)" (#3242).

An approval was a closed menu: the #2376 sink refused every unoffered string.
`SOMETHING_ELSE` is the one platform-reserved decision every approval accepts,
with the person's instruction in `response_text`; every other unoffered string
stays refused. Related flow: docs/memory/feature-flows/operating-room.md
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

from services.operator_queue_choices import (  # noqa: E402
    OPTIONS_DROPPED_MARKER,
    SOMETHING_ELSE,
    InstructionRequiredError,
    NotOffMenuError,
    ReservedValueError,
    ResponseNotOfferedError,
    usable_options,
    validate_response_choice,
)


def _approval(options, **over):
    return {"id": "q1", "agent_name": "a1", "type": "approval", "status": "pending",
            "options": options, **over}


# ---------------------------------------------------------------------------
# The sink
# ---------------------------------------------------------------------------

class TestTheSink:
    def test_accepted_on_any_approval_with_an_instruction(self):
        validate_response_choice(_approval(["Approve", "Deny"]), SOMETHING_ELSE,
                                 response_text="Ship it to staging first")

    @pytest.mark.parametrize("blank", [None, "", "   \n\t"])
    def test_refused_without_an_instruction(self, blank):
        with pytest.raises(InstructionRequiredError) as e:
            validate_response_choice(_approval(["Approve", "Deny"]), SOMETHING_ELSE,
                                     response_text=blank)
        assert e.value.code == "instruction_required"

    def test_a_random_unoffered_string_is_still_refused(self):
        with pytest.raises(ResponseNotOfferedError):
            validate_response_choice(_approval(["Approve", "Deny"]), "something else",
                                     response_text="do X")

    @pytest.mark.parametrize("kind", ["question", "alert", "mystery", None])
    def test_reserved_on_anything_but_an_approval(self, kind):
        with pytest.raises(ReservedValueError) as e:
            validate_response_choice({"id": "q", "type": kind}, SOMETHING_ELSE,
                                     response_text="do X")
        assert e.value.code == "reserved_value"

    def test_an_only_literal_item_stays_closed_to_everything_else(self):
        item = _approval([SOMETHING_ELSE])
        # Never filtered out: the item still constrains its answers.
        assert usable_options(item) == [SOMETHING_ELSE]
        validate_response_choice(item, SOMETHING_ELSE, response_text="do X")
        with pytest.raises(ResponseNotOfferedError):
            validate_response_choice(item, "Approve", response_text=None)

    def test_an_agent_offered_twin_folds_into_the_reserved_meaning(self):
        with pytest.raises(InstructionRequiredError):
            validate_response_choice(_approval(["Approve", SOMETHING_ELSE]),
                                     SOMETHING_ELSE, response_text="")

    def test_a_capped_item_accepts_the_literal_with_text(self):
        validate_response_choice(_approval([OPTIONS_DROPPED_MARKER]), SOMETHING_ELSE,
                                 response_text="do X")

    def test_response_text_is_keyword_only_and_required(self):
        with pytest.raises(TypeError):
            validate_response_choice(_approval(["Approve"]), "Approve")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            validate_response_choice(_approval(["Approve"]), "Approve", None)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The ask sink: gate / platform-minted approvals, and the raise door
# ---------------------------------------------------------------------------

class _NoWriteDb:
    def respond_to_operator_queue_item(self, **kw):  # pragma: no cover - must not run
        raise AssertionError("the refusal must come before the write")


class TestAskSink:
    def test_the_literal_on_a_gate_approval_is_refused_before_any_write(self, monkeypatch):
        from services import ask_service
        monkeypatch.setattr(ask_service, "db", _NoWriteDb())
        item = _approval(["Approve", "Deny"], id="gate-abc", request_id="gate-abc")
        with pytest.raises(NotOffMenuError) as e:
            ask_service.answer(item, response=SOMETHING_ELSE, response_text="do X",
                               actor=ask_service.Actor(email="p@example.com"))
        assert e.value.code == "not_off_menu"

    def test_an_offered_option_on_a_gate_approval_is_not_affected(self, monkeypatch):
        from services import ask_service
        written = {}

        class _Db:
            def respond_to_operator_queue_item(self, **kw):
                written.update(kw)
                return None  # -> AskNotFound; enough to prove the write was reached

        monkeypatch.setattr(ask_service, "db", _Db())
        monkeypatch.setattr(ask_service, "may_end", lambda *a, **k: True)
        item = _approval(["Approve", "Deny"], id="gate-abc", request_id="gate-abc")
        with pytest.raises(ask_service.AskNotFound):
            ask_service.answer(item, response="Approve", response_text=None,
                               actor=ask_service.Actor(email="p@example.com"))
        assert written["response"] == "Approve"

    def test_the_literal_with_text_reaches_the_write_verbatim(self, monkeypatch):
        from services import ask_service
        written = {}

        class _Db:
            def respond_to_operator_queue_item(self, **kw):
                written.update(kw)
                return None

        monkeypatch.setattr(ask_service, "db", _Db())
        monkeypatch.setattr(ask_service, "may_end", lambda *a, **k: True)
        with pytest.raises(ask_service.AskNotFound):
            ask_service.answer(_approval(["Approve", "Deny"]), response=SOMETHING_ELSE,
                               response_text="Use the blue bucket",
                               actor=ask_service.Actor(email="p@example.com"))
        assert written["response"] == SOMETHING_ELSE
        assert written["response_text"] == "Use the blue bucket"

    def test_raise_refuses_an_option_equal_to_the_literal(self):
        from services import ask_service
        from services import operator_queue_service as oqs
        with pytest.raises(ask_service.AskRejected) as e:
            ask_service._validated_ask(
                {"request_id": "approval-x-1", "type": "approval", "title": "t",
                 "options": ["Approve", SOMETHING_ELSE]}, oqs)
        assert e.value.code == "invalid_options"


# ---------------------------------------------------------------------------
# Both writers name the refusal
# ---------------------------------------------------------------------------

class TestWriters:
    @pytest.mark.parametrize("item,text,code", [
        (_approval(["Approve", "Deny"]), "  ", "instruction_required"),
        ({"id": "q1", "agent_name": "a1", "type": "question", "status": "pending"},
         "do X", "reserved_value"),
        (_approval(["Approve", "Deny"], id="gate-q1", request_id="gate-q1"),
         "do X", "not_off_menu"),
    ])
    def test_the_operator_route_answers_a_named_422(self, monkeypatch, item, text, code):
        from fastapi import HTTPException
        from models import OperatorResponse
        from routers import operator_queue as r
        from services import ask_service
        monkeypatch.setattr(ask_service, "db", _NoWriteDb())
        monkeypatch.setattr(r, "reject_non_person_principal", lambda u: None)
        monkeypatch.setattr(r.db, "get_operator_queue_item", lambda i: item)
        monkeypatch.setattr(r, "_accessible_set", lambda u: None)
        monkeypatch.setattr(r, "_assert_agent_accessible", lambda *a: None)
        monkeypatch.setattr(r, "_actor", lambda u, req: ask_service.Actor(email="p@example.com"))
        user = type("U", (), {"id": 1})()
        body = OperatorResponse(response=SOMETHING_ELSE, response_text=text)
        with pytest.raises(HTTPException) as e:
            asyncio.run(r.respond_to_queue_item(item["id"], body, None, user))
        assert e.value.status_code == 422
        assert e.value.detail["code"] == code

    @pytest.mark.parametrize("item,text,code", [
        (_approval(["Approve", "Deny"]), "", "instruction_required"),
        (_approval(["Approve", "Deny"], id="gate-q1", request_id="gate-q1"),
         "do X", "not_off_menu"),
    ])
    def test_the_workspace_path_answers_a_named_422(self, monkeypatch, item, text, code):
        from client_portal.asks import service as asks
        from services import ask_service
        monkeypatch.setattr(ask_service, "db", _NoWriteDb())
        monkeypatch.setattr(asks, "_owned_ask", lambda *a, **k: item)
        with pytest.raises(asks.AskError) as e:
            asks.answer_ask(item["id"], "p@example.com", False, SOMETHING_ELSE, text)
        assert e.value.status_code == 422
        assert e.value.code == code

    def test_operator_response_text_is_bounded_like_the_workspace_answer(self):
        from pydantic import ValidationError
        from models import OperatorResponse
        OperatorResponse(response=SOMETHING_ELSE, response_text="x" * 4000)
        with pytest.raises(ValidationError):
            OperatorResponse(response=SOMETHING_ELSE, response_text="x" * 4001)


# ---------------------------------------------------------------------------
# Consumers: the Workspace projection, recent answers, the resume frame
# ---------------------------------------------------------------------------

class TestConsumers:
    def _project(self, monkeypatch, item):
        from client_portal.asks import service as asks
        monkeypatch.setattr(asks, "_ending_of", lambda *a, **k: (None, None))
        return asks._project({"created_at": "2026-10-05T00:00:00Z", **item})

    def test_a_gate_approval_is_marked_decided_by_its_options(self, monkeypatch):
        ask = self._project(monkeypatch, _approval(["Approve", "Deny"], id="gate-q1",
                                                   request_id="gate-q1"))
        assert ask.decided_by_options is True

    @pytest.mark.parametrize("item", [
        _approval(["Approve", "Deny"]),
        {"id": "gate-q2", "agent_name": "a1", "type": "question", "status": "pending"},
    ])
    def test_every_other_ask_is_not(self, monkeypatch, item):
        assert self._project(monkeypatch, item).decided_by_options is False

    def test_the_marker_is_a_bare_boolean(self):
        from client_portal.asks.models import WorkspaceAsk
        assert WorkspaceAsk.model_fields["decided_by_options"].annotation is bool

    def test_recent_answers_never_show_the_literal_raw(self):
        from client_portal.asks import service as asks
        from client_portal.chat_previews import _arrival_excerpt
        label = asks._answer_label(
            {"response": SOMETHING_ELSE, "response_text": "Use the blue bucket"},
            _arrival_excerpt)
        assert label == "Something else: Use the blue bucket"
        assert asks._answer_label({"response": "Approve"}, _arrival_excerpt) == "Approve"

    def test_the_resume_frame_says_it_above_the_fence(self):
        from services.operator_resume_service import _framed_message
        msg = _framed_message({"id": "q1", "question": "Deploy?"}, SOMETHING_ELSE,
                              "Use the blue bucket")
        fence = msg.index("[Operator answer")
        sentence = msg.index("carry out none of them")
        assert sentence < fence
        assert f"answer: {SOMETHING_ELSE}" in msg[fence:]
        assert "notes: Use the blue bucket" in msg[fence:]

    def test_an_ordinary_answer_frame_is_unchanged(self):
        from services.operator_resume_service import _framed_message
        msg = _framed_message({"id": "q1", "question": "Deploy?"}, "Approve", None)
        assert "carry out none of them" not in msg
        assert "answer: Approve" in msg


# ---------------------------------------------------------------------------
# One literal, mirrored (the vendored-mirror convention, Invariant #5 spirit)
# ---------------------------------------------------------------------------

_MIRRORS = {
    "src/mcp-server/src/types.ts": r'export const SOMETHING_ELSE = "([^"]*)";',
}


@pytest.mark.parametrize("rel,pattern", sorted(_MIRRORS.items()))
def test_the_literal_is_one_definition(rel, pattern):
    """Source-text pin, deliberately: the mirrors' live consumers are the MCP
    server and the SPA, which cannot import the backend leaf; their own suites
    execute the constant. This pins that the spelling never forks."""
    import re
    m = re.search(pattern, (_REPO / rel).read_text(encoding="utf-8"))
    assert m, f"{rel} lost its SOMETHING_ELSE mirror"
    assert m.group(1) == SOMETHING_ELSE
