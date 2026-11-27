"""A re-raised alert keeps its age and its history (abilityai/trinity-enterprise#1367).

When an agent's alert replaces its own pending alert, the condition is being
raised again: the successor carries the date of the FIRST raise and a raise
count, the oldest-first orders and the aging bound read that date, and a
repeat that reaches the same people in the same room at no higher priority
announces nothing new.

Related flow: docs/memory/feature-flows/operating-room.md
Requirement: docs/memory/requirements/security.md §26.10

Harness: the unit island's real per-process SQLite (`init_database()` builds the
full schema). Agent names are unique to this file.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

COLUMNS = (("first_raised_at", "TEXT"), ("raise_count", "INTEGER"))
NAMES = tuple(name for name, _ in COLUMNS)
SQLITE_MIGRATION = "operator_queue_raise_history"
ALEMBIC_REVISION = "0127_operator_queue_raise_history"
ALEMBIC_PARENT = "0126_schedule_email_results"
OWNER = "owner-1367@example.com"


@pytest.fixture
def real_db():
    from database import db as real
    return real


def _iso(delta_minutes=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _native(real_db, agent, rid, *, replaces=None, carry=True, max_pending=10, **over):
    item = {"id": rid, "type": "alert", "priority": "medium", "title": f"Alert {rid}",
            "question": "The nightly export is failing."}
    item.update(over)
    return real_db.create_native_operator_queue_item(
        agent, item, max_pending=max_pending, channel="mcp", raised_by="agent",
        to_role=None, resolved_to=None, proposal=None, supersedes_expired=None,
        replaces=replaces, carry_raise_history=carry,
    )


def _row(real_db, agent, rid, **kw):
    out = _native(real_db, agent, rid, **kw)
    assert out["outcome"] == "created", out
    return out["row"]


def _set(real_db, row_id, **values):
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import operator_queue
    with get_engine().begin() as conn:
        conn.execute(update(operator_queue).where(operator_queue.c.id == row_id).values(**values))


def _delete(real_db, row_id):
    from sqlalchemy import delete
    from db.engine import get_engine
    from db.tables import operator_queue
    with get_engine().begin() as conn:
        conn.execute(delete(operator_queue).where(operator_queue.c.id == row_id))


def _pending_ids(real_db, agent):
    return [i["request_id"] for i in real_db.list_operator_queue_items(agent_name=agent, status="pending")]


# ===========================================================================
# 1. Schema: one migration pair, both tracks
# ===========================================================================

class TestSchema:
    @pytest.mark.parametrize("column", NAMES)
    def test_each_column_is_selectable_on_the_migrated_db(self, real_db, column):
        from sqlalchemy import select
        from db.engine import get_engine
        from db.tables import operator_queue
        with get_engine().connect() as conn:
            conn.execute(select(getattr(operator_queue.c, column)).limit(1)).all()

    def test_a_plain_row_carries_neither(self, real_db):
        row = _row(real_db, "agent-1367-schema", "s-1")
        assert {c: row[c] for c in NAMES} == dict.fromkeys(NAMES)

    def test_the_sqlite_migration_is_registered_once_and_is_idempotent(self):
        import sqlite3
        from db import migrations
        names = [name for name, _fn in migrations.MIGRATIONS]
        assert names.count(SQLITE_MIGRATION) == 1
        assert names.index(SQLITE_MIGRATION) > names.index("operator_queue_replace")
        fn = dict(migrations.MIGRATIONS)[SQLITE_MIGRATION]
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE operator_queue (id TEXT PRIMARY KEY)")
        fn(cur, conn)
        fn(cur, conn)
        cols = [(r[1], r[2]) for r in cur.execute("PRAGMA table_info(operator_queue)")]
        assert cols == [("id", "TEXT"), *COLUMNS]

    def test_the_alembic_revision_issues_the_same_ddl(self, monkeypatch):
        """`pg-migrations` cannot see an empty `upgrade()` (the baseline builds
        the head schema), so pin what an existing install is sent."""
        import importlib.util
        path = os.path.join(_BACKEND, "migrations", "versions", f"{ALEMBIC_REVISION}.py")
        spec = importlib.util.spec_from_file_location("rev_1367_raise_history", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert (mod.revision, mod.down_revision) == (ALEMBIC_REVISION, ALEMBIC_PARENT)

        class _Op:
            def __init__(self):
                self.sent = []

            def execute(self, sql):
                self.sent.append(" ".join(str(sql).split()))

        up, down = _Op(), _Op()
        monkeypatch.setattr(mod, "op", up)
        mod.upgrade()
        assert up.sent == [
            f"ALTER TABLE operator_queue ADD COLUMN IF NOT EXISTS {n} {t}" for n, t in COLUMNS]
        monkeypatch.setattr(mod, "op", down)
        mod.downgrade()
        assert down.sent == [
            f"ALTER TABLE operator_queue DROP COLUMN IF EXISTS {n}" for n, _ in reversed(COLUMNS)]

    def test_both_ddl_sources_carry_the_columns(self):
        from db.schema import TABLES as SCHEMA
        from db.tables import operator_queue
        for name, kind in COLUMNS:
            assert f"{name} {kind}" in SCHEMA["operator_queue"]
            assert name in operator_queue.c


# ===========================================================================
# 2. The carry-forward write, inside the replace's own transaction
# ===========================================================================

class TestCarryForward:
    AGENT = "agent-1367-carry"

    def test_a_repeat_stays_one_pending_row_with_the_first_date_and_a_count(self, real_db):
        agent = f"{self.AGENT}-one"
        first = _row(real_db, agent, "a-1", created_at=_iso(-3 * 24 * 60))
        second = _row(real_db, agent, "a-2", replaces=first["id"])
        assert second["first_raised_at"] == first["created_at"]
        assert second["raise_count"] == 2
        # this copy's own filing time is untouched
        assert second["created_at"] != first["created_at"]
        third = _row(real_db, agent, "a-3", replaces=second["id"])
        assert (third["first_raised_at"], third["raise_count"]) == (first["created_at"], 3)
        assert _pending_ids(real_db, agent) == ["a-3"]

    def test_the_service_must_ask_for_it(self, real_db):
        agent = f"{self.AGENT}-flag"
        first = _row(real_db, agent, "f-1")
        second = _row(real_db, agent, "f-2", replaces=first["id"], carry=False)
        assert {c: second[c] for c in NAMES} == dict.fromkeys(NAMES)

    @pytest.mark.parametrize("old_type,new_type", [
        ("question", "alert"), ("alert", "question"), ("approval", "approval")])
    def test_only_an_alert_replacing_an_alert_carries(self, real_db, old_type, new_type):
        """The types are re-checked on the rows the transaction holds: the
        keyword allows the carry, it does not assert it."""
        agent = f"{self.AGENT}-{old_type}-{new_type}"
        first = _row(real_db, agent, "t-1", type=old_type)
        second = _row(real_db, agent, "t-2", replaces=first["id"], type=new_type)
        assert {c: second[c] for c in NAMES} == dict.fromkeys(NAMES)

    def test_a_rolled_back_replace_writes_nothing(self, real_db):
        agent = f"{self.AGENT}-full"
        first = _row(real_db, agent, "r-1")
        _row(real_db, agent, "r-other")
        out = _native(real_db, agent, "r-2", replaces=first["id"], max_pending=1)
        assert out["outcome"] == "queue_full"
        assert real_db.get_operator_queue_item(first["id"])["status"] == "pending"
        assert real_db.get_operator_queue_item_for_agent_by_request_id(agent, "r-2") is None

    def test_a_retry_replays_the_first_row_untouched(self, real_db):
        agent = f"{self.AGENT}-replay"
        first = _row(real_db, agent, "p-1")
        second = _row(real_db, agent, "p-2", replaces=first["id"])
        again = _native(real_db, agent, "p-2", replaces=first["id"])
        assert again["outcome"] == "replayed"
        assert again["row"]["id"] == second["id"] and again["row"]["raise_count"] == 2

    def test_a_date_that_does_not_parse_is_not_copied(self, real_db):
        agent = f"{self.AGENT}-garbage"
        first = _row(real_db, agent, "g-1")
        _set(real_db, first["id"], created_at="not a date")
        second = _row(real_db, agent, "g-2", replaces=first["id"])
        assert second["first_raised_at"] is None and second["raise_count"] == 2


class TestChainRaisedBeforeTheColumns:
    """Gap B: a chain of replaces written before the columns existed is counted
    at raise time by walking `replaces` backwards. No data migration."""
    AGENT = "agent-1367-chain"

    def _legacy_chain(self, real_db, agent, n, **first_over):
        rows = [_row(real_db, agent, "c-1", created_at=_iso(-10 * 24 * 60), carry=False, **first_over)]
        for i in range(2, n + 1):
            rows.append(_row(real_db, agent, f"c-{i}", replaces=rows[-1]["id"], carry=False,
                             created_at=_iso(-(10 - i) * 24 * 60)))
        assert all(r["raise_count"] is None for r in rows)
        return rows

    def test_the_walk_finds_the_first_raise_and_counts_every_link(self, real_db):
        agent = f"{self.AGENT}-walk"
        rows = self._legacy_chain(real_db, agent, 4)
        new = _row(real_db, agent, "c-new", replaces=rows[-1]["id"])
        assert (new["first_raised_at"], new["raise_count"]) == (rows[0]["created_at"], 5)
        # and the next repeat reads it off its predecessor, no walk needed
        after = _row(real_db, agent, "c-after", replaces=new["id"])
        assert (after["first_raised_at"], after["raise_count"]) == (rows[0]["created_at"], 6)

    def test_the_walk_stops_at_a_link_that_is_not_an_alert(self, real_db):
        agent = f"{self.AGENT}-kind"
        rows = self._legacy_chain(real_db, agent, 3, type="question")
        new = _row(real_db, agent, "c-new", replaces=rows[-1]["id"])
        assert (new["first_raised_at"], new["raise_count"]) == (rows[1]["created_at"], 3)

    def test_the_walk_stops_at_a_link_retention_already_deleted(self, real_db):
        agent = f"{self.AGENT}-pruned"
        rows = self._legacy_chain(real_db, agent, 3)
        _delete(real_db, rows[0]["id"])
        new = _row(real_db, agent, "c-new", replaces=rows[-1]["id"])
        assert (new["first_raised_at"], new["raise_count"]) == (rows[1]["created_at"], 3)

    def test_the_walk_never_counts_another_agents_row(self, real_db):
        agent = f"{self.AGENT}-foreign"
        rows = self._legacy_chain(real_db, agent, 3)
        _set(real_db, rows[0]["id"], agent_name=f"{agent}-someone-else")
        new = _row(real_db, agent, "c-new", replaces=rows[-1]["id"])
        assert (new["first_raised_at"], new["raise_count"]) == (rows[1]["created_at"], 3)

    def test_a_link_must_be_confirmed_by_the_row_it_points_at(self, real_db):
        agent = f"{self.AGENT}-backlink"
        rows = self._legacy_chain(real_db, agent, 3)
        _set(real_db, rows[0]["id"], replaced_by="some-other-row")
        new = _row(real_db, agent, "c-new", replaces=rows[-1]["id"])
        assert (new["first_raised_at"], new["raise_count"]) == (rows[1]["created_at"], 3)

    def test_the_walk_is_bounded(self, real_db, monkeypatch):
        # Lower the cap in the dict the running method reads, not on
        # `sys.modules["db.operator_queue"]`: in a full-suite run a sibling test
        # can leave a stand-in there that re-exports the real class, and a patch
        # on the stand-in never reaches the walk.
        walk_globals = type(real_db._operator_queue_ops)._raise_history.__globals__
        monkeypatch.setitem(walk_globals, "_RAISE_CHAIN_WALK_MAX", 2)
        agent = f"{self.AGENT}-cap"
        rows = self._legacy_chain(real_db, agent, 6)
        new = _row(real_db, agent, "c-new", replaces=rows[-1]["id"])
        # the predecessor, two links back, and this raise
        assert (new["first_raised_at"], new["raise_count"]) == (rows[3]["created_at"], 4)

    def test_a_loop_in_the_links_ends_the_walk(self, real_db):
        agent = f"{self.AGENT}-loop"
        rows = self._legacy_chain(real_db, agent, 3)
        _set(real_db, rows[0]["id"], replaces=rows[2]["id"], replaced_by=rows[1]["id"])
        _set(real_db, rows[2]["id"], replaced_by=rows[0]["id"])
        new = _row(real_db, agent, "c-new", replaces=rows[2]["id"])
        assert new["raise_count"] == 4

    def test_the_walk_stops_at_a_link_that_already_carries_history(self, real_db):
        agent = f"{self.AGENT}-mixed"
        first = _row(real_db, agent, "m-1", created_at=_iso(-5 * 24 * 60))
        second = _row(real_db, agent, "m-2", replaces=first["id"])            # count 2
        third = _row(real_db, agent, "m-3", replaces=second["id"], carry=False)  # an older writer
        fourth = _row(real_db, agent, "m-4", replaces=third["id"])
        assert (fourth["first_raised_at"], fourth["raise_count"]) == (first["created_at"], 4)


# ===========================================================================
# 3. Sorting and ageing read the first-raised date
# ===========================================================================

class TestAge:
    AGENT = "agent-1367-age"

    def _standing_and_newer(self, real_db, agent):
        standing = _row(real_db, agent, "old-1", created_at=_iso(-3 * 24 * 60))
        _row(real_db, agent, "mid-1", created_at=_iso(-60))
        again = _row(real_db, agent, "old-2", replaces=standing["id"], created_at=_iso(-1))
        return standing, again

    def test_the_queue_list_puts_the_standing_condition_first(self, real_db):
        agent = f"{self.AGENT}-list"
        self._standing_and_newer(real_db, agent)
        assert _pending_ids(real_db, agent) == ["old-2", "mid-1"]

    def test_the_cursor_walk_stays_newest_first_by_the_latest_raise(self, real_db):
        agent = f"{self.AGENT}-walk"
        self._standing_and_newer(real_db, agent)
        # filed after the re-raise: newest-first puts it ahead, whichever date
        # the re-raised alert were sorted by — so the order below fits only
        # "newest first, by the latest raise"
        _row(real_db, agent, "late-1")
        page = real_db._operator_queue_ops.list_items_walk(
            watermark=_iso(1), agent_name=agent, status="pending")
        assert [item["request_id"] for item, _key in page] == ["late-1", "old-2", "mid-1"]
        # and the offset list is the opposite direction, on the first raise
        assert _pending_ids(real_db, agent) == ["old-2", "mid-1", "late-1"]

    def test_the_agents_own_pending_line_reads_the_first_raise(self, real_db):
        agent = f"{self.AGENT}-line"
        standing, _again = self._standing_and_newer(real_db, agent)
        asks = real_db.list_pending_operator_queue_asks(agent, 8)
        assert [a["request_id"] for a in asks] == ["old-2", "mid-1"]
        assert asks[0]["created_at"] == standing["created_at"]

    def test_the_aging_bound_starts_at_the_first_raise(self, real_db):
        from services.operator_queue_service import annotate_aging, is_aged
        agent = f"{self.AGENT}-bound"
        standing, again = self._standing_and_newer(real_db, agent)
        assert is_aged(again, hours=24) is True
        fresh = _row(real_db, agent, "fresh-1")
        assert is_aged(fresh, hours=24) is False
        [noted] = annotate_aging([dict(again)], hours=24)
        first = datetime.strptime(standing["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        assert noted["aging"] is True
        assert noted["aged_since"] == (first + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def test_a_stored_date_that_does_not_parse_falls_back_to_the_filing_time(self):
        from services.operator_queue_service import is_aged
        row = {"status": "pending", "created_at": _iso(-30 * 60), "first_raised_at": "nonsense"}
        assert is_aged(row, hours=24) is True
        assert is_aged(dict(row, created_at=_iso(-5)), hours=24) is False


# ===========================================================================
# 4. The sink: a repeat does not notify again
# ===========================================================================

@pytest.fixture
def ask(real_db, monkeypatch):
    """The real sink over the real SQLite; the world around it stubbed. The
    room's REAL ending observer and raised line run against a recording
    `_post_system`."""
    import json as _json
    from types import SimpleNamespace
    import services.ask_service as svc
    import services.operator_queue_service as oqs
    import services.operator_resume_service as ors
    from services import assignment_provider
    from services.rate_limiter import RateLimitResult
    from shared_sessions import service as rooms

    audit, sent, events, lines = [], [], [], []
    state = {"owner": OWNER, "room": None}

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
    monkeypatch.setattr(svc, "_raising_room", lambda agent, turn, kind: state["room"])
    monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (f"thread-{email}", False))
    monkeypatch.setattr(real_db, "get_wake_on_ending", lambda agent: False, raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    monkeypatch.setattr(ors, "spawn_ending_dispatch", lambda rows, **kw: None)
    monkeypatch.setattr(rooms, "_post_system", lambda room_id, content: lines.append((room_id, content)))
    monkeypatch.setattr(rooms, "_seated_identity", lambda room_id, email: None)
    svc.register_ending_observer(events.append)
    svc.register_ending_observer(rooms.on_ask_ending)
    assignment_provider.clear_provider()
    yield SimpleNamespace(svc=svc, audit=audit, sent=sent, events=events, lines=lines,
                          state=state, db=real_db)
    svc._observers.remove(events.append)
    svc._observers.remove(rooms.on_ask_ending)
    assignment_provider.clear_provider()


async def _drain():
    import asyncio
    import services.operator_resume_service as ors
    for _ in range(5):
        await asyncio.sleep(0)
        pending = list(ors._inflight)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _alert(request_id, **over):
    body = {"request_id": request_id, "type": "alert", "priority": "medium",
            "title": "Nightly export failing", "question": "The nightly export has failed again."}
    body.update(over)
    return body


async def _raise_twice(ask, agent, tag, *, second=None, between=None, **both):
    """Raise an alert, clear the recorders, raise it again; returns both receipts."""
    first = ask.svc.raise_ask(agent, _alert(f"{tag}-1", **both), raised_by="agent", channel="mcp")
    await _drain()
    for recorder in (ask.audit, ask.sent, ask.events, ask.lines):
        recorder.clear()
    if between:
        between()
    again = ask.svc.raise_ask(
        agent, _alert(f"{tag}-2", replaces=f"{tag}-1", **{**both, **(second or {})}),
        raised_by="agent", channel="mcp")
    await _drain()
    return first, again


class TestQuietRepeat:
    AGENT = "agent-1367-sink"

    @pytest.mark.asyncio
    async def test_a_repeat_announces_no_new_ask(self, ask):
        ask.state["room"] = "room-a"
        first, again = await _raise_twice(ask, f"{self.AGENT}-quiet", "q")
        row = ask.db.get_operator_queue_item(again["id"])
        assert row["raise_count"] == 2 and row["first_raised_at"]
        # the one thin trigger left is the predecessor's ending: it is what
        # makes an open page swap the row
        assert [s["type"] for s in ask.sent] == ["operator_queue_cancelled"]
        assert ask.sent[0]["data"] == {"id": first["id"], "agent_name": f"{self.AGENT}-quiet"}
        # the record is whole: both audit rows, and the raise says it is a repeat
        actions = {a["event_action"]: a for a in ask.audit}
        assert set(actions) == {"raised", "replaced"}
        assert actions["raised"]["details"]["raise_count"] == 2
        assert actions["raised"]["details"]["first_raised_at"] == row["first_raised_at"]
        # the room heard about it once, on the first raise, and nothing since
        assert ask.lines == []
        [ending] = ask.events
        assert ending.quiet is True and ending.reason == "replaced"

    @pytest.mark.asyncio
    async def test_the_first_raise_is_announced_as_ever(self, ask):
        ask.state["room"] = "room-a"
        ask.svc.raise_ask(f"{self.AGENT}-first", _alert("f-1"), raised_by="agent", channel="mcp")
        await _drain()
        assert [s["type"] for s in ask.sent] == ["operator_queue_new"]
        assert [room for room, _ in ask.lines] == ["room-a"]
        assert "raise_count" not in ask.audit[0]["details"]

    @pytest.mark.asyncio
    async def test_a_higher_priority_notifies_again_and_keeps_the_history(self, ask):
        ask.state["room"] = "room-a"
        _first, again = await _raise_twice(
            ask, f"{self.AGENT}-rise", "r", second={"priority": "high"})
        assert sorted(s["type"] for s in ask.sent) == ["operator_queue_cancelled", "operator_queue_new"]
        assert len(ask.lines) == 2 and {room for room, _ in ask.lines} == {"room-a"}
        assert ask.events[0].quiet is False
        assert ask.db.get_operator_queue_item(again["id"])["raise_count"] == 2

    @pytest.mark.asyncio
    async def test_a_lower_priority_stays_quiet(self, ask):
        await _raise_twice(ask, f"{self.AGENT}-lower", "l", second={"priority": "low"})
        assert [s["type"] for s in ask.sent] == ["operator_queue_cancelled"]

    @pytest.mark.asyncio
    async def test_a_repeat_that_reaches_someone_else_is_announced(self, ask):
        def _readdress():
            ask.state["owner"] = "someone-else-1367@example.com"
        first, again = await _raise_twice(
            ask, f"{self.AGENT}-people", "p", between=_readdress, to="primary")
        was, now = (ask.db.get_operator_queue_item(r["id"])["addressed_to_email"] for r in (first, again))
        assert (was, now) == (OWNER, "someone-else-1367@example.com")
        assert sorted(s["type"] for s in ask.sent) == ["operator_queue_cancelled", "operator_queue_new"]
        assert ask.events[0].quiet is False
        # the history is the condition's, whoever it now reaches
        assert ask.db.get_operator_queue_item(again["id"])["raise_count"] == 2

    @pytest.mark.asyncio
    async def test_a_repeat_to_the_same_person_stays_quiet(self, ask):
        first, again = await _raise_twice(ask, f"{self.AGENT}-same", "s", to="primary")
        assert ask.db.get_operator_queue_item(again["id"])["addressed_to_email"] == OWNER
        assert [s["type"] for s in ask.sent] == ["operator_queue_cancelled"]

    @pytest.mark.asyncio
    async def test_a_repeat_from_another_room_tells_both_rooms(self, ask):
        ask.state["room"] = "room-a"

        def _move():
            ask.state["room"] = "room-b"
        await _raise_twice(ask, f"{self.AGENT}-rooms", "m", between=_move)
        said = dict(ask.lines)
        assert set(said) == {"room-a", "room-b"}
        assert "was replaced by a newer ask" in said["room-a"]
        assert "asked:" in said["room-b"]
        assert sorted(s["type"] for s in ask.sent) == ["operator_queue_cancelled", "operator_queue_new"]

    @pytest.mark.asyncio
    async def test_in_a_room_a_changed_title_is_announced(self, ask):
        """The room's line quotes the title: a repeat under another title must
        not leave a line standing that names an ask which no longer exists."""
        ask.state["room"] = "room-a"
        await _raise_twice(ask, f"{self.AGENT}-title", "t",
                           second={"title": "Nightly export failing for 3 days"})
        assert [content for _room, content in ask.lines if "was replaced" in content]
        assert [content for _room, content in ask.lines if "Nightly export failing for 3 days" in content]
        assert sorted(s["type"] for s in ask.sent) == ["operator_queue_cancelled", "operator_queue_new"]

    @pytest.mark.asyncio
    async def test_outside_a_room_a_changed_title_stays_quiet(self, ask):
        await _raise_twice(ask, f"{self.AGENT}-title-noroom", "u",
                           second={"title": "Nightly export failing for 3 days"})
        assert [s["type"] for s in ask.sent] == ["operator_queue_cancelled"]

    @pytest.mark.asyncio
    async def test_a_replaced_question_is_announced_as_ever(self, ask):
        agent = f"{self.AGENT}-question"
        ask.svc.raise_ask(agent, _alert("k-1", type="question", options=["yes", "no"]),
                          raised_by="agent", channel="mcp")
        await _drain()
        ask.audit.clear(); ask.sent.clear(); ask.events.clear()
        again = ask.svc.raise_ask(
            agent, _alert("k-2", type="question", options=["yes", "no"], replaces="k-1"),
            raised_by="agent", channel="mcp")
        await _drain()
        assert sorted(s["type"] for s in ask.sent) == ["operator_queue_cancelled", "operator_queue_new"]
        assert ask.events[0].quiet is False
        raised = next(a for a in ask.audit if a["event_action"] == "raised")
        assert "raise_count" not in raised["details"]
        assert ask.db.get_operator_queue_item(again["id"])["raise_count"] is None

    @pytest.mark.asyncio
    async def test_the_receipt_is_unchanged(self, ask):
        _first, again = await _raise_twice(ask, f"{self.AGENT}-receipt", "c")
        assert again["status"] == "created" and again["replaces"] == "c-1"
        assert not set(NAMES) & set(again)

    @pytest.mark.asyncio
    async def test_a_quiet_repeat_survives_a_missing_websocket_manager(self, ask, monkeypatch):
        monkeypatch.setattr(ask.svc, "_websocket_manager", None)
        await _raise_twice(ask, f"{self.AGENT}-nows", "n")
        assert {a["event_action"] for a in ask.audit} == {"raised", "replaced"}


# ===========================================================================
# 5. Reads: the machine row and the Workspace projection
# ===========================================================================

class TestReads:
    AGENT = "agent-1367-reads"

    @pytest.fixture(autouse=True)
    def _roster(self, monkeypatch):
        import client_portal.service as portal_service
        monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a, **k: True)

    def test_a_machine_key_reads_both_fields(self):
        from routers.operator_queue import _MACHINE_ROW_FIELDS
        assert set(NAMES) <= set(_MACHINE_ROW_FIELDS)

    def _pair(self, real_db, tag, *, old_to, new_to):
        agent = f"{self.AGENT}-{tag}"
        old = _row(real_db, agent, "w-1", addressed_to_email=old_to, created_at=_iso(-2 * 24 * 60))
        new = _row(real_db, agent, "w-2", replaces=old["id"], addressed_to_email=new_to)
        return old, new

    def test_the_addressee_of_both_copies_sees_the_history(self, real_db):
        from client_portal.asks.service import _project
        to = "ws-1367@example.com"
        old, new = self._pair(real_db, "same", old_to=to, new_to=to)
        shown = _project(new, viewer_email=to)
        assert (shown.first_raised_at, shown.raise_count) == (old["created_at"], 2)
        # this copy's filing time still reads as its own (run provenance keys on it)
        assert shown.created_at == new["created_at"]

    def test_a_new_addressee_reads_the_history_and_no_earlier_asks_name(self, real_db):
        """The count and the date are the condition's, shown to whoever the
        alert reaches; the ask it replaced was someone else's and stays unnamed."""
        from client_portal.asks.service import _project
        old, new = self._pair(real_db, "moved", old_to="ws-1367-a@example.com",
                              new_to="ws-1367-b@example.com")
        shown = _project(new, viewer_email="ws-1367-b@example.com")
        assert (shown.first_raised_at, shown.raise_count) == (old["created_at"], 2)
        assert shown.replaces is None
        assert old["request_id"] not in shown.model_dump_json()
        assert old["id"] not in shown.model_dump_json()

    def test_a_plain_ask_projects_neither(self, real_db):
        from client_portal.asks.service import _project
        row = _row(real_db, f"{self.AGENT}-plain", "w-0", addressed_to_email="ws-1367@example.com")
        shown = _project(row, viewer_email="ws-1367@example.com")
        assert (shown.first_raised_at, shown.raise_count) == (None, None)
