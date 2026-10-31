"""trinity#3521 — a thumbs-down comment reaches the operator's alert.

The Workspace records a thumbs-down on the click and sends the optional comment
as a SECOND request (`PortalRating.vue`: `choose()` → `submitComment()`). Both
reach `raise_problem_report` with the same per-person / target / day alert id,
and `create_item` is ``INSERT ... ON CONFLICT DO NOTHING``, so the comment
request was a no-op: the alert raised by the click kept reading "They left no
comment." (`has_comment: false`) while the words sat only on the evaluation row,
which no reviewer surface shows.

The fix is a pending-only text update at the sink. These tests drive the real
service against a real (temp) database — the property is about two calls
meeting one row, which a stubbed sink cannot show.

#3254: the report now reports through `platform_alerts.observe`, and the two
requests meet on its subject row inside the seam's locked create: a reading
with words writes them, a bare one only counts (`keep_text`). Re-pins are
marked where they are.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT = "rated-agent"
EMAIL = "client@example.com"
TARGET = ("message", "msg-1")


def _rate(comment=None, *, target=TARGET, agent=AGENT):
    from client_portal import service
    return asyncio.run(service.raise_problem_report(
        agent, EMAIL, target_kind=target[0], target_id=target[1], comment=comment))


def _alerts(agent=AGENT):
    from database import db
    items = db.list_operator_queue_items(agent_name=agent)
    items = items.get("items", items) if isinstance(items, dict) else items
    return [i for i in items if i.get("type") == "workspace_problem_report"]


def _only_alert(agent=AGENT):
    rows = _alerts(agent)
    assert len(rows) == 1, f"expected one problem report, found {len(rows)}"
    return rows[0]


def _context(item):
    ctx = item.get("context")
    if isinstance(ctx, str):
        import json
        ctx = json.loads(ctx)
    return ctx or {}


# --- the bug: rating first, comment second ---------------------------------

def test_a_comment_sent_after_the_rating_is_quoted_in_the_alert(db_backend):
    """AC1 + AC3: the two-request UI flow."""
    assert _rate(None) is True                       # the click
    _rate("I wanted the Q3 numbers, not Q2.")        # the comment box
    item = _only_alert()
    assert "I wanted the Q3 numbers, not Q2." in item["question"]
    assert "They left no comment." not in item["question"]
    assert _context(item)["has_comment"] is True


def test_the_reverse_order_also_ends_with_the_comment(db_backend):
    _rate("Wrong customer.")
    _rate(None)
    item = _only_alert()
    assert "Wrong customer." in item["question"]
    assert _context(item)["has_comment"] is True


def test_a_later_rerate_without_a_comment_does_not_erase_it(db_backend):
    """AC2."""
    _rate(None)
    _rate("Too long.")
    _rate(None)
    _rate("   ")                                     # whitespace is not a comment
    item = _only_alert()
    assert "Too long." in item["question"]
    assert _context(item)["has_comment"] is True


def test_an_edited_comment_replaces_the_previous_one(db_backend):
    _rate(None)
    _rate("first words")
    _rate("second words")
    item = _only_alert()
    assert "second words" in item["question"]
    assert "first words" not in item["question"]


def test_the_comment_is_still_truncated_on_the_update_path(db_backend):
    from client_portal.service import PROBLEM_REPORT_COMMENT_CHARS
    _rate(None)
    _rate("x" * (PROBLEM_REPORT_COMMENT_CHARS * 3))
    item = _only_alert()
    assert "x" * (PROBLEM_REPORT_COMMENT_CHARS + 1) not in item["question"]


def test_the_words_stay_out_of_the_context_blob_on_update(db_backend):
    _rate(None)
    _rate("secret-ish words")
    assert "secret-ish words" not in str(_context(_only_alert()))


# --- what the update must not touch ----------------------------------------

def test_an_acknowledged_report_is_neither_rewritten_nor_reopened(db_backend):
    # #3254 re-pin (fixture shape): the operator acknowledges through the real
    # answer path (#2372: `acknowledged`, disposed by the person), which is the
    # ending the seam's snooze reads; a raw status UPDATE carries no ledger.
    from services import ask_service
    _rate(None)
    item = _only_alert()
    ask_service.answer(item, response="acknowledged", response_text=None,
                       actor=ask_service.Actor(email="owner@example.com"))
    _rate("arrived after the operator looked")
    rows = _alerts()
    assert len(rows) == 1
    assert rows[0]["status"] == "acknowledged"
    assert "arrived after the operator looked" not in rows[0]["question"]


def test_the_update_is_scoped_to_its_own_report(db_backend):
    _rate(None, target=("message", "msg-1"))
    _rate(None, target=("message", "msg-2"))
    _rate(None, agent="other-agent")
    _rate("only about msg-2", target=("message", "msg-2"))
    by_target = {_context(i)["target_id"]: i for i in _alerts()}
    assert "only about msg-2" in by_target["msg-2"]["question"]
    assert "only about msg-2" not in by_target["msg-1"]["question"]
    assert "only about msg-2" not in _only_alert("other-agent")["question"]


def test_the_update_never_writes_another_kind_of_row(db_backend):
    """The type guard: a row under the same request id that is not a problem
    report (only the platform can mint the reserved prefix, but the sink should
    not rely on that alone) is left alone.

    #3254 re-pin (accessor → seam): the retired `update_pending_problem_report`
    is gone; the rating's own write finds rows by the report's SUBJECT, so a
    foreign row under the report's old id is never written."""
    from client_portal.service import _problem_report_id, workspace_evaluator
    from database import db
    rid = _problem_report_id(workspace_evaluator(EMAIL), *TARGET)
    _hrun("INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, "
          "title, question, created_at) VALUES ('x1', :a, :r, 'alert', 'pending', 'medium', "
          "'t', 'original', '2026-10-12T00:00:00Z')", a=AGENT, r=rid)
    assert _rate("rewritten") is True
    assert db.get_operator_queue_item("x1")["question"] == "original"
    assert "rewritten" in _only_alert()["question"]


def test_a_comment_reaches_a_pending_report_even_at_the_alert_budget(db_backend, monkeypatch):
    """The update adds no row, so it spends no budget: a person who typed a
    comment is not refused because other reports about that agent are pending."""
    import services.operator_queue_service as oqs
    _rate(None)
    monkeypatch.setattr(oqs, "OPERATOR_ALERT_MAX_PENDING_PER_TYPE", 1)
    assert _rate("still reaches the operator") is True
    assert "still reaches the operator" in _only_alert()["question"]


def test_a_comment_that_loses_the_race_to_the_bare_create_still_lands(db_backend, monkeypatch):
    """Both requests run in the background. If the comment's update misses (no
    row yet) and the bare click's create inserts before the comment's own
    create, the comment's create is the ON CONFLICT no-op — the retry after the
    create is what lands the words.

    #3254 re-pin: the race is injected at the seam's create — the bare click's
    whole reading lands between the comment's read and its locked create, and
    the comment's write then finds the click's row and writes its words."""
    from database import db
    real_create = db.create_platform_operator_queue_item
    state = {"raced": False, "click": None, "row_before": None}

    def bare_click_wins(agent_name, item, **kw):
        if not state["raced"]:
            state["raced"] = True
            import threading
            click = threading.Thread(          # its own request
                target=lambda: state.update(click=_rate(None)))
            click.start()
            click.join()
            state["row_before"] = _only_alert()
        return real_create(agent_name, item, **kw)

    monkeypatch.setattr(db, "create_platform_operator_queue_item", bare_click_wins)
    _rate("words that lost the race")
    # The bare click really did land first: its own pending, comment-free row
    # was there when the comment's locked create ran.
    assert state["click"] is True
    assert state["row_before"]["status"] == "pending"
    assert "They left no comment." in state["row_before"]["question"]
    item = _only_alert()
    assert "words that lost the race" in item["question"]
    assert _context(item)["has_comment"] is True
