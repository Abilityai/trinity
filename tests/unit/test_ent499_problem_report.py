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
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


# --- the id ------------------------------------------------------------------

def test_one_item_per_person_per_target_per_day():
    from client_portal.service import _problem_report_id
    a = _problem_report_id("workspace:a@example.com", "message", "m1", day="2026-09-07")
    b = _problem_report_id("workspace:a@example.com", "message", "m1", day="2026-09-07")
    assert a == b


def test_a_complaint_the_next_day_is_not_silently_swallowed():
    """The review fix. `create_item`'s ON CONFLICT ignores the existing row's
    STATUS, so a same-id report raised after the operator acknowledged the first
    one was dropped FOREVER — worse than a duplicate, because the second
    complaint simply never reached anyone. Quantising to the UTC day keeps
    "never duplicates" true where it matters and lets tomorrow through."""
    from client_portal.service import _problem_report_id
    today = _problem_report_id("workspace:a@example.com", "message", "m1", day="2026-09-07")
    tomorrow = _problem_report_id("workspace:a@example.com", "message", "m1", day="2026-09-08")
    assert today != tomorrow


def test_the_bucket_defaults_to_today_not_to_a_constant():
    """A default of `None` collapsing to a fixed string would restore the
    permanent suppression while every equality test above still passed."""
    from client_portal.service import _problem_report_id
    from utils.helpers import utc_now_iso
    assert (_problem_report_id("workspace:a@example.com", "message", "m1")
            == _problem_report_id("workspace:a@example.com", "message", "m1",
                                  day=utc_now_iso()[:10]))


def test_the_id_does_not_move_with_the_comment():
    """`_problem_report_id` takes no comment at all — the signature IS the
    guarantee. A key that moves with the text is not a dedup, it is a rename of
    the attack (`claim_capture_feedback_dispatch`'s rule)."""
    import inspect
    from client_portal.service import _problem_report_id

    params = set(inspect.signature(_problem_report_id).parameters)
    # The invariant is that no COMMENT reaches the id — asserted as an absence,
    # not as an exact parameter set. The exact-set form failed the moment the
    # review added the `day` bucket, which is orthogonal to what this guards.
    assert not (params & {"comment", "text", "body", "excerpt", "message"})
    assert {"evaluator", "target_kind", "target_id"} <= params


def test_different_people_and_different_targets_are_different_items():
    from client_portal.service import _problem_report_id
    d = "2026-09-07"
    base = _problem_report_id("workspace:a@example.com", "message", "m1", day=d)
    assert base != _problem_report_id("workspace:b@example.com", "message", "m1", day=d)
    assert base != _problem_report_id("workspace:a@example.com", "message", "m2", day=d)
    assert base != _problem_report_id("workspace:a@example.com", "deliverable", "m1", day=d)


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


# --- the complaint must not travel back to the agent it is about -------------
#
# Found by an adversarial review pass, and it falsified this feature's own
# docstring ("the operator sees the comment; the agent still does not"). Two
# pre-existing return paths carry a RESPONDED item back to its agent, and both
# keyed on agent_name alone:
#
#   * operator_queue_service._write_responses_to_agent → the agent's own
#     ~/.trinity/operator-queue.json, `question` and `context` verbatim;
#   * routers/operator_queue respond → ent#329 spawn_resume_dispatch, whose
#     prompt embeds item["question"].
#
# Both exist to close a loop the AGENT opened. A platform alarm opened none —
# and for a problem report the agent is the SUBJECT, so returning it hands the
# rated agent the client's address and words, plus (under
# operator_resume_enabled) one of its own turns to read them with.

def test_a_platform_alarm_is_recognised_as_platform_minted():
    from services.operator_queue_service import is_platform_minted
    assert is_platform_minted({"request_id": "workspace-problem-abc123"}) is True
    assert is_platform_minted({"id": "skill-not-found-agent-2026"}) is True
    assert is_platform_minted("db-backup-20260907") is True


def test_an_agent_authored_item_is_not_platform_minted():
    """The predicate has to DISCRIMINATE: if it swallowed agent items too, the
    respond→resume loop and the answer write-back would both go dead and every
    parked agent question would stop being answerable."""
    from services.operator_queue_service import is_platform_minted
    assert is_platform_minted({"request_id": "deploy-approval-42"}) is False
    assert is_platform_minted({"request_id": ""}) is False
    assert is_platform_minted({}) is False


def test_the_agent_file_write_back_skips_platform_alarms():
    src = (REPO / "src/backend/services/operator_queue_service.py").read_text()
    fn = src[src.index("def _write_responses_to_agent"):]
    loop = fn[fn.index("for resp in responded_items:"):]
    assert "is_platform_minted(resp)" in loop[:400], (
        "a responded platform alarm is written into the agent's own queue file"
    )


def test_acknowledging_a_platform_alarm_does_not_spend_the_agents_turn():
    """'Got it' posts to /respond like any other answer, so without this gate an
    acknowledge dispatches a full execute_task on the rated agent."""
    src = (REPO / "src/backend/routers/operator_queue.py").read_text()
    assert "if item and not operator_queue_service.is_platform_minted(item):" in src


def test_the_context_blob_carries_no_client_words_or_address(monkeypatch):
    _, seen = _run(monkeypatch, comment="my card number is 4111 1111 1111 1111")
    ctx = str(seen["item"]["context"])
    assert "4111" not in ctx
    assert "client@example.com" not in ctx
