"""#2372 — acknowledging a platform-minted alert is its terminal event.

A row whose `request_id` carries a reserved platform prefix
(`operator_queue_service._RESERVED_ID_PREFIXES`) has no agent audience: the
agent never asked, is not waiting, and never acknowledges it. Before this fix an
operator's answer parked such a row in `responded` for the 90-day floor, and a
pre-ent#499 agent file still carrying it was refused at every boot with the
#1631 impersonation WARNING.

Pinned here:
  * answering a platform-minted row lands it `acknowledged` (+ `acknowledged_at`);
    an agent's own ask still lands `responded`; a skill-gate approval keeps the
    `answered` ending its resolver reads;
  * the acknowledged row is cleared by Clear All;
  * a leftover platform entry in the agent's file that is no longer `pending`
    is skipped without the WARNING, with or without its row, and writes nothing;
    a `pending` reserved entry still gets the WARNING and is never created;
  * the heal migration moves stuck `responded` platform rows to `acknowledged`
    once, matches ids the way `is_platform_minted` does, and is registered on
    both tracks.
"""
import asyncio
import json
import logging
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

import services.operator_queue_service as oqs  # noqa: E402
from services import ask_service  # noqa: E402
from services.operator_queue_service import OperatorQueueSyncService  # noqa: E402
from services.rate_limiter import RateLimitResult  # noqa: E402


# ---------------------------------------------------------------------------
# AC 1 + AC 5 — the answer sink, against the unit island's real SQLite
# ---------------------------------------------------------------------------

class TestAnswerSink:
    @pytest.fixture
    def agent(self):
        return f"agent-2372-{uuid.uuid4().hex[:6]}"

    @pytest.fixture
    def real_db(self):
        from database import db
        return db

    def _create(self, real_db, agent, rid, *, type_="alert", options=None):
        item = {"id": rid, "type": type_, "status": "pending", "title": "t", "question": "q"}
        if options is not None:
            item["options"] = options
        real_db.create_operator_queue_item(agent, item)
        return real_db.get_operator_queue_item_for_agent_by_request_id(agent, rid)

    def _answer(self, row, response="acknowledged"):
        return ask_service.answer(row, response=response, response_text=None,
                                  actor=ask_service.Actor(email="op@example.com")).rows[0]

    def test_a_platform_alert_lands_acknowledged(self, real_db, agent):
        row = self._create(real_db, agent, f"base-image-stale-{agent}-20260731")
        out = self._answer(row)
        assert out["status"] == "acknowledged"
        assert out["acknowledged_at"]
        assert out["responded_by_email"] == "op@example.com"
        assert out["disposition"] == "answered"
        assert real_db.get_operator_queue_item(row["id"])["status"] == "acknowledged"

    def test_an_agents_own_ask_still_lands_responded(self, real_db, agent):
        row = self._create(real_db, agent, "mine-1", type_="question")
        out = self._answer(row, response="yes")
        assert out["status"] == "responded"
        assert not out.get("acknowledged_at")

    def test_a_gate_approval_keeps_the_answered_ending(self, real_db, agent):
        # skill_gate_service.resolve reads `disposition` first, then maps
        # responded/acknowledged alike — the status change cannot reach it.
        row = self._create(real_db, agent, f"gate-{agent}-1", type_="approval",
                           options=["Approve", "Deny"])
        out = self._answer(row, response="Approve")
        assert out["status"] == "acknowledged"
        assert out["disposition"] == "answered"
        assert out["response"] == "Approve"

    def test_clear_all_hides_an_acknowledged_platform_alert(self, real_db, agent):
        row = self._create(real_db, agent, f"queue-flood-{agent}-x")
        self._answer(row)
        assert real_db.clear_resolved_operator_queue_items(agent_name=agent) == 1
        assert real_db.get_operator_queue_item(row["id"])["cleared_at"]


# ---------------------------------------------------------------------------
# AC 2 + AC 3 — the sync loop against a pre-ent#499 file
# ---------------------------------------------------------------------------

def _fake_db(index=None):
    db = MagicMock()
    db.count_operator_queue_pending_for_agent.return_value = 0
    db.get_operator_queue_responded_for_agent.return_value = []
    db.get_operator_queue_terminal_for_agent.return_value = []
    db.get_operator_queue_sync_index_for_agent.return_value = index or {
        "open": [], "terminal": {}, "foreign": []}
    db.set_operator_queue_sync_state.return_value = False
    db.set_operator_queue_delivery_state.return_value = False
    db.create_operator_queue_item_with_outcome.side_effect = (
        lambda agent, item, **kw: (db.create_operator_queue_item(agent, item, **kw), True))
    db.mark_operator_queue_unconfirmed.return_value = 0
    db.refresh_operator_queue_last_confirmed.return_value = 0
    db.get_setting_value.return_value = "24"
    return db


def _wire(monkeypatch, db, requests):
    monkeypatch.setattr(oqs, "db", db)
    state = {"content": json.dumps({"requests": requests}), "writes": []}
    client = MagicMock()

    async def _read(path, timeout=5.0):
        return {"success": True, "content": state["content"]}

    async def _write(path, content, **kw):
        state["writes"].append(json.loads(content))
        state["content"] = content
        return {"success": True}

    client.read_file = AsyncMock(side_effect=_read)
    client.write_file = AsyncMock(side_effect=_write)
    monkeypatch.setattr(oqs, "AgentClient", lambda name: client)
    monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    oqs.reset_alert_budget_state()
    return OperatorQueueSyncService(), state


def _impersonation_warnings(caplog):
    return [r for r in caplog.records
            if r.levelno >= logging.WARNING and "reserved platform prefix" in r.getMessage()]


_LEFTOVER = {"id": "base-image-stale-trinity-system-20260731", "status": "responded",
             "title": "Stale base image", "question": "q", "response": "acknowledged"}


class TestLeftoverFileEntry:
    def test_no_warning_and_no_write_with_the_row_gone(self, monkeypatch, caplog):
        # The row was pruned by retention; the entry the old write-back left stays.
        db = _fake_db()
        svc, state = _wire(monkeypatch, db, [dict(_LEFTOVER)])
        caplog.set_level(logging.DEBUG, logger=oqs.logger.name)
        for _ in range(2):
            asyncio.run(svc._sync_agent("trinity-system"))
        assert _impersonation_warnings(caplog) == []
        db.create_operator_queue_item.assert_not_called()
        db.mark_operator_queue_acknowledged.assert_not_called()
        assert state["writes"] == []

    def test_an_entry_the_agent_flipped_to_acknowledged_touches_nothing(self, monkeypatch, caplog):
        db = _fake_db()
        svc, state = _wire(monkeypatch, db, [{**_LEFTOVER, "status": "acknowledged"}])
        caplog.set_level(logging.DEBUG, logger=oqs.logger.name)
        asyncio.run(svc._sync_agent("trinity-system"))
        assert _impersonation_warnings(caplog) == []
        db.mark_operator_queue_acknowledged.assert_not_called()
        db.set_operator_queue_sync_state.assert_not_called()
        assert state["writes"] == []

    def test_a_pending_reserved_entry_is_still_an_impersonation(self, monkeypatch, caplog):
        db = _fake_db()
        svc, _ = _wire(monkeypatch, db, [{"id": "poison-fake", "status": "pending",
                                          "title": "t", "question": "q"}])
        caplog.set_level(logging.DEBUG, logger=oqs.logger.name)
        asyncio.run(svc._sync_agent("agent-x"))
        assert len(_impersonation_warnings(caplog)) == 1
        db.create_operator_queue_item.assert_not_called()


# ---------------------------------------------------------------------------
# AC 4 — the heal migration
# ---------------------------------------------------------------------------

class TestHealMigration:
    @pytest.fixture
    def conn(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE operator_queue (
            id TEXT PRIMARY KEY, agent_name TEXT, request_id TEXT, type TEXT,
            status TEXT, created_at TEXT, acknowledged_at TEXT)""")
        return conn

    def _add(self, conn, id_, rid, status="responded"):
        conn.execute("INSERT INTO operator_queue (id, agent_name, request_id, type, status, created_at) "
                     "VALUES (?, 'a', ?, 'alert', ?, '2026-07-31T00:00:00Z')", (id_, rid, status))

    def _rows(self, conn):
        return {i: (s, bool(a)) for i, s, a in
                conn.execute("SELECT id, status, acknowledged_at FROM operator_queue")}

    def test_moves_stuck_platform_rows_to_acknowledged(self, conn):
        from db.migrations import _migrate_platform_alert_responded_heal
        self._add(conn, "stale", "base-image-stale-trinity-system-1")
        self._add(conn, "lookalike", " Poison-x")       # legacy row; is_platform_minted says platform
        self._add(conn, "val", "val_abc")
        self._add(conn, "own-val", "valX1")             # `_` is a literal, never a wildcard
        self._add(conn, "own", "mine-1")
        self._add(conn, "pending", "queue-flood-a-1", status="pending")

        _migrate_platform_alert_responded_heal(conn.cursor(), conn)

        rows = self._rows(conn)
        assert rows["stale"] == ("acknowledged", True)
        assert rows["lookalike"] == ("acknowledged", True)
        assert rows["val"] == ("acknowledged", True)
        assert rows["own-val"] == ("responded", False)
        assert rows["own"] == ("responded", False)
        assert rows["pending"] == ("pending", False)

    def test_is_idempotent(self, conn):
        from db.migrations import _migrate_platform_alert_responded_heal
        self._add(conn, "stale", "sync-failing-a-1")
        _migrate_platform_alert_responded_heal(conn.cursor(), conn)
        first = conn.execute("SELECT acknowledged_at FROM operator_queue").fetchone()[0]
        _migrate_platform_alert_responded_heal(conn.cursor(), conn)
        assert conn.execute("SELECT acknowledged_at FROM operator_queue").fetchone()[0] == first

    def test_frozen_prefixes_cover_every_live_reserved_prefix(self):
        # The heal's list is frozen on purpose (a one-time fix), so it must be
        # complete on the day it ships.
        from db.migrations import PLATFORM_ALERT_HEAL_PREFIXES
        # Prefixes reserved AFTER the heal shipped, whose rows can never sit in
        # `responded` (the state the heal fixes), so the frozen list rightly
        # lacks them. trinity-enterprise#631: a person tag starts `delivered`
        # and ends `read` — it never enters the ask lifecycle at all.
        reserved_after_heal = {"mention-"}
        assert set(PLATFORM_ALERT_HEAL_PREFIXES) == set(oqs._RESERVED_ID_PREFIXES) - reserved_after_heal

    def test_is_registered_on_both_tracks(self):
        from pathlib import Path
        from db.migrations import MIGRATIONS
        assert "platform_alert_responded_heal" in [name for name, _ in MIGRATIONS]
        versions = Path(_BACKEND) / "migrations" / "versions"
        assert any("platform_alert_responded_heal" in p.name for p in versions.glob("*.py"))
