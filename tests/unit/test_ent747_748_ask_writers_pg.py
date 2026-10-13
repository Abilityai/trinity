"""The two new ask writers hold on BOTH backends (trinity-enterprise#747, #748).

* `set_discussion_link` — the Discuss link is a compare-and-set on the stored
  context text, so N clicks racing agree on ONE chat.
* `cancel_item(disposition="dismissed")` — Dismiss shares the cancel CAS, so a
  dismiss racing an answer records exactly one ending.

Plus the chat prefilter a discussion chat lists its ask by (`context_contains`
on the JSON text). SQLite is covered here and by
test_ent747_748_ask_discuss_dismiss.py; this module is the PostgreSQL guard:
`schema-parity.yml` selects the `requires_postgres` marker and runs it with
TEST_POSTGRES_URL set, so `db_backend` parametrizes onto real PostgreSQL.

Harness: tests/db_harness.py — a fresh schema per test, on each backend.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, scalar  # noqa: E402,F401

pytestmark = pytest.mark.requires_postgres

AGENT = "agent-747-pg"
KEY = "workspace_discussion_id"


def _ops():
    from db.operator_queue import OperatorQueueOperations
    return OperatorQueueOperations()


def _ask(ops, rid="r-1", context=None):
    return ops.create_item(AGENT, {
        "id": rid, "type": "question", "status": "pending", "priority": "medium",
        "title": "Pick", "question": "Which?", "options": ["a", "b"],
        "context": context if context is not None else {"workspace_session_id": "main-1"},
        "addressed_to_email": "client@example.com",
        "created_at": "2026-10-01T10:00:00Z",
    })


def _together(n, fn):
    barrier = threading.Barrier(n)
    out, errors = [None] * n, []

    def worker(i):
        try:
            barrier.wait()
            out[i] = fn(i)
        except Exception as e:  # noqa: BLE001 — the assertion reports it
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out, errors


def test_racing_discuss_clicks_link_one_chat(db_backend):
    ops = _ops()
    uid = _ask(ops)

    rows, errors = _together(6, lambda i: ops.set_discussion_link(uid, KEY, f"chat-{i}"))

    assert errors == []
    winners = {r["context"][KEY] for r in rows}
    assert len(winners) == 1
    stored = ops.get_item(uid)["context"]
    assert stored[KEY] in winners
    # The link adds a key; it never moves the ent#429 attachment.
    assert stored["workspace_session_id"] == "main-1"


def test_the_link_is_refused_once_the_ask_ended(db_backend):
    ops = _ops()
    uid = _ask(ops)
    ops.cancel_item(uid, disposed_by_email="client@example.com", disposition="dismissed")

    row = ops.set_discussion_link(uid, KEY, "late")

    assert KEY not in (row["context"] or {})


def test_a_null_context_row_can_be_linked(db_backend):
    ops = _ops()
    uid = _ask(ops, context={})

    row = ops.set_discussion_link(uid, KEY, "chat-x")

    assert row["context"] == {KEY: "chat-x"}


def test_dismiss_racing_answer_records_exactly_one_ending(db_backend):
    ops = _ops()
    uid = _ask(ops)

    def act(i):
        if i % 2:
            return ops.respond_to_item(uid, "a", None, None, "client@example.com")
        return ops.cancel_item(uid, disposed_by_email="client@example.com",
                               disposition="dismissed")

    rows, errors = _together(6, act)

    assert errors == []
    won = [r for r in rows if not r.get("_status_conflict")]
    assert len(won) == 1
    final = ops.get_item(uid)
    assert final["disposition"] in ("answered", "dismissed")
    assert (final["status"], final["disposition"]) in (("responded", "answered"),
                                                       ("cancelled", "dismissed"))


def test_the_chat_prefilter_finds_the_discussed_ask(db_backend):
    ops = _ops()
    uid = _ask(ops)
    _ask(ops, rid="r-2")
    ops.set_discussion_link(uid, KEY, "chat-7")

    fragment = json.dumps({KEY: "chat-7"})[1:-1]
    found = ops.list_items(context_contains=(fragment,), include_cleared=True)

    assert [r["id"] for r in found] == [uid]
    assert scalar("SELECT COUNT(*) FROM operator_queue WHERE agent_name = :a", a=AGENT) == 2
