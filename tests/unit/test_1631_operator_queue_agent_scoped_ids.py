"""Operator-queue ids are agent-scoped, not fleet-wide (#1631).

`operator_queue.id` used to be populated with an AGENT-AUTHORED correlation
string read from each agent's ~/.trinity/operator-queue.json — but it was also
the fleet-wide PRIMARY KEY, so two agents choosing the same id collided and the
second agent's item was silently never created (the id-only exists() guard
short-circuited it). The fix splits the id's two jobs: `id` is a platform-minted
uuid (global handle), the agent's string lives in a new `request_id` column that
is UNIQUE per agent, and every agent-facing lookup/write is scoped by agent.

DB-level tests run against the per-process temp DB the unit conftest pins (same
harness as test_1426); the sync-loop reserved-prefix test is pure/mocked (same
harness as test_1525).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("sqlalchemy")

# Backend config raises without these; keep all DB writes in the conftest temp DB.
os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")
# NB: do NOT override TRINITY_DB_PATH — the unit conftest pins a per-process temp
# DB and init_database() (run once at first `database` import) builds the full
# schema, incl. operator_queue + the #1631 request_id column and unique index.

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from database import db  # noqa: E402
import services.operator_queue_service as oqs  # noqa: E402
from services.operator_queue_service import OperatorQueueSyncService  # noqa: E402

pytestmark = pytest.mark.unit


def _item(req_id: str, **overrides) -> dict:
    item = {
        "id": req_id,
        "type": "approval",
        "priority": "high",
        "title": "Approve payout",
        "question": "Release 500 USDC?",
        "status": "pending",
        "created_at": "2026-07-02T16:00:00Z",
    }
    item.update(overrides)
    return item


# ===========================================================================
# The headline regression (issue AC#4) — two agents, same request id
# ===========================================================================


def test_two_agents_same_request_id_both_persist():
    """Two DIFFERENT agents choosing the SAME request id both get visible,
    distinct rows — the collision the id-only PK used to swallow."""
    req_id = f"dup-{uuid.uuid4().hex[:8]}"

    id_a = db.create_operator_queue_item("agent-a-1631", _item(req_id))
    id_b = db.create_operator_queue_item("agent-b-1631", _item(req_id))

    # Distinct platform-minted handles — the second create was NOT dropped.
    assert id_a != id_b

    row_a = db.get_operator_queue_item(id_a)
    row_b = db.get_operator_queue_item(id_b)
    assert row_a is not None and row_b is not None
    assert row_a["agent_name"] == "agent-a-1631"
    assert row_b["agent_name"] == "agent-b-1631"
    # Both keep the agent's authored string in request_id.
    assert row_a["request_id"] == req_id
    assert row_b["request_id"] == req_id


# ===========================================================================
# Idempotency preserved (issue AC#3) — same agent, same request id
# ===========================================================================


def test_same_agent_same_request_id_is_idempotent():
    """A re-insert of the SAME (agent, request id) is a no-op: one row, and
    create_item returns the EXISTING row's id both times (the re-read-on-conflict
    contract _create_park_item depends on)."""
    req_id = f"idem-{uuid.uuid4().hex[:8]}"

    id1 = db.create_operator_queue_item("agent-c-1631", _item(req_id))
    id2 = db.create_operator_queue_item("agent-c-1631", _item(req_id))

    # The second call minted a fresh uuid internally but must return the
    # surviving row's id, not that throwaway uuid.
    assert id1 == id2

    matching = [
        i
        for i in db.list_operator_queue_items(
            agent_name="agent-c-1631", include_cleared=True, limit=500
        )
        if i["request_id"] == req_id
    ]
    assert len(matching) == 1
    assert matching[0]["id"] == id1


# ===========================================================================
# item_exists is agent-scoped
# ===========================================================================


def test_item_exists_is_agent_scoped():
    """Agent A's request id must NOT read as existing for agent B — that
    false-positive was the exact bug that dropped B's item."""
    req_id = f"exist-{uuid.uuid4().hex[:8]}"
    db.create_operator_queue_item("agent-d-1631", _item(req_id))

    assert db.operator_queue_item_exists("agent-d-1631", req_id) is True
    assert db.operator_queue_item_exists("agent-e-1631", req_id) is False


# ===========================================================================
# mark_acknowledged is agent-scoped (fixes a real cross-agent write bug)
# ===========================================================================


def test_mark_acknowledged_is_agent_scoped():
    """Agent B acknowledging its own id must NOT flip agent A's identically-id'd
    row (today's id-only UPDATE did exactly that)."""
    req_id = f"ack-{uuid.uuid4().hex[:8]}"
    id_a = db.create_operator_queue_item("agent-f-1631", _item(req_id))
    id_g = db.create_operator_queue_item("agent-g-1631", _item(req_id))

    # Move both to 'responded' so acknowledgement has something to flip.
    db.respond_to_operator_queue_item(id_a, "approve", None, "1", "op@example.com")
    db.respond_to_operator_queue_item(id_g, "approve", None, "1", "op@example.com")

    # agent-g acknowledges ITS id — only agent-g's row flips. The return is the
    # row's platform uuid (not req_id), which the WS event + frontend key on.
    assert db.mark_operator_queue_acknowledged("agent-g-1631", req_id) == id_g
    assert db.get_operator_queue_item(id_g)["status"] == "acknowledged"
    assert db.get_operator_queue_item(id_a)["status"] == "responded"  # untouched

    # agent-f can still acknowledge its own row, and gets ITS own uuid back.
    assert db.mark_operator_queue_acknowledged("agent-f-1631", req_id) == id_a
    assert db.get_operator_queue_item(id_a)["status"] == "acknowledged"

    # A non-'responded' (already-acknowledged) row returns None, not a stale uuid.
    assert db.mark_operator_queue_acknowledged("agent-g-1631", req_id) is None


# ===========================================================================
# Reserved platform prefixes are rejected by the sync loop
# ===========================================================================


def _wire_sync(monkeypatch, db_mock, requests):
    monkeypatch.setattr(oqs, "db", db_mock)
    # #1632 added depth/rate ingestion caps to this sync seam. These #1631 tests
    # predate them and assert id-scoping behavior (the caps themselves are covered
    # by test_1632_operator_queue_caps.py), so neutralize the caps here: report
    # zero pending depth and always allow the rate limiter.
    db_mock.count_operator_queue_pending_for_agent.return_value = 0
    monkeypatch.setattr(
        oqs.rate_limiter, "check", MagicMock(return_value=MagicMock(allowed=True))
    )
    client = MagicMock()
    client.read_file = AsyncMock(return_value={
        "success": True,
        "content": json.dumps({"requests": list(requests)}),
    })
    monkeypatch.setattr(oqs, "AgentClient", lambda name: client)
    svc = OperatorQueueSyncService()
    svc._write_responses_to_agent = AsyncMock()
    return svc


def test_reserved_prefix_agent_ids_are_rejected_by_sync_loop(monkeypatch):
    """An agent pre-claiming a platform id (e.g. `poison-…`) to hijack/suppress
    the platform's own alert is skipped; a benign id in the same file still
    persists, and the reserved id is warned about only once (not per cycle)."""
    db_mock = MagicMock()
    db_mock.operator_queue_item_exists.return_value = False
    db_mock.get_operator_queue_responded_for_agent.return_value = []
    db_mock.get_operator_queue_terminal_for_agent.return_value = []

    reserved = _item("poison-hijack")
    benign = _item("req-legit")
    svc = _wire_sync(monkeypatch, db_mock, (reserved, benign))

    for _ in range(3):  # more than one cycle — warning must not repeat
        asyncio.run(svc._sync_agent("a"))

    created_ids = [
        c.args[1]["id"] for c in db_mock.create_operator_queue_item.call_args_list
    ]
    assert "poison-hijack" not in created_ids   # rejected, never created
    assert "req-legit" in created_ids           # benign sibling still persists

    # exists() was never even consulted for the reserved id (rejected earlier).
    checked = [c.args[1] for c in db_mock.operator_queue_item_exists.call_args_list]
    assert "poison-hijack" not in checked

    # Warned exactly once for this (agent, id).
    assert svc._rejected_reserved == {("a", "poison-hijack")}


def test_reserved_prefix_guard_folds_case_and_whitespace(monkeypatch):
    """A lookalike id (` Poison-…`, uppercase/padded) must still be rejected —
    the platform mints these prefixes lowercase and unpadded, so a normalized
    match can only be an impersonation attempt (an operator-phishing surface)."""
    db_mock = MagicMock()
    db_mock.operator_queue_item_exists.return_value = False
    db_mock.get_operator_queue_responded_for_agent.return_value = []
    db_mock.get_operator_queue_terminal_for_agent.return_value = []

    lookalikes = [_item(" Poison-x"), _item("SYNC-FAILING-x"), _item("\tgit-bloat-x")]
    svc = _wire_sync(monkeypatch, db_mock, lookalikes)
    asyncio.run(svc._sync_agent("a"))

    # None of the lookalikes were created — all folded onto a reserved prefix.
    assert db_mock.create_operator_queue_item.call_count == 0
