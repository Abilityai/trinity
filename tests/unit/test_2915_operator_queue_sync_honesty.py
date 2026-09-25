"""The operator-queue file sync tells the truth (#2915).

Trinity 1.0 release gate. A pending approval sat unseen for six days because
the card the human read went stale while the container↔platform sync stayed
silent. The file contract is unchanged (ingestion is create-only; an agent's
rewrite is NEVER applied in place); what this suite pins is that every way the
two sides can disagree is detected, recorded on the row, refused where it must
be, and audited — instead of presenting as fine.

Related flow: docs/memory/feature-flows/operating-room.md

Harness: the mocked shape of test_1632 (`_wire`: fake db + fake AgentClient),
plus a real per-process SQLite for the accessor tests (the unit conftest pins
`TRINITY_DB_PATH`; `init_database()` builds the full schema including the
#2915 columns — the live `select(table.c.<new_col>)` the ledger asks for).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import services.operator_queue_service as oqs  # noqa: E402
from services.operator_queue_service import (  # noqa: E402
    OperatorQueueSyncService,
    SYNC_CHANGED, SYNC_CLOSED_BY_FILER, SYNC_CONFIRMED, SYNC_MISSING, SYNC_STALE_ID,
    DELIVERY_DELIVERED, DELIVERY_NOT_APPLICABLE, DELIVERY_UNDELIVERED,
    REFUSE_RESPONSE_STATES, READ_FAILURE_THRESHOLD,
    changed_fields, is_aged, annotate_aging, _entry_content, _row_content,
)
from services.rate_limiter import RateLimitResult  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Wiring (mirrors test_1632 / test_1525; explicit returns, never MagicMock defaults)
# ---------------------------------------------------------------------------

def _recent(hours_ago=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(rid="req-1", status="pending", **over):
    """A fresh row by default (created an hour ago): the 24 h aging bound must
    not fire in tests that are not about aging, or the receipt write-back runs
    and consumes the fake client's read sequence."""
    r = {
        "id": f"uuid-{rid}", "request_id": rid, "agent_name": "a", "type": "approval",
        "status": status, "priority": "high", "title": "Approve payout",
        "question": "Release 500 USDC?", "options": ["approve", "reject"], "context": {},
        "execution_id": None, "created_at": _recent(), "expires_at": None,
        "response": "approve" if status == "responded" else None, "response_text": None,
        "responded_by_id": None, "responded_by_email": "op@example.com" if status == "responded" else None,
        "responded_at": "2026-09-02T10:00:00Z" if status == "responded" else None,
        "acknowledged_at": None, "cleared_at": None, "addressed_to_email": None,
        "sync_state": "confirmed", "sync_detail": None, "sync_updated_at": None,
        "last_confirmed_at": None, "delivery_state": None, "delivery_detail": None,
        "delivery_updated_at": None,
    }
    r.update(over)
    return r


def _entry(rid="req-1", status="pending", **over):
    e = {"id": rid, "type": "approval", "status": status, "priority": "high",
         "title": "Approve payout", "question": "Release 500 USDC?",
         "options": ["approve", "reject"], "created_at": _recent()}
    e.update(over)
    return e


def _fake_db(open_rows=(), terminal=None, responded=(), terminal_items=(), pending=0):
    db = MagicMock()
    db.count_operator_queue_pending_for_agent.return_value = pending
    db.get_operator_queue_sync_index_for_agent.return_value = {
        "open": [dict(r) for r in open_rows], "terminal": dict(terminal or {}),
    }
    db.set_operator_queue_sync_state.return_value = True
    db.set_operator_queue_delivery_state.return_value = True
    db.mark_operator_queue_unconfirmed.return_value = 1
    db.refresh_operator_queue_last_confirmed.return_value = 0
    db.mark_operator_queue_acknowledged.return_value = None
    db.get_operator_queue_responded_for_agent.return_value = [dict(r) for r in responded]
    db.get_operator_queue_terminal_for_agent.return_value = [dict(r) for r in terminal_items]
    db.get_setting_value.return_value = "24"
    db.create_operator_queue_item.return_value = "uuid-new"
    db.mark_operator_queue_expired.return_value = 0
    db.mark_operator_queue_undelivered_for_stopped_agents.return_value = []
    return db


def _client(file_content, *, reads=None, write=None):
    """A fake agent client. `reads`: a list of successive read_file results
    (the cycle-start read, then the pre-write re-read); `write`: write_file result."""
    client = MagicMock()
    if reads is None:
        reads = [{"success": True, "content": file_content}]
    client.read_file = AsyncMock(side_effect=list(reads) + [reads[-1]] * 5)
    client.write_file = AsyncMock(return_value=write or {"success": True})
    return client


def _wire(monkeypatch, db, client, audit=None):
    monkeypatch.setattr(oqs, "db", db)
    monkeypatch.setattr(oqs, "AgentClient", lambda name: client)
    monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    audit = audit if audit is not None else AsyncMock()
    monkeypatch.setattr(oqs, "_audit_sync", audit)
    svc = OperatorQueueSyncService()
    return svc, audit


def _file(*entries):
    return json.dumps({"$schema": "operator-queue-v1", "requests": list(entries)})


def _sync_calls(db):
    return [(c.args[1], c.args[2]) for c in db.set_operator_queue_sync_state.call_args_list]


def _delivery_calls(db):
    return [(c.args[0], c.args[1], c.args[2]) for c in db.set_operator_queue_delivery_state.call_args_list]


def _audit_actions(audit):
    return [c.args[0] for c in audit.call_args_list]


# ===========================================================================
# 1. The reproduction fixtures — the three mechanisms from the field trace
#    plus the four the issue body names. Every one used to be silent.
# ===========================================================================

class TestReproduceTheSilence:
    def test_a_rewritten_entry_is_changed_and_the_row_is_untouched(self, monkeypatch):
        """Item A of the field trace: the agent rewrote title/body/options/expiry
        on the container; the platform kept the original. Now: `changed` with
        the field names, and the row content is NOT rewritten (premise 1)."""
        row = _row()
        db = _fake_db(open_rows=[row])
        entry = _entry(title="Rewritten title", question="Update 2026-09-08: new facts",
                       options=["go", "no-go", "later"], expires_at="2026-09-22T23:59:00Z")
        svc, audit = _wire(monkeypatch, db, _client(_file(entry)))
        asyncio.run(svc._sync_agent("a"))

        assert (SYNC_CHANGED, "title,question,options,expires_at") in _sync_calls(db)
        db.create_operator_queue_item.assert_not_called()          # never re-ingested
        assert "diverged" in _audit_actions(audit)
        # the row's own content was not modified by anything the loop did
        assert row["title"] == "Approve payout"

    def test_an_agent_acknowledgement_on_a_pending_row_is_closed_by_filer(self, monkeypatch):
        """Item B: the agent marked its entry `acknowledged` although the platform
        never responded. The ack UPDATE matches only `responded` rows, so it used
        to be dropped and the human kept a pending card."""
        db = _fake_db(open_rows=[_row()])
        db.mark_operator_queue_acknowledged.return_value = None  # no responded row matched
        svc, audit = _wire(monkeypatch, db, _client(_file(_entry(status="acknowledged"))))
        asyncio.run(svc._sync_agent("a"))
        assert (SYNC_CLOSED_BY_FILER, "acknowledged") in _sync_calls(db)
        assert "diverged" in _audit_actions(audit)

    def test_an_agent_side_withdrawal_is_closed_by_filer_with_a_folded_status(self, monkeypatch):
        db = _fake_db(open_rows=[_row()])
        svc, _ = _wire(monkeypatch, db, _client(_file(_entry(status="Withdrawn — SEE NOTE"))))
        asyncio.run(svc._sync_agent("a"))
        # the agent's free text never reaches the column: folded or `other`
        states = _sync_calls(db)
        assert states and states[0][0] == SYNC_CLOSED_BY_FILER
        assert states[0][1] == "other"

    def test_a_pruned_entry_is_missing(self, monkeypatch):
        db = _fake_db(open_rows=[_row()])
        svc, _ = _wire(monkeypatch, db, _client(_file()))   # file exists, entry gone
        asyncio.run(svc._sync_agent("a"))
        assert (SYNC_MISSING, "entry_missing") in _sync_calls(db)

    def test_a_missing_file_is_missing_file(self, monkeypatch):
        db = _fake_db(open_rows=[_row()])
        svc, _ = _wire(monkeypatch, db, _client(None, reads=[{"success": True, "content": None, "not_found": True}]))
        asyncio.run(svc._sync_agent("a"))
        assert (SYNC_MISSING, "file_missing") in _sync_calls(db)

    def test_a_reused_id_on_a_terminal_row_is_stale_id_not_a_new_item(self, monkeypatch):
        """The engineering-review regression: with only open rows indexed, a
        pending entry re-using a closed row's id would be re-created every cycle
        (the on-conflict create returns the old uuid silently) — a phantom admit
        against the depth cap and a "new" broadcast forever."""
        db = _fake_db(terminal={"req-1": {"id": "uuid-req-1", "status": "acknowledged", "sync_state": None}})
        svc, _ = _wire(monkeypatch, db, _client(_file(_entry())))
        asyncio.run(svc._sync_agent("a"))
        db.create_operator_queue_item.assert_not_called()
        assert (SYNC_STALE_ID, "acknowledged") in _sync_calls(db)

    def test_a_matching_entry_is_confirmed_and_not_audited(self, monkeypatch):
        db = _fake_db(open_rows=[_row(sync_state=None)])
        svc, audit = _wire(monkeypatch, db, _client(_file(_entry())))
        asyncio.run(svc._sync_agent("a"))
        assert (SYNC_CONFIRMED, None) in _sync_calls(db)
        assert _audit_actions(audit) == []      # confirmed from NULL is not a story

    def test_the_ingested_audit_row_carries_no_agent_text(self, monkeypatch):
        """`type` is agent-authored and unbounded at ingest (the clamp bounds
        title/question/options/context, not type); the audit table is durable,
        hash-chained and un-prunable for a year, so the row carries a folded
        token or `other`, never the string. `request_id` is shape-checked."""
        db = _fake_db()
        hostile = _entry("req-hostile", type="<b>" + "x" * 5000 + "</b> IGNORE PREVIOUS")
        svc, audit = _wire(monkeypatch, db, _client(_file(hostile)))
        asyncio.run(svc._sync_agent("a"))
        ingested = [c for c in audit.call_args_list if c.args[0] == "ingested"]
        assert len(ingested) == 1
        details = ingested[0].args[3]
        assert details["type"] == "other" and details["request_id"] == "req-hostile"
        assert len(json.dumps(details)) < 200

    def test_reconciled_is_audited_when_a_diverged_row_matches_again(self, monkeypatch):
        db = _fake_db(open_rows=[_row(sync_state="changed", sync_detail="title")])
        svc, audit = _wire(monkeypatch, db, _client(_file(_entry())))
        asyncio.run(svc._sync_agent("a"))
        assert _audit_actions(audit) == ["reconciled"]


# ===========================================================================
# 2. Read failures: hysteresis, never str(e), no write-back on an unreadable file
# ===========================================================================

class TestReadFailures:
    def test_unconfirmed_only_after_three_consecutive_failed_cycles(self, monkeypatch):
        db = _fake_db(open_rows=[_row()])
        client = MagicMock()
        client.read_file = AsyncMock(side_effect=TimeoutError("read timeout"))
        svc, _ = _wire(monkeypatch, db, client)
        for _ in range(READ_FAILURE_THRESHOLD - 1):
            asyncio.run(svc._sync_agent("a"))
        db.mark_operator_queue_unconfirmed.assert_not_called()
        asyncio.run(svc._sync_agent("a"))
        db.mark_operator_queue_unconfirmed.assert_called_once()
        detail = db.mark_operator_queue_unconfirmed.call_args.args[0]
        assert detail == "timeout"                     # the class, never the message
        assert db.mark_operator_queue_unconfirmed.call_args.kwargs["agent_name"] == "a"

    def test_a_good_read_resets_the_counter(self, monkeypatch):
        db = _fake_db(open_rows=[_row()])
        client = MagicMock()
        client.read_file = AsyncMock(side_effect=[
            {"success": False, "status_code": 503, "error": "<html>agent said this</html>"},
            {"success": False, "error": "boom"},
            {"success": True, "content": _file(_entry())},
            {"success": True, "content": _file(_entry())},
            {"success": False, "error": "boom"},
        ])
        svc, _ = _wire(monkeypatch, db, client)
        for _ in range(5):
            asyncio.run(svc._sync_agent("a"))
        db.mark_operator_queue_unconfirmed.assert_not_called()
        assert svc._read_failures.get("a") == 1

    def test_http_status_becomes_a_token_never_the_body(self, monkeypatch):
        db = _fake_db(open_rows=[_row()])
        client = MagicMock()
        client.read_file = AsyncMock(return_value={"success": False, "status_code": 503, "error": "<b>agent text</b>"})
        svc, _ = _wire(monkeypatch, db, client)
        for _ in range(READ_FAILURE_THRESHOLD):
            asyncio.run(svc._sync_agent("a"))
        assert db.mark_operator_queue_unconfirmed.call_args.args[0] == "http_503"

    def test_invalid_json_never_writes_back(self, monkeypatch):
        """Before: an unparseable file became an empty request list and the
        write-back overwrote the agent's file with the reconstructed responses
        alone. Now: `unconfirmed:invalid_json`, and NO write this cycle."""
        db = _fake_db(open_rows=[_row()], responded=[_row(status="responded")])
        client = _client("{not json")
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_not_called()
        assert db.mark_operator_queue_unconfirmed.call_args.args[0] == "invalid_json"
        # The FIRST guard, not the pre-write re-read's: an unparseable file is
        # never reconciled, so no open row is called `missing` on its account.
        db.get_operator_queue_sync_index_for_agent.assert_not_called()
        assert not any(s == SYNC_MISSING for s, _ in _sync_calls(db))

    def test_oversize_file_is_unconfirmed_not_missing(self, monkeypatch):
        db = _fake_db(open_rows=[_row()])
        big = "x" * (oqs.OPERATOR_QUEUE_MAX_FILE_BYTES + 1)
        svc, _ = _wire(monkeypatch, db, _client(big))
        svc._maybe_emit_flood_alert = AsyncMock()
        asyncio.run(svc._sync_agent("a"))
        assert db.mark_operator_queue_unconfirmed.call_args.args[0] == "oversize_file"
        assert not any(s == SYNC_MISSING for s, _ in _sync_calls(db))


# ===========================================================================
# 3. The poll cycle: tri-state sweep, expiry hoisted, one broadcast per cycle
# ===========================================================================

class TestPollCycle:
    def _run_cycle(self, monkeypatch, states, db=None):
        db = db or _fake_db()
        monkeypatch.setattr(oqs, "db", db)
        import services.docker_service as ds
        monkeypatch.setattr(ds, "agent_container_states", lambda: states)
        svc = OperatorQueueSyncService()
        svc._try_acquire_leadership = lambda: True
        svc._sync_agent = AsyncMock()
        ws = MagicMock(); ws.broadcast = AsyncMock()
        monkeypatch.setattr(oqs, "_websocket_manager", ws)
        asyncio.run(svc._poll_cycle())
        return db, svc, ws

    def test_docker_unreadable_sweeps_nothing(self, monkeypatch):
        db, svc, _ = self._run_cycle(monkeypatch, None)
        db.mark_operator_queue_unconfirmed.assert_not_called()
        svc._sync_agent.assert_not_called()

    def test_stopped_agents_are_swept_and_running_ones_synced(self, monkeypatch):
        db, svc, _ = self._run_cycle(monkeypatch, {"a": "running", "b": "stopped"})
        db.mark_operator_queue_unconfirmed.assert_called_once()
        assert db.mark_operator_queue_unconfirmed.call_args.args[0] == "agent_not_running"
        assert db.mark_operator_queue_unconfirmed.call_args.kwargs["exclude_agents"] == ["a"]
        svc._sync_agent.assert_awaited_once_with("a")

    def test_an_all_stopped_fleet_still_expires_and_sweeps(self, monkeypatch):
        """The early return used to sit above expiry: nothing expired while no
        agent ran. Now expiry runs first, and the sweep covers every open row
        (an explicit empty-exclude branch — never `notin_([])`)."""
        db, svc, _ = self._run_cycle(monkeypatch, {"b": "stopped"})
        db.mark_operator_queue_expired.assert_called_once()
        assert db.mark_operator_queue_unconfirmed.call_args.kwargs["exclude_agents"] == []
        svc._sync_agent.assert_not_called()

    def test_one_thin_broadcast_per_cycle_only_when_something_changed(self, monkeypatch):
        db = _fake_db(); db.mark_operator_queue_unconfirmed.return_value = 3
        _, _, ws = self._run_cycle(monkeypatch, {"a": "running", "b": "stopped"}, db=db)
        payloads = [json.loads(c.args[0]) for c in ws.broadcast.call_args_list]
        assert [p["type"] for p in payloads] == ["operator_queue_sync"]
        assert payloads[0]["data"] == {}       # #918: a thin trigger, no payload
        db2 = _fake_db(); db2.mark_operator_queue_unconfirmed.return_value = 0
        _, _, ws2 = self._run_cycle(monkeypatch, {"a": "running"}, db=db2)
        ws2.broadcast.assert_not_called()


# ===========================================================================
# 4. The write-back: deliver only into a matching pending entry; record the rest
# ===========================================================================

class TestWriteBack:
    def test_a_matching_pending_entry_receives_the_answer_and_is_delivered(self, monkeypatch):
        resp = _row(status="responded")
        db = _fake_db(open_rows=[resp], responded=[resp])
        client = _client(_file(_entry()))
        svc, audit = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_awaited_once()
        written = json.loads(client.write_file.call_args.args[1])
        assert written["requests"][0]["status"] == "responded"
        assert written["requests"][0]["response"] == "approve"
        assert client.write_file.call_args.kwargs["if_match"]      # CAS on what was read
        assert ("uuid-req-1", DELIVERY_DELIVERED, None) in _delivery_calls(db)
        assert "written_back" in _audit_actions(audit)

    def test_an_answer_is_never_delivered_into_a_rewritten_entry(self, monkeypatch):
        """The ent#164 TOCTOU: human approved v1, agent rewrote to v2 in between."""
        resp = _row(status="responded")
        db = _fake_db(open_rows=[resp], responded=[resp])
        client = _client(_file(_entry(question="A DIFFERENT question")))
        svc, audit = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_not_called()
        assert ("uuid-req-1", DELIVERY_UNDELIVERED, "entry_changed") in _delivery_calls(db)
        assert "undeliverable" in _audit_actions(audit)

    def test_a_refused_write_is_recorded_as_conflict_and_not_lost(self, monkeypatch):
        resp = _row(status="responded")
        db = _fake_db(open_rows=[resp], responded=[resp])
        client = _client(_file(_entry()), write={"success": False, "status_code": 412, "error": "if_match mismatch"})
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        assert ("uuid-req-1", DELIVERY_UNDELIVERED, "conflict") in _delivery_calls(db)

    def test_a_write_failure_records_a_token_never_the_error_text(self, monkeypatch):
        resp = _row(status="responded")
        db = _fake_db(open_rows=[resp], responded=[resp])
        client = _client(_file(_entry()), write={"success": False, "status_code": 500, "error": "<agent controlled>"})
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        assert ("uuid-req-1", DELIVERY_UNDELIVERED, "http_500") in _delivery_calls(db)

    def test_the_pre_write_reread_is_what_gets_written(self, monkeypatch):
        """The agent appended an entry between the cycle-start read and the write:
        the write is built from the RE-READ, so the new entry survives (narrowed,
        not closed — the CAS on the agent server is the real guard)."""
        resp = _row(status="responded")
        db = _fake_db(open_rows=[resp], responded=[resp])
        first = _file(_entry())
        second = _file(_entry(), _entry("req-2", title="appended meanwhile"))
        client = _client(None, reads=[{"success": True, "content": first}, {"success": True, "content": second}])
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        written = json.loads(client.write_file.call_args.args[1])
        assert [r["id"] for r in written["requests"]] == ["req-1", "req-2"]
        import hashlib
        assert client.write_file.call_args.kwargs["if_match"] == hashlib.sha256(second.encode()).hexdigest()

    def test_a_platform_minted_responded_row_is_not_applicable_and_never_written(self, monkeypatch):
        alarm = _row("cb-dormant-a-2026", status="responded", title="circuit dormant")
        db = _fake_db(responded=[alarm])
        client = _client(_file())
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_not_called()
        assert ("uuid-cb-dormant-a-2026", DELIVERY_NOT_APPLICABLE, "platform_minted") in _delivery_calls(db)

    def test_a_platform_alarm_already_in_the_file_never_flip_flops(self, monkeypatch):
        """Seen live on the first run: files written before ent#499 still carry
        platform alarms as `responded` entries. Matching them flipped the row
        delivered ↔ not_applicable every cycle and minted an audit row each time."""
        alarm = _row("cb-dormant-a-2026", status="responded", title="circuit dormant")
        db = _fake_db(responded=[alarm])
        client = _client(_file(_entry("cb-dormant-a-2026", status="responded", title="circuit dormant")))
        svc, audit = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        alarm["delivery_state"] = "not_applicable"; alarm["delivery_detail"] = "platform_minted"
        asyncio.run(svc._sync_agent("a"))
        states = {s for _, s, _ in _delivery_calls(db)}
        assert states == {DELIVERY_NOT_APPLICABLE}
        assert "written_back" not in _audit_actions(audit)
        client.write_file.assert_not_called()

    def test_a_platform_minted_terminal_row_is_not_applicable_not_entry_missing(self, monkeypatch):
        term = _row("base-image-stale-a-2026", status="cancelled")
        db = _fake_db(terminal_items=[term])
        svc, _ = _wire(monkeypatch, db, _client(_file()))
        asyncio.run(svc._sync_agent("a"))
        calls = _delivery_calls(db)
        assert ("uuid-base-image-stale-a-2026", DELIVERY_NOT_APPLICABLE, "platform_minted") in calls
        assert not any(s == DELIVERY_UNDELIVERED for _, s, _ in calls)

    def test_a_terminal_flip_whose_entry_is_gone_is_undelivered_entry_missing(self, monkeypatch):
        term = _row("req-9", status="cancelled")
        db = _fake_db(terminal_items=[term])
        client = _client(_file())
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        assert ("uuid-req-9", DELIVERY_UNDELIVERED, "entry_missing") in _delivery_calls(db)

    def test_a_terminal_flip_lands_in_place_and_is_delivered(self, monkeypatch):
        term = _row("req-9", status="expired")
        db = _fake_db(terminal_items=[term])
        client = _client(_file(_entry("req-9")))
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        written = json.loads(client.write_file.call_args.args[1])
        assert written["requests"][0]["status"] == "expired"
        assert ("uuid-req-9", DELIVERY_DELIVERED, None) in _delivery_calls(db)

    def test_the_aging_receipt_is_written_once_at_the_crossing_and_never_at_ingest(self, monkeypatch):
        old = _row(created_at=_recent(hours_ago=48))
        db = _fake_db(open_rows=[old])
        client = _client(_file(_entry()))
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        written = json.loads(client.write_file.call_args.args[1])
        assert written["requests"][0]["platform"]["aging_since"]
        # second cycle: the entry already carries the receipt → no write
        stamped = json.loads(client.write_file.call_args.args[1])
        client2 = _client(json.dumps(stamped))
        svc2, _ = _wire(monkeypatch, _fake_db(open_rows=[old]), client2)
        asyncio.run(svc2._sync_agent("a"))
        client2.write_file.assert_not_called()
        # a fresh (not aged) item gets NO receipt write at ingest
        client3 = _client(_file(_entry()))
        svc3, _ = _wire(monkeypatch, _fake_db(open_rows=[_row()]), client3)
        asyncio.run(svc3._sync_agent("a"))
        client3.write_file.assert_not_called()


# ===========================================================================
# 5. Pure helpers: fingerprint stability, aging, the refusal set
# ===========================================================================

class TestHelpers:
    def test_expires_at_spelling_variants_do_not_diverge(self):
        row = _row(expires_at="2026-09-17T21:35:00Z")
        assert changed_fields(row, _entry(expires_at="2026-09-17T21:35:00+00:00")) == []
        assert changed_fields(row, _entry(expires_at="2026-09-17T21:35:00.000Z")) == []
        assert changed_fields(row, _entry(expires_at="2026-09-22T23:59:00Z")) == ["expires_at"]

    def test_options_order_and_spacing_do_not_diverge(self):
        row = _row(options=[{"b": 1, "a": 2}])
        assert changed_fields(row, _entry(options=[{"a": 2, "b": 1}])) == []

    def test_entry_content_applies_the_clamp_caps_and_create_defaults(self):
        long_title = "t" * (oqs.OPERATOR_QUEUE_TITLE_MAX + 50)
        c = _entry_content({"title": long_title})
        assert len(c["title"]) == oqs.OPERATOR_QUEUE_TITLE_MAX and c["title"].endswith(oqs._TRUNC_MARKER)
        assert _entry_content({})["title"] == "Agent request"
        assert _entry_content({})["question"] == "(no details provided)"
        assert _row_content({"title": None, "question": None})["question"] == "(no details provided)"

    def test_is_aged_reads_created_at_against_the_bound(self):
        now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        item = {"status": "pending", "created_at": "2026-09-22T11:00:00Z"}
        assert is_aged(item, 24, now) is True
        assert is_aged(item, 48, now) is False
        assert is_aged(item, 0, now) is False                       # 0 disables
        assert is_aged({**item, "status": "responded"}, 24, now) is False
        old = {"status": "pending", "created_at": _recent(hours_ago=25)}
        out = annotate_aging([dict(old), {"status": "pending", "created_at": _recent()}], 24)
        assert out[0]["aging"] is True and out[0]["aged_since"].endswith("Z")
        assert out[1]["aging"] is False and out[1]["aged_since"] is None

    def test_the_refusal_set_is_exactly_the_two_states_the_human_must_see(self):
        assert REFUSE_RESPONSE_STATES == {SYNC_CHANGED, SYNC_CLOSED_BY_FILER}


# ===========================================================================
# 6. The accessors against a REAL migrated SQLite (tables.py carries the columns)
# ===========================================================================

@pytest.fixture
def real_db():
    from database import db as real
    return real


class TestAccessorsOnAMigratedDb:
    def _seed(self, real_db, rid, status="pending", agent="agent-2915"):
        item = {"id": rid, "type": "approval", "status": "pending", "priority": "high",
                "title": "t", "question": "q", "options": ["a", "b"], "context": {},
                "created_at": "2026-09-01T10:00:00Z"}
        uuid = real_db.create_operator_queue_item(agent, item)
        if status == "responded":
            real_db.respond_to_operator_queue_item(uuid, "a", None, None, "op@example.com")
        elif status == "cancelled":
            real_db.cancel_operator_queue_item(uuid)
        return uuid

    def test_set_sync_state_is_an_edge_and_confirmed_stamps_last_confirmed(self, real_db):
        uid = self._seed(real_db, "e-1")
        now = "2026-09-23T10:00:00Z"
        assert real_db.set_operator_queue_sync_state(uid, "changed", "title", now) is True
        assert real_db.set_operator_queue_sync_state(uid, "changed", "title", now) is False   # unchanged → no write
        assert real_db.set_operator_queue_sync_state(uid, "confirmed", None, now) is True
        item = real_db.get_operator_queue_item(uid)
        assert item["sync_state"] == "confirmed" and item["last_confirmed_at"] == now
        assert item["sync_updated_at"] == now

    def test_sync_index_splits_open_and_terminal(self, real_db):
        self._seed(real_db, "i-open"); self._seed(real_db, "i-resp", status="responded")
        self._seed(real_db, "i-term", status="cancelled")
        idx = real_db.get_operator_queue_sync_index_for_agent("agent-2915")
        open_ids = {r["request_id"] for r in idx["open"]}
        assert {"i-open", "i-resp"} <= open_ids and "i-term" not in open_ids
        assert idx["terminal"]["i-term"]["status"] == "cancelled"

    def test_terminal_flips_are_fetched_by_delivery_state_not_age(self, real_db):
        uid = self._seed(real_db, "t-old", status="cancelled", agent="agent-2915-t")
        # make it older than the former 168 h window
        from db.engine import get_engine
        from db.tables import operator_queue
        from sqlalchemy import update
        with get_engine().begin() as conn:
            conn.execute(update(operator_queue).where(operator_queue.c.id == uid)
                         .values(created_at="2026-01-01T00:00:00Z"))
        ids = {r["id"] for r in real_db.get_operator_queue_terminal_for_agent("agent-2915-t")}
        assert uid in ids
        real_db.set_operator_queue_delivery_state(uid, "delivered", None, "2026-09-23T10:00:00Z")
        ids = {r["id"] for r in real_db.get_operator_queue_terminal_for_agent("agent-2915-t")}
        assert uid not in ids

    def test_mark_unconfirmed_sweeps_only_the_named_scope(self, real_db):
        a = self._seed(real_db, "u-a", agent="agent-2915-run")
        b = self._seed(real_db, "u-b", agent="agent-2915-stop")
        now = "2026-09-23T10:00:00Z"
        n = real_db.mark_operator_queue_unconfirmed("agent_not_running", now, exclude_agents=["agent-2915-run"])
        assert n >= 1
        assert real_db.get_operator_queue_item(b)["sync_state"] == "unconfirmed"
        assert real_db.get_operator_queue_item(a)["sync_state"] != "unconfirmed"
        assert real_db.mark_operator_queue_unconfirmed("agent_not_running", now, exclude_agents=["agent-2915-run"]) == 0

    def test_count_flags_counts_the_visible_escalation(self, real_db):
        uid = self._seed(real_db, "f-1", status="responded", agent="agent-2915-f")
        real_db.set_operator_queue_delivery_state(uid, "undelivered", "entry_changed", "2026-09-23T10:00:00Z")
        pid = self._seed(real_db, "f-2", agent="agent-2915-f")
        real_db.set_operator_queue_sync_state(pid, "closed_by_filer", "acknowledged", "2026-09-23T10:00:00Z")
        flags = real_db.count_operator_queue_flags({"agent-2915-f"})
        assert flags == {"undelivered": 1, "closed_by_filer": 1}
        assert real_db.count_operator_queue_flags(set()) == {"undelivered": 0, "closed_by_filer": 0}


# ===========================================================================
# 7. The two respond entry points refuse a diverged item until acknowledged
# ===========================================================================

_OP_APP = None


def _override_current_user(app, router, user):
    """Key the auth override off the `get_current_user` callables the routes
    ACTUALLY captured — found by walking each route's dependant tree — never off
    a fresh `from dependencies import get_current_user`.

    Under a seeded full-suite order the two are not always the same object: an
    earlier module that stubs or re-imports `dependencies` (the `sys.modules`
    class the pollution lint exists for) leaves the router holding one function
    while the fresh import returns another, and an override keyed on the wrong
    one is silently ignored — the real auth runs and answers 401 "Not
    authenticated" to a request with no Authorization header (seed 12345 on
    PR #2989). Returns the set it overrode; a route-shape change fails loudly.
    """
    found = set()

    def walk(dependant):
        for sub in dependant.dependencies:
            if getattr(sub.call, "__name__", "") == "get_current_user":
                found.add(sub.call)
            walk(sub)

    for route in router.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            walk(dependant)
    assert found, "no get_current_user dependency on the operator-queue routes"
    for call in found:
        app.dependency_overrides[call] = lambda: user
    return found


def _operator_queue_app():
    """One FastAPI app over the real operator-queue router, with `get_current_user`
    overridden by an admin stand-in (built once — see `op_client`)."""
    global _OP_APP
    if _OP_APP is None:
        from types import SimpleNamespace
        from fastapi import FastAPI
        from routers import operator_queue as r
        app = FastAPI()
        app.include_router(r.router)
        admin = SimpleNamespace(id="u-admin", username="admin", email="admin@example.com", role="admin", mcp_scope=None)
        _override_current_user(app, r.router, admin)
        _OP_APP = app
    return _OP_APP


class TestRefusal:
    @pytest.fixture(autouse=True)
    def _roster(self, monkeypatch):
        import client_portal.service as portal_service
        monkeypatch.setattr(portal_service, "agent_on_roster", lambda agent, email, include_owned=False: True)
        # the answer must never dispatch a resume from a unit test (the portal
        # service reaches the dispatcher through its module, so patch it there)
        import services.operator_resume_service as ors
        monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda *a, **k: None)

    def _addressed_pending(self, real_db, email, rid):
        from services.operator_queue_service import _clamp_ingested_item
        item = _clamp_ingested_item({"id": rid, "type": "approval", "title": "Ship it?",
                                     "question": "Ship v1?", "options": ["yes", "no"],
                                     "addressed_to_email": email}, "agent-2915-p")
        return real_db.create_operator_queue_item("agent-2915-p", item)

    def test_the_portal_answer_is_refused_with_a_named_409_until_acknowledged(self, real_db):
        from client_portal.asks.service import answer_ask, AskError
        email = "client-2915@example.com"
        uid = self._addressed_pending(real_db, email, "p-diverged-1")
        real_db.set_operator_queue_sync_state(uid, "changed", "title,options", "2026-09-23T10:00:00Z")
        with pytest.raises(AskError) as ei:
            answer_ask(uid, email, False, "yes", None)
        assert ei.value.status_code == 409 and ei.value.code == "item_diverged"
        assert real_db.get_operator_queue_item(uid)["status"] == "pending"      # nothing recorded
        answered = answer_ask(uid, email, False, "yes", None, acknowledge_divergence=True)
        assert answered.status == "answered"
        assert real_db.get_operator_queue_item(uid)["status"] == "responded"

    def test_a_closed_by_filer_ask_is_refused_the_same_way(self, real_db):
        from client_portal.asks.service import answer_ask, AskError
        email = "client-2915b@example.com"
        uid = self._addressed_pending(real_db, email, "p-diverged-2")
        real_db.set_operator_queue_sync_state(uid, "closed_by_filer", "acknowledged", "2026-09-23T10:00:00Z")
        with pytest.raises(AskError) as ei:
            answer_ask(uid, email, False, "yes", None)
        assert ei.value.code == "item_diverged"

    def test_the_projection_is_coarse_and_carries_aging(self, real_db):
        from client_portal.asks.service import _project, _coarse_sync
        assert _coarse_sync({"sync_state": "missing"}) == "unconfirmed"
        assert _coarse_sync({"sync_state": "stale_id"}) == "unconfirmed"
        assert _coarse_sync({"sync_state": "closed_by_filer"}) == "closed"
        assert _coarse_sync({"sync_state": None}) == "unconfirmed"
        ask = _project({"id": "x", "agent_name": "a", "type": "question", "priority": "medium",
                        "title": "t", "question": "q", "created_at": _recent(hours_ago=30),
                        "status": "pending", "sync_state": "missing", "sync_detail": "agent_not_running"})
        assert ask.sync == "unconfirmed" and not hasattr(ask, "sync_detail")
        assert ask.aging is True


    # ---- the operator route, through the real router (TestClient; the 1081 harness shape) ----

    @pytest.fixture
    def op_client(self, monkeypatch):
        """The real router under TestClient (the 1081 harness shape). ONE app per
        process: a second FastAPI app over the same module router answered 401
        on this machine although its override table carried `get_current_user`,
        so the app is built once and only the db seams are patched per test."""
        from fastapi.testclient import TestClient
        from routers import operator_queue as r
        app = _operator_queue_app()
        item = {"id": "op-1", "agent_name": "agent-x", "type": "approval", "status": "pending",
                "options": ["approve", "reject"], "title": "t", "question": "q", "context": {},
                "sync_state": "changed", "sync_detail": "question", "request_id": "req-op-1"}
        monkeypatch.setattr(r.db, "get_operator_queue_item", lambda item_id: dict(item))
        recorded = {}
        def _respond(item_id, response, response_text, responded_by_id, responded_by_email, **kw):
            recorded["response"] = response
            return {**item, "status": "responded", "response": response}
        monkeypatch.setattr(r.db, "respond_to_operator_queue_item", _respond)
        monkeypatch.setattr(r.operator_resume_service, "spawn_resume_dispatch", lambda *a, **k: None)
        monkeypatch.setattr(r, "_websocket_manager", None)
        return TestClient(app, raise_server_exceptions=True), recorded

    def test_the_operator_route_refuses_with_a_named_409_until_acknowledged(self, op_client):
        client, recorded = op_client
        res = client.post("/api/operator-queue/op-1/respond", json={"response": "approve"})
        assert res.status_code == 409
        body = res.json()["detail"]
        assert body["code"] == "item_diverged" and body["sync_state"] == "changed" and body["sync_detail"] == "question"
        assert recorded == {}                                           # nothing recorded
        res = client.post("/api/operator-queue/op-1/respond", json={"response": "approve", "acknowledge_divergence": True})
        assert res.status_code == 200, res.text
        assert recorded["response"] == "approve"

    def test_the_operator_route_still_refuses_a_non_offered_option_first(self, op_client):
        client, recorded = op_client
        res = client.post("/api/operator-queue/op-1/respond", json={"response": "maybe", "acknowledge_divergence": True})
        assert res.status_code == 422 and recorded == {}, (res.status_code, res.text)


# ===========================================================================
# 8. Review round 2 (PR #2989) — every finding the reviewers traced, pinned.
# ===========================================================================

class TestRound2Reconcile:
    def test_a_platform_minted_pending_row_is_never_missing(self, monkeypatch):
        """A platform alarm (reserved id prefix) was never in the agent's file;
        the "rows the file no longer carries" loop must skip it — it used to
        record `missing:entry_missing` for every alarm on the first cycle."""
        alarm = _row("cb-dormant-a-2026", sync_state=None, title="circuit dormant")
        db = _fake_db(open_rows=[alarm, _row("req-1")])
        svc, audit = _wire(monkeypatch, db, _client(_file()))
        asyncio.run(svc._sync_agent("a"))
        touched = [c.args[0] for c in db.set_operator_queue_sync_state.call_args_list]
        assert "uuid-cb-dormant-a-2026" not in touched
        assert "uuid-req-1" in touched and (SYNC_MISSING, "entry_missing") in _sync_calls(db)

    def test_a_responded_row_absent_from_the_file_is_reconstructed_not_marked_missing(self, monkeypatch):
        """The write-back re-appends a responded entry the file lost; marking it
        `missing` first minted a `diverged` + `reconciled` pair per cycle."""
        resp = _row(status="responded")
        db = _fake_db(open_rows=[resp], responded=[resp])
        client = _client(_file())
        svc, audit = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        assert (SYNC_MISSING, "entry_missing") not in _sync_calls(db)
        written = json.loads(client.write_file.call_args.args[1])
        assert [r["id"] for r in written["requests"]] == ["req-1"]
        assert ("uuid-req-1", DELIVERY_DELIVERED, None) in _delivery_calls(db)
        assert "diverged" not in _audit_actions(audit)

    def test_duplicate_ids_take_the_first_entry_and_never_flip(self, monkeypatch):
        db = _fake_db(open_rows=[_row()])
        dup = _file(_entry(), _entry(title="a second copy, rewritten"))
        svc, audit = _wire(monkeypatch, db, _client(dup))
        asyncio.run(svc._sync_agent("a"))
        asyncio.run(svc._sync_agent("a"))
        assert all(state == SYNC_CONFIRMED for state, _ in _sync_calls(db))
        assert "diverged" not in _audit_actions(audit)

    @pytest.mark.parametrize("field,over,detail", [
        ("type", {"type": "question"}, "type"),
        ("priority", {"priority": "low"}, "priority"),
        ("context", {"context": {"why": "new facts"}}, "context"),
        ("addressee", {"addressed_to_email": "someone-else@example.com"}, "addressee"),
    ])
    def test_a_change_to_type_priority_context_or_addressee_is_detected(self, monkeypatch, field, over, detail):
        row = _row(addressed_to_email="client@example.com")
        db = _fake_db(open_rows=[row])
        entry = _entry(addressed_to_email="client@example.com")
        entry.update(over)
        svc, _ = _wire(monkeypatch, db, _client(_file(entry)))
        asyncio.run(svc._sync_agent("a"))
        assert (SYNC_CHANGED, detail) in _sync_calls(db)

    def test_an_unpriced_priority_and_an_oversize_context_compare_as_ingested(self):
        """The clamp's own defaults: an invalid priority became `medium` at ingest
        and an oversize context became a marker — neither is a change."""
        row = _row(priority="medium", context={"_truncated": True, "_original_bytes": 99999, "execution_id": None})
        entry = _entry(priority="urgent!!", context={"blob": "x" * 20000})
        assert changed_fields(row, entry) == []

    def test_an_addressee_the_row_never_resolved_is_not_a_change(self):
        assert changed_fields(_row(addressed_to_email=None), _entry(addressed_to_email="x@example.com")) == []

    def test_wrong_shape_requests_is_unconfirmed_and_never_writes_back(self, monkeypatch):
        """`{"requests": {...}}` used to read as an EMPTY list: every row went
        `missing`, the write-back replaced the agent's file and recorded
        `delivered` — the destructive class this PR fixes for invalid JSON."""
        resp = _row(status="responded")
        db = _fake_db(open_rows=[_row("req-0"), resp], responded=[resp])
        client = _client(json.dumps({"$schema": "operator-queue-v1", "requests": {"req-0": {}}}))
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        db.mark_operator_queue_unconfirmed.assert_called_once()
        assert db.mark_operator_queue_unconfirmed.call_args.args[0] == "wrong_shape"
        assert (SYNC_MISSING, "entry_missing") not in _sync_calls(db)
        client.write_file.assert_not_called()
        db.set_operator_queue_delivery_state.assert_not_called()

    def test_a_top_level_array_is_wrong_shape_not_a_swallowed_exception(self, monkeypatch):
        db = _fake_db(open_rows=[_row()])
        svc, _ = _wire(monkeypatch, db, _client(json.dumps([_entry()])))
        asyncio.run(svc._sync_agent("a"))
        assert db.mark_operator_queue_unconfirmed.call_args.args[0] == "wrong_shape"
        assert _sync_calls(db) == []


class TestRound2WriteBack:
    def _acknowledged(self, **over):
        base = dict(status="responded", sync_state="changed", sync_detail="question",
                    divergence_acknowledged_at="2026-09-24T09:00:00Z")
        base.update(over)
        return _row(**base)

    def test_an_acknowledged_divergence_delivers_into_the_rewritten_entry(self, monkeypatch):
        """"Send again to answer anyway" must reach the file: the operator saw the
        rewritten question and answered it. Without the acknowledgement the
        row stayed `undelivered:entry_changed` forever."""
        resp = self._acknowledged()
        db = _fake_db(open_rows=[resp], responded=[resp])
        client = _client(_file(_entry(question="A DIFFERENT question")))
        svc, audit = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        written = json.loads(client.write_file.call_args.args[1])
        assert written["requests"][0]["status"] == "responded"
        assert written["requests"][0]["response"] == "approve"
        assert written["requests"][0]["question"] == "A DIFFERENT question"   # the agent's text is kept
        assert ("uuid-req-1", DELIVERY_DELIVERED, None) in _delivery_calls(db)

    def test_an_acknowledged_divergence_is_written_into_an_entry_the_agent_closed(self, monkeypatch):
        """The agent marked its entry `acknowledged` before anyone answered. An
        acknowledged answer is WRITTEN into it (status → responded) — the
        previous path recorded `delivered` with no write, and the next cycle
        then claimed the agent acknowledged an answer it never saw."""
        resp = self._acknowledged(sync_state="closed_by_filer", sync_detail="acknowledged")
        db = _fake_db(open_rows=[resp], responded=[resp])
        client = _client(_file(_entry(status="acknowledged")))
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_awaited_once()
        written = json.loads(client.write_file.call_args.args[1])
        assert written["requests"][0]["status"] == "responded"
        assert written["requests"][0]["responded_at"] == resp["responded_at"]
        assert ("uuid-req-1", DELIVERY_DELIVERED, None) in _delivery_calls(db)

    def test_an_agent_closed_entry_without_acknowledgement_is_undelivered_not_delivered(self, monkeypatch):
        resp = _row(status="responded")
        db = _fake_db(open_rows=[resp], responded=[resp])
        client = _client(_file(_entry(status="acknowledged")))      # no responded_at: not our write
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_not_called()
        assert ("uuid-req-1", DELIVERY_UNDELIVERED, "closed_by_filer") in _delivery_calls(db)
        assert ("uuid-req-1", DELIVERY_DELIVERED, None) not in _delivery_calls(db)

    def test_our_own_landed_answer_is_recognised_by_its_responded_at(self, monkeypatch):
        resp = _row(status="responded")
        landed = _entry(status="acknowledged", response="approve", responded_at=resp["responded_at"])
        db = _fake_db(open_rows=[resp], responded=[resp])
        client = _client(_file(landed))
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_not_called()
        assert ("uuid-req-1", DELIVERY_DELIVERED, None) in _delivery_calls(db)

    def test_wrong_shape_at_the_reread_never_writes(self, monkeypatch):
        resp = _row(status="responded")
        db = _fake_db(open_rows=[resp], responded=[resp])
        bad = json.dumps({"$schema": "operator-queue-v1", "requests": {"req-1": {}}})
        client = _client(None, reads=[{"success": True, "content": _file(_entry())}, {"success": True, "content": bad}])
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_not_called()

    def test_a_file_that_vanished_between_the_two_reads_is_left_alone(self, monkeypatch):
        """The cycle-start read had a file; the pre-write re-read does not. That
        is an agent mid-rewrite, not a lost file: nothing is written and
        nothing is recorded — next cycle decides."""
        resp = _row(status="responded")
        db = _fake_db(open_rows=[resp], responded=[resp])
        client = _client(None, reads=[{"success": True, "content": _file(_entry())},
                                      {"success": True, "content": None, "not_found": True}])
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_not_called()
        db.set_operator_queue_delivery_state.assert_not_called()

    def test_a_file_missing_at_both_reads_is_reconstructed(self, monkeypatch):
        resp = _row(status="responded")
        db = _fake_db(open_rows=[resp], responded=[resp])
        client = _client(None, reads=[{"success": True, "content": None, "not_found": True}])
        svc, _ = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        client.write_file.assert_awaited_once()
        assert client.write_file.call_args.kwargs["if_match"] is None


class TestRound2PollCycle:
    _run_cycle = TestPollCycle._run_cycle

    def test_answers_for_stopped_agents_are_undelivered_agent_not_running(self, monkeypatch):
        """AC3: a container that is down cannot receive the answer — the row says
        so (`undelivered:agent_not_running`), not just `unconfirmed`, and each
        transition is audited once."""
        db = _fake_db()
        db.mark_operator_queue_undelivered_for_stopped_agents.return_value = [
            {"id": "uuid-x", "status": "responded", "agent_name": "b"},
        ]
        audit = AsyncMock()
        monkeypatch.setattr(oqs, "_audit_sync", audit)
        db, svc, _ = self._run_cycle(monkeypatch, {"a": "running", "b": "stopped"}, db=db)
        call = db.mark_operator_queue_undelivered_for_stopped_agents.call_args
        assert call.kwargs["running_agents"] == ["a"]
        assert "undeliverable" in _audit_actions(audit)
        assert audit.call_args.args[2] == "uuid-x"


class TestRound2Accessors:
    _seed = TestAccessorsOnAMigratedDb._seed

    def _now(self):
        return "2026-09-24T09:00:00Z"

    def test_mark_acknowledged_needs_a_delivered_answer(self, real_db):
        """An entry the agent marked `acknowledged` flips a responded row ONLY when
        our answer was delivered into that entry — otherwise the platform claims
        the agent acknowledged an answer it never saw."""
        uid = self._seed(real_db, "ack-1", status="responded", agent="agent-2915-ack")
        assert real_db.mark_operator_queue_acknowledged("agent-2915-ack", "ack-1") is None
        real_db.set_operator_queue_delivery_state(uid, "delivered", None, self._now())
        assert real_db.mark_operator_queue_acknowledged("agent-2915-ack", "ack-1") == uid

    def test_mark_unconfirmed_skips_platform_minted_rows(self, real_db):
        from services.operator_queue_service import _RESERVED_ID_PREFIXES
        alarm = self._seed(real_db, "cb-dormant-2915", agent="agent-2915-pm")
        ask = self._seed(real_db, "pm-ask", agent="agent-2915-pm")
        real_db.mark_operator_queue_unconfirmed("agent_not_running", self._now(), agent_name="agent-2915-pm",
                                                exclude_request_id_prefixes=_RESERVED_ID_PREFIXES)
        assert real_db.get_operator_queue_item(alarm)["sync_state"] is None
        assert real_db.get_operator_queue_item(ask)["sync_state"] == "unconfirmed"

    def test_terminal_flips_skip_a_dropped_entry_and_go_oldest_first(self, real_db):
        from db.engine import get_engine
        from db.tables import operator_queue
        from sqlalchemy import update
        agent = "agent-2915-order"
        newer = self._seed(real_db, "o-new", status="cancelled", agent=agent)
        older = self._seed(real_db, "o-old", status="cancelled", agent=agent)
        dropped = self._seed(real_db, "o-gone", status="cancelled", agent=agent)
        with get_engine().begin() as conn:
            conn.execute(update(operator_queue).where(operator_queue.c.id == older).values(created_at="2026-01-01T00:00:00Z"))
            conn.execute(update(operator_queue).where(operator_queue.c.id == newer).values(created_at="2026-06-01T00:00:00Z"))
        real_db.set_operator_queue_delivery_state(dropped, "undelivered", "entry_missing", self._now())
        real_db.set_operator_queue_delivery_state(newer, "undelivered", "conflict", self._now())
        ids = [r["id"] for r in real_db.get_operator_queue_terminal_for_agent(agent)]
        assert dropped not in ids
        assert ids.index(older) < ids.index(newer)

    def test_count_flags_excludes_a_cancellation_the_agent_already_dropped(self, real_db):
        agent = "agent-2915-cnt"
        gone = self._seed(real_db, "c-gone", status="cancelled", agent=agent)
        real_db.set_operator_queue_delivery_state(gone, "undelivered", "entry_missing", self._now())
        answer = self._seed(real_db, "c-ans", status="responded", agent=agent)
        real_db.set_operator_queue_delivery_state(answer, "undelivered", "entry_missing", self._now())
        assert real_db.count_operator_queue_flags({agent})["undelivered"] == 1

    def test_respond_records_the_acknowledged_divergence(self, real_db):
        uid = self._seed(real_db, "d-ack", agent="agent-2915-d")
        item = real_db.respond_to_operator_queue_item(uid, "a", None, None, "op@example.com",
                                                     divergence_acknowledged=True)
        assert item["divergence_acknowledged_at"]
        plain = self._seed(real_db, "d-plain", agent="agent-2915-d")
        assert real_db.respond_to_operator_queue_item(plain, "a", None, None, "op@example.com")["divergence_acknowledged_at"] is None

    def test_mark_undelivered_for_stopped_agents_is_edge_triggered_and_skips_platform_rows(self, real_db):
        from services.operator_queue_service import _RESERVED_ID_PREFIXES
        stopped, running = "agent-2915-off", "agent-2915-on"
        a = self._seed(real_db, "s-ans", status="responded", agent=stopped)
        c = self._seed(real_db, "s-can", status="cancelled", agent=stopped)
        alarm = self._seed(real_db, "cb-dormant-off", status="responded", agent=stopped)
        r = self._seed(real_db, "s-run", status="responded", agent=running)
        p = self._seed(real_db, "s-pend", agent=stopped)
        rows = real_db.mark_operator_queue_undelivered_for_stopped_agents(
            self._now(), running_agents=[running], exclude_request_id_prefixes=_RESERVED_ID_PREFIXES)
        mine = {x["id"] for x in rows if x["agent_name"] in (stopped, running)}
        assert mine == {a, c}
        assert real_db.get_operator_queue_item(a)["delivery_detail"] == "agent_not_running"
        assert real_db.get_operator_queue_item(alarm)["delivery_state"] is None
        assert real_db.get_operator_queue_item(r)["delivery_state"] is None
        assert real_db.get_operator_queue_item(p)["delivery_state"] is None
        again = real_db.mark_operator_queue_undelivered_for_stopped_agents(
            self._now(), running_agents=[running], exclude_request_id_prefixes=_RESERVED_ID_PREFIXES)
        assert not [x for x in again if x["agent_name"] in (stopped, running)]


class TestRound2AcknowledgementReachesTheRow:
    def test_the_operator_route_records_the_acknowledgement_on_the_row(self, monkeypatch):
        from fastapi.testclient import TestClient
        from routers import operator_queue as r
        app = _operator_queue_app()
        item = {"id": "op-2", "agent_name": "agent-x", "type": "approval", "status": "pending",
                "options": ["approve", "reject"], "title": "t", "question": "q", "context": {},
                "sync_state": "changed", "sync_detail": "question", "request_id": "req-op-2"}
        monkeypatch.setattr(r.db, "get_operator_queue_item", lambda item_id: dict(item))
        recorded = {}
        def _respond(item_id, response, response_text, responded_by_id, responded_by_email, **kw):
            recorded.update(kw)
            return {**item, "status": "responded", "response": response}
        monkeypatch.setattr(r.db, "respond_to_operator_queue_item", _respond)
        monkeypatch.setattr(r.operator_resume_service, "spawn_resume_dispatch", lambda *a, **k: None)
        monkeypatch.setattr(r, "_websocket_manager", None)
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.post("/api/operator-queue/op-2/respond",
                      json={"response": "approve", "acknowledge_divergence": True})
        assert resp.status_code == 200, resp.text
        assert recorded == {"divergence_acknowledged": True}

    def test_the_portal_answer_records_the_acknowledgement_on_the_row(self, real_db, monkeypatch):
        import client_portal.service as portal_service
        monkeypatch.setattr(portal_service, "agent_on_roster", lambda agent, email, include_owned=False: True)
        import services.operator_resume_service as ors
        monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda *a, **k: None)
        from client_portal.asks.service import answer_ask
        from services.operator_queue_service import _clamp_ingested_item
        email = "client-2915c@example.com"
        item = _clamp_ingested_item({"id": "p-ack", "type": "approval", "title": "t", "question": "q",
                                     "options": ["yes", "no"], "addressed_to_email": email}, "agent-2915-pa")
        uid = real_db.create_operator_queue_item("agent-2915-pa", item)
        real_db.set_operator_queue_sync_state(uid, "closed_by_filer", "acknowledged", "2026-09-24T09:00:00Z")
        answer_ask(uid, email, False, "yes", None, acknowledge_divergence=True)
        assert real_db.get_operator_queue_item(uid)["divergence_acknowledged_at"]


# ===========================================================================
# #3024 — `stale_id` is a GENUINE re-use, never the entry awaiting its flip.
#   The reconcile (step 2) runs before the write-back (step 4), so in the cycle
#   right after the platform ends a row the agent's file still holds the
#   ORIGINAL pending entry — the one this cycle's write-back is about to flip.
#   Before #3024 every ask the platform ended read "Re-used id" and nothing
#   cleared it. The rows here sit in BOTH the sync index and the write-back set,
#   as they do in production (the earlier harness only ever filled one of them).
# ===========================================================================

def _term(rid="req-1", status="cancelled", **over):
    """A terminal row as the sync INDEX carries it."""
    t = {"id": f"uuid-{rid}", "status": status, "sync_state": None,
         "delivery_state": None, "delivery_detail": None}
    t.update(over)
    return t


class TestStaleIdIsOnlyAGenuineReuse:
    @pytest.mark.parametrize("status", ["cancelled", "expired"])
    @pytest.mark.parametrize("delivery_state, delivery_detail", [
        (None, None),                                  # the cycle right after the ending
        (DELIVERY_UNDELIVERED, "agent_not_running"),   # the agent was stopped when it ended
        (DELIVERY_UNDELIVERED, "file_missing"),        # the file was unreadable last time
    ])
    def test_the_original_entry_awaiting_its_flip_is_not_a_reused_id(
            self, monkeypatch, status, delivery_state, delivery_detail):
        db = _fake_db(
            terminal={"req-1": _term(status=status, delivery_state=delivery_state,
                                     delivery_detail=delivery_detail)},
            terminal_items=[_row(status=status, sync_state=None, delivery_state=delivery_state,
                                 delivery_detail=delivery_detail)],
        )
        client = _client(_file(_entry()))
        svc, audit = _wire(monkeypatch, db, client)
        asyncio.run(svc._sync_agent("a"))
        assert not [c for c in _sync_calls(db) if c[0] == SYNC_STALE_ID]
        assert "diverged" not in _audit_actions(audit)
        # ...and the same cycle's write-back still delivers the ending into it.
        written = json.loads(client.write_file.call_args.args[1])
        assert written["requests"][0]["status"] == status
        assert ("uuid-req-1", DELIVERY_DELIVERED, None) in _delivery_calls(db)

    @pytest.mark.parametrize("term", [
        pytest.param(_term(delivery_state=DELIVERY_DELIVERED), id="flip-already-delivered"),
        pytest.param(_term(status="expired", delivery_state=DELIVERY_UNDELIVERED,
                           delivery_detail="entry_missing"), id="entry-was-missing"),
        pytest.param(_term(status="acknowledged", delivery_state=DELIVERY_DELIVERED), id="acknowledged"),
    ])
    def test_a_pending_entry_the_write_back_will_not_flip_is_a_reused_id(self, monkeypatch, term):
        db = _fake_db(terminal={"req-1": term})
        svc, _ = _wire(monkeypatch, db, _client(_file(_entry())))
        asyncio.run(svc._sync_agent("a"))
        db.create_operator_queue_item.assert_not_called()
        assert (SYNC_STALE_ID, term["status"]) in _sync_calls(db)

    @pytest.mark.parametrize("status", ["cancelled", "expired"])
    def test_a_row_misflagged_since_2915_heals_once_its_entry_reads_the_ending(self, monkeypatch, status):
        db = _fake_db(terminal={"req-1": _term(status=status, sync_state=SYNC_STALE_ID,
                                               delivery_state=DELIVERY_DELIVERED)})
        svc, audit = _wire(monkeypatch, db, _client(_file(_entry(status=status))))
        asyncio.run(svc._sync_agent("a"))
        assert (SYNC_CONFIRMED, None) in _sync_calls(db)
        assert "reconciled" in _audit_actions(audit)

    def test_a_genuine_reuse_flag_is_not_cleared_while_its_entry_is_pending(self, monkeypatch):
        db = _fake_db(terminal={"req-1": _term(sync_state=SYNC_STALE_ID, delivery_state=DELIVERY_DELIVERED)})
        svc, _ = _wire(monkeypatch, db, _client(_file(_entry())))
        asyncio.run(svc._sync_agent("a"))
        assert (SYNC_CONFIRMED, None) not in _sync_calls(db)

    def test_an_entry_the_agent_closed_differently_is_not_healed(self, monkeypatch):
        """Only the row's OWN ending heals a flag: an entry the agent set to some
        other status is not the platform's flip having landed."""
        db = _fake_db(terminal={"req-1": _term(sync_state=SYNC_STALE_ID, delivery_state=DELIVERY_DELIVERED)})
        svc, _ = _wire(monkeypatch, db, _client(_file(_entry(status="acknowledged"))))
        asyncio.run(svc._sync_agent("a"))
        assert (SYNC_CONFIRMED, None) not in _sync_calls(db)

    def test_the_awaiting_flip_rule_is_the_write_back_selection(self, real_db):
        """One rule, two spellings: the reconcile's `awaits_terminal_flip` (Python)
        must pick exactly the rows `get_terminal_items_for_agent` (SQL) hands the
        write-back, read through the real sync index — so the index must carry the
        delivery columns the rule reads."""
        from db.engine import get_engine
        from db.tables import operator_queue
        from sqlalchemy import update
        agent = "agent-3024-parity"
        cases = [
            ("cancelled", None, None), ("cancelled", "undelivered", "agent_not_running"),
            ("cancelled", "undelivered", "file_missing"), ("cancelled", "undelivered", "entry_missing"),
            ("cancelled", "delivered", None), ("cancelled", "not_applicable", "platform_minted"),
            ("expired", None, None), ("expired", "delivered", None),
            ("acknowledged", "delivered", None), ("acknowledged", None, None),
        ]
        seeded = {}
        for n, (status, state, detail) in enumerate(cases):
            uid = real_db.create_operator_queue_item(agent, {
                "id": f"p-{n}", "type": "approval", "status": "pending", "priority": "high",
                "title": "t", "question": "q", "options": ["a", "b"], "context": {},
                "created_at": "2026-09-01T10:00:00Z"})
            with get_engine().begin() as conn:
                conn.execute(update(operator_queue).where(operator_queue.c.id == uid)
                             .values(status=status, delivery_state=state, delivery_detail=detail))
            seeded[uid] = (status, state, detail)
        fetched = {r["id"] for r in real_db.get_operator_queue_terminal_for_agent(agent)}
        index = {t["id"]: t for t in real_db.get_operator_queue_sync_index_for_agent(agent)["terminal"].values()}
        assert fetched, "the write-back selection picked nothing — the probe proves nothing"
        for uid, case in seeded.items():
            assert oqs.awaits_terminal_flip(index[uid]) is (uid in fetched), case
