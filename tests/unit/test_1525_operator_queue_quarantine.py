"""Regression guard for #1525 — operator-queue create hot-loop.

A `pending` request whose DB create keeps failing used to be re-attempted on
every ~5s sync cycle forever (the row never persists → `operator_queue_item_exists`
stays False → retry + ERROR-log indefinitely). These tests pin the quarantine:
after `MAX_CREATE_ATTEMPTS` consecutive failures the request is skipped, and a
create that later succeeds clears the counter.

Pure/mocked — no backend or DB.
"""
import json
import sys
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import services.operator_queue_service as oqs  # noqa: E402
from services.operator_queue_service import OperatorQueueSyncService, MAX_CREATE_ATTEMPTS  # noqa: E402

pytestmark = pytest.mark.unit

_REQ = {"id": "req-1", "status": "pending", "title": "t", "question": "q"}


def _fake_db(create_side_effect):
    db = MagicMock()
    db.operator_queue_item_exists.return_value = False   # never persists
    # #1632: the sync loop now measures per-agent pending depth before admitting.
    # Return 0 so the depth gate is a no-op and this test keeps exercising the
    # #1525 quarantine path (a MagicMock default would misfire the >= cap check).
    db.count_operator_queue_pending_for_agent.return_value = 0
    db.create_operator_queue_item.side_effect = create_side_effect
    db.get_operator_queue_responded_for_agent.return_value = []
    db.get_operator_queue_terminal_for_agent.return_value = []
    return db


def _wire(monkeypatch, db, requests=(_REQ,)):
    monkeypatch.setattr(oqs, "db", db)
    client = MagicMock()
    client.read_file = AsyncMock(return_value={
        "success": True,
        "content": json.dumps({"requests": list(requests)}),
    })
    monkeypatch.setattr(oqs, "AgentClient", lambda name: client)
    svc = OperatorQueueSyncService()
    svc._write_responses_to_agent = AsyncMock()
    return svc


def _run_cycles(svc, n):
    for _ in range(n):
        asyncio.run(svc._sync_agent("a"))


class TestQuarantine:
    def test_persistently_failing_create_is_quarantined(self, monkeypatch):
        db = _fake_db(create_side_effect=Exception("boom"))
        svc = _wire(monkeypatch, db)

        _run_cycles(svc, 6)  # many more cycles than the cap

        # Create attempted at most the cap, NOT once per cycle (the #1525 loop).
        assert db.create_operator_queue_item.call_count == MAX_CREATE_ATTEMPTS
        # #1631: quarantine map is keyed by the (agent, req_id) tuple.
        assert svc._create_failures.get(("a", "req-1")) == MAX_CREATE_ATTEMPTS

    def test_success_clears_the_counter(self, monkeypatch):
        # Fail once, then succeed on the next cycle.
        db = _fake_db(create_side_effect=[Exception("transient"), None, None])
        svc = _wire(monkeypatch, db)

        _run_cycles(svc, 1)
        assert svc._create_failures.get(("a", "req-1")) == 1   # one failure recorded

        _run_cycles(svc, 1)
        assert ("a", "req-1") not in svc._create_failures       # recovered → cleared
        assert db.create_operator_queue_item.call_count == 2

    def test_healthy_request_is_created_once_and_not_recounted(self, monkeypatch):
        db = _fake_db(create_side_effect=None)  # always succeeds
        # exists() stays False, so without the counter it would create every cycle;
        # that's fine (idempotent on_conflict_do_nothing) — we just assert no
        # quarantine state leaks for a healthy request.
        svc = _wire(monkeypatch, db)
        _run_cycles(svc, 3)
        assert svc._create_failures == {}


class TestCreateItemGuard:
    def test_missing_id_raises_valueerror_not_keyerror(self):
        # The DB boundary raises a clear ValueError (caller quarantines) rather
        # than an opaque KeyError when 'id' is absent (#1525 belt).
        from db.operator_queue import OperatorQueueOperations
        ops = OperatorQueueOperations.__new__(OperatorQueueOperations)  # no __init__ / engine needed
        with pytest.raises(ValueError, match="missing a required 'id'"):
            ops.create_item("agent", {"title": "t", "question": "q"})
