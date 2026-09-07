"""trinity-enterprise#499 — a thumbs-down reaches the operator, not the agent.

ent#366 writes the rating and, on down+comment, hands the words to the agent's
own `capture-feedback` skill. Nothing reached the person who runs the instance.
This is that half.

The properties worth guarding are the ones a diff cannot show: that the emitter
is BOUNDED (a client can click), that it is deduped on identity rather than on
the text, that the operator's copy is not gated on the client having bothered to
type anything, and that the agent-facing redaction is untouched.
"""
import asyncio

import pytest

pytestmark = pytest.mark.unit


# --- the id ------------------------------------------------------------------

def test_one_item_per_person_per_target():
    from client_portal.service import _problem_report_id
    a = _problem_report_id("workspace:a@example.com", "message", "m1")
    b = _problem_report_id("workspace:a@example.com", "message", "m1")
    assert a == b


def test_the_id_does_not_move_with_the_comment():
    """`_problem_report_id` takes no comment at all — the signature IS the
    guarantee. A key that moves with the text is not a dedup, it is a rename of
    the attack (`claim_capture_feedback_dispatch`'s rule)."""
    import inspect
    from client_portal.service import _problem_report_id
    assert set(inspect.signature(_problem_report_id).parameters) == {
        "evaluator", "target_kind", "target_id"}


def test_different_people_and_different_targets_are_different_items():
    from client_portal.service import _problem_report_id
    base = _problem_report_id("workspace:a@example.com", "message", "m1")
    assert base != _problem_report_id("workspace:b@example.com", "message", "m1")
    assert base != _problem_report_id("workspace:a@example.com", "message", "m2")
    assert base != _problem_report_id("workspace:a@example.com", "deliverable", "m1")


def test_the_id_carries_the_reserved_prefix_and_survives_the_sink_validator():
    """The prefix is reserved so an agent cannot pre-create the id of a complaint
    about ITSELF and silence it through ON CONFLICT. `_ID_RE` is the sink's own
    validator, and an email is neither bounded nor confined to its alphabet —
    which is why the id is hashed rather than interpolated."""
    from client_portal.service import _problem_report_id
    from services.operator_queue_service import _ID_RE, _RESERVED_ID_PREFIXES

    ident = _problem_report_id("workspace:Ünicode+weird/name@ex.com", "message", "m 1")
    assert ident.startswith("workspace-problem-")
    assert any(ident.startswith(p) for p in _RESERVED_ID_PREFIXES)
    assert _ID_RE.match(ident), "the id would be refused at the db sink"


def test_no_client_address_appears_in_the_id():
    from client_portal.service import _problem_report_id
    assert "a@example.com" not in _problem_report_id(
        "workspace:a@example.com", "message", "m1")


# --- the emitter -------------------------------------------------------------

def _run(monkeypatch, *, comment, created=True, raises=False):
    from client_portal import service
    import services.operator_queue_service as oqs

    seen = {}

    async def _fake(agent_name, item):
        if raises:
            raise RuntimeError("queue down")
        seen["agent_name"] = agent_name
        seen["item"] = item
        return created

    monkeypatch.setattr(oqs, "create_bounded_alert", _fake)
    ok = asyncio.run(service.raise_problem_report(
        "analyst", "client@example.com",
        target_kind="message", target_id="m1", comment=comment))
    return ok, seen


def test_a_bare_thumbs_down_still_reaches_the_operator(monkeypatch):
    """The operator's copy is NOT gated on a comment. 'This was not useful' is
    the report; the words are the elaboration. Gating on them would mean the
    quietest complaints — a bare thumb, which is what most people leave — reach
    nobody."""
    ok, seen = _run(monkeypatch, comment=None)
    assert ok is True
    assert "They left no comment." in seen["item"]["question"]


def test_the_comment_reaches_the_operator(monkeypatch):
    ok, seen = _run(monkeypatch, comment="it made up a number")
    assert ok is True
    assert "it made up a number" in seen["item"]["question"]


def test_the_comment_is_truncated_for_the_queue(monkeypatch):
    from client_portal.service import PROBLEM_REPORT_COMMENT_CHARS
    _, seen = _run(monkeypatch, comment="x" * 5000)
    assert len(seen["item"]["question"]) < 5000
    assert "x" * (PROBLEM_REPORT_COMMENT_CHARS + 1) not in seen["item"]["question"]


def test_the_words_are_never_put_in_the_context_blob(monkeypatch):
    """`context` is JSON-dumped into a column the operator queue renders and the
    agent's own queue file is synced against. The comment belongs in the
    operator-facing question only — `has_comment` is the machine-readable half
    (the G-04 rule: identifiers, never content)."""
    _, seen = _run(monkeypatch, comment="something sensitive")
    ctx = seen["item"]["context"]
    assert ctx["has_comment"] is True
    assert "something sensitive" not in str(ctx)


def test_the_item_is_typed_for_its_own_budget(monkeypatch):
    """Not the generic `alert`: the budget counts PENDING ROWS OF THAT TYPE
    including ones other emitters wrote, so reusing `alert` would let five
    unrelated alerts on this agent silence every problem report."""
    from services.operator_queue_service import _BUDGETED_ALERT_TYPES
    _, seen = _run(monkeypatch, comment="x")
    assert seen["item"]["type"] == "workspace_problem_report"
    assert seen["item"]["type"] in _BUDGETED_ALERT_TYPES


def test_it_says_nothing_is_waiting_on_an_answer(monkeypatch):
    """ent#329 HAS shipped, so the AC's 'acted on at the next wake-up' caveat is
    spent — but this is an alert, and `operator_resume_enabled` is per-agent and
    off by default, so a sentence promising a re-trigger would be wrong on most
    installs. It says the true thing instead."""
    _, seen = _run(monkeypatch, comment="x")
    assert "Nothing is waiting on a reply" in seen["item"]["question"]


def test_it_tells_the_operator_the_agent_cannot_read_the_words(monkeypatch):
    _, seen = _run(monkeypatch, comment="x")
    assert "never these words" in seen["item"]["question"]


def test_a_refused_budget_is_reported_not_raised(monkeypatch):
    ok, _ = _run(monkeypatch, comment="x", created=False)
    assert ok is False


def test_a_broken_queue_never_breaks_the_rating(monkeypatch):
    """The client's rating is already recorded by the time this runs."""
    ok, _ = _run(monkeypatch, comment="x", raises=True)
    assert ok is False


# --- classification + wiring -------------------------------------------------

def test_the_emitter_is_routed_not_allowlisted():
    """A client can click, so by the #1677 rule this is agent-influenceable and
    must go through the budget. A direct `create_operator_queue_item` here would
    fail `test_1677_operator_alert_emitters`, which is the guard that makes the
    classification real rather than a comment."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/backend/client_portal/service.py"
    tree = ast.parse(src.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "raise_problem_report")

    # CALLS, not source text — the docstring names the forbidden function in
    # order to explain why it is forbidden, and a substring guard reads that as
    # the violation. Same trap `test_1677`'s own scanner avoids with an AST.
    called = {
        (node.func.attr if isinstance(node.func, ast.Attribute) else
         node.func.id if isinstance(node.func, ast.Name) else "")
        for node in ast.walk(fn) if isinstance(node, ast.Call)
    }
    assert "create_bounded_alert" in called
    assert "create_operator_queue_item" not in called


def test_the_rating_route_emits_on_every_down_not_only_with_a_comment():
    """A source guard, because the alternative is mounting the app. The emit sits
    ABOVE the `comment_recorded` gate, and that ordering is the AC."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src/backend/client_portal/router.py"
    body = src.read_text()
    emit = body.index("service.raise_problem_report")
    gate = body.index('if result["comment_recorded"] and body.rating == "down":')
    assert emit < gate, "the operator's copy is gated on the client typing something"


def test_the_agent_facing_redaction_is_untouched():
    """ent#366's rule stands: the rated agent reads the score and never the
    words. This feature adds an operator channel; it must not open an agent one."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src/backend/routers/evaluations.py"
    assert "comment_withheld" in src.read_text()
