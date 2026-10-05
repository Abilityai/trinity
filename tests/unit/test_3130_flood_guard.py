"""#3130 — the operator-queue flood guard must not feed on itself.

Before: the #1632 flood alert was a raw platform create filed against the same
agent whose depth it reported, the depth read counted it, and a sustained
over-cap condition minted a fresh timestamped alert every cooldown window —
monotonic, self-sustaining growth (435 rows, 386 of them flood alerts, on one
install). A depth-held file entry was never reported back to the agent.

Pinned here:
  * the depth reads that bound an agent's OWN asks (the file seam's cap and the
    native `ask_operator` `queue_full`) do not count platform-minted rows;
  * the flood alert is a budgeted platform alert (#1677 `create_bounded_alert`,
    type `queue_flood`) and is edge-triggered: one per over-cap episode, however
    many cycles or cooldown windows it lasts, re-armed only when it clears;
  * a held file is told so, in a platform-owned `platform.ingestion` block,
    written once per state change and removed when nothing is held;
  * the backlog migration keeps the newest pending flood alert per agent and
    cancels the rest.
"""
import asyncio
import json
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
from services.operator_queue_service import (  # noqa: E402
    OPERATOR_QUEUE_MAX_PENDING_PER_AGENT,
    OperatorQueueSyncService,
)
from services.rate_limiter import RateLimitResult  # noqa: E402


# ---------------------------------------------------------------------------
# Mocked sync harness (the test_1632 shape, with the count keyed on its kwargs)
# ---------------------------------------------------------------------------

def _fake_db(pending=0, flood_pending=0):
    db = MagicMock()
    counts = []

    def _count(agent, item_type=None, exclude_request_id_prefixes=None):
        counts.append({"item_type": item_type, "exclude": exclude_request_id_prefixes})
        return flood_pending if item_type == "queue_flood" else pending

    db.count_operator_queue_pending_for_agent.side_effect = _count
    db._counts = counts
    db.get_operator_queue_responded_for_agent.return_value = []
    db.get_operator_queue_terminal_for_agent.return_value = []
    db.get_operator_queue_sync_index_for_agent.return_value = {"open": [], "terminal": {}, "foreign": []}
    db.set_operator_queue_sync_state.return_value = False
    db.set_operator_queue_delivery_state.return_value = False
    db.create_operator_queue_item_with_outcome.side_effect = (
        lambda agent, item, **kw: (db.create_operator_queue_item(agent, item, **kw), True))
    db.mark_operator_queue_unconfirmed.return_value = 0
    db.refresh_operator_queue_last_confirmed.return_value = 0
    db.get_setting_value.return_value = "24"
    return db


def _file(requests, platform=None):
    data = {"requests": list(requests)}
    if platform is not None:
        data["platform"] = platform
    return json.dumps(data)


def _pending(n, start=0):
    return [{"id": f"req-{i}", "status": "pending", "title": "t", "question": "q"}
            for i in range(start, start + n)]


def _wire(monkeypatch, db, content, *, write_ok=True, rate_allowed=True):
    monkeypatch.setattr(oqs, "db", db)
    state = {"content": content, "writes": []}
    client = MagicMock()

    async def _read(path, timeout=5.0):
        return {"success": True, "content": state["content"]}

    async def _write(path, content, **kw):
        state["writes"].append(json.loads(content))
        if write_ok:
            state["content"] = content
            return {"success": True}
        return {"success": False, "status_code": 412}

    client.read_file = AsyncMock(side_effect=_read)
    client.write_file = AsyncMock(side_effect=_write)
    monkeypatch.setattr(oqs, "AgentClient", lambda name: client)
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(rate_allowed, 10, 0 if rate_allowed else 60, 60))
    oqs.reset_alert_budget_state()
    return OperatorQueueSyncService(), state


def _flood_alerts(db):
    return [c.args[1] for c in db.create_operator_queue_item.call_args_list
            if str(c.args[1].get("id", "")).startswith("queue-flood-")]


def _run(svc, agent="a", cycles=1):
    for _ in range(cycles):
        asyncio.run(svc._sync_agent(agent))


# ---------------------------------------------------------------------------
# AC 1, half 2 — the cap measures the agent's own filing
# ---------------------------------------------------------------------------

class TestFileSeamCapReadsOwnRowsOnly:
    def test_the_depth_read_excludes_platform_minted_rows(self, monkeypatch):
        db = _fake_db(pending=0)
        svc, _ = _wire(monkeypatch, db, _file(_pending(1)))
        _run(svc)
        depth_reads = [c for c in db._counts if c["item_type"] is None]
        assert depth_reads, "the sync did not read the agent's depth"
        assert all(c["exclude"] == oqs._RESERVED_ID_PREFIXES for c in depth_reads)


class TestOwnBudgetCountRealDb:
    """Against the unit island's real SQLite (`init_database()` schema)."""

    @pytest.fixture
    def agent(self):
        return f"agent-3130-{uuid.uuid4().hex[:6]}"

    @pytest.fixture
    def real_db(self):
        from database import db
        return db

    def _row(self, real_db, agent, rid, *, type_="question"):
        real_db.create_operator_queue_item(agent, {
            "id": rid, "type": type_, "status": "pending", "title": "t", "question": "q",
        })

    def test_flood_rows_do_not_count_toward_the_agents_cap(self, real_db, agent):
        for i in range(3):
            self._row(real_db, agent, f"queue-flood-{agent}-2026-10-0{i}", type_="queue_flood")
        self._row(real_db, agent, "mine-1")
        self._row(real_db, agent, "mine-2")

        own = real_db.count_operator_queue_pending_for_agent(
            agent, exclude_request_id_prefixes=oqs._RESERVED_ID_PREFIXES)
        everything = real_db.count_operator_queue_pending_for_agent(agent)
        budget = real_db.count_operator_queue_pending_for_agent(agent, item_type="queue_flood")

        assert own == 2
        assert everything == 5                 # the default read is unchanged
        assert budget == 3                     # the #1677 per-type budget still sees them

    def _native(self, real_db, agent, rid, max_pending):
        return real_db.create_native_operator_queue_item(
            agent, {"id": rid, "type": "question", "status": "pending",
                    "title": "t", "question": "q"},
            max_pending=max_pending, channel="mcp", raised_by="agent", to_role=None,
            resolved_to=None, proposal=None, supersedes_expired=None,
            exclude_request_id_prefixes=oqs._RESERVED_ID_PREFIXES,
        )

    def test_platform_rows_never_make_ask_operator_queue_full(self, real_db, agent):
        for i in range(4):
            self._row(real_db, agent, f"queue-flood-{agent}-x{i}", type_="queue_flood")
        out = self._native(real_db, agent, "ask-1", max_pending=2)
        assert out["outcome"] == "created"

    def test_the_agents_own_asks_still_fill_the_native_cap(self, real_db, agent):
        self._row(real_db, agent, "mine-a")
        self._row(real_db, agent, "mine-b")
        assert self._native(real_db, agent, "ask-2", max_pending=2)["outcome"] == "queue_full"

    def test_ask_service_passes_the_reserved_prefixes(self, monkeypatch):
        import services.ask_service as ask_service
        seen = {}

        def _create(agent, item, **kw):
            seen.update(kw)
            return {"outcome": "queue_full", "row": None}

        monkeypatch.setattr(ask_service.db, "create_native_operator_queue_item", _create)
        monkeypatch.setattr(ask_service.db, "get_operator_queue_item_for_agent_by_request_id",
                            lambda *a: None)
        monkeypatch.setattr(ask_service, "_rate_allowed", lambda *a: True)
        with pytest.raises(ask_service.AskRejected):
            ask_service.raise_ask(
                "agent-x", {"request_id": "r-1", "type": "question", "title": "t",
                            "question": "q", "to": "operator"},
                raised_by="agent", channel="mcp", actor_user=None)
        assert seen.get("exclude_request_id_prefixes") == oqs._RESERVED_ID_PREFIXES


# ---------------------------------------------------------------------------
# AC 1, half 1 + AC 2 — a budgeted, edge-triggered flood alert
# ---------------------------------------------------------------------------

class TestFloodAlertIsBoundedAndEdgeTriggered:
    def test_the_flood_alert_is_a_registered_budgeted_type(self):
        assert "queue_flood" in oqs._BUDGETED_ALERT_TYPES

    def test_it_is_created_through_the_budget(self, monkeypatch):
        db = _fake_db(pending=OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        svc, _ = _wire(monkeypatch, db, _file(_pending(3)))
        _run(svc)
        alerts = _flood_alerts(db)
        assert len(alerts) == 1
        assert alerts[0]["type"] == "queue_flood"
        assert alerts[0]["priority"] == "high"
        assert {"item_type": "queue_flood", "exclude": None} in db._counts

    def test_a_sustained_over_cap_condition_alerts_once(self, monkeypatch):
        """The reported shape: over the cap for hours. Cycles span many cooldown
        windows (the clock is advanced past the cooldown every cycle)."""
        clock = {"t": 1000.0}
        monkeypatch.setattr(oqs.time, "monotonic", lambda: clock["t"])
        db = _fake_db(pending=OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        svc, _ = _wire(monkeypatch, db, _file(_pending(3)))
        for _ in range(50):
            _run(svc)
            clock["t"] += oqs.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS + 1
        assert len(_flood_alerts(db)) == 1

    def test_it_re_arms_only_after_the_condition_clears(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(oqs.time, "monotonic", lambda: clock["t"])
        db = _fake_db(pending=OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        svc, state = _wire(monkeypatch, db, _file(_pending(3)))
        _run(svc, cycles=3)
        assert len(_flood_alerts(db)) == 1

        # Cleared: the operator resolved rows, nothing is held this cycle.
        db.count_operator_queue_pending_for_agent.side_effect = (
            lambda agent, item_type=None, exclude_request_id_prefixes=None: 0)
        state["content"] = _file([])
        clock["t"] += oqs.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS + 1
        _run(svc)
        assert len(_flood_alerts(db)) == 1

        # A new episode.
        db.count_operator_queue_pending_for_agent.side_effect = (
            lambda agent, item_type=None, exclude_request_id_prefixes=None:
            0 if item_type else OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        state["content"] = _file(_pending(2, start=10))
        clock["t"] += oqs.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS + 1
        _run(svc)
        assert len(_flood_alerts(db)) == 2

    def test_the_budget_is_the_hard_backstop(self, monkeypatch):
        """With the per-agent edge state lost on every cycle (a fresh service
        each time — worst-case failover churn), the budget still refuses once
        `queue_flood` rows fill it."""
        clock = {"t": 1000.0}
        monkeypatch.setattr(oqs.time, "monotonic", lambda: clock["t"])
        db = _fake_db(pending=OPERATOR_QUEUE_MAX_PENDING_PER_AGENT,
                      flood_pending=oqs.OPERATOR_ALERT_MAX_PENDING_PER_TYPE)
        for _ in range(10):
            svc, _ = _wire(monkeypatch, db, _file(_pending(3)))
            _run(svc)
            clock["t"] += oqs.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS + 1
        assert _flood_alerts(db) == []


# ---------------------------------------------------------------------------
# AC 3 — a held file is told so
# ---------------------------------------------------------------------------

class TestIngestionMarker:
    def _marker(self, state):
        return json.loads(state["content"]).get("platform", {}).get("ingestion")

    def test_a_depth_held_file_carries_queue_full(self, monkeypatch):
        db = _fake_db(pending=OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        svc, state = _wire(monkeypatch, db, _file(_pending(3)))
        _run(svc)
        marker = self._marker(state)
        assert marker["reason"] == "queue_full"
        assert marker["max_pending"] == OPERATOR_QUEUE_MAX_PENDING_PER_AGENT
        assert marker["since"]
        # The entries themselves are untouched: still pending, still the agent's.
        assert [r["status"] for r in json.loads(state["content"])["requests"]] == ["pending"] * 3

    def test_a_rate_held_file_carries_rate_limited(self, monkeypatch):
        db = _fake_db(pending=0)
        svc, state = _wire(monkeypatch, db, _file(_pending(2)), rate_allowed=False)
        _run(svc)
        assert self._marker(state)["reason"] == "rate_limited"

    def test_an_unchanged_hold_is_not_rewritten(self, monkeypatch):
        db = _fake_db(pending=OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        svc, state = _wire(monkeypatch, db, _file(_pending(3)))
        _run(svc)
        first = self._marker(state)
        _run(svc, cycles=3)
        assert len(state["writes"]) == 1
        assert self._marker(state) == first

    def test_the_marker_is_removed_once_nothing_is_held(self, monkeypatch):
        db = _fake_db(pending=0)
        held = {"reason": "queue_full", "max_pending": 25, "since": "2026-10-01T00:00:00Z"}
        svc, state = _wire(monkeypatch, db, _file(_pending(1), platform={"ingestion": held}))
        _run(svc)
        assert self._marker(state) is None
        _run(svc)
        assert len(state["writes"]) == 1

    def test_a_refused_write_is_retried_only_after_the_file_changes(self, monkeypatch):
        db = _fake_db(pending=OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        svc, state = _wire(monkeypatch, db, _file(_pending(3)), write_ok=False)
        _run(svc, cycles=4)
        assert len(state["writes"]) == 1          # 412, then no retry on the same file
        state["content"] = _file(_pending(4))     # the agent wrote
        _run(svc)
        assert len(state["writes"]) == 2

    def test_a_write_back_without_the_marker_argument_leaves_the_marker_alone(self, monkeypatch):
        """The default is "unchanged", never "remove" — a future caller that
        omits the argument must not strip a hold or force a write."""
        held = {"reason": "queue_full", "max_pending": 25, "since": "2026-10-01T00:00:00Z"}
        db = _fake_db()
        svc, state = _wire(monkeypatch, db, _file(_pending(1), platform={"ingestion": held}))
        from services.agent_client import AgentClient  # noqa: F401 — type only
        client = oqs.AgentClient("a")
        out = asyncio.run(svc._write_responses_to_agent(
            "a", client, json.loads(state["content"]), [], [], True, content_sha="x"))
        assert out is None
        assert state["writes"] == []

    def test_a_marker_the_agent_wrote_is_not_trusted(self, monkeypatch):
        """An agent pre-writing `platform.ingestion` cannot suppress the
        platform's own reason: a different reason is overwritten."""
        db = _fake_db(pending=OPERATOR_QUEUE_MAX_PENDING_PER_AGENT)
        fake = {"reason": "rate_limited", "max_pending": 999, "since": "x"}
        svc, state = _wire(monkeypatch, db, _file(_pending(3), platform={"ingestion": fake}))
        _run(svc)
        marker = self._marker(state)
        assert marker["reason"] == "queue_full"
        assert marker["max_pending"] == OPERATOR_QUEUE_MAX_PENDING_PER_AGENT


# ---------------------------------------------------------------------------
# AC 4 — the backlog migration
# ---------------------------------------------------------------------------

class TestBacklogMigration:
    @pytest.fixture
    def conn(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE operator_queue (
            id TEXT PRIMARY KEY, agent_name TEXT, request_id TEXT, type TEXT,
            status TEXT, created_at TEXT, disposition TEXT, disposed_at TEXT,
            disposed_by TEXT, disposed_by_email TEXT, disposition_reason TEXT,
            batch_id TEXT)""")
        return conn

    def _add(self, conn, id_, agent, rid, created, status="pending"):
        conn.execute("INSERT INTO operator_queue (id, agent_name, request_id, type, status, created_at) "
                     "VALUES (?, ?, ?, 'alert', ?, ?)", (id_, agent, rid, status, created))

    def test_keeps_the_newest_pending_flood_alert_per_agent(self, conn):
        from db.migrations import _migrate_supersede_queue_flood_backlog
        for i in range(5):
            self._add(conn, f"a{i}", "a", f"queue-flood-a-2026-09-29T0{i}", f"2026-09-29T0{i}:00:00Z")
        self._add(conn, "b0", "b", "queue-flood-b-1", "2026-09-30T00:00:00Z")
        self._add(conn, "a-own", "a", "mine-1", "2026-09-28T00:00:00Z")
        self._add(conn, "a-old", "a", "queue-flood-a-old", "2026-09-01T00:00:00Z", status="responded")

        _migrate_supersede_queue_flood_backlog(conn.cursor(), conn)

        rows = dict(conn.execute("SELECT id, status FROM operator_queue").fetchall())
        assert rows["a4"] == "pending"                                  # the newest survives
        assert [rows[f"a{i}"] for i in range(4)] == ["cancelled"] * 4
        assert rows["b0"] == "pending"                                  # another agent's single row
        assert rows["a-own"] == "pending"                               # never the agent's own ask
        assert rows["a-old"] == "responded"                             # only pending rows
        cancelled = conn.execute(
            "SELECT DISTINCT disposition, disposed_by, disposition_reason, disposed_by_email "
            "FROM operator_queue WHERE status='cancelled'").fetchall()
        assert cancelled == [("cancelled", "platform", "superseded", None)]

    def test_is_idempotent(self, conn):
        from db.migrations import _migrate_supersede_queue_flood_backlog
        for i in range(3):
            self._add(conn, f"a{i}", "a", f"queue-flood-a-{i}", f"2026-09-29T0{i}:00:00Z")
        _migrate_supersede_queue_flood_backlog(conn.cursor(), conn)
        _migrate_supersede_queue_flood_backlog(conn.cursor(), conn)
        assert conn.execute("SELECT COUNT(*) FROM operator_queue WHERE status='pending'").fetchone()[0] == 1

    def test_is_registered_on_both_tracks(self):
        from pathlib import Path
        from db.migrations import MIGRATIONS
        assert "supersede_queue_flood_backlog" in [name for name, _ in MIGRATIONS]
        versions = Path(_BACKEND) / "migrations" / "versions"
        assert any("queue_flood_backlog" in p.name for p in versions.glob("*.py"))
