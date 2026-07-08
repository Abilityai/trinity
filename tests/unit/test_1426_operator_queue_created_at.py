"""Operator-queue ingest tolerates missing required fields (#1426).

Agents author `~/.trinity/operator-queue.json` free-form. A pending item that
omitted `created_at` (or `title`/`question`) made `OperatorQueueOperations.
create_item` raise KeyError; because the item stayed `pending` in the agent
file, the 5s sync loop retried and error-logged it *forever* and the request
never reached the Operating Room. `create_item` now defaults those fields
(mirroring the existing type/status/priority defaults), so the item is created
once and the loop stops.

DB-level unit test against a throwaway SQLite DB — no live backend.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

pytest.importorskip("sqlalchemy")

# Backend config raises without these; keep all DB writes in a temp file.
os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")
# NB: do NOT override TRINITY_DB_PATH here. The unit conftest pins a per-process
# temp DB and `init_database()` (run once at first `database` import) creates the
# full schema — incl. the operator_queue table — on it. Overriding the path after
# that first import points get_engine() at an uninitialized file, so writes fail
# with "no such table: operator_queue" (#1426 CI regression).

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from database import db  # noqa: E402

pytestmark = pytest.mark.unit


def _make_item(**overrides) -> dict:
    item = {
        "id": f"req-{uuid.uuid4().hex[:8]}",
        "type": "question",
        "priority": "high",
        "title": "Approve reward payout",
        "question": "Release 500 USDC?",
        "status": "pending",
        "created_at": "2026-07-02T16:00:00Z",
    }
    item.update(overrides)
    return item


def test_missing_created_at_is_defaulted_not_raised():
    """The exact eu2 case: a pending item with no `created_at` is created (loop
    stops) and gets a non-empty timestamp instead of raising KeyError."""
    item = _make_item()
    del item["created_at"]

    # Must NOT raise (pre-fix: KeyError('created_at')).
    db.create_operator_queue_item("agent-x", item)

    row = db.get_operator_queue_item(item["id"])
    assert row is not None, "item should have been created"
    assert row.get("created_at"), "created_at should be defaulted to a non-empty value"


def test_missing_title_and_question_are_defaulted():
    """title/question share the same KeyError class — also defaulted."""
    item = _make_item()
    del item["title"]
    del item["question"]

    db.create_operator_queue_item("agent-x", item)

    row = db.get_operator_queue_item(item["id"])
    assert row is not None
    assert row.get("title"), "title should be defaulted"
    assert row.get("question"), "question should be defaulted"


def test_present_fields_are_preserved():
    """Defaulting must not clobber a well-formed item."""
    item = _make_item()
    db.create_operator_queue_item("agent-x", item)

    row = db.get_operator_queue_item(item["id"])
    assert row["created_at"] == "2026-07-02T16:00:00Z"
    assert row["title"] == "Approve reward payout"
    assert row["question"] == "Release 500 USDC?"
