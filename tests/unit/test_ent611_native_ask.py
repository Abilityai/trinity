"""Agent-raised asks over the platform (abilityai/trinity-enterprise#611, PR B).

An agent asks a person for a decision through ONE platform call — validated at
the call, stored, broadcast and answered with a receipt — instead of appending
to `~/.trinity/operator-queue.json`. The file stays as the compatibility path.

This suite pins, layer by layer:
- the db layer: the native create is atomic per agent (replay, depth cap and
  insert in one serialized transaction), and a native row never takes part in
  the file contract (the poller neither reconciles, writes back nor flags it);
- the ask sink's `raise_ask`: strict validation with named refusals, replay of
  the first receipt, role addressing, the re-ask link, the audit row and the
  thin broadcast;
- the route and the file poller's side of the seam.

Related flow: docs/memory/feature-flows/operating-room.md (Raising an ask)
Requirement: docs/memory/requirements/security.md §26.9 (OPS-001-ENDINGS)

Harness: the real per-process SQLite the unit conftest pins; rows are seeded
under agent names unique to this file and every assertion filters to them. The
database lives for the whole run and pytest-randomly reorders the tests, so a
test that asserts a row is ABSENT, or that depends on which asks have expired,
uses an agent no other test writes to.
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


@pytest.fixture
def real_db():
    from database import db as real
    return real


def _native(request_id, **over):
    item = {"id": request_id, "type": "approval", "priority": "high", "title": "Pay invoice",
            "question": "Release 500 USDC to the vendor?", "options": ["approve", "reject"],
            "context": {}, "expires_at": None}
    item.update(over)
    return item


def _create(real_db, agent, request_id, *, max_pending=25, **over):
    return real_db.create_native_operator_queue_item(
        agent, _native(request_id, **over), max_pending=max_pending,
        channel="mcp", raised_by="agent", to_role="primary",
        resolved_to=["owner@example.com"], proposal={"pay": 500},
        supersedes_expired=None,
    )


def _file_row(real_db, agent, request_id, **over):
    item = {"id": request_id, "type": "question", "title": "t", "question": "q", "context": {}}
    item.update(over)
    return real_db.create_operator_queue_item(agent, item, channel="file", raised_by="agent")


# ===========================================================================
# 1. The native create — atomic replay / depth cap / insert, platform columns
# ===========================================================================

class TestNativeCreate:
    AGENT = "agent-611b-create"

    def test_a_new_ask_is_created_with_the_platform_columns_and_outside_the_file_contract(self, real_db):
        out = _create(real_db, self.AGENT, "nc-1")
        assert out["outcome"] == "created"
        row = out["row"]
        assert (row["channel"], row["raised_by"], row["to_role"]) == ("mcp", "agent", "primary")
        assert row["resolved_to"] == ["owner@example.com"] and row["proposal"] == {"pay": 500}
        # never enters a file-delivery set, never reads as out of sync with a file
        assert (row["delivery_state"], row["delivery_detail"]) == ("not_applicable", "mcp_raised")
        assert row["sync_state"] is None and row["status"] == "pending"
        assert real_db.get_operator_queue_item(row["id"])["request_id"] == "nc-1"

    def test_the_same_request_id_replays_the_first_row_and_writes_nothing(self, real_db):
        first = _create(real_db, self.AGENT, "nc-2")["row"]
        again = _create(real_db, self.AGENT, "nc-2", title="Something else entirely")
        assert again["outcome"] == "replayed" and again["row"]["id"] == first["id"]
        assert real_db.get_operator_queue_item(first["id"])["title"] == "Pay invoice"

    def test_a_full_queue_refuses_and_writes_nothing(self, real_db):
        agent = "agent-611b-full"
        for n in range(3):
            assert _create(real_db, agent, f"f-{n}", max_pending=3)["outcome"] == "created"
        out = _create(real_db, agent, "f-over", max_pending=3)
        assert out["outcome"] == "queue_full" and out["row"] is None
        assert real_db.get_operator_queue_item_for_agent_by_request_id(agent, "f-over") is None

    def test_a_replay_is_answered_even_when_the_queue_is_full(self, real_db):
        """A retried call must get its first receipt back, never a refusal it
        did not earn the first time (the gate's retry contract)."""
        agent = "agent-611b-full-replay"
        first = _create(real_db, agent, "r-0", max_pending=1)["row"]
        again = _create(real_db, agent, "r-0", max_pending=1)
        assert again["outcome"] == "replayed" and again["row"]["id"] == first["id"]

    def test_concurrent_creates_at_the_cap_admit_exactly_one(self, real_db, monkeypatch):
        """The count and the insert are ONE serialized step per agent: a
        count-then-insert across workers would turn the cap into a rate limit.
        Each create is held inside its critical section (after the count,
        before the insert) so the racers overlap deterministically; unforced,
        the threads serialize on connection setup and a missing lock passes.
        test_ent611_native_ask_pg.py pins the same on real PostgreSQL."""
        import time
        # Patch the dict the running method reads, not the module name: a sibling
        # test may leave a stand-in in sys.modules["db.operator_queue"] that
        # re-exports the real class, and a patch on the stand-in never fires
        # (the module-identity gotcha; test_1632's `_patch_engine`).
        g = type(real_db._operator_queue_ops).create_native_item.__globals__
        real_insert = g["make_insert"]
        monkeypatch.setitem(g, "make_insert", lambda table: (time.sleep(0.15), real_insert(table))[1])
        agent = "agent-611b-race"
        for n in range(4):
            _create(real_db, agent, f"seed-{n}", max_pending=5)
        outcomes, barrier = [], threading.Barrier(6)

        def racer(i):
            barrier.wait()
            outcomes.append(_create(real_db, agent, f"race-{i}", max_pending=5)["outcome"])

        threads = [threading.Thread(target=racer, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sorted(outcomes) == ["created"] + ["queue_full"] * 5

    def test_a_file_row_that_wins_the_race_turns_the_insert_into_a_replay(self, real_db, monkeypatch):
        """The file poller does not take the per-agent lock, so on PostgreSQL a
        file entry re-using the id can commit between the native replay check and
        the native insert. The insert must then be a replay of that row, never a
        unique-index error (the agent would get a 500). Staged on the native
        transaction itself: the competing row lands after the replay check found
        nothing, as the insert is built. The same interleaving on either backend."""
        agent = "agent-611b-lost-race"
        ops = real_db._operator_queue_ops
        cls = type(ops)
        # Patch the dict the running method reads, not the module name: a sibling
        # test may leave a stand-in in sys.modules["db.operator_queue"] that
        # re-exports the real class, and a patch on the stand-in never fires
        # (the module-identity gotcha; test_1632's `_patch_engine`).
        g = cls.create_native_item.__globals__
        operator_queue = g["operator_queue"]
        real_lock, real_insert = cls._lock_agent_for_create, g["make_insert"]
        held = {}

        def lock_and_hold(conn, agent_name):
            real_lock(conn, agent_name)
            held["conn"], held["agent"] = conn, agent_name

        def lose_then_insert(table):
            # Built AFTER the replay check found nothing: the file row lands now.
            conn = held.pop("conn", None)
            if conn is not None:
                _, values = ops._insert_values(
                    held["agent"], {"id": "lost-1", "type": "question", "title": "t",
                                      "question": "q", "context": {}}, channel="file", raised_by="agent")
                conn.execute(real_insert(operator_queue).values(**values))
            return real_insert(table)

        monkeypatch.setattr(cls, "_lock_agent_for_create", staticmethod(lock_and_hold))
        monkeypatch.setitem(g, "make_insert", lose_then_insert)
        out = _create(real_db, agent, "lost-1")
        assert out["outcome"] == "replayed"
        assert (out["row"]["channel"], out["row"]["proposal"]) == ("file", None)  # the row that won

    def test_expires_at_is_stored_as_iso_z(self, real_db):
        row = _create(real_db, self.AGENT, "nc-exp", expires_at="2031-01-02T03:04:05+02:00")["row"]
        assert row["expires_at"] == "2031-01-02T01:04:05.000000Z"


class TestFileCreateWithOutcome:
    AGENT = "agent-611b-file"

    def test_the_file_create_reports_whether_it_inserted(self, real_db):
        item = {"id": "fo-1", "type": "question", "title": "t", "question": "q", "context": {}}
        uid, inserted = real_db.create_operator_queue_item_with_outcome(
            self.AGENT, item, channel="file", raised_by="agent")
        assert inserted is True
        uid2, inserted2 = real_db.create_operator_queue_item_with_outcome(
            self.AGENT, dict(item, title="changed"), channel="file", raised_by="agent")
        assert (uid2, inserted2) == (uid, False)
        assert real_db.get_operator_queue_item(uid)["title"] == "t"


# ===========================================================================
# 2. A native row never takes part in the file contract
# ===========================================================================

class TestFileContractExclusion:
    def test_the_sync_index_names_native_ids_as_foreign_and_leaves_them_out(self, real_db):
        agent = "agent-611b-index"
        native = _create(real_db, agent, "native-1")["row"]
        filed = _file_row(real_db, agent, "file-1")
        real_db.cancel_operator_queue_item(_create(real_db, agent, "native-2")["row"]["id"],
                                           disposed_by_email="op@example.com")
        idx = real_db.get_operator_queue_sync_index_for_agent(agent)
        assert {r["request_id"] for r in idx["open"]} == {"file-1"}
        assert "native-2" not in idx["terminal"]
        assert set(idx["foreign"]) == {"native-1", "native-2"}
        assert native["id"] != filed

    def test_a_responded_native_row_is_never_written_back(self, real_db):
        agent = "agent-611b-resp"
        uid = _create(real_db, agent, "nr-1")["row"]["id"]
        real_db.respond_to_operator_queue_item(uid, "approve", None, "1", "op@example.com")
        fid = _file_row(real_db, agent, "fr-1")
        real_db.respond_to_operator_queue_item(fid, "yes", None, "1", "op@example.com")
        assert {r["request_id"] for r in real_db.get_operator_queue_responded_for_agent(agent)} == {"fr-1"}

    def test_the_sweeps_never_flag_a_native_row(self, real_db):
        agent = "agent-611b-sweep"
        uid = _create(real_db, agent, "ns-1")["row"]["id"]
        done = _create(real_db, agent, "ns-2")["row"]["id"]
        real_db.respond_to_operator_queue_item(done, "approve", None, "1", "op@example.com")
        now = "2026-09-25T12:00:00Z"
        real_db.mark_operator_queue_unconfirmed("agent_not_running", now, agent_name=agent)
        real_db.mark_operator_queue_undelivered_for_stopped_agents(now, running_agents=["someone-else"])
        assert real_db.get_operator_queue_item(uid)["sync_state"] is None
        assert real_db.get_operator_queue_item(done)["delivery_state"] == "not_applicable"

    def test_clear_all_hides_an_answered_native_row_there_is_nothing_left_to_deliver(self, real_db):
        agent = "agent-611b-clear"
        uid = _create(real_db, agent, "nc-r")["row"]["id"]
        real_db.respond_to_operator_queue_item(uid, "approve", None, "1", "op@example.com")
        fid = _file_row(real_db, agent, "fc-r")
        real_db.respond_to_operator_queue_item(fid, "yes", None, "1", "op@example.com")
        assert real_db.clear_resolved_operator_queue_items(agent_name=agent) >= 1
        assert real_db.get_operator_queue_item(uid)["cleared_at"]
        assert real_db.get_operator_queue_item(fid)["cleared_at"] is None   # still owed to the file


_FACADE = [
    ("create_native_operator_queue_item", "create_native_item"),
    ("create_operator_queue_item_with_outcome", "create_item_with_outcome"),
    ("list_expired_operator_queue_proposals", "list_expired_proposals_for_agent"),
]


@pytest.mark.parametrize("facade_name, ops_name", _FACADE)
def test_the_facade_signature_matches_the_operation(facade_name, ops_name):
    import inspect
    from database import DatabaseManager
    from db.operator_queue import OperatorQueueOperations

    def shape(fn):
        return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()
                if p.name != "self"]

    assert shape(getattr(DatabaseManager, facade_name)) == shape(getattr(OperatorQueueOperations, ops_name))


# ===========================================================================
# 3. The sink's `raise_ask` — validation, replay, addressing, caps, receipt
# ===========================================================================

OWNER = "owner-611b@example.com"
RECEIPT_KEYS = {"status", "id", "request_id", "raised_by", "channel", "type", "to_role",
                "resolved", "ask_status", "disposition", "disposed_at", "expires_at",
                "wakes_on_ending", "supersedes_expired"}


def _in(minutes):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Rejected:
    """`pytest.raises` for the sink's refusal, asserting its status and code."""

    def __init__(self, svc, status, code):
        self.svc, self.status, self.code = svc, status, code

    def __enter__(self):
        self._cm = pytest.raises(self.svc.AskRejected)
        self._info = self._cm.__enter__()
        return self._info

    def __exit__(self, *exc):
        ok = self._cm.__exit__(*exc)
        assert (self._info.value.status_code, self._info.value.code) == (self.status, self.code)
        return ok


@pytest.fixture
def ask(real_db, monkeypatch):
    """The real sink over the real SQLite; the world around it stubbed:
    audit + broadcast recorded, the owner lookup, the workspace thread, the
    wake opt-in and the rate limiter."""
    import json as _json
    from types import SimpleNamespace
    import services.ask_service as svc
    import services.operator_queue_service as oqs
    from services import assignment_provider
    from services.rate_limiter import RateLimitResult

    audit, sent, state = [], [], {"owner": OWNER, "rate_ok": True, "opted_in": False}
    real_owner_email = svc._owner_email

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    class _WS:
        async def broadcast(self, message):
            sent.append(_json.loads(message))

    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: state["owner"])
    monkeypatch.setattr(oqs, "_workspace_thread_for", lambda agent, email, **_: f"thread-{email}")
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda agent: state["opted_in"], raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(state["rate_ok"], 10, 0, 60))
    assignment_provider.clear_provider()
    yield SimpleNamespace(svc=svc, audit=audit, sent=sent, state=state, db=real_db,
                          real_owner_email=real_owner_email)
    assignment_provider.clear_provider()


async def _drain():
    """Wait out the side effects the sink hopped onto the loop (the ent#611 PR A
    helper): the audit row and the broadcast are background work by design."""
    import asyncio
    import services.operator_resume_service as ors
    for _ in range(5):
        await asyncio.sleep(0)
        pending = list(ors._inflight)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _body(request_id, **over):
    b = {"request_id": request_id, "type": "approval", "title": "Pay invoice",
         "question": "Release 500 USDC to the vendor?", "options": ["approve", "reject"],
         "proposal": {"pay": 500, "to": "vendor-7"}}
    b.update(over)
    return b


def _raise(ask, agent, body):
    return ask.svc.raise_ask(agent, body, raised_by="agent", channel="mcp")


class TestRaiseAsk:
    AGENT = "agent-611b-raise"

    def test_a_created_receipt_carries_the_role_and_never_an_email(self, ask):
        r = _raise(ask, self.AGENT, _body("ra-1"))
        assert set(r) == RECEIPT_KEYS
        assert (r["status"], r["channel"], r["type"], r["to_role"], r["resolved"]) == (
            "created", "mcp", "approval", "primary", True)
        assert (r["ask_status"], r["disposition"], r["request_id"]) == ("pending", None, "ra-1")
        assert "@" not in repr(r)

    def test_the_row_records_who_raised_it_and_whom_it_is_for(self, ask):
        r = _raise(ask, self.AGENT, _body("ra-2"))
        row = ask.db.get_operator_queue_item(r["id"])
        assert (row["raised_by"], row["channel"], row["to_role"]) == ("agent", "mcp", "primary")
        assert row["resolved_to"] == [OWNER] and row["addressed_to_email"] == OWNER
        assert row["context"]["workspace_session_id"] == f"thread-{OWNER}"   # the owner's Main chat
        assert row["proposal"] == {"pay": 500, "to": "vendor-7"}

    @pytest.mark.parametrize("kind, role", [("alert", "operator"), ("question", "primary")])
    def test_the_default_role_follows_the_kind(self, ask, kind, role):
        body = _body(f"ra-def-{kind}", type=kind)
        body.pop("options"); body.pop("proposal")
        r = _raise(ask, self.AGENT, body)
        assert r["to_role"] == role

    def test_an_operator_ask_names_no_person(self, ask):
        r = _raise(ask, self.AGENT, _body("ra-op", to="operator"))
        row = ask.db.get_operator_queue_item(r["id"])
        assert r["resolved"] is True and row["resolved_to"] is None and row["addressed_to_email"] is None

    def test_an_owner_without_an_email_makes_it_an_operator_ask_and_says_so(self, ask):
        ask.state["owner"] = None
        r = _raise(ask, self.AGENT, _body("ra-noemail"))
        row = ask.db.get_operator_queue_item(r["id"])
        assert (r["to_role"], r["resolved"]) == ("primary", False)
        assert row["addressed_to_email"] is None and row["resolved_to"] is None

    @pytest.mark.parametrize("role", ["approver", "viewer"])
    def test_a_role_nobody_fills_is_refused_by_name(self, ask, role):
        with _Rejected(ask.svc, 422, "role_unassigned"):
            _raise(ask, self.AGENT, _body(f"ra-{role}", to=role))
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, f"ra-{role}") is None

    def test_a_provider_that_fills_the_role_addresses_the_person(self, ask):
        from services import assignment_provider

        class _P:
            def assignment_for(self, agent, trig):
                return None

            def people_for(self, agent, role):
                return {"emails": ["Approver@Example.com"]} if role == "approver" else None

        assignment_provider.register_provider(_P())
        r = _raise(ask, self.AGENT, _body("ra-prov", to="approver"))
        row = ask.db.get_operator_queue_item(r["id"])
        assert row["resolved_to"] == ["approver@example.com"]
        assert row["addressed_to_email"] == "approver@example.com" and r["resolved"] is True

    def test_a_provider_that_says_nobody_fills_primary_sends_the_ask_to_the_operators(self, ask):
        """ent#606: an ask to `primary` falls back to `operator` when no primary is
        assigned; the owner stands in only when no provider answers at all. An
        empty list IS an answer ("nobody fills it", resolve_role_people)."""
        from services import assignment_provider

        class _P:
            def assignment_for(self, agent, trig):
                return None

            def people_for(self, agent, role):
                return {"emails": []}

        assignment_provider.register_provider(_P())
        r = _raise(ask, self.AGENT, _body("ra-nobody"))
        row = ask.db.get_operator_queue_item(r["id"])
        assert (r["to_role"], r["resolved"]) == ("primary", False)
        assert row["addressed_to_email"] is None and row["resolved_to"] is None
        assert not (row["context"] or {}).get("workspace_session_id")   # nobody's Main chat

    def test_several_people_are_recorded_but_none_is_the_single_addressee(self, ask):
        from services import assignment_provider

        class _P:
            def assignment_for(self, agent, trig):
                return None

            def people_for(self, agent, role):
                return {"emails": ["a@example.com", "b@example.com"]}

        assignment_provider.register_provider(_P())
        r = _raise(ask, self.AGENT, _body("ra-many", to="viewer"))
        row = ask.db.get_operator_queue_item(r["id"])
        assert row["resolved_to"] == ["a@example.com", "b@example.com"]
        assert row["addressed_to_email"] is None

    def test_a_provider_that_raises_is_no_answer(self, ask):
        from services import assignment_provider

        class _P:
            def assignment_for(self, agent, trig):
                return None

            def people_for(self, agent, role):
                raise RuntimeError("provider down")

        assignment_provider.register_provider(_P())
        with _Rejected(ask.svc, 422, "role_unassigned"):
            _raise(ask, self.AGENT, _body("ra-provx", to="approver"))
        assert _raise(ask, self.AGENT, _body("ra-provx-primary"))["resolved"] is True   # OSS default still holds

    @pytest.mark.parametrize("over, code", [
        ({"request_id": "has spaces"}, "invalid_request_id"),
        ({"request_id": "x" * 300}, "invalid_request_id"),
        ({"request_id": "Queue-Flood-mine"}, "reserved_request_id"),
        ({"title": ""}, "invalid_title"),
        ({"title": "t" * 5000}, "field_too_large"),
        ({"question": "q" * 50_000}, "field_too_large"),
        ({"options": ["o" * 3000, "p" * 3000]}, "field_too_large"),
        ({"context": {"blob": "c" * 20_000}}, "field_too_large"),
        ({"proposal": {"blob": "p" * 20_000}}, "field_too_large"),
        ({"options": None}, "options_required"),
        ({"options": []}, "options_required"),
        ({"options": ["approve", ""]}, "invalid_options"),
        ({"expires_at": "tomorrow at noon"}, "invalid_expires_at"),
        ({"expires_at": "2031-01-01T00:00:00"}, "invalid_expires_at"),   # naive: which zone?
        ({"to": "boss"}, "invalid_to"),
    ])
    @pytest.mark.asyncio
    async def test_a_malformed_ask_is_refused_by_name_and_writes_nothing(self, ask, over, code):
        body = {**_body("ra-bad"), **over}
        with _Rejected(ask.svc, 422, code):
            _raise(ask, self.AGENT, body)
        await _drain()
        assert ask.audit == [] and ask.sent == []
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, body["request_id"]) is None

    def test_a_deadline_under_fifteen_minutes_is_refused(self, ask):
        with _Rejected(ask.svc, 422, "invalid_expires_at"):
            _raise(ask, self.AGENT, _body("ra-soon", expires_at=_in(10)))
        r = _raise(ask, self.AGENT, _body("ra-later", expires_at=_in(20)))
        assert r["expires_at"].endswith("Z")

    def test_the_agent_cannot_author_the_workspace_thread(self, ask):
        ask.state["owner"] = None   # an unaddressed ask gets no thread
        r = _raise(ask, self.AGENT, _body("ra-thread", context={"workspace_session_id": "forged", "k": 1}))
        assert ask.db.get_operator_queue_item(r["id"])["context"] == {"k": 1}


class TestReplay:
    AGENT = "agent-611b-replay"

    @pytest.mark.asyncio
    async def test_the_same_request_id_returns_the_first_receipt(self, ask):
        first = _raise(ask, self.AGENT, _body("rp-1"))
        again = _raise(ask, self.AGENT, _body("rp-1"))
        assert again["status"] == "replayed" and again["id"] == first["id"]
        assert again["differs"] == []
        await _drain()
        assert len([a for a in ask.audit if a["event_action"] == "raised"]) == 1
        assert len(ask.sent) == 1

    def test_a_replay_names_what_differs_from_the_first_call(self, ask):
        _raise(ask, self.AGENT, _body("rp-2"))
        again = _raise(ask, self.AGENT, _body("rp-2", title="Pay a different invoice",
                                              to="operator", proposal={"pay": 900}))
        assert again["differs"] == ["proposal", "title", "to"]

    @pytest.mark.parametrize("offset", ["Z", "+02:00", "-05:30"])
    def test_an_identical_retry_with_a_deadline_differs_in_nothing(self, ask, offset):
        """The stored deadline is ISO-Z; the retry's is compared after the same
        normalisation, whatever offset the agent wrote it in."""
        from datetime import datetime, timedelta, timezone
        sign = -1 if offset.startswith("-") else 1
        hours, minutes = (0, 0) if offset == "Z" else map(int, offset[1:].split(":"))
        tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
        when = (datetime.now(timezone.utc) + timedelta(hours=3)).astimezone(tz).replace(microsecond=0)
        text = when.strftime("%Y-%m-%dT%H:%M:%S") + ("Z" if offset == "Z" else offset)
        rid = "rp-dl-" + offset.replace("+", "p").replace("-", "m").replace(":", "")
        body = _body(rid, expires_at=text)
        first = _raise(ask, self.AGENT, body)
        again = _raise(ask, self.AGENT, body)
        assert (first["status"], again["status"], again["differs"]) == ("created", "replayed", [])

    def test_a_replay_is_answered_before_the_deadline_floor(self, ask):
        """A retry minutes later carries a deadline that has come closer; it
        must get its first receipt back, not a refusal (the gate's retry)."""
        ask.db.create_native_operator_queue_item(
            self.AGENT, {"id": "rp-late", "type": "question", "title": "t", "question": "q",
                         "expires_at": _in(5)},
            max_pending=25, channel="mcp", raised_by="agent", to_role="primary",
            resolved_to=None, proposal=None, supersedes_expired=None)
        body = _body("rp-late", type="question", expires_at=_in(5))
        body.pop("options"); body.pop("proposal")
        assert _raise(ask, self.AGENT, body)["status"] == "replayed"

    def test_a_replay_of_an_ended_ask_carries_how_it_ended(self, ask):
        first = _raise(ask, self.AGENT, _body("rp-end"))
        ask.db.cancel_operator_queue_item(first["id"], disposed_by_email="op@example.com")
        again = _raise(ask, self.AGENT, _body("rp-end"))
        assert (again["ask_status"], again["disposition"]) == ("cancelled", "cancelled")
        assert again["disposed_at"]


class TestReask:
    AGENT = "agent-611b-reask"

    def _expired(self, ask, rid, proposal):
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import operator_queue
        uid = _raise(ask, self.AGENT, _body(rid, proposal=proposal))["id"]
        with get_engine().begin() as conn:
            conn.execute(update(operator_queue).where(operator_queue.c.id == uid)
                         .values(status="expired", disposition="expired",
                                 disposed_by="timeout", disposed_at="2026-09-25T10:00:00Z"))
        return uid

    def test_a_reask_links_its_own_expired_predecessor(self, ask):
        pred = self._expired(ask, "rk-old", {"pay": 1})
        r = _raise(ask, self.AGENT, _body("rk-new", proposal={"pay": 1}, supersedes_expired="rk-old"))
        assert r["supersedes_expired"] == "rk-old"
        assert ask.db.get_operator_queue_item(r["id"])["supersedes_expired"] == pred

    @pytest.mark.parametrize("target", ["rk-missing", "rk-pending", "rk-other-agent"])
    def test_a_link_to_anything_but_an_own_expired_ask_is_one_uniform_refusal(self, ask, target):
        _raise(ask, self.AGENT, _body("rk-pending", proposal={"pay": 2}))
        _raise(ask, "agent-611b-someone-else", _body("rk-other-agent", proposal={"pay": 3}))
        with _Rejected(ask.svc, 422, "invalid_supersedes_expired"):
            _raise(ask, self.AGENT, _body(f"rk-try-{target}", supersedes_expired=target))

    def test_repeating_an_expired_proposal_without_the_link_is_refused(self, ask):
        self._expired(ask, "rk-denied", {"pay": 7, "to": "v"})
        with _Rejected(ask.svc, 422, "reask_requires_link") as info:
            _raise(ask, self.AGENT, _body("rk-again", proposal={"to": "v", "pay": 7}))
        assert info.value.extra["expired_request_id"] == "rk-denied"
        assert _raise(ask, self.AGENT, _body("rk-other", proposal={"pay": 8, "to": "v"}))["status"] == "created"


class TestCapsAndAnnouncement:
    def test_a_denied_rate_is_a_named_429_and_writes_nothing(self, ask):
        ask.state["rate_ok"] = False
        with _Rejected(ask.svc, 429, "rate_limited"):
            _raise(ask, "agent-611b-rate", _body("cap-1"))
        assert ask.db.get_operator_queue_item_for_agent_by_request_id("agent-611b-rate", "cap-1") is None

    def test_a_full_queue_is_a_named_429(self, ask, monkeypatch):
        monkeypatch.setattr(ask.svc, "_max_pending", lambda: 1)
        _raise(ask, "agent-611b-cap", _body("cap-a"))
        with _Rejected(ask.svc, 429, "queue_full"):
            _raise(ask, "agent-611b-cap", _body("cap-b"))

    @pytest.mark.asyncio
    async def test_a_create_is_audited_once_by_ids_and_announced_thinly(self, ask):
        r = _raise(ask, "agent-611b-audit", _body("au-1"))
        await _drain()
        raised = [a for a in ask.audit if a["event_action"] == "raised"]
        assert len(raised) == 1
        details = raised[0]["details"]
        assert details == {"agent_name": "agent-611b-audit", "request_id": "au-1", "channel": "mcp",
                           "raised_by": "agent", "type": "approval", "to_role": "primary"}
        assert raised[0]["actor_agent_name"] == "agent-611b-audit"
        assert ask.sent == [{"type": "operator_queue_new",
                             "data": {"id": r["id"], "agent_name": "agent-611b-audit"}}]

    def test_the_receipt_says_whether_an_ending_will_wake_the_agent(self, ask):
        ask.state["opted_in"] = True
        assert _raise(ask, "agent-611b-wake", _body("wk-1"))["wakes_on_ending"] is True


def _seed_owner(db, agent, username, email, **owner_kw):
    """A real user and ownership row on the per-process SQLite (idempotent)."""
    from db_models import UserCreate
    if not db.get_user_by_username(username):
        db.create_user(UserCreate(username=username, role="user", email=email))
    if not db.get_agent_owner(agent):
        db.register_agent_owner(agent, username, **owner_kw)


class TestTheRealOwnerLookup:
    """Every other test stubs `_owner_email`; these run the default `primary`
    addressing against real users and ownership rows (vybe's review on #3028)."""

    def test_the_default_primary_is_the_owners_real_email(self, ask, monkeypatch):
        monkeypatch.setattr(ask.svc, "_owner_email", ask.real_owner_email)
        _seed_owner(ask.db, "agent-611b-own", "owner-611b-real", "Owner.611b@Example.com")
        r = _raise(ask, "agent-611b-own", _body("own-1"))
        row = ask.db.get_operator_queue_item(r["id"])
        assert r["resolved"] is True
        assert row["addressed_to_email"] == "owner.611b@example.com"
        assert row["resolved_to"] == ["owner.611b@example.com"]

    def test_an_owner_with_no_email_makes_it_an_operator_ask(self, ask, monkeypatch):
        monkeypatch.setattr(ask.svc, "_owner_email", ask.real_owner_email)
        _seed_owner(ask.db, "agent-611b-own-noemail", "owner-611b-noemail", None)
        r = _raise(ask, "agent-611b-own-noemail", _body("own-2"))
        row = ask.db.get_operator_queue_item(r["id"])
        assert r["resolved"] is False and row["addressed_to_email"] is None


class TestPlatformRaises:
    """The gate (trinity-enterprise#164) raises through the same sink with
    `raised_by="gate"`, under the platform-reserved `gate-` id prefix. An agent
    therefore cannot pre-create a gate id (and have the gate's raise answered as
    a replay of the AGENT's proposal), and a gate row never wakes the agent to
    redo an action the gate resumes itself: `is_platform_minted` keys on the
    reserved prefixes, and it gates both wakes."""
    AGENT = "agent-611b-gate"

    def _gate(self, ask, body):
        return ask.svc.raise_ask(self.AGENT, body, raised_by="gate", channel="gate")

    def test_an_agent_may_not_author_a_gate_id(self, ask):
        # Its own agent: a sibling test creates `gate-call-abc` on AGENT, and the
        # database keeps rows between tests (order-dependent on 2 of the 3
        # nightly seeds before this, review on #3028).
        squatter = "agent-611b-gate-squatter"
        for rid in ("gate-agent-squat", "GATE-agent-squat"):
            with _Rejected(ask.svc, 422, "reserved_request_id"):
                _raise(ask, squatter, _body(rid))
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(squatter, "gate-agent-squat") is None

    def test_the_gate_raises_under_its_own_prefix_and_the_receipt_says_so(self, ask):
        r = self._gate(ask, _body("gate-call-abc", to="operator"))
        assert (r["status"], r["raised_by"], r["channel"]) == ("created", "gate", "gate")
        row = ask.db.get_operator_queue_item(r["id"])
        assert (row["raised_by"], row["channel"]) == ("gate", "gate")
        replay = self._gate(ask, _body("gate-call-abc", to="operator"))
        assert (replay["status"], replay["raised_by"]) == ("replayed", "gate")

    @pytest.mark.parametrize("rid", ["call-abc", "poison-gate-1", "xgate-call"])
    def test_a_gate_raise_must_carry_the_gate_prefix(self, ask, rid):
        with _Rejected(ask.svc, 422, "invalid_request_id"):
            self._gate(ask, _body(rid, to="operator"))
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, rid) is None

    def test_an_agents_own_receipt_says_the_agent_raised_it(self, ask):
        assert _raise(ask, self.AGENT, _body("ag-own"))["raised_by"] == "agent"

    def test_a_gate_row_never_wakes_the_agent(self, ask, monkeypatch):
        import services.operator_resume_service as ors
        from services.operator_queue_service import is_platform_minted

        ask.state["opted_in"] = True
        gate = ask.db.get_operator_queue_item(self._gate(ask, _body("gate-call-wake", to="operator"))["id"])
        mine = ask.db.get_operator_queue_item(_raise(ask, self.AGENT, _body("agent-call-wake"))["id"])
        assert is_platform_minted(gate) and not is_platform_minted(mine)
        woken = []
        monkeypatch.setattr(ors, "spawn_resume_dispatch",
                            lambda row, **kw: woken.append(row["request_id"]))
        ask.svc._wake_filer(ask.svc.EndingEvent(
            disposition=ask.svc.ANSWERED, rows=(gate, mine), actor_email="person@example.com"))
        assert woken == ["agent-call-wake"]

    def test_a_gate_rows_receipt_never_promises_a_wake(self, ask):
        """`_wake_filer` skips a platform-minted row, so the receipt of a gate
        raise must not say an ending will wake the agent, even when the owner
        opted in (review on #3028)."""
        ask.state["opted_in"] = True
        agent = "agent-611b-gate-wakes"
        first = ask.svc.raise_ask(agent, _body("gate-wk-1", to="operator"), raised_by="gate", channel="gate")
        again = ask.svc.raise_ask(agent, _body("gate-wk-1", to="operator"), raised_by="gate", channel="gate")
        assert (first["wakes_on_ending"], again["wakes_on_ending"]) == (False, False)
        assert _raise(ask, agent, _body("agent-wk-1"))["wakes_on_ending"] is True

    @pytest.mark.parametrize("raised_by, channel", [("gate", "mcp"), ("agent", "gate")])
    def test_the_gate_channel_goes_with_the_gate_raiser(self, ask, raised_by, channel):
        agent = f"agent-611b-gate-pin-{raised_by}"
        rid = "gate-pin-1" if raised_by == "gate" else "pin-1"
        with pytest.raises(ValueError):
            ask.svc.raise_ask(agent, _body(rid, to="operator"), raised_by=raised_by, channel=channel)
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(agent, rid) is None

    def test_a_gate_raise_never_answers_as_a_replay_of_a_row_it_did_not_raise(self, ask):
        """A file row named `gate-…` written before the prefix was reserved would
        otherwise come back to the gate as `replayed`, holding the agent's
        proposal. A 409 makes that impossible rather than merely detectable."""
        agent = "agent-611b-gate-legacy"
        legacy = ask.db.create_operator_queue_item(
            agent, {"id": "gate-legacy-1", "type": "question", "title": "t", "question": "q", "context": {}},
            channel="file", raised_by="agent")
        with _Rejected(ask.svc, 409, "request_id_taken"):
            ask.svc.raise_ask(agent, _body("gate-legacy-1", to="operator"), raised_by="gate", channel="gate")
        row = ask.db.get_operator_queue_item(legacy)
        assert (row["raised_by"], row["channel"]) == ("agent", "file")   # untouched

    def test_a_non_gate_row_that_wins_the_create_is_refused_too(self, ask, monkeypatch):
        """The same rule where the create itself reports a replay (a row that
        landed between the check and the insert)."""
        agent = "agent-611b-gate-lost"
        other = ask.db.get_operator_queue_item(ask.db.create_operator_queue_item(
            agent, {"id": "gate-lost-other", "type": "question", "title": "t", "question": "q", "context": {}},
            channel="file", raised_by="agent"))
        monkeypatch.setattr(ask.db, "create_native_operator_queue_item",
                            lambda *a, **k: {"outcome": "replayed", "row": other})
        with _Rejected(ask.svc, 409, "request_id_taken"):
            ask.svc.raise_ask(agent, _body("gate-lost-1", to="operator"), raised_by="gate", channel="gate")

    @pytest.mark.parametrize("kw", [{"raised_by": "bot"}, {"channel": "file"}, {"channel": "api"}])
    def test_an_unknown_raiser_or_channel_is_a_programming_error(self, ask, kw):
        args = {"raised_by": "agent", "channel": "mcp", **kw}
        with pytest.raises(ValueError):
            ask.svc.raise_ask(self.AGENT, _body("pe-1"), **args)
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "pe-1") is None


class TestTheWorkspaceShowsTheProposal:
    """The Workspace projection (`WorkspaceAsk`) is an allowlist: a field reaches
    the person because it is named there. `proposal` is named — it is the exact
    action the addressee is asked to approve, and an ask to `primary` is decided
    in the owner's Workspace — while `context` stays off (agent scratch that may
    hold execution ids or internal paths)."""
    AGENT = "agent-611b-ws"

    def test_the_proposal_reaches_the_addressee_and_the_context_does_not(self, ask):
        from client_portal.asks.service import _project
        r = _raise(ask, self.AGENT, _body("ws-1", context={"internal_path": "/srv/agent-scratch"}))
        view = _project(ask.db.get_operator_queue_item(r["id"])).model_dump()
        assert view["proposal"] == {"pay": 500, "to": "vendor-7"}
        assert "context" not in view and "/srv/agent-scratch" not in repr(view)

    def test_an_ask_without_a_proposal_projects_none(self, ask):
        from client_portal.asks.service import _project
        body = _body("ws-2", type="question")
        body.pop("options"); body.pop("proposal")
        r = _raise(ask, self.AGENT, body)
        assert _project(ask.db.get_operator_queue_item(r["id"])).proposal is None


def _expire(uid):
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import operator_queue
    with get_engine().begin() as conn:
        conn.execute(update(operator_queue).where(operator_queue.c.id == uid)
                     .values(status="expired", disposition="expired", disposed_by="timeout",
                             disposed_at="2026-09-25T10:00:00Z"))


class TestTheReaskGuardStaysWithItsRaiser:
    """C6 and the re-ask link compare an ask with the SAME raiser's expired asks
    (review on #3028). Across raisers they are different actions by different
    parties: an agent's expired ask must not make the gate's raise 422
    `reask_requires_link`, and a gate approval must not badge as a re-ask of
    the agent's own. Each test has its own agent: expiring an ask changes what
    later raises on that agent are refused."""

    def _gate(self, ask, agent, body):
        return ask.svc.raise_ask(agent, body, raised_by="gate", channel="gate")

    def test_the_gate_is_not_blocked_by_the_agents_expired_proposal(self, ask):
        agent = "agent-611b-rr-1"
        _expire(_raise(ask, agent, _body("rr-agent-old", to="operator", proposal={"deploy": "v2"}))["id"])
        r = self._gate(ask, agent, _body("gate-rr-1", to="operator", proposal={"deploy": "v2"}))
        assert r["status"] == "created"

    def test_the_agent_is_not_blocked_by_the_gates_expired_proposal(self, ask):
        agent = "agent-611b-rr-2"
        _expire(self._gate(ask, agent, _body("gate-rr-old", to="operator", proposal={"deploy": "v3"}))["id"])
        assert _raise(ask, agent, _body("rr-agent-new", to="operator", proposal={"deploy": "v3"}))["status"] == "created"

    def test_the_gate_still_needs_the_link_after_its_own_timeout(self, ask):
        agent = "agent-611b-rr-3"
        _expire(self._gate(ask, agent, _body("gate-rr-own", to="operator", proposal={"deploy": "v4"}))["id"])
        with _Rejected(ask.svc, 422, "reask_requires_link") as info:
            self._gate(ask, agent, _body("gate-rr-own-2", to="operator", proposal={"deploy": "v4"}))
        assert info.value.extra["expired_request_id"] == "gate-rr-own"
        linked = self._gate(ask, agent, _body("gate-rr-own-3", to="operator", proposal={"deploy": "v4"},
                                              supersedes_expired="gate-rr-own"))
        assert (linked["status"], linked["supersedes_expired"]) == ("created", "gate-rr-own")

    def test_a_link_across_raisers_is_the_uniform_refusal(self, ask):
        agent = "agent-611b-rr-4"
        _expire(_raise(ask, agent, _body("rr-agent-old", to="operator", proposal={"deploy": "v5"}))["id"])
        _expire(self._gate(ask, agent, _body("gate-rr-old", to="operator", proposal={"deploy": "v6"}))["id"])
        with _Rejected(ask.svc, 422, "invalid_supersedes_expired"):
            self._gate(ask, agent, _body("gate-rr-x", to="operator", supersedes_expired="rr-agent-old"))
        with _Rejected(ask.svc, 422, "invalid_supersedes_expired"):
            _raise(ask, agent, _body("rr-agent-x", to="operator", supersedes_expired="gate-rr-old"))
        for rid in ("gate-rr-x", "rr-agent-x"):
            assert ask.db.get_operator_queue_item_for_agent_by_request_id(agent, rid) is None

    def test_an_agent_still_links_its_own_ask_from_before_the_column(self, ask):
        """A row older than `raised_by` (NULL there) is the agent's own file ask,
        and an agent's re-ask may link it, as before."""
        agent = "agent-611b-rr-5"
        _expire(ask.db.create_operator_queue_item(
            agent, {"id": "rr-legacy-old", "type": "question", "title": "t", "question": "q", "context": {}}))
        r = _raise(ask, agent, _body("rr-legacy-new", to="operator", supersedes_expired="rr-legacy-old"))
        assert (r["status"], r["supersedes_expired"]) == ("created", "rr-legacy-old")

    def test_nobody_links_a_platform_alarm(self, ask):
        """A platform-minted row (a reserved-prefix alarm, NULL `raised_by`) is
        not an ask anyone raised, so nothing re-asks it."""
        agent = "agent-611b-rr-6"
        _expire(ask.db.create_operator_queue_item(
            agent, {"id": "queue-flood-rr-6", "type": "alert", "title": "t", "question": "q", "context": {}}))
        with _Rejected(ask.svc, 422, "invalid_supersedes_expired"):
            _raise(ask, agent, _body("rr-after-alarm", to="operator", supersedes_expired="queue-flood-rr-6"))


class TestTheRateCheckComesBeforeTheWork:
    """Every call that is not a replay spends a rate token before the sink reads
    the database (vybe's review on #3028): the re-ask scan reads up to 200 stored
    proposals and the addressing reads the owner, so a refusal that came after
    them could be repeated for free. A replay still costs nothing."""
    AGENT = "agent-611b-order"

    def test_a_rate_limited_call_reads_no_proposals_and_no_owner(self, ask, monkeypatch):
        reads = []
        monkeypatch.setattr(ask.db, "list_expired_operator_queue_proposals",
                            lambda *a, **k: reads.append("scan") or [])
        monkeypatch.setattr(ask.svc, "_owner_email", lambda agent: reads.append("owner") or OWNER)
        ask.state["rate_ok"] = False
        with _Rejected(ask.svc, 429, "rate_limited"):
            _raise(ask, self.AGENT, _body("ord-1"))
        assert reads == []

    def test_a_refused_ask_still_spent_its_token(self, ask, monkeypatch):
        import services.operator_queue_service as oqs
        from services.rate_limiter import RateLimitResult

        checks = []
        monkeypatch.setattr(oqs.rate_limiter, "check",
                            lambda key, *a, **k: checks.append(key) or RateLimitResult(True, 10, 0, 60))
        with _Rejected(ask.svc, 422, "invalid_supersedes_expired"):
            _raise(ask, self.AGENT, _body("ord-2", supersedes_expired="ord-nothing"))
        assert f"operator_queue_create:{self.AGENT}" in checks

    def test_a_replay_is_answered_even_when_the_rate_is_spent(self, ask):
        first = _raise(ask, self.AGENT, _body("ord-3"))
        ask.state["rate_ok"] = False
        again = _raise(ask, self.AGENT, _body("ord-3"))
        assert (again["status"], again["id"]) == ("replayed", first["id"])


# ===========================================================================
# 4. The route — POST /api/agents/{name}/operator-queue, the agent as itself
# ===========================================================================

_ROUTE_APP = None
_PRINCIPAL = {"user": None}


def _principal(**kw):
    from models import User
    base = {"id": 7, "username": "op", "email": "op@example.com", "role": "admin"}
    base.update(kw)
    return User(**base)


def _route_client():
    """ONE app over both operator-queue routers; `get_current_user` overridden by
    walking the routes' own dependant trees (never a fresh import)."""
    global _ROUTE_APP
    from fastapi.testclient import TestClient
    if _ROUTE_APP is None:
        from fastapi import FastAPI
        from routers import operator_queue as r
        app = FastAPI()
        app.include_router(r.router)
        app.include_router(r.agent_router)
        found = set()

        def walk(dependant):
            for sub in dependant.dependencies:
                if getattr(sub.call, "__name__", "") == "get_current_user":
                    found.add(sub.call)
                walk(sub)

        for route in list(r.router.routes) + list(r.agent_router.routes):
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
        assert found, "no get_current_user dependency on the operator-queue routes"
        for call in found:
            app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
        _ROUTE_APP = app
    return TestClient(_ROUTE_APP, raise_server_exceptions=True)


class TestRaiseRoute:
    AGENT = "agent-611b-route"

    @pytest.fixture
    def route(self, ask, monkeypatch):
        """The real route over the real sink (the `ask` fixture's world), with
        ownership answered for this file's agents only."""
        from types import SimpleNamespace
        from routers import operator_queue as r
        monkeypatch.setattr(r, "_websocket_manager", None)
        mine = {self.AGENT, "trinity-system"}
        real_owner, real_access = ask.db.get_agent_owner, ask.db.can_user_access_agent
        monkeypatch.setattr(ask.db, "get_agent_owner",
                            lambda name: {"owner_username": "op"} if name in mine else real_owner(name))
        monkeypatch.setattr(ask.db, "can_user_access_agent",
                            lambda user, name: True if name in mine else real_access(user, name))

        def as_(**principal):
            _PRINCIPAL["user"] = _principal(**principal)

        as_(mcp_scope="agent", agent_name=self.AGENT)
        return SimpleNamespace(client=_route_client(), as_=as_, ask=ask)

    def _post(self, route, body, agent=None):
        return route.client.post(f"/api/agents/{agent or self.AGENT}/operator-queue", json=body)

    def test_the_agent_raises_its_own_ask_and_gets_a_201_receipt(self, route):
        res = self._post(route, _body("rt-1"))
        assert res.status_code == 201, res.text
        assert set(res.json()) == RECEIPT_KEYS and res.json()["status"] == "created"

    def test_a_retry_is_a_200_replay_of_the_first_receipt(self, route):
        first = self._post(route, _body("rt-2")).json()
        res = self._post(route, _body("rt-2"))
        assert res.status_code == 200 and res.json()["status"] == "replayed"
        assert res.json()["id"] == first["id"]

    @pytest.mark.parametrize("principal", [
        pytest.param({"mcp_scope": "agent", "agent_name": "agent-611b-sibling"}, id="another-agents-key"),
        pytest.param({}, id="a-person"),
        pytest.param({"mcp_scope": "user"}, id="a-user-key"),
        pytest.param({"mcp_scope": "system"}, id="system-key-for-another-agent"),
    ])
    def test_only_the_agent_itself_may_raise(self, route, principal):
        route.as_(**principal)
        res = self._post(route, _body("rt-who"))
        assert res.status_code == 403 and res.json()["detail"]["code"] == "agent_identity_required"
        assert route.ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "rt-who") is None

    def test_the_system_key_raises_as_the_system_agent(self, route):
        route.as_(mcp_scope="system")
        res = self._post(route, _body("rt-sys"), agent="trinity-system")
        assert res.status_code == 201, res.text

    def test_a_refusal_carries_its_named_code(self, route):
        res = self._post(route, _body("rt-bad", options=None))
        assert res.status_code == 422 and res.json()["detail"]["code"] == "options_required"
        route.ask.state["rate_ok"] = False
        res = self._post(route, _body("rt-rate"))
        assert res.status_code == 429 and res.json()["detail"]["code"] == "rate_limited"

    @pytest.mark.parametrize("path", ["item", "list", "agent_list"])
    def test_a_native_asks_addressee_is_withheld_from_machine_keys(self, route, path):
        """The receipt never names the person a role resolved to, and neither
        does a read: `resolved_to` and `addressed_to_email` hold the same email,
        so both are withheld from machines. Today that person is the owner
        (whose email an agent key can read anyway); once an assignment provider
        answers `people_for` it can be someone else. Since trinity-enterprise#715
        a file entry's addressee is withheld too: the agent wrote it, but the
        same key reads its siblings' rows."""
        uid = self._post(route, _body(f"rt-wh-{path}")).json()["id"]
        filed = route.ask.db.create_operator_queue_item(
            self.AGENT, {"id": f"rt-file-{path}", "type": "question", "title": "t", "question": "q",
                         "context": {}, "addressed_to_email": "client@example.com"},
            channel="file", raised_by="agent")
        urls = {"item": [f"/api/operator-queue/{uid}", f"/api/operator-queue/{filed}"],
                "list": [f"/api/operator-queue?agent_name={self.AGENT}"] * 2,
                "agent_list": [f"/api/operator-queue/agents/{self.AGENT}"] * 2}[path]

        def rows(principal):
            principal()
            got = []
            for url, want in zip(urls, (uid, filed)):
                body = route.client.get(url).json()
                got.append(body if path == "item" else next(i for i in body["items"] if i["id"] == want))
            return got

        native, file_row = rows(lambda: route.as_())                      # a person
        assert native["addressed_to_email"] == OWNER
        native, file_row = rows(lambda: route.as_(mcp_scope="agent", agent_name=self.AGENT))
        assert "addressed_to_email" not in native and "resolved_to" not in native
        assert "addressed_to_email" not in file_row

    def test_a_null_context_or_proposal_reads_as_absent(self, route):
        """The MCP tool publishes both as object-or-null (so the published
        schema can say "any keys"); null must mean the same as leaving it out."""
        res = self._post(route, {**_body("rt-null", type="question"), "options": None,
                                 "proposal": None, "context": None})
        assert res.status_code == 201, res.text
        row = route.ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "rt-null")
        assert row["proposal"] is None
        # Nothing agent-authored: only the platform's own Workspace thread key.
        assert set(row["context"]) <= {"workspace_session_id"}

    def test_the_readback_names_the_predecessor_by_request_id_like_the_receipt(self, route):
        """The receipt's `supersedes_expired` is the predecessor's request_id (the
        id the agent chose); the row stores its uuid. `get_my_ask` must say the
        same thing the receipt says (vybe's review on #3028)."""
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import operator_queue

        old = self._post(route, _body("rt-old", proposal={"pay": 9})).json()
        with get_engine().begin() as conn:
            conn.execute(update(operator_queue).where(operator_queue.c.id == old["id"])
                         .values(status="expired", disposition="expired", disposed_by="timeout",
                                 disposed_at="2026-09-25T10:00:00Z"))
        receipt = self._post(route, _body("rt-new", proposal={"pay": 9}, supersedes_expired="rt-old")).json()
        got = route.client.get(f"/api/agents/{self.AGENT}/operator-queue/rt-new")
        assert got.status_code == 200, got.text
        assert receipt["supersedes_expired"] == got.json()["supersedes_expired"] == "rt-old"

    def test_an_unknown_field_is_refused(self, route):
        res = self._post(route, {**_body("rt-extra"), "channel": "file"})
        assert res.status_code == 422
        assert route.ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "rt-extra") is None

    @pytest.mark.asyncio
    async def test_the_audit_row_is_the_agents_and_names_the_key_that_raised_it(self, route):
        """The AGENT raised the ask, not the owner its key resolves to (the
        ent#614 rule, routers/fan_out.py). The resolver ranks a user first, so
        the row carries no `actor_user`; the REAL resolver then makes it the
        agent's, with the presented credential and the owner's email (the join
        back to the human) carried explicitly."""
        from services.platform_audit_service import PlatformAuditService
        route.as_(mcp_scope="agent", agent_name=self.AGENT, mcp_key_id="key-611", mcp_key_name="agent-key")
        res = self._post(route, _body("rt-aud"))
        await _drain()
        raised = [a for a in route.ask.audit if a["event_action"] == "raised" and a["target_id"] == res.json()["id"]]
        assert len(raised) == 1
        row = raised[0]
        assert row.get("actor_user") is None
        assert (row["actor_agent_name"], row["actor_email"], row["mcp_key_id"], row["mcp_key_name"],
                row["mcp_scope"]) == (self.AGENT, "op@example.com", "key-611", "agent-key", "agent")
        assert PlatformAuditService._resolve_actor(
            actor_user=row.get("actor_user"), actor_agent_name=row["actor_agent_name"],
            mcp_scope=row["mcp_scope"], mcp_key_id=row["mcp_key_id"],
        ) == ("agent", self.AGENT, None)


class TestRaiseRouteWithRealKeys:
    """The route tests above replace `get_current_user`. These present a real
    minted agent key to an app with no overrides, so key resolution, the
    self-acting identity check and the ephemeral fence
    (`dependencies._enforce_ephemeral_key_fence`) run as in production (vybe's
    review on #3028)."""
    OWNER_USER = "owner-611b-keys"
    AGENT = "agent-611b-keys"
    SIBLING = "agent-611b-keys-sibling"
    GHOST = "agent-611b-ghost"

    @pytest.fixture
    def keys(self, ask, monkeypatch):
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import operator_queue as r

        monkeypatch.setattr(r, "_websocket_manager", None)
        for agent, extra in ((self.AGENT, {}), (self.SIBLING, {}), (self.GHOST, {"is_ephemeral": True})):
            _seed_owner(ask.db, agent, self.OWNER_USER, "keys-611b@example.com", **extra)
        app = FastAPI()
        app.include_router(r.router)
        app.include_router(r.agent_router)

        def bearer(agent):
            key = ask.db.create_agent_mcp_api_key(agent, self.OWNER_USER)
            return {"Authorization": f"Bearer {key.api_key}"}

        return SimpleNamespace(client=TestClient(app), bearer=bearer, db=ask.db)

    def _post(self, keys, agent, as_agent, rid):
        return keys.client.post(f"/api/agents/{agent}/operator-queue", json=_body(rid),
                                headers=keys.bearer(as_agent))

    def test_a_real_agent_key_raises_its_own_ask(self, keys):
        res = self._post(keys, self.AGENT, self.AGENT, "rk-own")
        assert res.status_code == 201, res.text
        row = keys.db.get_operator_queue_item(res.json()["id"])
        assert (row["agent_name"], row["raised_by"], row["channel"]) == (self.AGENT, "agent", "mcp")

    def test_a_real_key_may_not_raise_for_another_agent(self, keys):
        res = self._post(keys, self.AGENT, self.SIBLING, "rk-sib")
        assert res.status_code == 403 and res.json()["detail"]["code"] == "agent_identity_required"
        assert keys.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "rk-sib") is None

    def test_an_ephemeral_agents_own_key_is_fenced_off_the_route(self, keys):
        res = self._post(keys, self.GHOST, self.GHOST, "rk-ghost")
        assert res.status_code == 403, res.text
        assert "Ephemeral agent keys" in res.json()["detail"]
        assert keys.db.get_operator_queue_item_for_agent_by_request_id(self.GHOST, "rk-ghost") is None


# ===========================================================================
# 6. The file poller's side of the seam
# ===========================================================================

def _poller_db(*, foreign=(), pending=0, outcomes=()):
    """The poller's db with an explicit return for every accessor a cycle
    reaches: a MagicMock default iterates empty and keeps a broken path green
    (the #2915 harness rule). The plain create raises, so a poller that still
    called it would land in its own quarantine branch and create nothing."""
    from unittest.mock import MagicMock
    db = MagicMock()
    db.count_operator_queue_pending_for_agent.return_value = pending
    db.get_operator_queue_sync_index_for_agent.return_value = {
        "open": [], "terminal": {}, "foreign": list(foreign)}
    db.set_operator_queue_sync_state.return_value = True
    db.set_operator_queue_delivery_state.return_value = True
    db.mark_operator_queue_unconfirmed.return_value = 0
    db.refresh_operator_queue_last_confirmed.return_value = 0
    db.mark_operator_queue_acknowledged.return_value = None
    db.get_operator_queue_responded_for_agent.return_value = []
    db.get_operator_queue_terminal_for_agent.return_value = []
    db.get_setting_value.return_value = "24"
    db.create_operator_queue_item.side_effect = AssertionError(
        "the poller creates through create_operator_queue_item_with_outcome")
    queued = list(outcomes)
    db.create_operator_queue_item_with_outcome.side_effect = (
        lambda agent, item, **kw: queued.pop(0) if queued else (f"uuid-{item['id']}", True))
    return db


def _file_entry(rid, status="pending"):
    return {"id": rid, "type": "approval", "status": status, "priority": "high",
            "title": "Approve payout", "question": "Release 500 USDC?",
            "options": ["approve", "reject"]}


def _poller(monkeypatch, db, *entries):
    import json
    from unittest.mock import AsyncMock, MagicMock
    import services.operator_queue_service as oqs
    from services.rate_limiter import RateLimitResult
    content = json.dumps({"$schema": "operator-queue-v1", "requests": list(entries)})
    client = MagicMock()
    client.read_file = AsyncMock(return_value={"success": True, "content": content})
    client.write_file = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(oqs, "db", db)
    monkeypatch.setattr(oqs, "AgentClient", lambda name: client)
    monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    audit = AsyncMock()
    monkeypatch.setattr(oqs, "_audit_sync", audit)
    ws = MagicMock()
    ws.broadcast = AsyncMock()
    monkeypatch.setattr(oqs, "_websocket_manager", ws)
    return oqs.OperatorQueueSyncService(), audit, ws


def _cycle(svc, agent):
    import asyncio
    asyncio.run(svc._sync_agent(agent))


def _announced(ws):
    import json
    frames = [json.loads(c.args[0]) for c in ws.broadcast.call_args_list]
    return [f["data"]["id"] for f in frames if f["type"] == "operator_queue_new"]


def _created(db):
    return [c.args[1]["id"] for c in db.create_operator_queue_item_with_outcome.call_args_list]


def _poller_lines(caplog, needle):
    return [r for r in caplog.records
            if r.name == "services.operator_queue_service" and needle in r.getMessage()]


class TestThePollerLeavesNativeAsksAlone:
    """A file entry re-using the id of an ask the agent raised over the platform
    is skipped before any branch reads it. The acknowledged branch matches rows
    by request_id alone, and the create branch would read the id as brand new
    every cycle (the phantom admit)."""

    @pytest.mark.parametrize("status", ["pending", "acknowledged", "responded", "cancelled"])
    def test_an_entry_reusing_a_native_id_is_skipped_before_any_branch(self, monkeypatch, status):
        db = _poller_db(foreign=["ask-1"])
        svc, audit, ws = _poller(monkeypatch, db, _file_entry("ask-1", status))
        _cycle(svc, "agent-611b-poll")
        assert _created(db) == []
        db.mark_operator_queue_acknowledged.assert_not_called()
        db.set_operator_queue_sync_state.assert_not_called()
        audit.assert_not_called()
        assert _announced(ws) == []

    def test_a_file_entry_beside_a_native_one_is_still_ingested(self, monkeypatch):
        db = _poller_db(foreign=["ask-1"])
        svc, _, ws = _poller(monkeypatch, db, _file_entry("ask-1"), _file_entry("file-2"))
        _cycle(svc, "agent-611b-poll")
        assert _created(db) == ["file-2"]
        assert _announced(ws) == ["file-2"]

    def test_the_skip_is_logged_once_per_agent_and_id(self, monkeypatch, caplog):
        db = _poller_db(foreign=["ask-1"])
        svc, _, _ = _poller(monkeypatch, db, _file_entry("ask-1"))
        with caplog.at_level("INFO"):
            _cycle(svc, "agent-611b-poll")
            _cycle(svc, "agent-611b-poll")
        assert len(_poller_lines(caplog, "'ask-1'")) == 1


class TestThePollerCountsOnlyWhatItInserted:
    def test_it_creates_through_the_outcome_accessor_with_file_provenance(self, monkeypatch):
        db = _poller_db()
        svc, audit, ws = _poller(monkeypatch, db, _file_entry("f-9"))
        _cycle(svc, "agent-611b-prov")
        call = db.create_operator_queue_item_with_outcome.call_args
        assert call.args[0] == "agent-611b-prov" and call.args[1]["id"] == "f-9"
        assert call.kwargs == {"channel": "file", "raised_by": "agent"}
        assert _announced(ws) == ["f-9"]
        assert [c.args[0] for c in audit.call_args_list] == ["ingested"]

    def test_a_create_that_inserted_nothing_is_not_counted_announced_or_audited(self, monkeypatch):
        # One slot left under the depth cap: a row raised natively between the
        # cycle's index read and this create comes back `inserted=False`, and
        # must not spend that slot, announce itself or open an audit story.
        from services.operator_queue_service import OPERATOR_QUEUE_MAX_PENDING_PER_AGENT as cap
        db = _poller_db(pending=cap - 1, outcomes=[("uuid-raced", False)])
        svc, audit, ws = _poller(monkeypatch, db, _file_entry("race-1"), _file_entry("fresh-2"))
        _cycle(svc, "agent-611b-race")
        assert _created(db) == ["race-1", "fresh-2"]
        assert _announced(ws) == ["fresh-2"]
        assert [c.args[0] for c in audit.call_args_list] == ["ingested"]
        assert [c.args[0] for c in db.set_operator_queue_sync_state.call_args_list] == ["uuid-fresh-2"]


class TestTheFileChannelDeprecationNotice:
    """While the file stays supported (two releases), an agent that still
    raises through it is named once per agent per process, with the tool to
    move to."""

    def test_a_new_file_ingest_names_the_tool_once_per_agent(self, monkeypatch, caplog):
        db = _poller_db()
        svc, _, _ = _poller(monkeypatch, db, _file_entry("d-1"))
        with caplog.at_level("INFO"):
            _cycle(svc, "agent-611b-dep")
            _cycle(svc, "agent-611b-dep")          # ingests again: the fake index never learns
            _cycle(svc, "agent-611b-dep2")
        notices = _poller_lines(caplog, "ask_operator")
        assert [("agent-611b-dep" in r.getMessage(), "agent-611b-dep2" in r.getMessage())
                for r in notices] == [(True, False), (True, True)]
        assert all("two releases" in r.getMessage() and r.levelname == "WARNING" for r in notices)

    @pytest.mark.parametrize("foreign,outcomes", [(["d-1"], []), ([], [("uuid-raced", False)])])
    def test_nothing_ingested_names_nothing(self, monkeypatch, caplog, foreign, outcomes):
        db = _poller_db(foreign=foreign, outcomes=outcomes)
        svc, _, _ = _poller(monkeypatch, db, _file_entry("d-1"))
        with caplog.at_level("INFO"):
            _cycle(svc, "agent-611b-dep3")
        assert _poller_lines(caplog, "two releases") == []
