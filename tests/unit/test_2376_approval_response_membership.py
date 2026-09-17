"""#2376 — an approval's recorded decision must be one the agent offered.

`OperatorResponse.response` is a bare `str` and every layer passed it through
verbatim, so nothing checked membership. #2370 shipped `response: "approved"`
against `options: ["Approve", "Deny"]` for five months without a single 4xx —
the agent read back a decision string it never offered.

This is the approval channel for irreversible actions, with four producers
today, so the check belongs at the SINK rather than in whichever router happens
to be next. The suite therefore pins three things: the rule itself, that BOTH
entry points reach it, and that no future third writer can skip it.
"""
from __future__ import annotations

import ast
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
    ResponseNotOfferedError,
    usable_options,
    validate_response_choice,
)


def _approval(options):
    return {"id": "q1", "type": "approval", "status": "pending", "options": options}


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

def test_the_exact_2370_case_is_refused():
    """The case that shipped: `"approved"` against `["Approve", "Deny"]`."""
    with pytest.raises(ResponseNotOfferedError) as e:
        validate_response_choice(_approval(["Approve", "Deny"]), "approved")
    assert e.value.code == "response_not_an_offered_option"
    # The refusal must NAME the options — they are agent-authored, so an
    # operator cannot guess them from a bare "invalid".
    assert e.value.options == ["Approve", "Deny"]
    assert "Approve" in str(e.value)


def test_an_offered_option_is_accepted():
    validate_response_choice(_approval(["Approve", "Deny"]), "Approve")
    validate_response_choice(_approval(["Approve", "Deny"]), "Deny")


def test_matching_is_exact_because_the_options_are_agent_authored():
    """Only the agent knows whether `approve` and `Approve` mean the same thing
    to it; normalising here would answer that on its behalf, and the recorded
    decision has to be one it can compare against its own list."""
    for near_miss in ("approve", "APPROVE", " Approve", "Approve "):
        with pytest.raises(ResponseNotOfferedError):
            validate_response_choice(_approval(["Approve", "Deny"]), near_miss)


# ---------------------------------------------------------------------------
# Everything that must stay answerable (AC #2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("item", [
    pytest.param({"type": "question", "options": ["a", "b"]}, id="question"),
    pytest.param({"type": "alert", "options": ["a", "b"]}, id="alert"),
    pytest.param({"type": "approval", "options": None}, id="approval-no-options"),
    pytest.param({"type": "approval", "options": []}, id="approval-empty-options"),
    pytest.param({"type": "approval", "options": "Approve"}, id="options-not-a-list"),
    pytest.param({"type": "approval", "options": [1, 2]}, id="options-not-strings"),
    pytest.param({}, id="no-type"),
])
def test_items_that_do_not_constrain_the_answer_are_unaffected(item):
    validate_response_choice(item, "anything at all")
    assert usable_options(item) is None


def test_the_size_cap_marker_is_not_an_offered_choice():
    """#1632 replaces an oversize options blob with a placeholder. It records
    that the choices were DROPPED — treating it as an offered option would let
    the placeholder itself be recorded as a decision, and treating the item as
    constrained would make it unanswerable."""
    item = _approval([OPTIONS_DROPPED_MARKER])
    assert usable_options(item) is None
    validate_response_choice(item, "Approve")          # answerable
    validate_response_choice(item, OPTIONS_DROPPED_MARKER)


def test_the_marker_alongside_real_options_leaves_the_real_ones_binding():
    item = _approval(["Approve", OPTIONS_DROPPED_MARKER])
    assert usable_options(item) == ["Approve"]
    validate_response_choice(item, "Approve")
    with pytest.raises(ResponseNotOfferedError):
        validate_response_choice(item, OPTIONS_DROPPED_MARKER)


@pytest.mark.parametrize("empty", [None, ""])
def test_a_text_only_answer_is_not_rejected(empty):
    """Both entry points allow an answer carried entirely by `response_text`,
    and the asks path has its own `empty_answer` refusal for a truly empty one.
    Turning a text-only answer into a 422 would break a working path in the name
    of validating a field nobody filled in."""
    validate_response_choice(_approval(["Approve", "Deny"]), empty)


def test_acknowledged_items_never_reach_the_rule():
    """AC #2 names `acknowledged` explicitly. Both entry points refuse a
    non-pending item BEFORE the validator, so the exemption is structural."""
    import inspect
    from routers import operator_queue as r
    src = inspect.getsource(r.respond_to_queue_item)
    assert src.index("!= \"pending\"") < src.index("validate_response_choice")


# ---------------------------------------------------------------------------
# Both entry points, and no third one (AC #3)
# ---------------------------------------------------------------------------

_WRITER = "respond_to_operator_queue_item"

_EXPECTED_CALLERS = {
    "routers/operator_queue.py": "the operator route (JWT / MCP)",
    "client_portal/asks/service.py": "the Workspace asks answer path",
}


def _writer_callers() -> dict:
    found = {}
    for path in (_REPO / "src" / "backend").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == _WRITER:
                found[str(path.relative_to(_REPO / "src" / "backend"))] = True
    return found


def test_every_writer_of_a_response_validates_it_first():
    """The #1677 caller-parity shape. A validator both routers happen to call is
    one PR away from a third writer that does not — and this sink is the
    approval channel for irreversible actions, so the failure is silent and
    consequential. A new caller must be added to `_EXPECTED_CALLERS` and must
    validate, or this fails at CI."""
    callers = _writer_callers()
    assert set(callers) == set(_EXPECTED_CALLERS), (
        f"writers of {_WRITER} changed: {sorted(callers)}. Every one must call "
        f"validate_response_choice first (#2376); add it here with its reason."
    )
    for rel in callers:
        src = (_REPO / "src" / "backend" / rel).read_text(encoding="utf-8")
        assert "validate_response_choice" in src, (
            f"{rel} writes an operator-queue response without validating it "
            f"against the item's offered options (#2376)"
        )


def test_the_dropped_marker_has_one_definition():
    """The validator must exempt the same string the ingestion clamp writes. Two
    literals drift the day either side is reworded, and the sink would then
    accept a placeholder as a decision."""
    from services import operator_queue_service as svc
    assert svc._OPTIONS_DROPPED_MARKER is OPTIONS_DROPPED_MARKER


def test_both_entry_points_surface_a_named_422_listing_the_options():
    """AC #1 and #4: the refusal has to be actionable at the HTTP boundary, not
    just raised. The MCP tool stringifies the raw body into its structured
    error, so the options reach the agent."""
    import inspect
    from routers import operator_queue as r
    from client_portal.asks import service as asks

    op_src = inspect.getsource(r.respond_to_queue_item)
    assert "offered_options" in op_src and "422" in op_src

    ask_src = inspect.getsource(asks.answer_ask)
    assert "offered_options" in ask_src
    assert "e.code" in ask_src, "the asks refusal must carry the shared code"
